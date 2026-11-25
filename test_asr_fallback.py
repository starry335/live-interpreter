# -*- coding: utf-8 -*-
"""Paraformer warm-standby and PCM ring-buffer tests."""
from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import asr_fallback
import qwen_asr_stream_worker as qwen


FAKE_WORKER = r"""
import json, sys
print(json.dumps({"event": "ready"}), flush=True)
print(json.dumps({"event": "session_config", "model": "fake"}), flush=True)
total = 0
while True:
    data = sys.stdin.buffer.read(3200)
    if not data:
        break
    total += len(data)
    print(json.dumps({"event": "result", "sentence_id": "8",
                      "translation": "got", "end_time": total // 32}), flush=True)
print(json.dumps({"event": "usage_summary", "bytes": total}), flush=True)
"""


class PcmRingTests(unittest.TestCase):
    def test_read_from_absolute_offset(self):
        ring = asr_fallback.PcmRing(seconds=1.0)  # 32 000 bytes
        ring.append(b"a" * 1000)
        ring.append(b"b" * 1000)
        self.assertEqual(ring.total, 2000)
        self.assertEqual(ring.read_from(0), b"a" * 1000 + b"b" * 1000)
        self.assertEqual(ring.read_from(1500), b"b" * 500)

    def test_old_audio_is_dropped_and_clamped(self):
        ring = asr_fallback.PcmRing(seconds=0.1)  # 3 200 bytes
        for _ in range(5):
            ring.append(b"x" * 1000)
        self.assertEqual(ring.total, 5000)
        self.assertEqual(ring.oldest, 2000)
        self.assertEqual(len(ring.read_from(0)), 3000)
        self.assertEqual(ring.read_from(0), ring.read_from(2000))
        self.assertEqual(ring.read_from(99999), b"")

    def test_offset_for_ms_is_clamped(self):
        ring = asr_fallback.PcmRing(seconds=0.1)
        ring.append(b"x" * 1000)
        self.assertEqual(ring.offset_for_ms(10), 320)
        self.assertEqual(ring.offset_for_ms(0), ring.oldest)
        self.assertEqual(ring.offset_for_ms(10_000), ring.total)


class ParaformerStandbyTests(unittest.TestCase):
    def make_standby(self):
        events = []
        with mock.patch.dict(os.environ, {"LIVE_INTERPRETER_WORK_DIR": ""}):
            standby = asr_fallback.ParaformerStandby(
                mock.Mock(), events.append,
                command=[sys.executable, "-u", "-c", FAKE_WORKER])
        return standby, events

    def test_activation_replays_the_ring_and_forwards_only_live_results(self):
        standby, events = self.make_standby()
        ring = asr_fallback.PcmRing(seconds=1.0)
        ring.append(b"\x01\x00" * 1600 * 3)  # 9 600 bytes / 300 ms
        try:
            self.assertTrue(standby.activate(ring, 0))
            self.assertTrue(standby.active)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any(
                    isinstance(event, dict) and event.get("translation") == "got" for event in events):
                time.sleep(0.02)
        finally:
            standby.stop()
        translations = [event.get("translation") for event in events if isinstance(event, dict)]
        self.assertEqual(translations.count("got"), 3)
        got = [event for event in events if isinstance(event, dict) and event.get("translation") == "got"]
        self.assertTrue(got)
        self.assertTrue(str(got[0]["sentence_id"]).startswith(standby.tag))
        self.assertTrue(got[0]["fallback"])
        replay = [event for event in events if isinstance(event, dict) and event.get("event") == "asr_fallback"]
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["replayed_ms"], 300)

    def test_second_activation_reuses_the_same_process(self):
        standby, _events = self.make_standby()
        ring = asr_fallback.PcmRing(seconds=1.0)
        ring.append(b"\x00\x00" * 1600)
        try:
            self.assertTrue(standby.activate(ring, 0))
            proc = standby.proc
            self.assertTrue(standby.activate(ring, 0))
            self.assertIs(standby.proc, proc)
        finally:
            standby.stop()

    def test_standby_errors_never_reach_the_main_session(self):
        standby, events = self.make_standby()
        idle = b'{"event": "error", "code": "NO_INPUT_AUDIO_ERROR"}\n'
        standby._pump_stdout(mock.Mock(stdout=io.BytesIO(idle)))
        self.assertEqual(events, [])
        self.assertIn("NO_INPUT_AUDIO_ERROR", standby.last_idle_error)
        standby.active = True
        standby._pump_stdout(mock.Mock(stdout=io.BytesIO(idle)))
        self.assertEqual(events, [])
        self.assertIn("NO_INPUT_AUDIO_ERROR", standby.last_idle_error)

    def test_keeper_reconnects_a_dead_standby(self):
        args = mock.Mock()
        events = []
        controller = asr_fallback.FallbackController(args, events.append, work_dir=Path("."))
        controller.standby.stop()
        stub = mock.Mock()
        stub.alive.return_value = False
        stub.start.return_value = True
        controller.standby = stub
        controller.warm_up()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not stub.start.called:
                time.sleep(0.02)
        finally:
            controller.close()
        self.assertTrue(stub.start.called)


class QwenFallbackTests(unittest.TestCase):
    def make_connection(self):
        args = qwen.parse_args(["--no-paraformer-fallback"])
        connection = qwen.QwenASRStreamConnection(args, "test-key")
        return connection

    def test_stall_hands_over_to_the_standby_and_routes_audio(self):
        connection = self.make_connection()
        stub = mock.Mock()
        stub.active = False
        stub.activate.return_value = True
        connection.fallback = stub
        connection.last_result_end_ms = 4200.0
        connection.fallback_bytes = 0

        self.assertTrue(connection.on_stall({"age_s": 10}))
        self.assertTrue(stub.activate.called)
        self.assertEqual(stub.activate.call_args.args[0], 4200.0)

        stub.active = True
        frame = b"\x00\x00" * 1600
        connection.send_audio(frame)
        stub.feed.assert_called_with(frame)
        self.assertEqual(connection.fallback_bytes, len(frame))
        self.assertEqual(connection.audio_bytes, 0)

    def test_controller_routes_live_frames_only_after_activation(self):
        args = mock.Mock(translation_enabled=True, source_language="ja", target_language="Chinese",
                         mt_model="qwen3.8-max", max_end_silence=500, workspace_id="", hotwords="")
        events = []
        controller = asr_fallback.FallbackController(args, events.append, work_dir=Path("."))
        standby = mock.Mock()
        controller.standby.stop()
        controller.standby = standby
        controller.feed(b"a" * 3200)
        standby.send.assert_not_called()
        controller.active = True
        controller.feed(b"b" * 3200)
        standby.send.assert_called_with(b"b" * 3200)
        self.assertEqual(controller.ring.total, 6400)
        controller.close()

    def test_stall_without_a_usable_standby_keeps_the_normal_exit_path(self):
        connection = self.make_connection()
        stub = mock.Mock()
        stub.active = False
        stub.activate.return_value = False
        connection.fallback = stub
        self.assertFalse(connection.on_stall({"age_s": 10}))

    def test_fallback_can_be_disabled(self):
        connection = self.make_connection()
        self.assertIsNone(connection.fallback)


if __name__ == "__main__":
    unittest.main()
