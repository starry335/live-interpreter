import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from pathlib import Path

from live_interpreter import BrowserAudioInput, BrowserVisualInput, State, make_handler


class EdgeAudioInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.audio = BrowserAudioInput()
        self.temporary = tempfile.TemporaryDirectory()
        self.visual = BrowserVisualInput(Path(self.temporary.name) / "frame.jpg", True)
        self.state = State()
        self.stop_event = threading.Event()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.state, self.audio, self.stop_event, self.visual),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def post(self, path: str, body: bytes = b"", authorized: bool = True) -> int:
        headers = {"X-Live-Interpreter": "edge-extension"} if authorized else {}
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_authorized_pcm_lifecycle(self) -> None:
        self.state.upsert_event("old", source="old", translated="旧字幕", final=True)
        self.assertEqual(self.post("/audio/start"), 204)
        self.assertEqual(self.state.snapshot()["events"], [])
        frame = b"\x01\x00\x02\x00"
        self.assertEqual(self.post("/audio", frame), 204)
        self.assertEqual(self.audio.read(0.01), frame)
        self.assertEqual(self.post("/audio/stop"), 204)
        self.assertEqual(self.post("/audio", frame), 409)

    def test_rejects_web_page_posts(self) -> None:
        self.assertEqual(self.post("/audio/start", authorized=False), 403)

    def test_shutdown_signals_backend(self) -> None:
        self.assertEqual(self.post("/shutdown"), 204)
        self.assertTrue(self.stop_event.is_set())

    def test_visual_frame_is_written_after_capture_starts(self) -> None:
        frame = b"\xff\xd8visual-jpeg\xff\xd9"
        self.assertEqual(self.post("/audio/start"), 204)
        self.assertEqual(self.post("/visual", frame), 204)
        self.assertEqual(self.visual.path.read_bytes(), frame)
        self.assertEqual(self.post("/visual", b"not-jpeg"), 415)


if __name__ == "__main__":
    unittest.main()
