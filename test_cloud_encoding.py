import gc
import io
import json
import sys
import unittest
from tkinter import Tk, ttk
from pathlib import Path
from unittest import mock

import gummy_realtime_worker
import live_interpreter_launcher
import qwen_livetranslate_worker
import paraformer_mt_worker


class CloudEncodingTests(unittest.TestCase):
    def tearDown(self) -> None:
        # Reclaim destroyed Tk objects on the UI thread before threaded tests run.
        gc.collect()

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

    def test_paraformer_worker_output_survives_gbk_system_locale(self) -> None:
        self.assert_ascii_transport(paraformer_mt_worker)

    def test_paraformer_launcher_uses_beijing_without_visual_or_local_models(self) -> None:
        root = Tk()
        root.withdraw()
        try:
            with mock.patch.object(live_interpreter_launcher, "Tk", return_value=root), mock.patch.object(
                live_interpreter_launcher, "list_audio_devices", return_value=[live_interpreter_launcher.EDGE_TAB_LABEL]
            ), mock.patch.object(sys, "frozen", True, create=True):
                launcher = live_interpreter_launcher.Launcher()
                launcher.asr_engine.set("paraformer-mt")
                launcher.aliyun_region.set("singapore")
                launcher.update_model_controls()
                with mock.patch.object(live_interpreter_launcher, "backend_executable_path", return_value=Path(sys.executable)):
                    command = launcher.command()
                self.assertEqual(command[command.index("--asr-engine") + 1], "paraformer-mt")
                self.assertEqual(command[command.index("--aliyun-region") + 1], "beijing")
                self.assertIn("--no-visual-input", command)
                self.assertEqual(command[command.index("--mt-model") + 1], launcher.mt_model.get())
                self.assertEqual(str(launcher.mt_combo.cget("state")), "readonly")
                self.assertEqual(tuple(launcher.mt_combo["values"]), paraformer_mt_worker.MT_MODELS)
                self.assertEqual(str(launcher.visual_button.cget("state")), "disabled")
                self.assertEqual(str(launcher.source_combo.cget("state")), "readonly")
        finally:
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()

    def test_packaged_launcher_forces_utf8_and_preserves_proxy_rules(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.dict(
            "os.environ", {"NO_PROXY": "corp.local"}, clear=True
        ):
            env = live_interpreter_launcher.child_environment()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["NO_PROXY"], "corp.local,127.0.0.1,localhost")

    def test_pricing_dialog_costs_selection_and_running_guard(self) -> None:
        root = Tk()
        root.withdraw()
        try:
            with mock.patch.object(live_interpreter_launcher, "Tk", return_value=root), mock.patch.object(
                live_interpreter_launcher, "list_audio_devices", return_value=[live_interpreter_launcher.EDGE_TAB_LABEL]
            ), mock.patch.object(sys, "frozen", True, create=True):
                launcher = live_interpreter_launcher.Launcher()
                launcher.mt_model.set("qwen-mt-plus")
                dialog = launcher.show_model_pricing()
                self.assertIs(launcher.show_model_pricing(), dialog)
                table = next(child for child in dialog.winfo_children() if isinstance(child, ttk.Treeview))
                buttons = [button for child in dialog.winfo_children() for button in child.winfo_children()
                           if isinstance(button, ttk.Button)]
                apply_button = next(button for button in buttons if button["text"] == "使用选中模型")
                self.assertEqual(len(table.get_children()), 7)
                self.assertEqual({row[0] for row in live_interpreter_launcher.MT_PRICES}, set(paraformer_mt_worker.MT_MODELS))
                self.assertEqual(table.selection(), ("2",))
                self.assertEqual(tuple(table.item("2", "values")[1:]), ("1.80", "5.40", "0.132", "0.996"))
                table.selection_set("6")
                with mock.patch.object(launcher, "is_running", return_value=True):
                    apply_button.invoke()
                self.assertEqual(launcher.mt_model.get(), "qwen-mt-plus")
                self.assertTrue(dialog.winfo_exists())
                root.deiconify()
                dialog.geometry("900x460")
                root.update()
                self.assertTrue(table.bbox("6"), "Last price row must remain visible")
                self.assertLessEqual(apply_button.winfo_rooty() + apply_button.winfo_height(),
                                     dialog.winfo_rooty() + dialog.winfo_height())
                launcher.aliyun_region.set("singapore")
                apply_button.invoke()
                self.assertEqual(launcher.mt_model.get(), "deepseek-v4.1-flash-1017")
                self.assertEqual(launcher.asr_engine.get(), "paraformer-mt")
                self.assertEqual(launcher.aliyun_region.get(), "beijing")
                self.assertFalse(launcher.visual_enabled.get())
                with mock.patch.object(live_interpreter_launcher, "backend_executable_path", return_value=Path(sys.executable)):
                    command = launcher.command()
                self.assertEqual(command[command.index("--mt-model") + 1], "deepseek-v4.1-flash-1017")
                self.assertFalse(dialog.winfo_exists())
                self.assertTrue(launcher.show_model_pricing().winfo_exists())
        finally:
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()

    def test_packaged_launcher_resolves_install_root(self) -> None:
        executable = Path(r"C:\Users\test\AppData\Local\Programs\LiveInterpreter\launcher\LiveInterpreter.exe")
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(executable)
        ):
            self.assertEqual(live_interpreter_launcher.install_root(), executable.parent.parent)

    def test_qwen38_launcher_controls_command_and_workspace_validation(self) -> None:
        root = Tk()
        root.withdraw()
        try:
            with mock.patch.object(live_interpreter_launcher, "Tk", return_value=root), mock.patch.object(
                live_interpreter_launcher, "list_audio_devices", return_value=[live_interpreter_launcher.EDGE_TAB_LABEL]
            ), mock.patch.object(sys, "frozen", True, create=True):
                launcher = live_interpreter_launcher.Launcher()
                launcher.asr_engine.set("qwen3.8-live")
                launcher.visual_enabled.set(True)
                launcher.update_model_controls()
                self.assertEqual(launcher.source_language.get(), "auto")
                self.assertEqual(str(launcher.source_combo.cget("state")), "disabled")
                self.assertEqual(str(launcher.silence_entry.cget("state")), "disabled")
                with mock.patch.object(live_interpreter_launcher, "backend_executable_path", return_value=Path(sys.executable)):
                    command = launcher.command()
                self.assertEqual(command[command.index("--asr-engine") + 1], "qwen3.8-live")
                self.assertEqual(command[command.index("--source-language") + 1], "auto")
                self.assertIn("--visual-input", command)
                launcher.aliyun_api_key.set("test")
                launcher.aliyun_workspace_id.set("")
                with mock.patch.object(live_interpreter_launcher.messagebox, "showwarning") as warning:
                    self.assertFalse(launcher.start())
                self.assertIn("Workspace ID", warning.call_args.args[1])
                launcher.asr_engine.set("qwen3.5-live")
                launcher.update_model_controls()
                self.assertEqual(str(launcher.source_combo.cget("state")), "readonly")
                self.assertEqual(str(launcher.silence_entry.cget("state")), "normal")
        finally:
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()


if __name__ == "__main__":
    unittest.main()
