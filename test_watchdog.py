"""ASR watchdog tests: only reconnect when speech audio keeps arriving."""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

import gummy_realtime_worker as grw
import qwen_asr_stream_worker as qwen


class WatchdogTests(unittest.TestCase):
    def make_conn(self, last_result_age: float, speech_age: float) -> grw.GummyConnection:
        conn = grw.GummyConnection.__new__(grw.GummyConnection)
        conn.engine_name = "Qwen-Audio-ASR-Streaming"
        conn.task_id = "test-task"
        conn.finished = threading.Event()
        conn.failed = threading.Event()
        conn.watchdog_fired = False
        conn.fallback = None
        conn.probation = False
        conn.last_result_end_ms = 0.0
        conn.handed_over = False
        conn.handing_over = False
        conn.ws = None
        conn.audio_bytes = 32000
        conn.close_code = None
        now = time.monotonic()
        conn.last_result_monotonic = now - last_result_age
        conn.last_audio_monotonic = now
        conn.last_speech_monotonic = now - speech_age
        return conn

    def test_speech_without_results_triggers_reconnect(self) -> None:
        conn = self.make_conn(last_result_age=12, speech_age=0)
        events = []
        with mock.patch.dict(os.environ, {"LIVE_INTERPRETER_ASR_WATCHDOG": "0.5"}):
            with mock.patch.object(grw, "emit", lambda payload: events.append(payload)):
                with mock.patch.object(grw.runtime_safety, "log_disconnect"):
                    conn._watchdog_loop()
        self.assertTrue(conn.failed.is_set())
        self.assertTrue(any(event.get("event") == "asr_stall" for event in events))

    def test_silence_does_not_trigger(self) -> None:
        conn = self.make_conn(last_result_age=12, speech_age=30)
        events = []
        timer = threading.Timer(1.1, conn.finished.set)
        timer.start()
        with mock.patch.dict(os.environ, {"LIVE_INTERPRETER_ASR_WATCHDOG": "0.5"}):
            with mock.patch.object(grw, "emit", lambda payload: events.append(payload)):
                with mock.patch.object(grw.runtime_safety, "log_disconnect"):
                    conn._watchdog_loop()
        self.assertFalse(conn.failed.is_set())
        self.assertFalse(any(event.get("event") == "asr_stall" for event in events))

    def test_rms_detects_speech_energy(self) -> None:
        quiet = b"\x00\x00" * 1600
        loud = b"\x00\x10" * 1600
        self.assertEqual(grw.GummyConnection._pcm_rms(quiet), 0)
        self.assertGreater(grw.GummyConnection._pcm_rms(loud), 500)

    def test_first_audio_restarts_the_silence_clock_for_a_warm_standby(self) -> None:
        conn = self.make_conn(last_result_age=60, speech_age=0)
        conn.last_audio_monotonic = 0.0
        conn.ws = mock.Mock()
        old = conn.last_result_monotonic
        conn.send_audio(b"\x00\x10" * 1600)
        self.assertGreater(conn.last_result_monotonic, old)
        self.assertGreater(conn.last_audio_monotonic, 0.0)

    def test_qwen_heartbeat_does_not_count_as_a_result(self) -> None:
        args = qwen.parse_args(["--no-asr-backup"])
        with mock.patch.object(qwen, "emit"):
            conn = qwen.QwenASRStreamConnection(args, "test-key")
        conn.last_result_monotonic = 100.0
        with mock.patch.object(qwen, "emit"):
            conn._emit_result({"payload": {"output": {"sentence": {
                "heartbeat": True, "text": "", "sentence_end": False}}}})
        self.assertEqual(conn.last_result_monotonic, 100.0)
        with mock.patch.object(qwen, "emit"):
            conn._emit_result({"payload": {"output": {"sentence": {
                "heartbeat": False, "text": "こんにちは", "sentence_end": False,
                "begin_time": 0, "end_time": 1000}}}})
        self.assertGreater(conn.last_result_monotonic, 100.0)


if __name__ == "__main__":
    unittest.main()
