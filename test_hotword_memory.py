import gc
import json
from pathlib import Path
import tempfile
import threading
from tkinter import Text, Tk, ttk
import unittest
from unittest import mock

import hotword_memory as memory
import live_interpreter as live
import live_interpreter_launcher as launcher
import paraformer_mt_worker as mt
import qwen_livetranslate_worker as qwen


class HotwordMemoryTests(unittest.TestCase):
    def test_persistence_disable_and_invalid_edits_preserve_saved_terms(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotwords.json"
            terms = memory.parse_hotwords("# 直播术语\n山田リョウ = 山田凉\nQoder => Qoder\n")
            memory.save_memory(path, {"enabled": True, "terms": terms})
            self.assertEqual(memory.load_hotwords(path), terms)
            for invalid in ("缺少译法", "a = b\na = c", "a = " + "字" * 201):
                with self.subTest(invalid=invalid[:20]), self.assertRaises(ValueError):
                    memory.parse_hotwords(invalid)
                self.assertEqual(memory.load_hotwords(path), terms)
            memory.save_memory(path, {"enabled": False, "terms": terms})
            self.assertEqual(memory.load_hotwords(path), {})
            self.assertEqual(memory.load_memory(path)["terms"], terms)

    def test_native_qwen_hotwords_and_worker_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotwords.json"
            memory.save_memory(path, {"enabled": True, "terms": {"山田リョウ": "山田凉"}})
            for model, engine in ((qwen.MODEL, "qwen3.5-live"), (qwen.MODEL_38, "qwen3.8-live"),
                                  (None, "paraformer-mt")):
                if model:
                    config = qwen.session_config(qwen.parse_args(["--model", model, "--hotwords-file", str(path)]))
                    self.assertEqual(config["translation"]["corpus"]["phrases"]["山田リョウ"], "山田凉")
                    self.assertEqual(config["translation"]["corpus"]["phrases"]["あられ"], "阿拉蕾")
                args = live.parse_args(["--asr-engine", engine, "--work-dir", directory,
                                        "--hotwords-file", str(path), "--aliyun-workspace-id", "test"])
                stop = threading.Event()
                with mock.patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test"}), \
                        mock.patch.object(live, "run_cloud_session", side_effect=lambda *args: stop.set()) as session:
                    live.cloud_stream_loop(args, live.State(), stop, {}, {}, live.BrowserAudioInput())
                command = session.call_args.args[6]
                self.assertEqual(command[command.index("--hotwords-file") + 1], str(path))

    def test_translation_sends_only_matching_terms_for_native_and_general_models(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotwords.json"
            memory.save_memory(path, {"enabled": True, "terms": {"山田リョウ": "山田凉", "unused": "无关词"}})
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "choices": [{"message": {"content": "山田凉来了"}, "finish_reason": "stop"}], "usage": {},
            }).encode()
            for model in mt.MT_MODELS:
                connection = mt.ParaformerMTConnection(mt.parse_args(["--mt-model", model, "--hotwords-file", str(path)]), "test")
                with self.subTest(model=model), mock.patch.object(mt.urllib.request, "urlopen", return_value=response) as post, \
                        mock.patch.object(mt, "emit"):
                    connection.translate("山田リョウが来た")
                    body = json.loads(post.call_args.args[0].data)
                    terms = [{"source": "山田リョウ", "target": "山田凉"}]
                    if model.startswith("qwen-mt-"):
                        self.assertEqual(body["translation_options"]["terms"], terms)
                        self.assertEqual(len(body["messages"]), 1)
                    else:
                        self.assertEqual(json.loads(body["messages"][1]["content"])["glossary"], terms)
                    self.assertNotIn("unused", post.call_args.args[0].data.decode())
                    self.assertEqual(post.call_count, 1)

    def test_editor_save_reload_and_launcher_command(self):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotwords.json"
            root = Tk()
            root.withdraw()
            try:
                with mock.patch.object(launcher, "Tk", return_value=root), \
                        mock.patch.object(launcher, "hotword_memory_path", return_value=path), \
                        mock.patch.object(launcher, "list_audio_devices", return_value=[launcher.LOOPBACK_LABEL]), \
                        mock.patch.object(launcher, "runtime_script_path", return_value=Path(__file__)), \
                        mock.patch.object(launcher, "ENV_PYTHON", Path(__file__)):
                    app = launcher.Launcher()
                    first_command = app.command()
                    self.assertEqual(first_command[first_command.index("--hotwords") + 1], str(launcher.hotwords.builtin_path()))
                    self.assertFalse(path.exists())
                    app.show_hotword_memory()
                    widgets = list(descendants(app.hotword_window))
                    editor = next(widget for widget in widgets if isinstance(widget, Text))
                    editor.insert("1.0", "山田リョウ = 山田凉")
                    save = next(widget for widget in widgets if isinstance(widget, ttk.Button) and widget.cget("text") == "保存")
                    save.invoke()
                    self.assertEqual(memory.load_hotwords(path), {"山田リョウ": "山田凉"})
                    for engine in ("qwen3.5-live", "qwen3.8-live", "paraformer-mt"):
                        app.asr_engine.set(engine)
                        command = app.command()
                        self.assertEqual(command[command.index("--hotwords-file") + 1], str(path))
                    app.show_hotword_memory()
                    editor = next(widget for widget in descendants(app.hotword_window) if isinstance(widget, Text))
                    self.assertEqual(editor.get("1.0", "end-1c"), "山田リョウ = 山田凉")
                    external = Path(directory) / "advanced.txt"
                    external.write_text("~戦国ユーノ => 由乃\n!战国由乃 => 由乃\n", encoding="utf-8")
                    app.hotwords.set(str(external))
                    command = app.command()
                    self.assertEqual(command[command.index("--hotwords") + 1], str(external.resolve()))
                    self.assertNotIn("--hotwords-file", command)
                    app.hotwords.set("")
                    self.assertIn("--hotwords-file", app.command())
                    self.assertIsNone(app.process)
            finally:
                for callback in root.tk.call("after", "info"):
                    root.after_cancel(callback)
                root.destroy()
                gc.collect()


if __name__ == "__main__":
    unittest.main()
