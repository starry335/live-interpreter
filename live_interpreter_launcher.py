from __future__ import annotations

import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk


APP_TITLE = "Live Interpreter"
ENV_ROOT = Path(os.environ.get("LIVE_INTERPRETER_ENV", r"E:\ANACONDA\envs\funasr_py38"))
ENV_PYTHON = ENV_ROOT / "python.exe"
FFMPEG = ENV_ROOT / "Library" / "bin" / "ffmpeg.exe"
FUNASR_MODELS = Path(os.environ.get("FUNASR_ROOT", r"E:\FunAsr")) / "models"
ENV_TCL = ENV_ROOT / "tcl" / "tcl8.6"
ENV_TK = ENV_ROOT / "tcl" / "tk8.6"
LOOPBACK_LABEL = "默认电脑声音 (WASAPI Loopback)"
LOOPBACK_DEVICE = "__wasapi_loopback__"
EDGE_TAB_LABEL = "Edge 当前标签页 (浏览器扩展)"
EDGE_TAB_DEVICE = "__edge_tab_audio__"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", app_dir()))
    candidate = base / name
    if candidate.exists():
        return candidate
    return app_dir() / name


def copy_runtime_asset(name: str) -> Path:
    target = app_dir() / name
    source = resource_path(name)
    if source.resolve() != target.resolve():
        data = source.read_bytes()
        if not target.exists() or target.read_bytes() != data:
            target.write_bytes(data)
    return target


def extension_dir() -> Path:
    target = app_dir() / "edge_extension"
    source = resource_path("edge_extension")
    if source.exists() and source.resolve() != target.resolve():
        shutil.copytree(source, target, dirs_exist_ok=True)
    return target


def runtime_script_path() -> Path:
    script = copy_runtime_asset("live_interpreter.py")
    copy_runtime_asset("gummy_realtime_worker.py")
    copy_runtime_asset("qwen_livetranslate_worker.py")
    return script


def child_environment() -> dict[str, str]:
    env = os.environ.copy()
    system_root = env.get("SystemRoot", r"C:\Windows")
    inherited_path = [
        part
        for part in env.get("PATH", "").split(os.pathsep)
        if part
        and "_MEI" not in part
        and "E:\\ANACONDA" not in part.upper()
        and "E:\\TOOL-LIVE\\.PACKENV" not in part.upper()
    ]
    path_parts = [
        str(ENV_ROOT),
        str(ENV_ROOT / "DLLs"),
        str(ENV_ROOT / "Scripts"),
        str(ENV_ROOT / "Library" / "bin"),
        str(ENV_ROOT / "Library" / "usr" / "bin"),
        str(ENV_ROOT / "Library" / "mingw-w64" / "bin"),
        str(Path(system_root) / "System32"),
        str(Path(system_root)),
        str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0"),
    ]
    path_parts.extend(inherited_path)
    env.update(
        {
            "PATH": os.pathsep.join(path_parts),
            "NO_PROXY": "*",
            "no_proxy": "*",
            "MODELSCOPE_CACHE": str(FUNASR_MODELS),
            "HF_HOME": str(FUNASR_MODELS / "huggingface"),
            "TRANSFORMERS_CACHE": str(FUNASR_MODELS / "huggingface"),
            "CONDA_PREFIX": str(ENV_ROOT),
            "CONDA_DEFAULT_ENV": "funasr_py38",
            "PYTHONNOUSERSITE": "1",
            "TCL_LIBRARY": str(ENV_TCL),
            "TK_LIBRARY": str(ENV_TK),
        }
    )
    for key in list(env):
        if key.startswith("_PYI") or key in {"PYTHONHOME", "PYTHONPATH"}:
            env.pop(key, None)
    return env


