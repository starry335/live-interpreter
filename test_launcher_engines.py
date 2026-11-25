# -*- coding: utf-8 -*-
"""Engine selection, defaults and legacy-ICL removal in the launcher."""
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
class LauncherEngineTests(unittest.TestCase):
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
        app.update_model_controls()
        self.enable_frozen(app)
        command = app.command()
        self.assertEqual(command[command.index("--mt-mode") + 1], "reference")
        self.assertEqual(command[command.index("--mt-model") + 1], "qwen3.8-max")
        self.assertEqual(command[command.index("--gummy-max-end-silence") + 1], "500")
        self.assertNotIn("--hotword-cards", command)
        hotwords_arg = command[command.index("--hotwords") + 1]
        self.assertTrue(hotwords_arg.endswith("yumemita_bangdream_strict.txt"))

    def test_switching_to_3_1_keeps_the_longer_silence_default(self):
        app = self.make_app()
        self.assertEqual(str(app.gummy_max_end_silence.get()), "500")
        app.asr_engine.set("qwen3.1-audio")
        app.update_model_controls()
        self.assertEqual(str(app.gummy_max_end_silence.get()), "500")

    def test_qwen_audio_3_1_gets_the_builtin_hotword_list(self):
        app = self.make_app()
        app.asr_engine.set("qwen3.1-audio")
        self.enable_frozen(app)
        command = app.command()
        self.assertIn("--hotwords", command)
        self.assertTrue(command[command.index("--hotwords") + 1].endswith("yumemita_bangdream_strict.txt"))
        self.assertEqual(command[command.index("--mt-mode") + 1], "reference")

    def test_no_legacy_icl_toggle_and_always_reference_prompt(self):
        app = self.make_app()
        self.assertFalse(hasattr(app, "icl_check"))
        self.assertFalse(hasattr(app, "icl_enabled"))
        app.asr_engine.set("paraformer-mt")
        app.mt_model.set("qwen3.8-max")
        self.enable_frozen(app)
        command = app.command()
        self.assertEqual(command[command.index("--mt-mode") + 1], "reference")
        self.assertNotIn("--hotword-cards", command)

    def test_hotword_editor_shows_the_currently_loaded_external_file(self):
        app = self.make_app()
        external = self.directory / "custom_hotwords.txt"
        external.write_text("[hotwords]\nカスタム => 自定义\n", encoding="utf-8")
        app.hotwords.set(str(external))
        app.show_hotword_editor()
        try:
            texts = app._hotword_editor_texts
            self.assertIn("カスタム => 自定义", texts["hotwords"].get("1.0", "end"))
        finally:
            app.hotword_editor_window.destroy()

    def test_hotword_sections_have_titles_and_descriptions(self):
        self.assertEqual(len(launcher.HOTWORD_SECTIONS), 5)
        for section, title, description in launcher.HOTWORD_SECTIONS:
            self.assertTrue(section)
            self.assertTrue(title)
            self.assertGreater(len(description), 10)
        self.assertIn("hotwords", [item[0] for item in launcher.HOTWORD_SECTIONS])
        self.assertIn("asr-warm", [item[0] for item in launcher.HOTWORD_SECTIONS])

    def test_qwen_audio_3_1_engine_builds_the_right_command(self):
        app = self.make_app()
        app.asr_engine.set("qwen3.1-audio")
        app.mt_model.set("qwen3.8-max")
        self.enable_frozen(app)
        command = app.command()
        self.assertEqual(command[command.index("--asr-engine") + 1], "qwen3.1-audio")
        self.assertEqual(command[command.index("--mt-mode") + 1], "reference")

    def test_qwen_audio_3_1_with_edge_audio_is_not_silently_switched(self):
        app = self.make_app()
        app.asr_engine.set("qwen3.1-audio")
        app.audio_device.set(launcher.EDGE_TAB_LABEL)
        self.enable_frozen(app)
        command = app.command()
        self.assertEqual(command[command.index("--asr-engine") + 1], "qwen3.1-audio")

    def test_engine_id_is_listed_in_the_dropdown_source(self):
        source = Path(launcher.__file__).read_text(encoding="utf-8")
        self.assertIn("qwen3.1-audio", source)

    def test_old_3_1_settings_migrate_to_paraformer_defaults(self):
        launcher_settings.save(self.directory / "settings.json", {
            "settings_version": 2, "asr_engine": "qwen3.1-audio",
            "gummy_max_end_silence": "300", "icl_enabled": True,
        })
        app = self.make_app()
        self.assertEqual(app.asr_engine.get(), "paraformer-mt")
        self.assertEqual(str(app.gummy_max_end_silence.get()), "500")

    def test_043_migrates_to_3_1_when_workspace_is_set(self):
        launcher_settings.save(self.directory / "settings.json", {
            "settings_version": 3, "asr_engine": "paraformer-mt",
            "gummy_max_end_silence": "400", "aliyun_workspace_id": "ws-test",
        })
        app = self.make_app()
        self.assertEqual(app.asr_engine.get(), "qwen3.1-audio")
        self.assertEqual(str(app.gummy_max_end_silence.get()), "500")

    def test_043_without_workspace_stays_on_paraformer(self):
        launcher_settings.save(self.directory / "settings.json", {
            "settings_version": 3, "asr_engine": "qwen3.1-audio",
            "gummy_max_end_silence": "300", "aliyun_workspace_id": "",
        })
        app = self.make_app()
        self.assertEqual(app.asr_engine.get(), "paraformer-mt")
        self.assertEqual(str(app.gummy_max_end_silence.get()), "500")

    def test_legacy_engine_setting_is_migrated_to_paraformer(self):
        launcher_settings.save(self.directory / "settings.json", {"asr_engine": "qwen-audio-stream"})
        app = self.make_app()
        self.assertEqual(app.asr_engine.get(), "paraformer-mt")
        self.assertEqual(str(app.gummy_max_end_silence.get()), "500")


if __name__ == "__main__":
    unittest.main()
