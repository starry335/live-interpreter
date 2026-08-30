import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import gummy_realtime_worker
import live_interpreter_launcher
import qwen_livetranslate_worker


class CloudEncodingTests(unittest.TestCase):
    def assert_ascii_transport(self, module) -> None:
        payload = {"event": "result", "source": "日本語", "translation": "中文翻译"}
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="gbk", errors="replace")
        original = sys.stdout
        try:
            sys.stdout = stream
            module.emit(payload)
            stream.flush()
        finally:
            sys.stdout = original
        encoded = output.getvalue()
        self.assertTrue(encoded.isascii())
        self.assertEqual(json.loads(encoded.decode("utf-8")), payload)

    def test_qwen_worker_output_survives_gbk_system_locale(self) -> None:
        self.assert_ascii_transport(qwen_livetranslate_worker)

    def test_gummy_worker_output_survives_gbk_system_locale(self) -> None:
        self.assert_ascii_transport(gummy_realtime_worker)

    def test_packaged_launcher_forces_utf8_and_preserves_proxy_rules(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.dict(
            "os.environ", {"NO_PROXY": "corp.local"}, clear=True
        ):
            env = live_interpreter_launcher.child_environment()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["NO_PROXY"], "corp.local,127.0.0.1,localhost")

    def test_packaged_launcher_resolves_install_root(self) -> None:
        executable = Path(r"C:\Users\test\AppData\Local\Programs\LiveInterpreter\launcher\LiveInterpreter.exe")
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(executable)
        ):
            self.assertEqual(live_interpreter_launcher.install_root(), executable.parent.parent)


if __name__ == "__main__":
    unittest.main()