def list_audio_devices() -> list[str]:
    if not FFMPEG.exists():
        raise RuntimeError(f"FFmpeg not found: {FFMPEG}")
    proc = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = proc.stdout.decode("utf-8", errors="replace")
    devices: list[str] = []
    for line in output.splitlines():
        match = re.search(r'"(.+?)"\s+\(audio\)', line)
        if match:
            devices.append(match.group(1))
    return [EDGE_TAB_LABEL, LOOPBACK_LABEL, *devices]


class Launcher:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("820x720")
        self.root.minsize(760, 660)

        self.process: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self.audio_device = StringVar()
        self.source_language = StringVar(value="ja")
        self.target_language = StringVar(value="Chinese")
        self.asr_engine = StringVar(value="qwen3.5-live")
        self.aliyun_api_key = StringVar(value=os.environ.get("DASHSCOPE_API_KEY", ""))
        self.aliyun_workspace_id = StringVar(
            value=os.environ.get("DASHSCOPE_WORKSPACE_ID", "")
        )
        self.aliyun_region = StringVar(value="beijing")
        self.gummy_max_end_silence = StringVar(value="500")
        self.chunk_seconds = StringVar(value="1")
        self.overlay_geometry = StringVar(value="980x280+240+620")
        self.overlay_font_size = StringVar(value="15")
        self.overlay_hold_seconds = StringVar(value="8")
        self.port = StringVar(value="8765")
        self.overlay_enabled = BooleanVar(value=True)
        self.overlay_show_source = BooleanVar(value=False)
        self.record_screen = BooleanVar(value=False)
        self.translate_enabled = BooleanVar(value=True)
        self.visual_enabled = BooleanVar(value=True)

        self._build_ui()
        self.refresh_devices()
        self.root.after(200, self.drain_logs)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self.root, padding=14)
        root.pack(fill=BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(header, text="电脑直播同声传译", font=("Microsoft YaHei UI", 18, "bold")).pack(side=LEFT)
        ttk.Button(header, text="打开字幕页", command=self.open_subtitle_page).pack(side=RIGHT)
        ttk.Button(header, text="Edge插件目录", command=self.open_extension_folder).pack(
            side=RIGHT, padx=(0, 8)
        )

        settings = ttk.LabelFrame(root, text="设置", padding=12)
        settings.pack(fill="x", pady=(14, 10))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="音频设备").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        self.device_combo = ttk.Combobox(settings, textvariable=self.audio_device, state="readonly")
        self.device_combo.grid(row=0, column=1, sticky="ew", pady=5)
        device_buttons = ttk.Frame(settings)
        device_buttons.grid(row=0, column=2, padx=(8, 0), pady=5)
        ttk.Button(device_buttons, text="刷新", command=self.refresh_devices).pack(side=LEFT)
        ttk.Button(device_buttons, text="测试音量", command=self.test_audio_level).pack(side=LEFT, padx=(8, 0))

        ttk.Label(settings, text="目标语言").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        language_box = ttk.Frame(settings)
        language_box.grid(row=1, column=1, sticky="ew", pady=5)
        language_box.columnconfigure(1, weight=1)
        ttk.Label(language_box, text="source").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Combobox(
            language_box,
            textvariable=self.source_language,
            values=("ja", "auto", "en", "zh", "ko", "yue"),
            state="readonly",
            width=8,
        ).grid(row=0, column=1, sticky="w", padx=(0, 14))
        ttk.Label(language_box, text="target").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Entry(language_box, textvariable=self.target_language).grid(row=0, column=3, sticky="ew")
        asr_box = ttk.Frame(settings)
        asr_box.grid(row=1, column=2, sticky="e", padx=(8, 0), pady=5)
        ttk.Label(asr_box, text="ASR").pack(side=LEFT, padx=(0, 6))
        ttk.Combobox(
            asr_box,
            textvariable=self.asr_engine,
            values=("qwen3.5-live", "gummy", "sensevoice", "streaming"),
            state="readonly",
            width=15,
        ).pack(side=LEFT)

        ttk.Label(settings, text="阿里云 API Key").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=5)
        cloud_box = ttk.Frame(settings)
        cloud_box.grid(row=2, column=1, columnspan=2, sticky="ew", pady=5)
        cloud_box.columnconfigure(0, weight=1)
        ttk.Entry(cloud_box, textvariable=self.aliyun_api_key, show="*").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(cloud_box, text="断句静音(ms)").grid(row=0, column=1, padx=(14, 6))
        ttk.Entry(cloud_box, textvariable=self.gummy_max_end_silence, width=8).grid(
            row=0, column=2, sticky="e"
        )
        ttk.Label(cloud_box, text="Workspace ID").grid(row=1, column=0, sticky="w", pady=(6, 0))
        workspace_row = ttk.Frame(cloud_box)
        workspace_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(3, 0))
        workspace_row.columnconfigure(0, weight=1)
        ttk.Entry(workspace_row, textvariable=self.aliyun_workspace_id).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(workspace_row, text="地域").grid(row=0, column=1, padx=(14, 6))
        ttk.Combobox(
            workspace_row,
            textvariable=self.aliyun_region,
            values=("beijing", "singapore"),
            state="readonly",
            width=11,
        ).grid(row=0, column=2)

        small = ttk.Frame(settings)
        small.grid(row=3, column=1, sticky="ew", pady=5)
        small.columnconfigure((1, 3, 5, 7), weight=1)
        ttk.Label(settings, text="字幕显示").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Label(small, text="分段秒数").grid(row=0, column=0, sticky="w")
        ttk.Entry(small, textvariable=self.chunk_seconds, width=6).grid(row=0, column=1, sticky="w", padx=(6, 14))
        ttk.Label(small, text="字号").grid(row=0, column=2, sticky="w")
        ttk.Entry(small, textvariable=self.overlay_font_size, width=6).grid(row=0, column=3, sticky="w", padx=(6, 14))
        ttk.Label(small, text="停留秒数").grid(row=0, column=4, sticky="w")
        ttk.Entry(small, textvariable=self.overlay_hold_seconds, width=6).grid(
            row=0, column=5, sticky="w", padx=(6, 14)
        )
        ttk.Label(small, text="端口").grid(row=0, column=6, sticky="w")
        ttk.Entry(small, textvariable=self.port, width=7).grid(row=0, column=7, sticky="w", padx=(6, 0))

        ttk.Label(settings, text="窗口位置").grid(row=4, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(settings, textvariable=self.overlay_geometry).grid(row=4, column=1, sticky="ew", pady=5)

        checks = ttk.Frame(settings)
        checks.grid(row=5, column=1, columnspan=2, sticky="w", pady=7)
        ttk.Checkbutton(checks, text="透明字幕窗口", variable=self.overlay_enabled).pack(side=LEFT, padx=(0, 18))
        ttk.Checkbutton(checks, text="显示原文", variable=self.overlay_show_source).pack(side=LEFT, padx=(0, 18))
        ttk.Checkbutton(checks, text="启用翻译", variable=self.translate_enabled).pack(side=LEFT, padx=(0, 18))
        ttk.Checkbutton(checks, text="录制屏幕", variable=self.record_screen).pack(side=LEFT)

        visual_check = ttk.Frame(settings)
        visual_check.grid(row=6, column=1, columnspan=2, sticky="w", pady=(0, 7))
        ttk.Checkbutton(
            visual_check,
            text="画面辅助（每秒上传约 1 张 Edge 当前标签页截图）",
            variable=self.visual_enabled,
        ).pack(side=LEFT)

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(0, 10))
        self.start_button = ttk.Button(actions, text="启动同传", command=self.start)
        self.start_button.pack(side=LEFT)
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side=LEFT, padx=(10, 0))
        ttk.Button(actions, text="选择工作目录", command=self.choose_work_dir).pack(side=RIGHT)

        self.work_dir = StringVar(value=str(app_dir() / "runtime"))
        ttk.Label(root, textvariable=self.work_dir).pack(fill="x", pady=(0, 8))

        log_frame = ttk.LabelFrame(root, text="运行日志", padding=8)
        log_frame.pack(fill=BOTH, expand=True)
        self.log_text = ttk.Treeview(log_frame, columns=("line",), show="headings", height=13)
        self.log_text.heading("line", text="日志")
        self.log_text.column("line", width=720, anchor="w")
        self.log_text.pack(fill=BOTH, expand=True)

    def refresh_devices(self) -> None:
        try:
            devices = list_audio_devices()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.device_combo["values"] = devices
        if devices and not self.audio_device.get():
            preferred = EDGE_TAB_LABEL
            self.audio_device.set(preferred)
        self.add_log(f"检测到 {len(devices)} 个音频设备")

    def choose_work_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.work_dir.get())
        if chosen:
            self.work_dir.set(chosen)

    def open_extension_folder(self) -> None:
        path = extension_dir()
        if not path.exists():
            messagebox.showerror(APP_TITLE, "Edge 插件文件不存在，请重新构建应用。")
            return
        os.startfile(path)
        self.add_log("请在 edge://extensions 开启开发人员模式，然后加载此解压缩目录。")

    def command(self) -> list[str]:
        script = runtime_script_path()
        if not ENV_PYTHON.exists():
            raise RuntimeError(f"Python environment not found: {ENV_PYTHON}")
        if not script.exists():
            raise RuntimeError(f"live_interpreter.py not found: {script}")
        source_language = self.source_language.get() or "auto"
        asr_engine = self.asr_engine.get() or "qwen3.5-live"
        edge_tab_audio = self.audio_device.get() == EDGE_TAB_LABEL
        cloud_engines = {"qwen3.5-live", "gummy"}
        if edge_tab_audio and asr_engine not in cloud_engines:
            asr_engine = "qwen3.5-live"
            self.asr_engine.set(asr_engine)
            self.add_log("Edge 标签页音频使用云端实时翻译，已自动切换到 Qwen3.5。")
        if asr_engine == "streaming" and source_language not in ("zh", "cn", "Chinese", "chinese"):
            asr_engine = "sensevoice"
            self.asr_engine.set("sensevoice")
            self.add_log("streaming 只支持中文原声；当前源语言不是中文，已自动切换为 sensevoice。")
        try:
            chunk_seconds = float(self.chunk_seconds.get() or "1")
        except ValueError:
            chunk_seconds = 1.0
        try:
            gummy_max_end_silence = int(self.gummy_max_end_silence.get() or "500")
        except ValueError:
            gummy_max_end_silence = 500
        gummy_max_end_silence = max(200, min(6000, gummy_max_end_silence))
        self.gummy_max_end_silence.set(str(gummy_max_end_silence))
        try:
            overlay_hold_seconds = float(self.overlay_hold_seconds.get() or "8")
        except ValueError:
            overlay_hold_seconds = 8.0
        overlay_hold_seconds = max(2.0, min(30.0, overlay_hold_seconds))
        self.overlay_hold_seconds.set(f"{overlay_hold_seconds:g}")
        cmd = [
            str(ENV_PYTHON),
            str(script),
            "--audio-device",
            (
                EDGE_TAB_DEVICE
                if edge_tab_audio
                else LOOPBACK_DEVICE
                if self.audio_device.get() == LOOPBACK_LABEL
                else self.audio_device.get()
            ),
            "--source-language",
            source_language,
            "--target-language",
            self.target_language.get() or "Chinese",
            "--asr-engine",
            asr_engine,
            "--gummy-max-end-silence",
            str(gummy_max_end_silence),
            "--aliyun-workspace-id",
            self.aliyun_workspace_id.get().strip(),
            "--aliyun-region",
            self.aliyun_region.get() or "beijing",
            "--chunk-seconds",
            str(chunk_seconds),
            "--streaming-chunk-stride",
            "10",
            "--streaming-encoder-lookback",
            "4",
            "--streaming-decoder-lookback",
            "1",
            "--translation-max-new-tokens",
            "72",
            "--overlay-geometry",
            self.overlay_geometry.get() or "980x280+240+620",
            "--overlay-font-size",
            self.overlay_font_size.get() or "15",
            "--overlay-hold-seconds",
            str(overlay_hold_seconds),
            "--overlay-batch-min-chars",
            "80",
            "--overlay-batch-max-chars",
            "170",
            "--overlay-live-mode",
            "--overlay-live-events",
            "3",
            "--overlay-live-max-events",
            "6",
            "--overlay-live-max-chars",
            "360",
            "--port",
            self.port.get() or "8765",
            "--work-dir",
            self.work_dir.get(),
        ]
        if edge_tab_audio or not self.overlay_enabled.get():
            cmd.append("--no-overlay")
        if self.overlay_show_source.get():
            cmd.append("--overlay-show-source")
        else:
            cmd.append("--overlay-hide-source")
        if self.record_screen.get():
            cmd.append("--record-screen")
        cmd.append("--visual-input" if self.visual_enabled.get() else "--no-visual-input")
        if not self.translate_enabled.get():
            cmd.extend(["--translation-engine", "none"])
        return cmd

    def audio_test_command(self) -> list[str]:
        cmd = self.command()
        cmd.append("--test-audio")
        return cmd

    def test_audio_level(self) -> None:
        if not self.audio_device.get():
            messagebox.showwarning(APP_TITLE, "请先选择音频设备。")
            return
        if self.audio_device.get() == EDGE_TAB_LABEL:
            messagebox.showinfo(APP_TITLE, "请启动同传，然后在 Edge 插件中点击“翻译当前标签页”。")
            return
        self.add_log("正在测试当前设备音量，请保持视频正在播放...")
        threading.Thread(target=self.run_audio_level_test, daemon=True).start()

    def run_audio_level_test(self) -> None:
        try:
            proc = subprocess.run(
                self.audio_test_command(),
                cwd=str(app_dir()),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_environment(),
                timeout=20,
            )
            output = proc.stdout.strip()
            for line in output.splitlines():
                self.log_queue.put(line)
            if proc.returncode == 0:
                self.log_queue.put("当前设备有声音，可以用于同传。")
            else:
                self.log_queue.put("当前设备没有收到可用声音。请把浏览器声音路由到 VoiceMeeter 后再测。")
        except Exception as exc:
            self.log_queue.put(f"音量测试失败：{exc}")

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> bool:
        if self.process and self.process.poll() is None:
            return True
        if not self.audio_device.get():
            messagebox.showwarning(APP_TITLE, "请先选择音频设备。")
            return False
        selected_engine = self.asr_engine.get()
        if self.audio_device.get() == EDGE_TAB_LABEL and selected_engine not in {
            "gummy",
            "qwen3.5-live",
        }:
            selected_engine = "qwen3.5-live"
        if selected_engine in {"gummy", "qwen3.5-live"} and not self.aliyun_api_key.get().strip():
            messagebox.showwarning(APP_TITLE, "请先填写阿里云百炼 API Key。")
            return False
        if selected_engine == "qwen3.5-live" and not self.aliyun_workspace_id.get().strip():
            messagebox.showwarning(APP_TITLE, "Qwen3.5 LiveTranslate 还需要填写 Workspace ID。")
            return False
        try:
            port = int(self.port.get() or "8765")
        except ValueError:
            messagebox.showerror(APP_TITLE, "端口必须是数字。")
            return False
        if self.can_connect("127.0.0.1", port):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/shutdown",
                data=b"",
                headers={"X-Live-Interpreter": "edge-extension"},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=2).close()
            except (OSError, urllib.error.HTTPError):
                messagebox.showerror(
                    APP_TITLE,
                    "端口已被旧版 Live Interpreter 占用。请关闭旧程序后再启动。",
                )
                return False
            deadline = time.time() + 5
            while self.can_connect("127.0.0.1", port) and time.time() < deadline:
                time.sleep(0.1)
            if self.can_connect("127.0.0.1", port):
                messagebox.showerror(APP_TITLE, "旧同传进程未能停止，请重新启动电脑后再试。")
                return False
        try:
            cmd = self.command()
            env = child_environment()
            api_key = self.aliyun_api_key.get().strip()
            if api_key:
                env["DASHSCOPE_API_KEY"] = api_key
            workspace_id = self.aliyun_workspace_id.get().strip()
            if workspace_id:
                env["DASHSCOPE_WORKSPACE_ID"] = workspace_id
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.process = subprocess.Popen(
                cmd,
                cwd=str(app_dir()),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creationflags,
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return False
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        if self.asr_engine.get() == "qwen3.5-live":
            self.add_log("已启动同传，正在连接 Qwen3.5 LiveTranslate（仅文本）...")
            if self.visual_enabled.get() and self.audio_device.get() == EDGE_TAB_LABEL:
                self.add_log("画面辅助已开启：Edge 插件将每秒上传约 1 张当前标签页截图。")
        elif self.asr_engine.get() == "gummy":
            self.add_log("已启动同传，正在连接阿里云 Gummy 实时翻译...")
        else:
            self.add_log("已启动同传，正在加载本地模型和字幕服务...")
        threading.Thread(target=self.read_process_output, daemon=True).start()
        threading.Thread(target=self.watch_process, daemon=True).start()
        return True

    def stop(self) -> None:
        proc = self.process
        if not proc or proc.poll() is not None:
            self.set_stopped()
            return
        self.add_log("正在停止...")
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except Exception:
            proc.terminate()
        deadline = time.time() + 8
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if proc.poll() is None:
            proc.kill()
        self.set_stopped()

    def read_process_output(self) -> None:
        proc = self.process
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            self.log_queue.put(line.rstrip())

    def watch_process(self) -> None:
        proc = self.process
        if not proc:
            return
        code = proc.wait()
        self.log_queue.put(f"进程已退出，代码 {code}")
        self.root.after(0, self.set_stopped)

    def drain_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.add_log(line)
        self.root.after(200, self.drain_logs)

    def add_log(self, line: str) -> None:
        if not line:
            return
        self.log_text.insert("", END, values=(line,))
        children = self.log_text.get_children()
        if len(children) > 300:
            self.log_text.delete(children[0])
        self.log_text.yview_moveto(1.0)

    def set_stopped(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def open_subtitle_page(self) -> None:
        if not self.is_running() and not self.start():
            return
        threading.Thread(target=self.wait_and_open_subtitle_page, daemon=True).start()

    def wait_and_open_subtitle_page(self) -> None:
        port_text = self.port.get() or "8765"
        try:
            port = int(port_text)
        except ValueError:
            self.log_queue.put(f"端口无效：{port_text}")
            return
        self.log_queue.put(f"等待字幕页服务：http://127.0.0.1:{port}")
        deadline = time.time() + 90
        while time.time() < deadline:
            if not self.is_running():
                self.log_queue.put("同传进程未运行，字幕页无法打开。请查看上方日志。")
                return
            if self.can_connect("127.0.0.1", port):
                webbrowser.open(f"http://127.0.0.1:{port}")
                self.log_queue.put("字幕页已打开")
                return
            time.sleep(0.5)
        self.log_queue.put("等待字幕页服务超时。请确认同传进程没有报错退出。")

    @staticmethod
    def can_connect(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    def on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno(APP_TITLE, "同传仍在运行，是否停止并退出？"):
                return
            self.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    Launcher().run()
