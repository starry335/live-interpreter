import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

import gummy_realtime_worker
import live_interpreter as live
import paraformer_mt_worker as worker


def sentence(text, final, begin=100):
    return {"payload": {"output": {"sentence": {
        "text": text, "sentence_end": final, "begin_time": begin, "end_time": 1200,
    }}, "usage": {"duration": 2}}}


class ParaformerMTTests(unittest.TestCase):
    def setUp(self):
        self.connection = worker.ParaformerMTConnection(worker.parse_args([]), "test-key")

    def test_only_final_sentences_are_translated_and_duplicates_are_free(self):
        with mock.patch.object(worker, "emit"):
            self.connection._emit_result(sentence("今日は", False))
            self.assertTrue(self.connection.pending.empty())
            self.connection._emit_result(sentence("今日は晴れです。", True))
            self.connection._emit_result(sentence("今日は晴れです。", True))
            # Identical speech later in the stream is a different sentence.
            self.connection._emit_result(sentence("今日は晴れです。", True, begin=2000))
        self.assertEqual(self.connection.pending.qsize(), 2)
        self.assertEqual(self.connection.asr_duration, 2)
        request = self.connection.task_request()
        self.assertEqual(request["payload"]["model"], worker.ASR_MODEL)
        self.assertEqual(request["payload"]["parameters"]["language_hints"], ["ja"])
        self.assertNotIn("translation_enabled", request["payload"]["parameters"])

    def test_http_translation_uses_one_user_message_and_records_usage(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "id": "request-test", "choices": [{"message": {"content": "今天晴天。"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5},
        }).encode()
        with mock.patch.object(worker.urllib.request, "urlopen", return_value=response) as post, mock.patch.object(worker, "emit"):
            self.assertEqual(self.connection.translate("今日は晴れです。"), "今天晴天。")
        body = json.loads(post.call_args.args[0].data)
        self.assertEqual(body["model"], "qwen-mt-lite")
        self.assertEqual(body["messages"], [{"role": "user", "content": "今日は晴れです。"}])
        self.assertEqual(body["translation_options"], {"source_lang": "ja", "target_lang": "zh"})
        self.assertEqual((self.connection.input_tokens, self.connection.output_tokens), (12, 5))

    def test_translation_permission_error_stops_without_paid_retry(self):
        with mock.patch.object(worker, "emit") as output:
            self.connection._emit_result(sentence("こんにちは", True))
            error = urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"invalid key"))
            with mock.patch.object(self.connection, "translate", side_effect=error) as translate:
                self.connection._translate_loop()
        self.assertEqual(translate.call_count, 1)
        self.assertTrue(self.connection.mt_fatal.is_set())
        self.assertFalse(self.connection.failed.is_set())
        self.assertTrue(any(call.args[0]["event"] == "fatal_error" for call in output.call_args_list))
        self.connection.ws = mock.Mock()
        self.connection.started.set()
        with mock.patch.object(self.connection.finished, "wait", return_value=True), mock.patch.object(worker, "emit"):
            self.connection.close()
        self.assertEqual(json.loads(self.connection.ws.send.call_args.args[0])["header"]["action"], "finish-task")

    def test_general_models_disable_thinking_and_keep_bounded_context(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "译文"}, "finish_reason": "stop"}], "usage": {},
        }).encode()
        for model in worker.MT_MODELS:
            connection = worker.ParaformerMTConnection(worker.parse_args(["--mt-model", model]), "test")
            with self.subTest(model=model), mock.patch.object(worker.urllib.request, "urlopen", return_value=response) as post, mock.patch.object(worker, "emit"):
                for n in range(4):
                    connection.translate(f"{n}" + "あ" * 300)
                body = json.loads(post.call_args.args[0].data)
                self.assertEqual(body["model"], model)
                if model.startswith("qwen-mt-"):
                    self.assertEqual(len(body["messages"]), 1)
                    self.assertIn("translation_options", body)
                else:
                    self.assertFalse(body["enable_thinking"])
                    self.assertNotIn("translation_options", body)
                    self.assertEqual(body["messages"][0]["role"], "system")
                    content = json.loads(body["messages"][1]["content"])
                    self.assertTrue(content["current_text"].startswith("3"))
                    self.assertEqual([c["source"][0] for c in content["previous_subtitles"]], ["1", "2"])
                    self.assertTrue(all(len(c["source"]) <= 240 for c in content["previous_subtitles"]))

    def test_truncated_output_is_not_shown_as_a_complete_translation(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "不完整"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 1024},
        }).encode()
        with mock.patch.object(worker.urllib.request, "urlopen", return_value=response), mock.patch.object(worker, "emit"):
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                self.connection.translate("こんにちは")
        self.assertFalse(self.connection.translation_context)
        self.assertEqual(self.connection.output_tokens, 1024)

    def test_heartbeat_disabled_translation_and_pcm_validation(self):
        self.connection.args.translation_enabled = False
        heartbeat = sentence("", True)
        heartbeat["payload"]["output"]["sentence"]["heartbeat"] = True
        with mock.patch.object(worker, "emit") as output:
            self.connection._emit_result(heartbeat)
            output.assert_not_called()
            self.connection._emit_result(sentence("こんにちは", True))
        self.assertTrue(self.connection.pending.empty())
        with self.assertRaises(ValueError):
            self.connection.send_audio(b"q")

    def test_pcm_shutdown_sends_eof_without_ffmpeg_quit_byte(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex(), flush=True)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        process.stdin.write(b"\x00\x00")
        process.stdin.flush()
        live.stop_process(process, pcm_input=True)
        self.assertEqual(process.stdout.read().strip(), b"0000")
        process.stdout.close()

    def test_backend_routing_and_fatal_error_does_not_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            args = live.parse_args(["--asr-engine", "paraformer-mt", "--audio-device", live.BROWSER_DEVICE,
                                    "--work-dir", directory, "--mt-model", "qwen3.8-max"])
            stop = threading.Event()
            def failed(*values):
                values[-1].update(error="invalid MT key", fatal=True)
            with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test"}), mock.patch.object(
                live, "run_cloud_session", side_effect=failed
            ) as session:
                live.cloud_stream_loop(args, live.State(), stop, {"proc": None}, {"proc": None}, live.BrowserAudioInput())
            self.assertTrue(stop.is_set())
            self.assertEqual(session.call_count, 1)
            command = session.call_args.args[6]
            self.assertIn(str(live.PARAFORMER_MT_WORKER), command)
            self.assertIn(str(Path(directory) / "paraformer_mt.jsonl"), command)
            self.assertEqual(command[command.index("--mt-model") + 1], "qwen3.8-max")

    def test_gummy_run_task_remains_unchanged(self):
        gummy = gummy_realtime_worker.GummyConnection(gummy_realtime_worker.parse_args([]), "test")
        payload = gummy.task_request()["payload"]
        self.assertEqual(payload["model"], "gummy-realtime-v1")
        self.assertTrue(payload["parameters"]["translation_enabled"])


if __name__ == "__main__":
    unittest.main()
