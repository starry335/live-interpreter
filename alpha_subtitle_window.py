"""Windows subtitle surface with real premultiplied per-pixel alpha (no color key)."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W


class RectF(C.Structure):
    _fields_ = [("x", C.c_float), ("y", C.c_float), ("width", C.c_float), ("height", C.c_float)]


class BitmapHeader(C.Structure):
    _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
                ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD),
                ("image_size", W.DWORD), ("xppm", W.LONG), ("yppm", W.LONG),
                ("colors", W.DWORD), ("important", W.DWORD)]


class StartupInput(C.Structure):
    _fields_ = [("version", W.UINT), ("callback", C.c_void_p), ("background", W.BOOL), ("codecs", W.BOOL)]


class Blend(C.Structure):
    _fields_ = [("operation", W.BYTE), ("flags", W.BYTE), ("opacity", W.BYTE), ("alpha", W.BYTE)]


def native_functions():
    user, gdi = C.WinDLL("user32", use_last_error=True), C.WinDLL("gdi32", use_last_error=True)
    signatures = (
        (user, "GetAncestor", W.HWND, [W.HWND, W.UINT]),
        (user, "GetWindowLongPtrW", C.c_ssize_t, [W.HWND, C.c_int]),
        (user, "SetWindowLongPtrW", C.c_ssize_t, [W.HWND, C.c_int, C.c_ssize_t]),
        (user, "SetWindowPos", W.BOOL, [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]),
        (user, "UpdateLayeredWindow", W.BOOL, [W.HWND, W.HDC, C.POINTER(W.POINT), C.POINTER(W.SIZE),
                                              W.HDC, C.POINTER(W.POINT), W.DWORD, C.POINTER(Blend), W.DWORD]),
        (gdi, "CreateCompatibleDC", W.HDC, [W.HDC]),
        (gdi, "CreateDIBSection", W.HANDLE, [W.HDC, C.POINTER(BitmapHeader), W.UINT,
                                          C.POINTER(C.c_void_p), W.HANDLE, W.DWORD]),
        (gdi, "SelectObject", W.HANDLE, [W.HDC, W.HANDLE]),
        (gdi, "DeleteObject", W.BOOL, [W.HANDLE]),
        (gdi, "DeleteDC", W.BOOL, [W.HDC]),
    )
    for library, name, result, arguments in signatures:
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
    return user, gdi


def make_capturable(root) -> int:
    """Keep Tk's borderless HWND in normal capture/taskbar lists."""
    user, unused = native_functions()
    root.update_idletasks()
    hwnd = user.GetAncestor(root.winfo_id(), 2)
    style = user.GetWindowLongPtrW(hwnd, -20)
    C.set_last_error(0)
    previous = user.SetWindowLongPtrW(hwnd, -20, (style & ~0x80) | 0x40000 | 0x80000)
    if not previous and C.get_last_error():
        raise C.WinError(C.get_last_error())
    if not user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x37):
        raise C.WinError(C.get_last_error())
    return hwnd


