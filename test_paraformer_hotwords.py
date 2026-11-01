import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import gummy_realtime_worker
import hotwords
import live_interpreter as live
import paraformer_mt_worker as worker
import qwen_livetranslate_worker as qwen_worker


HOTWORDS = """\
あられ => 阿拉蕾
ゆのち => 由乃亲
ゆの => 由乃
!战国由乃 => 由乃
"""


def response_with(content):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "id": "request-test",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }).encode()
    return response


class ParaformerHotwordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "hotwords.txt"
        self.path.write_text(HOTWORDS, encoding="utf-8")

    def connection(self, extra=None):
        return worker.ParaformerMTConnection(
            worker.parse_args(["--hotwords", str(self.path), *(extra or [])]), "test-key"
        )

    def test_vocabulary_id_is_added_to_run_task(self):
        connection = self.connection()
        with mock.patch.object(worker.hotwords, "ensure_vocabulary", return_value="vocab-123") as ensure, \
                mock.patch.object(worker, "emit") as output:
            connection.prepare_hotwords()
        request = connection.task_request()
        self.assertEqual(request["payload"]["parameters"]["vocabulary_id"], "vocab-123")
        self.assertEqual(ensure.call_args.args[2], ["あられ", "ゆのち", "ゆの"])
        self.assertTrue(any(call.args[0].get("event") == "hotword_vocabulary" for call in output.call_args_list))

    def test_vocabulary_failure_falls_back_to_translation_terms(self):
        connection = self.connection()
        with mock.patch.object(worker.hotwords, "ensure_vocabulary", side_effect=RuntimeError("quota")), \
                mock.patch.object(worker.LOGGER, "warning"), \
                mock.patch.object(worker, "emit") as output:
            connection.prepare_hotwords()
        self.assertEqual(connection.vocabulary_id, "")
        self.assertNotIn("vocabulary_id", connection.task_request()["payload"]["parameters"])
        warnings = [call.args[0] for call in output.call_args_list if call.args[0].get("event") == "warning"]
        self.assertTrue(warnings)
        self.assertIn("ASR hotwords disabled", warnings[0]["message"])

    def test_qwen_mt_receives_only_sentence_terms(self):
        connection = self.connection()
        with mock.patch.object(worker.urllib.request, "urlopen", return_value=response_with("和由乃亲一起做")) as post, \
                mock.patch.object(worker, "emit"):
            translated = connection.translate("ゆのちと一緒にやる")
        body = json.loads(post.call_args.args[0].data)
        self.assertEqual(translated, "和由乃亲一起做")
        self.assertEqual(body["translation_options"]["terms"], [
            {"source": "ゆのち", "target": "由乃亲"},
        ])
        self.assertNotIn("あられ", [term["source"] for term in body["translation_options"]["terms"]])

    def test_forbidden_output_is_rewritten(self):
        connection = self.connection()
        with mock.patch.object(worker.urllib.request, "urlopen", return_value=response_with("我是战国由乃")), \
                mock.patch.object(worker, "emit"):
            self.assertEqual(connection.translate("千石ユノです"), "我是由乃")

    def test_general_model_prompt_contains_glossary(self):
        connection = self.connection(["--mt-model", "qwen3.8-flash"])
        with mock.patch.object(worker.urllib.request, "urlopen", return_value=response_with("译文")) as post, \
                mock.patch.object(worker, "emit"):
            connection.translate("ゆのちと一緒にやる")
        body = json.loads(post.call_args.args[0].data)
        self.assertNotIn("translation_options", body)
        self.assertIn("ゆのち=>由乃亲", body["messages"][0]["content"])

    def test_bad_hotword_file_is_a_fatal_startup_error(self):
        missing = str(Path(self.directory.name) / "missing.txt")
        with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test"}), \
                mock.patch.object(worker, "emit") as output:
            code = worker.main(["--hotwords", missing])
        self.assertEqual(code, 2)
        self.assertTrue(any(call.args[0].get("event") == "fatal_error" for call in output.call_args_list))

    def test_cloud_worker_command_passes_the_resolved_path(self):
        args = live.parse_args([
            "--asr-engine", "paraformer-mt", "--audio-device", live.BROWSER_DEVICE,
            "--work-dir", self.directory.name, "--hotwords", str(self.path),
        ])
        stop = threading.Event()

        def failed(*values):
            values[-1].update(error="stop", fatal=True)

        with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test"}), \
                mock.patch.object(live, "run_cloud_session", side_effect=failed) as session:
            live.cloud_stream_loop(args, live.State(), stop, {"proc": None}, {"proc": None}, live.BrowserAudioInput())
        command = session.call_args.args[6]
        self.assertIn("--hotwords", command)
        self.assertEqual(command[command.index("--hotwords") + 1], str(self.path.resolve()))


class OtherEngineHotwordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "hotwords.txt"
        self.path.write_text(HOTWORDS, encoding="utf-8")

    def test_livetranslate_gets_translation_corpus(self):
        args = qwen_worker.parse_args(["--hotwords", str(self.path), "--workspace-id", "ws"])
        args.hotword_config = hotwords.load(self.path)
        session = qwen_worker.session_config(args)
        self.assertEqual(session["translation"]["corpus"]["phrases"]["ゆのち"], "由乃亲")

    def test_gummy_accepts_the_shared_flag_but_has_no_terms_api(self):
        args = gummy_realtime_worker.parse_args(["--hotwords", str(self.path)])
        self.assertEqual(args.hotwords, str(self.path))


if __name__ == "__main__":
    unittest.main()
