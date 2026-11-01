import gc
import json
import os
from pathlib import Path
import tempfile
import time
from tkinter import Tk
import unittest
from unittest import mock

import launcher_settings as settings
import live_interpreter_launcher as launcher


@unittest.skipUnless(os.name == "nt", "Windows DPAPI settings")
class LauncherSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def make_app(self, devices=None):
        root = Tk()
        root.withdraw()
        with mock.patch.object(launcher, "Tk", return_value=root), \
                mock.patch.object(launcher, "hotword_memory_path", return_value=self.directory / "hotwords.json"), \
                mock.patch.object(launcher, "list_audio_devices", return_value=devices or [launcher.EDGE_TAB_LABEL, launcher.LOOPBACK_LABEL]):
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

    def test_dpapi_round_trip_and_plaintext_never_written(self):
        secret = "test-api-key-仅用于测试"
        encrypted = settings.protect_api_key(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(settings.unprotect_api_key(encrypted), secret)
        with self.assertRaises(ValueError):
            settings.save(self.directory / "unsafe.json", {"aliyun_api_key": secret})

    def test_all_configurations_survive_closing_and_reopening(self):
        first = self.make_app()
        strings = {name: getattr(first, name).get() for name in launcher.STRING_SETTINGS}
        strings.update(audio_device=launcher.LOOPBACK_LABEL, source_language="en", target_language="Chinese",
                       asr_engine="qwen3.5-live", mt_model="qwen-mt-flash", aliyun_workspace_id="ws-test",
                       aliyun_region="singapore", gummy_max_end_silence="900", chunk_seconds="2",
                       overlay_geometry="1000x300+30+40", overlay_font_size="29", overlay_hold_seconds="12",
                       port="8877", overlay_mode="直播透明窗口（免抠像）", work_dir=str(self.directory / "runtime"))
        for name, value in strings.items():
            getattr(first, name).set(value)
        booleans = {name: bool(getattr(first, name).get()) for name in launcher.BOOL_SETTINGS}
        booleans.update(overlay_enabled=False, overlay_show_source=True, translate_enabled=False, visual_enabled=True)
        for name, value in booleans.items():
            getattr(first, name).set(value)
        secret = "test-key-never-call-a-model"
        first.aliyun_api_key.set(secret)
        first.on_close()  # Flush pending edits even before the debounce timer fires.
        raw = (self.directory / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        second = self.make_app()
        self.assertEqual(second.aliyun_api_key.get(), secret)
        for name, value in {**strings, **booleans}.items():
            self.assertEqual(getattr(second, name).get(), value, name)
        self.assertIsNone(second.process)

    def test_idle_autosave_and_clearing_key_are_persistent(self):
        app = self.make_app()
        app.aliyun_api_key.set("temporary-test-key")
        app.overlay_font_size.set("33")
        deadline = time.monotonic() + 2
        while not app.settings_path.exists() and time.monotonic() < deadline:
            app.root.update()
            time.sleep(0.01)
        self.assertTrue(app.settings_path.exists())
        self.assertEqual(settings.load(app.settings_path)["overlay_font_size"], "33")
        app.aliyun_api_key.set("")
        self.assertTrue(app.save_settings())
        reopened = self.make_app()
        self.assertEqual(reopened.aliyun_api_key.get(), "")

    def test_unreadable_key_preserved_when_other_fields_change(self):
        path = self.directory / "settings.json"
        settings.save(path, {"overlay_font_size": "28", "api_key_dpapi": "dpapi:bad-ciphertext"})
        app = self.make_app()
        self.assertEqual(app.overlay_font_size.get(), "28")
        self.assertEqual(app.aliyun_api_key.get(), "")
        app.overlay_hold_seconds.set("15")
        self.assertTrue(app.save_settings())
        self.assertEqual(settings.load(path)["api_key_dpapi"], "dpapi:bad-ciphertext")
        app.aliyun_api_key.set("")  # Explicitly clearing it must also clear the old encrypted blob.
        self.assertTrue(app.save_settings())
        self.assertEqual(settings.load(path)["api_key_dpapi"], "")

    def test_failed_write_preserves_previous_file_and_missing_device_falls_back(self):
        path = self.directory / "settings.json"
        settings.save(path, {"audio_device": "removed microphone", "overlay_font_size": "26"})
        previous = path.read_bytes()
        app = self.make_app()
        self.assertEqual(app.audio_device.get(), launcher.EDGE_TAB_LABEL)
        app.overlay_font_size.set("30")
        with mock.patch.object(settings.os, "replace", side_effect=OSError("simulated write failure")):
            self.assertFalse(app.save_settings())
        self.assertEqual(path.read_bytes(), previous)
        self.assertTrue(app.save_settings())


if __name__ == "__main__":
    unittest.main()