class AlphaSurface:
    def __init__(self, width: int, height: int, family: str, font_size: int):
        self.width, self.height = width, height
        self.user, self.gdi = native_functions()
        self.gp = C.WinDLL("gdiplus")
        ptr = C.c_void_p
        output = C.POINTER(ptr)
        signatures = {
            "GdiplusStartup": [C.POINTER(C.c_size_t), C.POINTER(StartupInput), ptr],
            "GdipCreateBitmapFromScan0": [C.c_int, C.c_int, C.c_int, C.c_int, ptr, output],
            "GdipGetImageGraphicsContext": [ptr, output],
            "GdipCreateFontFamilyFromName": [W.LPCWSTR, ptr, output],
            "GdipGetGenericFontFamilySansSerif": [output],
            "GdipCreateFont": [ptr, C.c_float, C.c_int, C.c_int, output],
            "GdipCreateSolidFill": [W.DWORD, output],
            "GdipCreateStringFormat": [C.c_int, W.WORD, output],
            "GdipSetTextRenderingHint": [ptr, C.c_int],
            "GdipGraphicsClear": [ptr, W.DWORD],
            "GdipDrawString": [ptr, W.LPCWSTR, C.c_int, ptr, C.POINTER(RectF), ptr, ptr],
            "GdipMeasureString": [ptr, W.LPCWSTR, C.c_int, ptr, C.POINTER(RectF), ptr,
                                  C.POINTER(RectF), C.POINTER(C.c_int), C.POINTER(C.c_int)],
            "GdipFlush": [ptr, C.c_int],
        }
        for name, arguments in signatures.items():
            function = getattr(self.gp, name)
            function.argtypes, function.restype = arguments, C.c_int
        self.gp.GdiplusShutdown.argtypes = [C.c_size_t]
        self.token = C.c_size_t()
        self.resources = []
        self.dc = self.bitmap = self.previous = None
        try:
            self.call("GdiplusStartup", C.byref(self.token), C.byref(StartupInput(1, None, False, False)), None)
            self.dc = self.gdi.CreateCompatibleDC(None)
            header = BitmapHeader(C.sizeof(BitmapHeader), width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
            self.bits = ptr()
            self.bitmap = self.gdi.CreateDIBSection(self.dc, C.byref(header), 0, C.byref(self.bits), None, 0)
            if not self.dc or not self.bitmap:
                raise C.WinError(C.get_last_error())
            self.previous = self.gdi.SelectObject(self.dc, self.bitmap)
            self.image = self.create("GdipCreateBitmapFromScan0", "GdipDisposeImage", width, height, width * 4, 0xE200B, self.bits)
            self.graphics = self.create("GdipGetImageGraphicsContext", "GdipDeleteGraphics", self.image)
            self.call("GdipSetTextRenderingHint", self.graphics, 4)  # Grayscale antialias; ClearType is unsuitable for alpha.
            self.family = ptr()
            if self.gp.GdipCreateFontFamilyFromName(family, None, C.byref(self.family)):
                self.call("GdipGetGenericFontFamilySansSerif", C.byref(self.family))
            self.resources.append(("GdipDeleteFontFamily", self.family))
            self.font = self.create("GdipCreateFont", "GdipDeleteFont", self.family, float(font_size), 1, 2)
            self.source_font = self.create("GdipCreateFont", "GdipDeleteFont", self.family, float(max(13, font_size * 0.65)), 0, 2)
            self.white = self.create("GdipCreateSolidFill", "GdipDeleteBrush", 0xFFFFFFFF)
            self.gray = self.create("GdipCreateSolidFill", "GdipDeleteBrush", 0xFFD0D0D0)
            self.black = self.create("GdipCreateSolidFill", "GdipDeleteBrush", 0xFF000000)
            self.format = self.create("GdipCreateStringFormat", "GdipDeleteStringFormat", 0, 0)
        except Exception:
            self.close()
            raise

    def call(self, name, *args):
        status = getattr(self.gp, name)(*args)
        if status:
            raise RuntimeError(f"{name} failed: GDI+ status {status}")

    def create(self, name, destructor, *args):
        value = C.c_void_p()
        self.call(name, *args, C.byref(value))
        self.resources.append((destructor, value))
        return value

    def measure(self, text, font, width):
        bounds, fitted, lines = RectF(), C.c_int(), C.c_int()
        self.call("GdipMeasureString", self.graphics, text, -1, font,
                  C.byref(RectF(0, 0, width, 100000)), self.format,
                  C.byref(bounds), C.byref(fitted), C.byref(lines))
        return bounds.height

    def fit(self, text, font, width, height):
        paragraphs = text.splitlines()
        while paragraphs and self.measure("\n".join(paragraphs), font, width) > height:
            if len(paragraphs) > 1:
                paragraphs.pop(0)
            elif len(paragraphs[0]) > 1:
                paragraphs[0] = paragraphs[0][max(1, len(paragraphs[0]) // 10):]
            else:
                paragraphs.clear()
        result = "\n".join(paragraphs)
        return result, self.measure(result, font, width) if result else 0

    def draw(self, text: str, source: str = ""):
        self.call("GdipGraphicsClear", self.graphics, 0)
        width, height = max(1, self.width - 40), max(1, self.height - 40)
        source, source_height = self.fit(source, self.source_font, width, height * 0.3) if source else ("", 0)
        gap = 12 if source else 0
        text, text_height = self.fit(text, self.font, width, height - source_height - gap) if text else ("", 0)
        y = self.height - 20 - text_height - source_height - gap
        for value, font, brush, block_height in ((source, self.source_font, self.gray, source_height + gap),
                                                 (text, self.font, self.white, text_height)):
            if value:
                for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                    self.call("GdipDrawString", self.graphics, value, -1, font,
                              C.byref(RectF(20 + dx, y + dy, width, block_height + 8)), self.format, self.black)
                self.call("GdipDrawString", self.graphics, value, -1, font,
                          C.byref(RectF(20, y, width, block_height + 8)), self.format, brush)
            y += block_height
        self.call("GdipFlush", self.graphics, 1)

    def present(self, hwnd: int, x: int, y: int):
        blend = Blend(0, 0, 255, 1)  # AC_SRC_ALPHA, not a color key or global opacity fade.
        if not self.user.UpdateLayeredWindow(hwnd, None, C.byref(W.POINT(x, y)),
                                             C.byref(W.SIZE(self.width, self.height)), self.dc,
                                             C.byref(W.POINT(0, 0)), 0, C.byref(blend), 2):
            raise C.WinError(C.get_last_error())

    def pixels(self):
        return C.string_at(self.bits, self.width * self.height * 4)

    def close(self):
        for name, value in reversed(self.resources):
            function = getattr(self.gp, name)
            function.argtypes = [C.c_void_p]
            function(value)
        self.resources.clear()
        if self.previous:
            self.gdi.SelectObject(self.dc, self.previous)
        if self.bitmap:
            self.gdi.DeleteObject(self.bitmap)
        if self.dc:
            self.gdi.DeleteDC(self.dc)
        self.bitmap = self.dc = self.previous = None
        if self.token.value:
            self.gp.GdiplusShutdown(self.token)
            self.token.value = 0


def run_alpha_subtitle_window(args, state, stop_event):
    import tkinter as tk
    from live_interpreter import compose_live_subtitles, load_overlay_geometry, save_overlay_geometry

    path = args.work_dir / "alpha_window.json"
    width, height, x, y = load_overlay_geometry(path, args.overlay_geometry)
    root = tk.Tk()
    root.title("Live Interpreter Subtitles")
    root.overrideredirect(True)
    root.geometry(f"{width}x{height}{x:+d}{y:+d}")
    root.minsize(480, 180)
    hwnd = make_capturable(root)
    surface, rendered = None, None
    failure = []
    drag = {}

    def close():
        save_overlay_geometry(path, (root.winfo_width(), root.winfo_height(), root.winfo_x(), root.winfo_y()))
        stop_event.set()
        root.destroy()

    def refresh():
        nonlocal surface, rendered
        if stop_event.is_set():
            close()
            return
        try:
            snapshot = state.snapshot()
            text, source = compose_live_subtitles(snapshot["events"], args.overlay_live_events,
                args.overlay_live_max_events, args.overlay_hold_seconds, args.overlay_live_max_chars,
                now=float(snapshot["monotonic_now"]))
            source = source if args.overlay_show_source else ""
            size = (max(1, root.winfo_width()), max(1, root.winfo_height()))
            key = (text, source, size)
            if key != rendered:
                if surface is None or size != (surface.width, surface.height):
                    if surface:
                        surface.close()
                    surface = AlphaSurface(*size, args.overlay_font, max(15, args.overlay_font_size))
                surface.draw(text, source)
                surface.present(hwnd, root.winfo_x(), root.winfo_y())
                rendered = key
            root.after(100, refresh)
        except Exception as exc:
            failure.append(exc)
            close()

    root.bind("<ButtonPress-1>", lambda event: drag.update(x=event.x_root - root.winfo_x(), y=event.y_root - root.winfo_y()))
    root.bind("<B1-Motion>", lambda event: root.geometry(f"{event.x_root-drag['x']:+d}{event.y_root-drag['y']:+d}") if drag else None)
    def remap(event):
        nonlocal hwnd, rendered
        if event.widget is root:
            hwnd = make_capturable(root)
            rendered = None
    root.bind("<Map>", remap)
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(0, refresh)
    try:
        root.mainloop()
    finally:
        if surface:
            surface.close()
    if failure:
        raise failure[0]
