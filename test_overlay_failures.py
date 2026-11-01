import gc
import io
import os
from pathlib import Path
import tempfile
import threading
from tkinter import Tk
import unittest
from unittest import mock

import live_interpreter as live


@unittest.skipUnless(os.name == "nt", "Windows subtitle windows")
class OverlayFailureTests(unittest.TestCase):
    def tearDown(self):
        gc.collect()

    def test_alpha_window_stays_above_other_apps(self):
        with tempfile.TemporaryDirectory() as directory:
            args = live.parse_args(["--overlay-window-mode", "alpha", "--work-dir", directory])
            stopped = threading.Event()
            root = Tk()
            root.after(2000, root.destroy)

            def check_window(*unused):
                self.assertTrue(root.attributes("-topmost"))
                stopped.set()

            with mock.patch("tkinter.Tk", return_value=root), \
                    mock.patch("alpha_subtitle_window.AlphaSurface.draw", side_effect=check_window):
                live.run_overlay_window(args, live.State(), stopped)
            self.assertTrue(stopped.is_set())

    def test_render_failure_stops_translation_and_records_traceback(self):
        for mode, renderer in (("desktop", "tkinter.Canvas.create_text"),
                               ("alpha", "alpha_subtitle_window.AlphaSurface.draw")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                args = live.parse_args(["--overlay-window-mode", mode, "--work-dir", directory])
                state = live.State()
                state.upsert_event("test-1", translated="字幕测试", final=True)
                stopped = threading.Event()
                root = Tk()
                # Bound the test even if the old silent callback failure returns.
                root.after(2000, root.destroy)
                with mock.patch("tkinter.Tk", return_value=root), \
                        mock.patch(renderer, side_effect=RuntimeError("simulated render failure")), \
                        mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    with self.assertRaisesRegex(RuntimeError, "simulated render failure"):
                        live.run_overlay_window(args, state, stopped)
                self.assertTrue(stopped.is_set())
                detail = (Path(directory) / "overlay_error.log").read_text(encoding="utf-8")
                self.assertIn("Traceback", detail)
                self.assertIn("simulated render failure", detail)
                self.assertIn("stopping translation", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
