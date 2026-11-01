import gc
import io
import os
import tempfile
from tkinter import Tk
import unittest
from unittest import mock

import live_interpreter as live
import live_interpreter_launcher as launcher_module


class CaptureWindowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patcher = mock.patch.object(launcher_module, "launcher_settings_path", return_value=launcher_module.Path(temporary.name) / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        gc.collect()

    @unittest.skipUnless(os.name == "nt", "Windows desktop overlay transparency")
    def test_desktop_overlay_preserves_its_transparency(self):
        root = Tk()
        try:
            args = live.parse_args(["--overlay-window-mode", "desktop"])
            self.assertEqual(live.configure_subtitle_window(root, args, (640, 240, 20, 20)), "#010203")
            root.update()
            self.assertEqual(root.title(), "Live Interpreter Overlay")
            self.assertTrue(root.overrideredirect())
            self.assertAlmostEqual(float(root.attributes("-alpha")), args.overlay_alpha, places=2)
            self.assertEqual(str(root.wm_attributes("-transparentcolor")), "#010203")
            self.assertTrue(root.attributes("-topmost"))
        finally:
            root.destroy()

    def test_only_transparent_modes_are_available(self):
        self.assertEqual(set(launcher_module.OVERLAY_MODES.values()), {"desktop", "alpha"})
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
            live.parse_args(["--overlay-window-mode", "capture"])
        self.assertEqual(error.exception.code, 2)

    def test_preview_needs_no_audio_key_server_or_model(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}), \
                mock.patch.object(live, "run_overlay_window") as window, \
                mock.patch.object(live, "ThreadingHTTPServer") as server, \
                mock.patch.object(live, "cloud_stream_loop") as cloud, \
                mock.patch.object(live, "start_audio_capture") as audio:
            self.assertEqual(live.main(["--preview-overlay", "--overlay-window-mode", "alpha", "--work-dir", directory]), 0)
            args, state, unused = window.call_args.args
            self.assertEqual(args.overlay_window_mode, "alpha")
            self.assertIn("字幕捕获测试", state.snapshot()["events"][0]["translated"])
            server.assert_not_called()
            cloud.assert_not_called()
            audio.assert_not_called()

    def test_launcher_capture_mode_and_preview_command(self):
        root = Tk()
        root.withdraw()
        try:
            with mock.patch.object(launcher_module, "Tk", return_value=root), \
                    mock.patch.object(launcher_module, "list_audio_devices", return_value=[launcher_module.LOOPBACK_LABEL]), \
                    mock.patch.object(launcher_module, "runtime_script_path", return_value=launcher_module.Path(__file__)), \
                    mock.patch.object(launcher_module, "ENV_PYTHON", launcher_module.Path(__file__)):
                launcher = launcher_module.Launcher()
                launcher.overlay_mode.set("直播透明窗口（免抠像）")
                launcher.audio_device.set(launcher_module.LOOPBACK_LABEL)
                command = launcher.command()
                self.assertEqual(command[command.index("--overlay-window-mode") + 1], "alpha")
                self.assertEqual(command[command.index("--audio-device") + 1], "__wasapi_loopback__")
                self.assertNotIn("--no-overlay", command)
                launcher.audio_device.set(launcher_module.EDGE_TAB_LABEL)
                self.assertNotIn("--no-overlay", launcher.command())
                with mock.patch.object(root, "clipboard_clear"), mock.patch.object(root, "clipboard_append") as clipboard:
                    launcher.copy_transparent_source()
                    clipboard.assert_called_once_with("http://127.0.0.1:8765/overlay")
                with mock.patch.object(launcher_module.subprocess, "Popen") as process:
                    launcher.preview_overlay()
                    self.assertIn("--preview-overlay", process.call_args.args[0])
                    self.assertNotIn("DASHSCOPE_API_KEY", process.call_args.kwargs["env"])
                launcher.overlay_enabled.set(False)
                self.assertIn("--no-overlay", launcher.command())
        finally:
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()


if __name__ == "__main__":
    unittest.main()
