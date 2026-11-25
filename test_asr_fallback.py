# -*- coding: utf-8 -*-
"""Public Paraformer backup: PCM ring, process pump and handover routing."""
from __future__ import annotations

import io
import json
import os
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


class ParaformerBackupTests(unittest.TestCase):
    def make_backup(self):
        events = []
        with mock.patch.dict(os.environ, {"LIVE_INTERPRETER_WORK_DIR": ""}):
            backup = asr_fallback.ParaformerBackup(
                mock.Mock(), events.append,
                command=[sys.executable, "-u", "-c", FAKE_WORKER])
        return backup, events

    def test_default_command_is_public(self):
        args = mock.Mock(source_language="ja", target_language="zh",
                         mt_model="qwen3.8-max", max_end_silence=500,
                         workspace_id="ws-abc", hotwords="", translation_enabled=True)
        command = asr_fallback.build_paraformer_command(args, Path("."))
        self.assertEqual(command[command.index("--workspace-id") + 1], "")
        self.assertIn("--no-asr-backup", command)
        self.assertTrue(command[1].endswith("paraformer_mt_worker.py"))

    def test_activation_replays_the_ring_and_forwards_results(self):
        backup, events = self.make_backup()
        ring = asr_fallback.PcmRing(seconds=1.0)
        ring.append(b"\x01\x00" * 1600 * 3)  # 9 600 bytes / 300 ms
        try:
            self.assertTrue(backup.activate(ring, 0))
            self.assertTrue(backup.active)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any(
                    isinstance(event, dict) and event.get("translation") == "got" for event in events):
                time.sleep(0.02)
        finally:
            backup.stop()
        got = [event for event in events if isinstance(event, dict) and event.get("translation") == "got"]
        self.assertTrue(got)
        self.assertTrue(str(got[0]["sentence_id"]).startswith(backup.tag))
        self.assertTrue(got[0]["fallback"])
        replay = [event for event in events if isinstance(event, dict) and event.get("event") == "asr_fallback"]
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["replayed_ms"], 300)

    def test_errors_never_reach_the_main_session(self):
        backup, events = self.make_backup()
        error = b'{"event": "error", "code": "NO_INPUT_AUDIO_ERROR"}\n'
        backup._pump_stdout(mock.Mock(stdout=io.BytesIO(error)))
        self.assertEqual(events, [])
        self.assertIn("NO_INPUT_AUDIO_ERROR", backup.last_idle_error)


class ControllerTests(unittest.TestCase):
    def test_backup_starts_only_when_activated_and_stops_afterwards(self):
        args = mock.Mock(translation_enabled=True, source_language="ja", target_language="Chinese",
                         mt_model="qwen3.8-max", max_end_silence=500, workspace_id="",
                         hotwords="", asr_backup=True)
        events = []
        controller = asr_fallback.FallbackController(
            args, events.append, work_dir=Path("."),
            command=[sys.executable, "-u", "-c", FAKE_WORKER])
        self.assertFalse(controller.active)
        self.assertIsNone(controller.backup.proc)
        controller.feed(b"a" * 3200)
        self.assertIsNone(controller.backup.proc)
        self.assertTrue(controller.activate(0))
        self.assertTrue(controller.active)
        self.assertIsNotNone(controller.backup.proc)
        controller.deactivate()
        self.assertFalse(controller.active)
        self.assertIsNone(controller.backup.proc)


class QwenBackupRoutingTests(unittest.TestCase):
    def make_connection(self, *extra):
        args = qwen.parse_args(["--no-asr-backup", *extra])
        with mock.patch.object(qwen, "emit"):
            return qwen.QwenASRStreamConnection(args, "test-key")

    def test_backup_can_be_disabled(self):
        self.assertIsNone(self.make_connection().fallback)

    def test_stall_starts_the_backup_with_the_last_displayed_offset(self):
        connection = self.make_connection()
        stub = mock.Mock()
        stub.active = False
        stub.activate.return_value = True
        connection.fallback = stub
        connection.last_result_end_ms = 4200.0
        with mock.patch("gummy_realtime_worker.emit"):
            self.assertTrue(connection.on_stall({"age_s": 10}))
        stub.activate.assert_called_once_with(4200.0)
        self.assertTrue(connection.handed_over)

    def test_stall_without_a_usable_backup_keeps_the_normal_exit_path(self):
        connection = self.make_connection()
        stub = mock.Mock()
        stub.active = False
        stub.activate.return_value = False
        connection.fallback = stub
        with mock.patch("gummy_realtime_worker.emit"):
            self.assertFalse(connection.on_stall({"age_s": 10}))

    def test_audio_route_switches_between_main_backup_and_probation(self):
        connection = self.make_connection()
        self.assertEqual(connection.audio_route(), "main")
        connection.handing_over = True
        self.assertEqual(connection.audio_route(), "backup")
        connection.handing_over = False
        connection.fallback = mock.Mock(active=True)
        self.assertEqual(connection.audio_route(), "backup")
        connection.probation = True
        self.assertEqual(connection.audio_route(), "both")


if __name__ == "__main__":
    unittest.main()
