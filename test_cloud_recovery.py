import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from live_interpreter import (
    BrowserAudioInput, State, cloud_retry_delay, cloud_stream_loop, handle_cloud_output,
    load_overlay_geometry, make_handler, parse_args, save_overlay_geometry,
)


class CloudRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the extension's subtitle filter")
    def test_updated_final_translation_is_not_hidden_as_expired(self):
        snapshots = []
        for engine in ("Qwen3.5 LiveTranslate", "Qwen3.8 LiveTranslate"):
            state = State()
            state.set_status("waiting for Edge tab audio")
            for timestamp, text, final in ((100, "第一句", True), (130, "新的正确译文", False),
                                           (160, "新的正确译文", True)):
                message = {"event": "result", "sentence_id": "1", "translation": text,
                           "translation_final": final}
                process = mock.Mock(stdout=io.BytesIO((json.dumps(message) + "\n").encode()))
                with mock.patch("live_interpreter.time.monotonic", return_value=timestamp):
                    handle_cloud_output(process, state, threading.Event(), {}, True, engine, "test")
                    snapshot = state.snapshot()
                if timestamp > 100:
                    snapshots.append(snapshot)
            self.assertEqual(snapshot["status"], f"listening ({engine})")
            self.assertEqual(snapshot["events"][0]["finalized_at"], 160)

        # Run the actual content.js filter, including its expiry checks.
        script = r'''
const fs = require('node:fs');
const source = fs.readFileSync('edge_extension/content.js', 'utf8');
const body = source.slice(source.indexOf('    const now = Number(snapshot.monotonic_now);'),
                          source.indexOf('    const translated = events.filter'));
const select = new Function('snapshot', 'fontSize', 'holdSeconds', body + '\nreturn events;');
const snapshots = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(snapshots.map(snapshot => select(snapshot, 16, 8))));
'''
        result = subprocess.run([shutil.which("node"), "-e", script],
                                cwd=Path(__file__).parent, input=json.dumps(snapshots),
                                text=True, encoding="utf-8", capture_output=True, check=True)
        for events in json.loads(result.stdout):
            self.assertEqual([item["translated"] for item in events], ["新的正确译文"])

    def test_qwen38_routes_to_qwen_worker_with_model_and_visual_input(self):
        with tempfile.TemporaryDirectory() as directory:
            args = parse_args(["--asr-engine", "qwen3.8-live", "--audio-device", "__edge_tab_audio__",
                               "--visual-input", "--work-dir", directory, "--aliyun-workspace-id", "test"])
            stop = threading.Event()
            with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test"}), mock.patch(
                "live_interpreter.run_cloud_session", side_effect=lambda *unused: stop.set()
            ) as session:
                cloud_stream_loop(args, State(), stop, {"proc": None}, {"proc": None}, BrowserAudioInput())
            command = session.call_args.args[6]
            self.assertEqual(command[command.index("--model") + 1], "qwen3.8-livetranslate-flash-realtime")
            self.assertEqual(command[command.index("--visual-file") + 1], str(Path(directory) / "visual_frame.jpg"))
            self.assertEqual(session.call_args.args[7], "Qwen3.8 LiveTranslate")

    def test_backoff_is_capped(self):
        self.assertEqual([cloud_retry_delay(n) for n in range(1, 9)], [1, 2, 4, 8, 16, 30, 30, 30])

    def test_retention_clock_pauses_and_partial_is_retired_on_recovery(self):
        state = State()
        with mock.patch("live_interpreter.time.monotonic", return_value=100):
            state.upsert_event("old", translated="complete", final=True)
            state.upsert_event("partial", translated="unfinished", final=False)
        with mock.patch("live_interpreter.time.monotonic", return_value=103):
            state.begin_reconnect()
        with mock.patch("live_interpreter.time.monotonic", return_value=200):
            self.assertEqual(state.snapshot()["monotonic_now"], 103)
            state.finish_reconnect()
            snapshot = state.snapshot()
        self.assertFalse(snapshot["reconnecting"])
        self.assertEqual(snapshot["events"][0]["finalized_at"], 197)
        self.assertEqual(snapshot["events"][1]["finalized_at"], 200)
        self.assertTrue(snapshot["events"][1]["final"])

    def test_geometry_round_trip_and_invalid_file_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overlay_window.json"
            geometry = (1100, 350, -250, 470)
            save_overlay_geometry(path, geometry)
            self.assertEqual(load_overlay_geometry(path, (980, 280, 0, 0)), geometry)
            path.write_text('{"width": 1}', encoding="utf-8")
            self.assertEqual(load_overlay_geometry(path, geometry), geometry)

    def test_repeated_startup_failures_back_off_and_stop_interrupts_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            args = parse_args(["--audio-device", "__edge_tab_audio__", "--work-dir", directory,
                               "--aliyun-workspace-id", "test"])
            stop = threading.Event()
            waits = []

            def wait(delay):
                waits.append(delay)
                if len(waits) == 4:
                    stop.set()
                    return True
                return False

            def fail(*args):
                args[-1]["error"] = "injected startup failure"

            with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test"}), mock.patch(
                "live_interpreter.run_cloud_session", side_effect=fail
            ) as session, mock.patch.object(stop, "wait", side_effect=wait):
                cloud_stream_loop(args, State(), stop, {"proc": None}, {"proc": None}, BrowserAudioInput())
            self.assertEqual(waits, [1, 2, 4, 8])
            self.assertEqual(session.call_count, 4)

    def test_session_ids_prevent_overwriting_repeated_sentences(self):
        state = State()
        for session in ("first", "second"):
            line = json.dumps({"event": "result", "sentence_id": "1", "translation": "same word", "translation_final": True})
            process = mock.Mock(stdout=io.BytesIO((line + "\n").encode()))
            handle_cloud_output(process, state, threading.Event(), {}, True, "Qwen", session)
        self.assertEqual([item["chunk"] for item in state.snapshot()["events"]], ["Qwen-first-1", "Qwen-second-1"])

    def test_crashed_worker_restarts_without_stopping_http_or_clearing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            args = parse_args(["--audio-device", "__edge_tab_audio__", "--work-dir", directory,
                               "--aliyun-workspace-id", "test", "--no-overlay"])
            state = State()
            state.upsert_event("history", translated="keep me", final=True)
            audio = BrowserAudioInput()
            audio.start()
            stop = threading.Event()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, audio, stop))
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            original_popen = subprocess.Popen
            processes = []

            def spawn(_command, **kwargs):
                attempt = len(processes)
                script = (
                    "import sys,json\n"
                    "print(json.dumps({'event':'ready'}),flush=True)\n"
                    "sys.stdin.buffer.read(3200)\n"
                    "print(json.dumps({'event':'result','sentence_id':'1','translation':'same word','translation_final':True}),flush=True)\n"
                )
                if attempt == 0:
                    script += "print(json.dumps({'event':'error','code':'InjectedTimeout','message':'test disconnect'}),flush=True)\n"
                else:
                    script += "while sys.stdin.buffer.read(3200): pass\n"
                proc = original_popen([sys.executable, "-u", "-c", script], **kwargs)
                processes.append(proc)
                return proc

            def feed():
                while not stop.wait(0.02):
                    audio.push(b"\x00\x00" * 1600)

            worker = threading.Thread(target=cloud_stream_loop,
                                      args=(args, state, stop, {"proc": None}, {"proc": None}, audio))
            feeder = threading.Thread(target=feed)
            try:
                with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-api-key"}), mock.patch(
                    "live_interpreter.subprocess.Popen", side_effect=spawn
                ):
                    worker.start()
                    feeder.start()
                    deadline = time.monotonic() + 8
                    saw_reconnecting = False
                    while time.monotonic() < deadline:
                        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/events", timeout=1) as response:
                            snapshot = json.load(response)
                        saw_reconnecting |= snapshot["reconnecting"]
                        self.assertFalse(stop.is_set())
                        if len(snapshot["events"]) >= 3 and len(processes) >= 2:
                            break
                        time.sleep(0.02)
                    self.assertTrue(saw_reconnecting)
                    self.assertGreaterEqual(len(processes), 2)
                    self.assertEqual(snapshot["events"][0]["translated"], "keep me")
                    self.assertEqual(len(snapshot["events"]), 3)
                    self.assertNotEqual(snapshot["events"][1]["chunk"], snapshot["events"][2]["chunk"])
                    self.assertIn("InjectedTimeout", (Path(directory) / "cloud_recovery.log").read_text(encoding="utf-8"))
                    stop.set()
                    worker.join(timeout=4)
                    self.assertFalse(worker.is_alive())
            finally:
                stop.set()
                for proc in processes:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=3)
                if worker.ident:
                    worker.join(timeout=3)
                if feeder.ident:
                    feeder.join(timeout=1)
                server.shutdown()
                server.server_close()
            self.assertTrue(all(proc.poll() is not None for proc in processes))


if __name__ == "__main__":
    unittest.main()
