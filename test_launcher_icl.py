# -*- coding: utf-8 -*-
"""ICL toggle and the qwen-audio 3.1 engine entry in the launcher."""
import gc
import os
import sys
import tempfile
import unittest
from pathlib import Path
from tkinter import Tk
from unittest import mock

import launcher_settings
import live_interpreter_launcher as launcher


@unittest.skipUnless(os.name == "nt", "launcher UI tests are Windows only")
class IclToggleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def make_app(self):
        root = Tk()
        root.withdraw()
        with mock.patch.object(launcher, "Tk", return_value=root), \
                mock.patch.object(launcher, "hotword_memory_path", return_value=self.directory / "hotwords.json"), \
                mock.patch.object(launcher, "launcher_settings_path", return_value=self.directory / "settings.json"), \
                mock.patch.object(launcher, "list_audio_devices",
                                  return_value=[launcher.LOOPBACK_LABEL]):
            app = launcher.Launcher()
        self.addCleanup(self.dispose, root)
        return app

    @staticmethod
    def dispose(root):
        try:
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()
        except Exception:
            pass
        gc.collect()

    def enable_frozen(self, app):
        backend = self.directory / "LiveInterpreterBackend.exe"
        backend.write_bytes(b"stub")
        frozen = mock.patch.object(sys, "frozen", True, create=True)
        backend_patch = mock.patch.object(launcher, "backend_executable_path", return_value=backend)
        frozen.start()
        backend_patch.start()
        self.addCleanup(backend_patch.stop)
        self.addCleanup(frozen.stop)

    def test_fresh_install_defaults(self):
        app = self.make_app()
        self.assertEqual(app.asr_engine.get(), "paraformer-mt")
        self.assertEqual(app.mt_model.get(), "qwen3.8-max")
        self.assertTrue(app.icl_enabled.get())
        app.update_model_controls()
        self.assertNotEqual(str(app.icl_check["state"]), "disabled")
        self.enable_frozen(app)
        command = app.command()
        self.assertEqual(command[command.index("--mt-mode") + 1], "icl")
        self.assertIn("--hotword-cards", command)
        hotwords_arg = command[command.index("--hotwords") + 1]
        self.assertTrue(hotwords_arg.endswith("yumemita_bangdream.txt"))

    def test_qwen_audio_3_1_gets_the_builtin_hotword_list(self):
        app = self.make_app()
        app.asr_engine.set("qwen3.1-audio")
        self.enable_frozen(app)
        command = app.command()
        self.assertIn("--hotwords", command)
        self.assertTrue(command[command.index("--hotwords") + 1].endswith("yumemita_bangdream.txt"))
        self.assertEqual(command[command.index("--mt-mode") + 1], "icl")

    def test_icl_is_on_by_default_and_can_be_switched_off(self):
        app = self.make_app()
        app.asr_engine.set("paraformer-mt")
        app.mt_model.set("qwen3.8-max")
        self.assertTrue(app.icl_enabled.get())
        self.enable_frozen(app)
        command = app.command()
        self.assertEqual(command[command.index("--mt-mode") + 1], "icl")
        self.assertIn("--hotword-cards", command)

        app.icl_enabled.set(False)
        command = app.command()
        self.assertEqual(command[command.index("--mt-mode") + 1], "force")
        self.assertNotIn("--hotword-cards", command)

    def test_qwen_mt_models_disable_the_toggle(self):
        app = self.make_app()
        app.asr_engine.set("paraformer-mt")
        app.mt_model.set("qwen-mt-plus")
        app.update_model_controls()
        self.assertEqual(str(app.icl_check["state"]), "disabled")

        app.mt_model.set("qwen3.8-max")
        app.update_model_controls()
        self.assertNotEqual(str(app.icl_check["state"]), "disabled")

    def test_qwen_audio_3_1_engine_builds_the_right_command(self):
        app = self.make_app()
        app.asr_engine.set("qwen3.1-audio")
        app.mt_model.set("qwen3.8-max")
        self.enable_frozen(app)
        command = app.command()
        self.assertEqual(command[command.index("--asr-engine") + 1], "qwen3.1-audio")
        self.assertEqual(command[command.index("--mt-mode") + 1], "icl")

    def test_engine_id_is_listed_in_the_dropdown_source(self):
        source = Path(launcher.__file__).read_text(encoding="utf-8")
        self.assertIn("qwen3.1-audio", source)

    def test_legacy_engine_setting_is_mapped(self):
        launcher_settings.save(self.directory / "settings.json", {"asr_engine": "qwen-audio-stream"})
        app = self.make_app()
        self.assertEqual(app.asr_engine.get(), "qwen3.1-audio")


if __name__ == "__main__":
    unittest.main()
