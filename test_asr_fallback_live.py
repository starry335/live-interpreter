# -*- coding: utf-8 -*-
"""Forced-stall simulation: watchdog -> warm Paraformer handover -> live results.

The DashScope WebSocket is replaced by an in-process fake; the Paraformer
standby is a small subprocess that echoes one result per audio frame.  This
exercises the real watchdog, the real handover path and the real ring buffer
without any network access.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import asr_fallback
import gummy_realtime_worker as gummy
import qwen_asr_stream_worker as qwen


class FakeTimeout(Exception):
    pass


class FakeWebSocket:
    def __init__(self) -> None:
        self.inbox: queue.Queue = queue.Queue()
        self.sent: list = []
        self.binary: list = []
        self.closed = False
        self.sock = None
        self.close_frame = None

    def settimeout(self, _timeout) -> None:
        pass

    def send(self, text: str) -> None:
        self.sent.append(text)
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return
        action = (message.get("header") or {}).get("action")
        if action == "run-task":
            self.inbox.put(json.dumps({"header": {"event": "task-started"}, "payload": {}}))
        elif action == "finish-task":
            self.inbox.put(json.dumps({"header": {"event": "task-finished"}, "payload": {}}))

    def send_binary(self, data: bytes) -> None:
        self.binary.append(bytes(data))

    def recv(self):
        try:
            return self.inbox.get(timeout=0.05)
        except queue.Empty:
            raise FakeTimeout()

    def ping(self, _payload: str = "") -> bool:
        return True

    def close(self) -> None:
        self.closed = True
        self.inbox.put("")


STANDBY = r"""
import json, sys
print(json.dumps({"event": "ready"}), flush=True)
count = 0
while True:
    data = sys.stdin.buffer.read(3200)
    if not data:
        break
    count += 1
    print(json.dumps({"event": "result", "sentence_id": "9",
                      "translation": "fb%d" % count,
                      "translation_final": True}), flush=True)
"""


class ForcedStallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.events: list = []
        self.ws = FakeWebSocket()
        websocket_module = types.ModuleType("websocket")
        websocket_module.create_connection = lambda *args, **kwargs: self.ws
        websocket_module.WebSocketTimeoutException = FakeTimeout
        certifi_module = types.ModuleType("certifi")
        certifi_module.where = lambda: "dummy.pem"
        self.patches = [
            mock.patch.dict(sys.modules, {"websocket": websocket_module,
                                          "certifi": certifi_module}),
            mock.patch.object(qwen.ssl, "create_default_context", return_value=None),
            mock.patch.object(qwen, "emit", self.events.append),
            mock.patch.object(gummy, "emit", self.events.append),
            mock.patch.dict(os.environ, {
                "LIVE_INTERPRETER_WORK_DIR": self.directory.name,
                "LIVE_INTERPRETER_WS_PING": "0",
                "LIVE_INTERPRETER_ASR_WATCHDOG": "1",
                "LIVE_INTERPRETER_ASR_WATCHDOG_RMS": "300",
                "LIVE_INTERPRETER_FALLBACK_RING_SECONDS": "30",
            }),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.connection = None

    def tearDown(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass

    def push_result(self, end_time: int) -> None:
        self.ws.inbox.put(json.dumps({
            "header": {"event": "result-generated"},
            "payload": {"output": {"sentence": {
                "begin_time": 0, "end_time": end_time,
                "text": "こんにちは", "sentence_end": False,
            }, "usage": {"duration": 1.0}}},
        }))

    def test_forced_silence_hands_over_to_the_standby(self):
        args = qwen.parse_args(["--no-paraformer-fallback", "--no-translation"])
        self.connection = qwen.QwenASRStreamConnection(args, "test-key")
        controller = asr_fallback.FallbackController(
            args, self.events.append, work_dir=Path(self.directory.name),
            command=[sys.executable, "-u", "-c", STANDBY])
        self.connection.fallback = controller
        self.connection.open()
        self.push_result(1000)
        frame = b"\x10\x27" * 1600  # sample 10000: speech energy
        deadline = time.monotonic() + 10
        handover = threading.Event()

        def feed() -> None:
            while time.monotonic() < deadline and not controller.active:
                try:
                    self.connection.send_audio(frame)
                except RuntimeError:
                    return
                time.sleep(0.02)
            handover.set()
            # Keep feeding so live audio follows the replay.
            while time.monotonic() < deadline and controller.active:
                try:
                    self.connection.send_audio(frame)
                except RuntimeError:
                    return
                time.sleep(0.02)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        try:
            self.assertTrue(handover.wait(10), "watchdog never handed over")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if any(event.get("event") == "asr_stall_handover" for event in self.events) and any(
                        event.get("fallback") and str(event.get("translation", "")).startswith("fb")
                        for event in self.events):
                    break
                time.sleep(0.05)
        finally:
            feeder.join(timeout=1)

        kinds = [event.get("event") for event in self.events]
        self.assertIn("asr_stall", kinds)
        self.assertIn("asr_stall_handover", kinds)
        fallback_results = [event for event in self.events
                            if event.get("fallback") and event.get("event") == "result"]
        self.assertTrue(fallback_results)
        self.assertTrue(all(str(event["sentence_id"]).startswith(controller.standby.tag)
                            for event in fallback_results))
        self.assertGreater(len(self.ws.binary), 0)
        replay = [event for event in self.events if event.get("event") == "asr_fallback"]
        self.assertEqual(len(replay), 1)
        self.assertGreater(replay[0]["replayed_ms"], 0)


if __name__ == "__main__":
    unittest.main()
