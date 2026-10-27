import gc
import os
from tkinter import Tk
import unittest

from alpha_subtitle_window import AlphaSurface, make_capturable


@unittest.skipUnless(os.name == "nt", "Native Windows alpha surface")
class AlphaSubtitleTests(unittest.TestCase):
    def tearDown(self):
        gc.collect()

    def test_real_alpha_unicode_and_premultiplied_edges(self):
        surface = AlphaSurface(640, 240, "Microsoft YaHei UI", 32)
        try:
            surface.draw("透明字幕：中文 English 123", "日本語の原文")
            pixels = surface.pixels()
            alpha = pixels[3::4]
            self.assertGreater(alpha.count(0), 640 * 240 * 0.7)
            self.assertGreater(alpha.count(255), 100)
            self.assertGreater(sum(0 < value < 255 for value in alpha), 100)
            self.assertTrue(all(pixels[i] <= pixels[(i // 4) * 4 + 3]
                                for i in range(len(pixels)) if i % 4 != 3))
            surface.draw("")
            self.assertEqual(surface.pixels(), b"\0" * (640 * 240 * 4))
        finally:
            surface.close()

    def test_native_window_is_enumerable_and_accepts_per_pixel_alpha(self):
        root = Tk()
        surface = None
        try:
            root.overrideredirect(True)
            root.title("Live Interpreter Subtitles")
            root.geometry("640x240+20+20")
            hwnd = make_capturable(root)
            surface = AlphaSurface(640, 240, "Microsoft YaHei UI", 32)
            surface.draw("真正透明的字幕")
            surface.present(hwnd, 20, 20)
            root.update()
            style = surface.user.GetWindowLongPtrW(hwnd, -20)
            self.assertFalse(style & 0x80)  # Not WS_EX_TOOLWINDOW.
            self.assertTrue(style & 0x40000)  # WS_EX_APPWINDOW.
            self.assertTrue(style & 0x80000)  # WS_EX_LAYERED for actual alpha.
            self.assertFalse(surface.user.GetWindowLongPtrW(hwnd, -16) & 0x40000000)
        finally:
            root.destroy()
            if surface:
                surface.close()


if __name__ == "__main__":
    unittest.main()
