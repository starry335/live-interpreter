from __future__ import annotations

import argparse
import audioop
import array
import ctypes
import datetime as _dt
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
FUNASR_ROOT = Path(os.environ.get("FUNASR_ROOT", r"E:\FunAsr"))
ENV_ROOT = Path(os.environ.get("LIVE_INTERPRETER_ENV", r"E:\ANACONDA\envs\funasr_py38"))
GUMMY_WORKER = ROOT / "gummy_realtime_worker.py"
QWEN_LIVE_WORKER = ROOT / "qwen_livetranslate_worker.py"
FFMPEG = ENV_ROOT / "Library" / "bin" / "ffmpeg.exe"
LOOPBACK_DEVICE = "__wasapi_loopback__"
BROWSER_DEVICE = "__edge_tab_audio__"
MODEL_CACHE = FUNASR_ROOT / "models"
QWEN_MODEL = MODEL_CACHE / "Qwen" / "Qwen2___5-3B-Instruct-GPTQ-Int4"
SENSEVOICE_MODEL = MODEL_CACHE / "models" / "iic" / "SenseVoiceSmall"
VAD_MODEL = MODEL_CACHE / "models" / "iic" / "speech_fsmn_vad_zh-cn-16k-common-pytorch"
PARAFORMER_STREAMING_MODEL = (
    MODEL_CACHE
    / "models"
    / "iic"
    / "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
)
ENV_TCL = ENV_ROOT / "tcl" / "tcl8.6"
ENV_TK = ENV_ROOT / "tcl" / "tk8.6"

TAG_RE = re.compile(r"<\|[^>]+?\|>")
AUDCLNT_E_DEVICE_INVALIDATED = 0x88890004
WASAPI_RESTART_EXIT_CODE = 77
PENDING_TRANSLATION = ""
GUMMY_LANGUAGE_MAP = {
    "auto": "auto",
    "chinese": "zh",
    "中文": "zh",
    "mandarin": "zh",
    "japanese": "ja",
    "日语": "ja",
    "english": "en",
    "英语": "en",
    "korean": "ko",
    "韩语": "ko",
}


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_ulong),
        ("nAvgBytesPerSec", ctypes.c_ulong),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class WasapiDeviceInvalidated(OSError):
    pass


def make_guid(value: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def com_call(obj: ctypes.c_void_p, index: int, restype, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def check_hr(hr: int, action: str) -> None:
    if hr < 0:
        code = hr & 0xFFFFFFFF
        if code == AUDCLNT_E_DEVICE_INVALIDATED:
            raise WasapiDeviceInvalidated(f"{action} failed: HRESULT 0x{code:08X}")
        raise OSError(f"{action} failed: HRESULT 0x{code:08X}")


def configure_environment() -> None:
    os.environ.setdefault("MODELSCOPE_CACHE", str(MODEL_CACHE))
    os.environ.setdefault("HF_HOME", str(MODEL_CACHE / "huggingface"))
    os.environ.setdefault("TRANSFORMERS_CACHE", str(MODEL_CACHE / "huggingface"))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ["TCL_LIBRARY"] = str(ENV_TCL)
    os.environ["TK_LIBRARY"] = str(ENV_TK)
    os.environ["PATH"] = str(ENV_ROOT / "Library" / "bin") + os.pathsep + os.environ.get("PATH", "")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch

        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def run_ffmpeg(args: List[str], *, quiet: bool = False) -> subprocess.Popen:
    if not FFMPEG.exists():
        raise RuntimeError(f"FFmpeg not found: {FFMPEG}")
    stderr = subprocess.DEVNULL if quiet else None
    return subprocess.Popen([str(FFMPEG), *args], stdin=subprocess.PIPE, stderr=stderr)


def gummy_language_code(language: str) -> str:
    normalized = (language or "auto").strip()
    return GUMMY_LANGUAGE_MAP.get(normalized.lower(), normalized.lower())


def pcm_bytes_to_mono16(data: bytes, channels: int, bits: int, is_float: bool) -> bytes:
    if not data or channels <= 0:
        return b""
    out = array.array("h")
    if is_float:
        samples = array.array("f")
        samples.frombytes(data)
        frame_count = len(samples) // channels
        for frame in range(frame_count):
            value = 0.0
            base = frame * channels
            for channel in range(channels):
                value += samples[base + channel]
            value = max(-1.0, min(1.0, value / channels))
            out.append(int(value * 32767.0))
    elif bits == 16:
        samples = array.array("h")
        samples.frombytes(data)
        frame_count = len(samples) // channels
        for frame in range(frame_count):
            total = 0
            base = frame * channels
            for channel in range(channels):
                total += samples[base + channel]
            out.append(int(total / channels))
    elif bits == 32:
        samples = array.array("i")
        samples.frombytes(data)
        frame_count = len(samples) // channels
        for frame in range(frame_count):
            total = 0
            base = frame * channels
            for channel in range(channels):
                total += samples[base + channel]
            out.append(int(max(-32768, min(32767, (total / channels) / 65536))))
    else:
        raise RuntimeError(f"Unsupported WASAPI sample format: {bits}-bit")
    return out.tobytes()


def record_wasapi_loopback(
    chunks_dir: Optional[Path],
    chunk_seconds: float,
    max_chunks: Optional[int] = None,
    pcm_output: Optional[BinaryIO] = None,
) -> None:
    ole32 = ctypes.oledll.ole32
    clsid_mmdevice_enumerator = make_guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
    iid_immdevice_enumerator = make_guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
    iid_iaudio_client = make_guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
    iid_iaudio_capture_client = make_guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317")

    clsctx_all = 23
    e_render = 0
    e_multimedia = 1
    audclnt_sharemode_shared = 0
    audclnt_streamflags_loopback = 0x00020000
    audclnt_bufferflags_silent = 0x00000002

    hr = ole32.CoInitializeEx(None, 0)
    if hr not in (0, 1, 0x80010106):
        check_hr(hr, "CoInitializeEx")

    enumerator = ctypes.c_void_p()
    device = ctypes.c_void_p()
    audio_client = ctypes.c_void_p()
    capture_client = ctypes.c_void_p()
    mix_format_ptr = ctypes.POINTER(WAVEFORMATEX)()
    state = None
    chunk_index = 0

    try:
        check_hr(
            ole32.CoCreateInstance(
                ctypes.byref(clsid_mmdevice_enumerator),
                None,
                clsctx_all,
                ctypes.byref(iid_immdevice_enumerator),
                ctypes.byref(enumerator),
            ),
            "CoCreateInstance(IMMDeviceEnumerator)",
        )
        get_default = com_call(
            enumerator,
            4,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )
        check_hr(get_default(enumerator, e_render, e_multimedia, ctypes.byref(device)), "GetDefaultAudioEndpoint")

        activate = com_call(
            device,
            3,
            ctypes.c_long,
            ctypes.POINTER(GUID),
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        check_hr(
            activate(device, ctypes.byref(iid_iaudio_client), clsctx_all, None, ctypes.byref(audio_client)),
            "IMMDevice.Activate(IAudioClient)",
        )

        get_mix_format = com_call(
            audio_client,
            8,
            ctypes.c_long,
            ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)),
        )
        check_hr(get_mix_format(audio_client, ctypes.byref(mix_format_ptr)), "IAudioClient.GetMixFormat")
        mix_format = mix_format_ptr.contents
        source_rate = int(mix_format.nSamplesPerSec)
        channels = int(mix_format.nChannels)
        bits = int(mix_format.wBitsPerSample)
        is_float = int(mix_format.wFormatTag) == 3 or bits == 32
        print(
            f"WASAPI loopback: {source_rate} Hz, {channels} ch, {bits}-bit, "
            f"{'float' if is_float else 'pcm'}",
            file=sys.stderr if pcm_output is not None else sys.stdout,
            flush=True,
        )

        initialize = com_call(
            audio_client,
            3,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.POINTER(WAVEFORMATEX),
            ctypes.c_void_p,
        )
        check_hr(
            initialize(
                audio_client,
                audclnt_sharemode_shared,
                audclnt_streamflags_loopback,
                10_000_000,
                0,
                mix_format_ptr,
                None,
            ),
            "IAudioClient.Initialize(loopback)",
        )

        get_service = com_call(
            audio_client,
            14,
            ctypes.c_long,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )
        check_hr(
            get_service(audio_client, ctypes.byref(iid_iaudio_capture_client), ctypes.byref(capture_client)),
            "IAudioClient.GetService(IAudioCaptureClient)",
        )

        start = com_call(audio_client, 10, ctypes.c_long)
        stop = com_call(audio_client, 11, ctypes.c_long)
        get_next_packet_size = com_call(
            capture_client,
            5,
            ctypes.c_long,
            ctypes.POINTER(ctypes.c_uint32),
        )
        get_buffer = com_call(
            capture_client,
            3,
            ctypes.c_long,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        )
        release_buffer = com_call(capture_client, 4, ctypes.c_long, ctypes.c_uint32)

        if pcm_output is None:
            if chunks_dir is None:
                raise RuntimeError("Chunk output directory is required.")
            chunks_dir.mkdir(parents=True, exist_ok=True)
        check_hr(start(audio_client), "IAudioClient.Start")
        try:
            if pcm_output is not None:
                last_pcm_write = time.monotonic()
                while True:
                    packet_size = ctypes.c_uint32()
                    check_hr(
                        get_next_packet_size(capture_client, ctypes.byref(packet_size)),
                        "GetNextPacketSize",
                    )
                    if packet_size.value == 0:
                        if time.monotonic() - last_pcm_write >= 0.1:
                            pcm_output.write(b"\x00\x00" * 1600)
                            pcm_output.flush()
                            last_pcm_write = time.monotonic()
                        time.sleep(0.005)
                        continue
                    while packet_size.value:
                        data_ptr = ctypes.c_void_p()
                        frames = ctypes.c_uint32()
                        flags = ctypes.c_ulong()
                        device_position = ctypes.c_uint64()
                        qpc_position = ctypes.c_uint64()
                        check_hr(
                            get_buffer(
                                capture_client,
                                ctypes.byref(data_ptr),
                                ctypes.byref(frames),
                                ctypes.byref(flags),
                                ctypes.byref(device_position),
                                ctypes.byref(qpc_position),
                            ),
                            "IAudioCaptureClient.GetBuffer",
                        )
                        try:
                            byte_count = int(frames.value) * int(mix_format.nBlockAlign)
                            if flags.value & audclnt_bufferflags_silent:
                                mono16 = b"\x00\x00" * int(frames.value)
                            else:
                                raw = ctypes.string_at(data_ptr, byte_count)
                                mono16 = pcm_bytes_to_mono16(raw, channels, bits, is_float)
                            converted, state = audioop.ratecv(mono16, 2, 1, source_rate, 16000, state)
                            if converted:
                                pcm_output.write(converted)
                                pcm_output.flush()
                                last_pcm_write = time.monotonic()
                        finally:
                            check_hr(release_buffer(capture_client, frames), "ReleaseBuffer")
                        check_hr(
                            get_next_packet_size(capture_client, ctypes.byref(packet_size)),
                            "GetNextPacketSize",
                        )
            else:
                while max_chunks is None or chunk_index < max_chunks:
                    output = chunks_dir / f"chunk_{chunk_index:06d}.wav"
                    deadline = time.monotonic() + chunk_seconds
                    try:
                        with wave.open(str(output), "wb") as wav:
                            wav.setnchannels(1)
                            wav.setsampwidth(2)
                            wav.setframerate(16000)
                            while time.monotonic() < deadline:
                                packet_size = ctypes.c_uint32()
                                check_hr(
                                    get_next_packet_size(capture_client, ctypes.byref(packet_size)),
                                    "GetNextPacketSize",
                                )
                                if packet_size.value == 0:
                                    time.sleep(0.01)
                                    continue
                                while packet_size.value:
                                    data_ptr = ctypes.c_void_p()
                                    frames = ctypes.c_uint32()
                                    flags = ctypes.c_ulong()
                                    device_position = ctypes.c_uint64()
                                    qpc_position = ctypes.c_uint64()
                                    check_hr(
                                        get_buffer(
                                            capture_client,
                                            ctypes.byref(data_ptr),
                                            ctypes.byref(frames),
                                            ctypes.byref(flags),
                                            ctypes.byref(device_position),
                                            ctypes.byref(qpc_position),
                                        ),
                                        "IAudioCaptureClient.GetBuffer",
                                    )
                                    try:
                                        byte_count = int(frames.value) * int(mix_format.nBlockAlign)
                                        if flags.value & audclnt_bufferflags_silent:
                                            mono16 = b"\x00\x00" * int(frames.value)
                                        else:
                                            raw = ctypes.string_at(data_ptr, byte_count)
                                            mono16 = pcm_bytes_to_mono16(raw, channels, bits, is_float)
                                        converted, state = audioop.ratecv(
                                            mono16, 2, 1, source_rate, 16000, state
                                        )
                                        if converted:
                                            wav.writeframes(converted)
                                    finally:
                                        check_hr(release_buffer(capture_client, frames), "ReleaseBuffer")
                                    check_hr(
                                        get_next_packet_size(capture_client, ctypes.byref(packet_size)),
                                        "GetNextPacketSize",
                                    )
                    except WasapiDeviceInvalidated:
                        try:
                            output.unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise
                    print(f"WASAPI chunk written: {output}", flush=True)
                    chunk_index += 1
        except BrokenPipeError:
            return
        finally:
            stop(audio_client)
    finally:
        if mix_format_ptr:
            ole32.CoTaskMemFree(mix_format_ptr)
        release = lambda obj: com_call(obj, 2, ctypes.c_ulong)(obj) if obj else None
        release(capture_client)
        release(audio_client)
        release(device)
        release(enumerator)
        ole32.CoUninitialize()


def list_devices() -> int:
    print("DirectShow devices detected by FFmpeg:\n")
    proc = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = proc.stdout.decode("utf-8", errors="replace")
    print(output)
    print("Use an audio device exactly as shown, for example:")
    print('  --audio-device "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)"')
    return 0


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: List[Dict[str, object]] = []
        self.status = "starting"

    def set_status(self, status: str) -> None:
        with self.lock:
            self.status = status

    def add_event(self, source: str, translated: str, chunk: str) -> None:
        now = time.monotonic()
        item = {
            "time": _dt.datetime.now().strftime("%H:%M:%S"),
            "source": source,
            "translated": translated,
            "chunk": chunk,
            "final": False,
            "updated_at": now,
            "finalized_at": None,
        }
        with self.lock:
            self.events.append(item)
            self.events = self.events[-80:]

    def update_event(self, chunk: str, translated: str) -> None:
        with self.lock:
            for item in reversed(self.events):
                if item.get("chunk") == chunk:
                    now = time.monotonic()
                    item["translated"] = translated
                    item["time"] = _dt.datetime.now().strftime("%H:%M:%S")
                    item["updated_at"] = now
                    item["final"] = True
                    item["finalized_at"] = now
                    return

    def upsert_event(
        self,
        chunk: str,
        source: Optional[str] = None,
        translated: Optional[str] = None,
        final: Optional[bool] = None,
    ) -> None:
        with self.lock:
            now = time.monotonic()
            item = next((event for event in reversed(self.events) if event.get("chunk") == chunk), None)
            if item is None:
                item = {
                    "time": _dt.datetime.now().strftime("%H:%M:%S"),
                    "source": source or "",
                    "translated": translated or "",
                    "chunk": chunk,
                    "final": bool(final),
                    "updated_at": now,
                    "finalized_at": now if final else None,
                }
                self.events.append(item)
                self.events = self.events[-80:]
                return
            if source is not None:
                item["source"] = source
            if translated is not None:
                item["translated"] = translated
            if final and not item.get("final"):
                item["final"] = True
                item["finalized_at"] = now
            item["time"] = _dt.datetime.now().strftime("%H:%M:%S")
            item["updated_at"] = now

    def snapshot(self) -> Dict[str, object]:
        with self.lock:
            return {
                "service": "live-interpreter",
                "api_version": 2,
                "status": self.status,
                "monotonic_now": time.monotonic(),
                "events": list(self.events),
            }

    def clear_events(self) -> None:
        with self.lock:
            self.events.clear()

    def latest_event(self) -> Optional[Dict[str, object]]:
        with self.lock:
            return self.events[-1] if self.events else None


class BrowserAudioInput:
    def __init__(self) -> None:
        self.frames: "queue.Queue[bytes]" = queue.Queue(maxsize=256)
        self.active = threading.Event()

    def start(self) -> None:
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break
        self.active.set()

    def stop(self) -> None:
        self.active.clear()

    def push(self, frame: bytes) -> bool:
        if not self.active.is_set() or not frame:
            return False
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            self.frames.put_nowait(frame)
        return True

    def read(self, timeout: float = 0.5) -> bytes:
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return b""


class BrowserVisualInput:
    def __init__(self, path: Path, enabled: bool) -> None:
        self.path = path
        self.enabled = enabled
        self.active = threading.Event()

    def start(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        if self.enabled:
            self.active.set()

    def stop(self) -> None:
        self.active.clear()

    def push(self, frame: bytes) -> bool:
        if not self.active.is_set() or not frame:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(frame)
        os.replace(str(temporary), str(self.path))
        return True


def is_pending_translation(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    return not normalized or normalized in {"翻译中...", "translating...", "缈昏瘧涓?.."}


def compose_live_subtitles(
    events: List[Dict[str, object]],
    minimum_events: int,
    maximum_events: int,
    hold_seconds: float,
    maximum_chars: int,
    now: Optional[float] = None,
) -> Tuple[str, str]:
    current_time = time.monotonic() if now is None else now
    minimum_events = max(1, minimum_events)
    maximum_events = max(minimum_events, maximum_events)
    candidates = [
        item
        for item in events
        if str(item.get("source", "")).strip() or str(item.get("translated", "")).strip()
    ]
    selected: List[Dict[str, object]] = []
    for item in reversed(candidates):
        finalized_at = item.get("finalized_at")
        age = current_time - float(finalized_at) if finalized_at is not None else 0.0
        must_keep = (
            len(selected) < minimum_events
            or not bool(item.get("final"))
            or age < hold_seconds
        )
        if must_keep and len(selected) < maximum_events:
            selected.append(item)
        elif len(selected) >= minimum_events:
            break
    selected.reverse()

    translated_parts = [
        str(item.get("translated", "")).strip()
        for item in selected
        if str(item.get("translated", "")).strip()
        and not is_pending_translation(str(item.get("translated", "")))
    ]
    source_parts = [
        str(item.get("source", "")).strip()
        for item in selected[-2:]
        if str(item.get("source", "")).strip()
    ]
    while len("\n".join(translated_parts)) > maximum_chars and len(translated_parts) > minimum_events:
        translated_parts.pop(0)
    translated = "\n".join(translated_parts)
    source = "\n".join(source_parts)
    if not translated and source:
        return source, ""
    return translated, source


def make_handler(
    state: State,
    browser_audio: BrowserAudioInput,
    stop_event: Optional[threading.Event] = None,
    browser_visual: Optional[BrowserVisualInput] = None,
):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/events"):
                snapshot = state.snapshot()
                snapshot["visual_enabled"] = bool(browser_visual and browser_visual.enabled)
                body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("X-Live-Interpreter") != "edge-extension":
                self.respond(403)
                return
            if self.path == "/audio/start":
                state.clear_events()
                browser_audio.start()
                if browser_visual:
                    browser_visual.start()
                state.set_status("waiting for Edge tab audio")
                self.respond(204)
                return
            if self.path == "/audio/stop":
                browser_audio.stop()
                if browser_visual:
                    browser_visual.stop()
                state.set_status("Edge tab audio stopped")
                self.respond(204)
                return
            if self.path == "/shutdown" and stop_event is not None:
                stop_event.set()
                state.set_status("stopping")
                self.respond(204)
                return
            if self.path not in {"/audio", "/visual"}:
                self.respond(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.respond(400)
                return
            maximum = 512000 if self.path == "/visual" else 131072
            if length <= 0 or length > maximum or (self.path == "/audio" and length % 2):
                self.respond(413)
                return
            frame = self.rfile.read(length)
            if self.path == "/visual":
                if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
                    self.respond(415)
                    return
                if browser_visual and browser_visual.push(frame):
                    self.respond(204)
                else:
                    self.respond(409)
                return
            if not browser_audio.push(frame):
                self.respond(409)
                return
            state.set_status("listening (Edge tab audio)")
            self.respond(204)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return Handler


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Live Interpreter</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      background: #101418;
      color: #f4f7fa;
    }
    body {
      margin: 0;
      min-height: 100vh;
      background: #101418;
    }
    header {
      position: sticky;
      top: 0;
      padding: 14px 20px;
      background: rgba(16, 20, 24, .92);
      border-bottom: 1px solid #28323b;
      backdrop-filter: blur(10px);
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
    }
    #status {
      color: #9fb2c6;
      font-size: 13px;
    }
    main {
      max-width: 1040px;
      margin: 0 auto;
      padding: 18px 20px 60px;
    }
    .event {
      border-bottom: 1px solid #27323b;
      padding: 16px 0;
    }
    .meta {
      color: #8ba1b7;
      font-size: 12px;
      margin-bottom: 8px;
    }
    .translated {
      font-size: clamp(28px, 5vw, 58px);
      line-height: 1.18;
      font-weight: 760;
      overflow-wrap: anywhere;
    }
    .source {
      margin-top: 8px;
      color: #b9c6d2;
      font-size: clamp(16px, 2.2vw, 24px);
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .empty {
      color: #93a6b7;
      margin-top: 20vh;
      text-align: center;
    }
  </style>
</head>
<body>
  <header>
    <h1>Live Interpreter</h1>
    <div id="status">connecting</div>
  </header>
  <main id="events"><div class="empty">等待识别结果</div></main>
  <script>
    const eventsEl = document.getElementById("events");
    const statusEl = document.getElementById("status");

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }

    async function refresh() {
      try {
        const res = await fetch("/events?ts=" + Date.now());
        const data = await res.json();
        statusEl.textContent = data.status;
        if (!data.events.length) return;
        eventsEl.innerHTML = data.events.slice().reverse().map(item => `
          <section class="event">
            <div class="meta">${escapeHtml(item.time)} · ${escapeHtml(item.chunk)}</div>
            <div class="translated">${escapeHtml(item.translated || item.source)}</div>
            <div class="source">${escapeHtml(item.source)}</div>
          </section>
        `).join("");
      } catch (err) {
        statusEl.textContent = "browser disconnected";
      }
    }

    refresh();
    setInterval(refresh, 700);
  </script>
</body>
</html>
"""


def parse_geometry(value: str) -> Tuple[int, int, int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("Expected geometry like 1100x220+120+720")
    width, height, x, y = match.groups()
    return int(width), int(height), int(x), int(y)


def resize_geometry(
    mode: str,
    x: int,
    y: int,
    width: int,
    height: int,
    dx: int,
    dy: int,
    min_width: int = 480,
    min_height: int = 180,
) -> Tuple[int, int, int, int]:
    next_x, next_y, next_width, next_height = x, y, width, height
    if "w" in mode:
        next_x, next_width = x + dx, width - dx
    elif "e" in mode:
        next_width = width + dx
    if "n" in mode:
        next_y, next_height = y + dy, height - dy
    elif "s" in mode:
        next_height = height + dy
    if next_width < min_width:
        next_x = x + width - min_width if "w" in mode else x
        next_width = min_width
    if next_height < min_height:
        next_y = y + height - min_height if "n" in mode else y
        next_height = min_height
    return next_x, next_y, next_width, next_height


def visibility_progress(visible: bool, changed_at: float, now: float, duration: float = 0.15) -> float:
    elapsed = min(1.0, max(0.0, (now - changed_at) / duration))
    return elapsed if visible else 1.0 - elapsed


def subtitle_spacing(linespace: int) -> Tuple[int, int]:
    line_height = max(25, round(linespace * 1.35))
    return line_height, max(9, round(line_height * 0.2))


def run_overlay_window(args: argparse.Namespace, state: State, stop_event: threading.Event) -> None:
    import tkinter as tk
    from tkinter import font as tkfont

    width, height, x, y = args.overlay_geometry
    transparent_color = "#010203"
    drag_offset = {"x": None, "y": None}
    resize_state = {
        "mode": "",
        "pointer_x": 0,
        "pointer_y": 0,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }
    pointer_state = {"x": 0, "y": 0, "moved": False, "controls_were_visible": False}
    last_rendered = {"text": ""}
    display_state = {"count": 0, "translated": "", "source": "", "updated_at": 0.0}
    now = time.monotonic()
    ui_state = {
        "show_source": bool(args.overlay_show_source),
        "locked": False,
        "expanded": False,
        "restore_height": height,
        "controls_visible": False,
        "controls_changed_at": now - 0.15,
        "controls_deadline": 0.0,
        "toolbar_hover": False,
        "hover": "",
        "pressed": "",
        "sentence_count": 0,
        "fade_started": 0.0,
        "empty_since": now,
        "had_text": False,
        "status": "",
        "status_changed_at": now,
    }
    button_bounds: Dict[str, Tuple[int, int, int, int]] = {}
    panel_radius = 8

    root = tk.Tk()
    root.title("Live Interpreter Overlay")
    root.overrideredirect(True)
    root.geometry(f"{width}x{height}{x:+d}{y:+d}")
    root.minsize(480, 180)
    root.configure(bg=transparent_color)
    root.attributes("-topmost", True)
    root.attributes("-alpha", args.overlay_alpha)
    try:
        root.wm_attributes("-transparentcolor", transparent_color)
    except tk.TclError:
        pass

    canvas = tk.Canvas(root, bg=transparent_color, bd=0, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    subtitle_font = tkfont.Font(family=args.overlay_font, size=-max(15, args.overlay_font_size), weight="bold")
    source_font = tkfont.Font(family=args.overlay_font, size=-13, weight="normal")
    toolbar_font = tkfont.Font(family=args.overlay_font, size=-12, weight="normal")
    icon_font = tkfont.Font(family="Segoe UI Symbol", size=-16, weight="normal")
    subtitle_line_height, subtitle_sentence_gap = subtitle_spacing(subtitle_font.metrics("linespace"))
    source_line_height = max(20, source_font.metrics("linespace") + 2)

    def rounded_rect(x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs) -> int:
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=12, **kwargs)

    def wrap_pixels(text: str, font: tkfont.Font, max_width: int) -> List[str]:
        lines: List[str] = []
        for paragraph in text.splitlines() or [text]:
            tokens = re.findall(r"\S+\s*|\s+", paragraph)
            current = ""
            for token in tokens:
                if font.measure(token) > max_width:
                    for char in token:
                        if current and font.measure(current + char) > max_width:
                            lines.append(current.rstrip())
                            current = ""
                        current += char
                elif current and font.measure(current + token) > max_width:
                    lines.append(current.rstrip())
                    current = token.lstrip()
                else:
                    current += token
            if current.strip() or not lines:
                lines.append(current.rstrip())
        return lines

    def mix_color(start: str, end: str, progress: float) -> str:
        values = []
        for index in (1, 3, 5):
            first = int(start[index : index + 2], 16)
            last = int(end[index : index + 2], 16)
            values.append(round(first + (last - first) * progress))
        return "#" + "".join(f"{value:02x}" for value in values)

    def hit_test(x_pos: int, y_pos: int) -> str:
        for name, (x1, y1, x2, y2) in button_bounds.items():
            if x1 <= x_pos <= x2 and y1 <= y_pos <= y2:
                return name
        return ""

    def resize_mode(x_pos: int, y_pos: int) -> str:
        edge = 12
        west = x_pos <= edge
        east = x_pos >= canvas.winfo_width() - edge
        north = y_pos <= edge
        south = y_pos >= canvas.winfo_height() - edge
        return (
            ("n" if north else "s" if south else "")
            + ("w" if west else "e" if east else "")
        )

    def invalidate() -> None:
        last_rendered["text"] = ""

    def show_controls() -> None:
        current = time.monotonic()
        if not ui_state["controls_visible"]:
            ui_state["controls_visible"] = True
            ui_state["controls_changed_at"] = current
        ui_state["controls_deadline"] = current + 4.0
        invalidate()

    def hide_controls(event: Optional[tk.Event] = None) -> None:
        if ui_state["controls_visible"]:
            ui_state["controls_visible"] = False
            ui_state["controls_changed_at"] = time.monotonic()
            ui_state["hover"] = ""
            ui_state["pressed"] = ""
            invalidate()

    def close_overlay(event: Optional[tk.Event] = None) -> None:
        stop_event.set()
        state.set_status("stopping")
        root.destroy()

    def toggle_expand() -> None:
        current_width = root.winfo_width()
        current_x = root.winfo_x()
        current_y = root.winfo_y()
        if ui_state["expanded"]:
            next_height = int(ui_state["restore_height"])
        else:
            ui_state["restore_height"] = root.winfo_height()
            next_height = max(400, root.winfo_height())
        ui_state["expanded"] = not bool(ui_state["expanded"])
        root.geometry(f"{current_width}x{next_height}{current_x:+d}{current_y:+d}")
        invalidate()

    def run_action(name: str) -> None:
        if name == "close":
            close_overlay()
        elif name == "expand":
            toggle_expand()
        elif name == "lock":
            ui_state["locked"] = not bool(ui_state["locked"])
            invalidate()
        elif name == "cc":
            ui_state["show_source"] = not bool(ui_state["show_source"])
            invalidate()
        if name != "close":
            show_controls()

    def begin_pointer(event: tk.Event) -> None:
        controls_were_visible = bool(ui_state["controls_visible"])
        pointer_state.update(
            x=event.x_root,
            y=event.y_root,
            moved=False,
            controls_were_visible=controls_were_visible,
        )
        show_controls()
        name = hit_test(event.x, event.y) if controls_were_visible else ""
        if name:
            ui_state["pressed"] = name
            invalidate()
            return
        mode = resize_mode(event.x, event.y)
        if not ui_state["locked"] and mode:
            resize_state.update(
                mode=mode,
                pointer_x=event.x_root,
                pointer_y=event.y_root,
                x=root.winfo_x(),
                y=root.winfo_y(),
                width=root.winfo_width(),
                height=root.winfo_height(),
            )
            return
        if not ui_state["locked"]:
            drag_offset["x"] = event.x_root - root.winfo_x()
            drag_offset["y"] = event.y_root - root.winfo_y()

    def move_pointer(event: tk.Event) -> None:
        if abs(event.x_root - int(pointer_state["x"])) > 3 or abs(event.y_root - int(pointer_state["y"])) > 3:
            pointer_state["moved"] = True
        if resize_state["mode"] or drag_offset.get("x") is not None:
            show_controls()
        if resize_state["mode"]:
            next_x, next_y, next_width, next_height = resize_geometry(
                str(resize_state["mode"]),
                int(resize_state["x"]),
                int(resize_state["y"]),
                int(resize_state["width"]),
                int(resize_state["height"]),
                event.x_root - int(resize_state["pointer_x"]),
                event.y_root - int(resize_state["pointer_y"]),
            )
            root.geometry(f"{next_width}x{next_height}{next_x:+d}{next_y:+d}")
            return
        if drag_offset.get("x") is not None and not ui_state["pressed"] and not ui_state["locked"]:
            next_x = event.x_root - int(drag_offset["x"])
            next_y = event.y_root - int(drag_offset["y"])
            root.geometry(f"{next_x:+d}{next_y:+d}")

    def hover_pointer(event: tk.Event) -> None:
        ui_state["toolbar_hover"] = bool(ui_state["controls_visible"] and event.y <= 52)
        if ui_state["toolbar_hover"]:
            ui_state["controls_deadline"] = time.monotonic() + 4.0
        name = hit_test(event.x, event.y) if ui_state["controls_visible"] else ""
        if name != ui_state["hover"]:
            ui_state["hover"] = name
            invalidate()
        if name:
            canvas.configure(cursor="hand2")
        elif not ui_state["locked"] and resize_mode(event.x, event.y):
            cursors = {
                "n": "size_ns",
                "s": "size_ns",
                "e": "size_we",
                "w": "size_we",
                "nw": "size_nw_se",
                "se": "size_nw_se",
                "ne": "size_ne_sw",
                "sw": "size_ne_sw",
            }
            canvas.configure(cursor=cursors[resize_mode(event.x, event.y)])
        elif not ui_state["locked"]:
            canvas.configure(cursor="fleur")
        else:
            canvas.configure(cursor="arrow")

    def end_pointer(event: tk.Event) -> None:
        pressed = str(ui_state["pressed"])
        moved = bool(pointer_state["moved"])
        controls_were_visible = bool(pointer_state["controls_were_visible"])
        ui_state["pressed"] = ""
        resize_state["mode"] = ""
        drag_offset["x"] = None
        drag_offset["y"] = None
        if pressed and hit_test(event.x, event.y) == pressed:
            run_action(pressed)
        elif not moved and controls_were_visible:
            hide_controls()
        invalidate()

    def leave_pointer(event: tk.Event) -> None:
        ui_state["hover"] = ""
        ui_state["toolbar_hover"] = False
        invalidate()

    def status_view(status: str) -> Tuple[str, str]:
        lowered = status.lower()
        if "error" in lowered or "failed" in lowered:
            return "连接异常", "#E56A6A"
        if "reconnect" in lowered or "changed" in lowered:
            return "正在重连", "#D9A441"
        if "no audible" in lowered or "waiting" in lowered or "starting" in lowered:
            return "等待声音", "#9AA1AA"
        if "stopping" in lowered:
            return "正在停止", "#9AA1AA"
        return "正在监听", "#63B58A"

    def draw_text(
        text: str,
        source: str,
        status: str,
        fade_progress: float,
        control_progress: float,
        current_time: float,
    ) -> None:
        canvas.delete("all")
        button_bounds.clear()
        current_width = max(canvas.winfo_width(), 1)
        current_height = max(canvas.winfo_height(), 1)
        panel_x1, panel_y1 = 7, 7
        panel_x2, panel_y2 = current_width - 9, current_height - 10
        toolbar_height = 40
        content_x = panel_x1 + 20
        content_width = max(300, panel_x2 - content_x - 20)
        content_top = panel_y1 + toolbar_height + 14
        content_bottom = max(content_top + 25, panel_y2 - 18)

        def outlined_line(x_pos: int, y_pos: int, value: str, font: tkfont.Font, fill: str) -> None:
            canvas.create_text(x_pos + 2, y_pos + 2, text=value, fill="#050607", font=font, anchor="nw")
            for offset_x, offset_y in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                canvas.create_text(x_pos + offset_x, y_pos + offset_y, text=value, fill="#0B0C0E", font=font, anchor="nw")
            canvas.create_text(x_pos, y_pos, text=value, fill=fill, font=font, anchor="nw")

        if control_progress > 0.0:
            toolbar_bottom = panel_y1 + toolbar_height
            control_fill = mix_color(transparent_color, "#141619", control_progress)
            control_shadow = mix_color(transparent_color, "#070809", control_progress)
            control_border = mix_color(transparent_color, "#44484F", control_progress)
            control_text = mix_color(transparent_color, "#E7E9EC", control_progress)
            control_muted = mix_color(transparent_color, "#969DA6", control_progress)
            rounded_rect(panel_x1 + 2, panel_y1 + 3, panel_x2 + 2, toolbar_bottom + 3, panel_radius, fill=control_shadow, outline="")
            rounded_rect(panel_x1, panel_y1, panel_x2, toolbar_bottom, panel_radius, fill=control_fill, outline=control_border, width=1)
            rounded_rect(panel_x1, panel_y1, panel_x2, panel_y2, panel_radius, fill="", outline=control_border, width=1)

            globe_x, globe_y = panel_x1 + 20, panel_y1 + 20
            canvas.create_oval(globe_x - 7, globe_y - 7, globe_x + 7, globe_y + 7, outline=control_text, width=1)
            canvas.create_arc(globe_x - 4, globe_y - 7, globe_x + 4, globe_y + 7, start=90, extent=180, style="arc", outline=control_text)
            canvas.create_arc(globe_x - 4, globe_y - 7, globe_x + 4, globe_y + 7, start=270, extent=180, style="arc", outline=control_text)
            canvas.create_line(globe_x - 6, globe_y, globe_x + 6, globe_y, fill=control_text)
            target_label = "中文" if str(args.target_language).lower() in {"chinese", "zh", "中文"} else str(args.target_language)
            canvas.create_text(globe_x + 14, globe_y, text=f"翻译为：{target_label}", fill=control_text, font=toolbar_font, anchor="w")

            cc_x1 = globe_x + 122
            cc_x2 = cc_x1 + 72
            if ui_state["controls_visible"]:
                button_bounds["cc"] = (cc_x1, panel_y1 + 5, cc_x2, panel_y1 + 35)
            if ui_state["hover"] == "cc" or ui_state["pressed"] == "cc":
                rounded_rect(cc_x1, panel_y1 + 6, cc_x2, panel_y1 + 34, 6, fill=mix_color(transparent_color, "#2A2D31", control_progress), outline="")
            cc_active = "#F0F2F4" if ui_state["show_source"] else "#757C85"
            canvas.create_text(cc_x1 + 8, globe_y, text="CC", fill=mix_color(transparent_color, cc_active, control_progress), font=toolbar_font, anchor="w")
            canvas.create_text(cc_x1 + 34, globe_y, text="原文", fill=mix_color(transparent_color, "#D5D9DE" if ui_state["show_source"] else "#757C85", control_progress), font=toolbar_font, anchor="w")
            if ui_state["show_source"]:
                canvas.create_line(cc_x1 + 8, panel_y1 + 33, cc_x2 - 8, panel_y1 + 33, fill=mix_color(transparent_color, "#63B58A", control_progress), width=2)

            icons = [("close", "×"), ("expand", "↙" if ui_state["expanded"] else "↗"), ("lock", "●" if ui_state["locked"] else "⌖")]
            for index, (name, glyph) in enumerate(icons):
                x2 = panel_x2 - 8 - index * 34
                x1 = x2 - 30
                if ui_state["controls_visible"]:
                    button_bounds[name] = (x1, panel_y1 + 5, x2, panel_y1 + 35)
                if ui_state["hover"] == name or ui_state["pressed"] == name:
                    hover_fill = "#503033" if name == "close" else "#2A2D31"
                    rounded_rect(x1, panel_y1 + 6, x2, panel_y1 + 34, 6, fill=mix_color(transparent_color, hover_fill, control_progress), outline="")
                icon_color = "#63B58A" if name == "lock" and ui_state["locked"] else "#F0F2F4"
                canvas.create_text((x1 + x2) // 2, globe_y, text=glyph, fill=mix_color(transparent_color, icon_color, control_progress), font=icon_font, anchor="center")

            tooltip_names = {
                "lock": "解除锁定" if ui_state["locked"] else "锁定位置",
                "expand": "收起字幕" if ui_state["expanded"] else "展开字幕",
                "close": "关闭",
            }
            hovered = str(ui_state["hover"])
            status_text, status_color = status_view(status)
            status_x = panel_x2 - 220
            if hovered in tooltip_names:
                canvas.create_text(panel_x2 - 118, globe_y, text=tooltip_names[hovered], fill=control_text, font=toolbar_font, anchor="e")
            elif current_width >= 760:
                canvas.create_oval(status_x - 7, globe_y - 3, status_x - 1, globe_y + 3, fill=mix_color(transparent_color, status_color, control_progress), outline="")
                canvas.create_text(status_x + 5, globe_y, text=status_text, fill=control_muted, font=toolbar_font, anchor="w")

            if not ui_state["locked"]:
                grip_color = mix_color(transparent_color, "#737A83", control_progress)
                grip_x, grip_y = panel_x2 - 9, panel_y2 - 9
                canvas.create_line(grip_x - 8, grip_y, grip_x, grip_y - 8, fill=grip_color)
                canvas.create_line(grip_x - 4, grip_y, grip_x, grip_y - 4, fill=grip_color)

        status_lower = status.lower()
        show_error = (
            ("error" in status_lower or "failed" in status_lower)
            and current_time - float(ui_state["status_changed_at"]) < 4.0
        )
        if show_error:
            error_text, _ = status_view(status)
            outlined_line(content_x, content_top, error_text, toolbar_font, "#E56A6A")
            content_top += 22

        if not text:
            if ui_state["controls_visible"] or current_time - float(ui_state["empty_since"]) < 3.0:
                outlined_line(content_x, content_bottom - 25, "等待声音", subtitle_font, "#8E959E")
            return

        line_height = subtitle_line_height
        sentence_gap = subtitle_sentence_gap
        source_gap = 16
        source_lines: List[str] = []
        if ui_state["show_source"] and source:
            wrapped_source = wrap_pixels(source, source_font, content_width)[-3:]
            max_source_lines = max(0, int((content_bottom - content_top - line_height - source_gap) // source_line_height))
            source_lines = wrapped_source[-min(len(wrapped_source), max_source_lines) :] if max_source_lines else []
        source_height = len(source_lines) * source_line_height
        available_height = max(line_height, content_bottom - content_top - source_height - (source_gap if source_lines else 0))

        sentences = [part.strip() for part in text.splitlines() if part.strip()]
        blocks = [(sentence, wrap_pixels(sentence, subtitle_font, content_width)) for sentence in sentences]
        visible: List[Tuple[str, List[str]]] = []
        used_height = 0
        for sentence, lines in reversed(blocks):
            block_height = len(lines) * line_height + (sentence_gap if visible else 0)
            if used_height + block_height <= available_height:
                visible.append((sentence, lines))
                used_height += block_height
                continue
            if not visible:
                max_lines = max(1, int(available_height // line_height))
                visible.append((sentence, lines[-max_lines:]))
                used_height = min(available_height, len(lines[-max_lines:]) * line_height)
            break
        visible.reverse()

        text_y = content_bottom - used_height
        if source_lines:
            source_y = text_y - source_gap - source_height
            for line in source_lines:
                outlined_line(content_x, source_y, line, source_font, "#AEB4BD")
                source_y += source_line_height

        for index, (_, lines) in enumerate(visible):
            is_current = index == len(visible) - 1
            fill = mix_color("#777D85", "#F7F8FA", fade_progress) if is_current else "#CDD1D7"
            for line in lines:
                outlined_line(content_x, text_y, line, subtitle_font, fill)
                text_y += line_height
            if index < len(visible) - 1:
                text_y += sentence_gap

    def compose_batch(events: List[Dict[str, object]], start: int) -> Tuple[str, str, int]:
        translated_parts: List[str] = []
        source_parts: List[str] = []
        index = start
        while index < len(events):
            item = events[index]
            translated = str(item.get("translated", ""))
            source_text = str(item.get("source", ""))
            if translated and not is_pending_translation(translated):
                translated_parts.append(translated)
            if source_text:
                source_parts.append(source_text)
            index += 1
            translated_text = " ".join(translated_parts)
            if (
                len(translated_text) >= args.overlay_batch_min_chars
                or len(translated_text) >= args.overlay_batch_max_chars
                or index - start >= args.overlay_batch_max_events
            ):
                break
        return (
            " ".join(translated_parts)[: args.overlay_batch_max_chars],
            " ".join(source_parts)[: args.overlay_batch_max_chars],
            index,
        )

    def compose_live(events: List[Dict[str, object]]) -> Tuple[str, str]:
        return compose_live_subtitles(
            events,
            args.overlay_live_events,
            args.overlay_live_max_events,
            args.overlay_hold_seconds,
            args.overlay_live_max_chars,
        )

    def refresh() -> None:
        if stop_event.is_set():
            root.destroy()
            return
        snapshot = state.snapshot()
        current_time = time.monotonic()
        current_status = str(snapshot["status"])
        if current_status != ui_state["status"]:
            ui_state["status"] = current_status
            ui_state["status_changed_at"] = current_time
        events = snapshot["events"]
        if args.overlay_live_mode:
            text, source = compose_live(events)
        else:
            if len(events) < int(display_state["count"]):
                display_state["count"] = 0
                display_state["translated"] = ""
                display_state["source"] = ""
            now = time.monotonic()
            has_pending = len(events) > int(display_state["count"])
            should_update = (
                has_pending
                and (
                    not display_state["translated"]
                    or now - float(display_state["updated_at"]) >= args.overlay_hold_seconds
                )
            )
            if should_update:
                translated, source, next_count = compose_batch(events, int(display_state["count"]))
                if translated or source:
                    display_state["translated"] = translated
                    display_state["source"] = source
                    display_state["count"] = next_count
                    display_state["updated_at"] = now
            text = str(display_state["translated"])
            source = str(display_state["source"])
        has_text = bool(text.strip())
        if has_text != bool(ui_state["had_text"]):
            ui_state["had_text"] = has_text
            if not has_text:
                ui_state["empty_since"] = current_time
        interaction_active = bool(
            ui_state["toolbar_hover"]
            or ui_state["pressed"]
            or resize_state["mode"]
            or drag_offset.get("x") is not None
        )
        if (
            ui_state["controls_visible"]
            and not interaction_active
            and current_time >= float(ui_state["controls_deadline"])
        ):
            hide_controls()
        sentence_count = len([part for part in text.splitlines() if part.strip()])
        if sentence_count > int(ui_state["sentence_count"]):
            ui_state["fade_started"] = current_time
        ui_state["sentence_count"] = sentence_count
        fade_progress = min(1.0, (current_time - float(ui_state["fade_started"])) / 0.16) if ui_state["fade_started"] else 1.0
        control_progress = visibility_progress(
            bool(ui_state["controls_visible"]),
            float(ui_state["controls_changed_at"]),
            current_time,
        )
        render_key = f"{current_status}\n{text}\n{source}\n{canvas.winfo_width()}x{canvas.winfo_height()}\n{ui_state['show_source']}\n{ui_state['locked']}\n{ui_state['expanded']}\n{ui_state['hover']}\n{ui_state['controls_visible']}\n{round(fade_progress, 1)}\n{round(control_progress, 1)}\n{int(current_time - float(ui_state['empty_since']))}\n{int(current_time - float(ui_state['status_changed_at']))}"
        if render_key != last_rendered["text"]:
            draw_text(text, source, current_status, fade_progress, control_progress, current_time)
            last_rendered["text"] = render_key
        animating = fade_progress < 1.0 or 0.0 < control_progress < 1.0
        root.after(30 if animating else (100 if ui_state["controls_visible"] else 200), refresh)

    canvas.bind("<ButtonPress-1>", begin_pointer)
    canvas.bind("<B1-Motion>", move_pointer)
    canvas.bind("<ButtonRelease-1>", end_pointer)
    canvas.bind("<Motion>", hover_pointer)
    canvas.bind("<Leave>", leave_pointer)
    root.bind("<Button-3>", close_overlay)
    root.bind("<Escape>", hide_controls)
    canvas.bind("<Button-3>", close_overlay)
    canvas.bind("<Configure>", lambda event: invalidate())
    root.after(100, refresh)
    root.mainloop()


class AsrEngine:
    def __init__(self, engine: str, language: str, device: str, args: argparse.Namespace) -> None:
        from funasr import AutoModel

        actual_device = resolve_device(device)
        self.engine = engine
        self.language = language
        self.cache: Dict[str, object] = {}
        self.streaming_chunk_size = [
            args.streaming_chunk_pad_left,
            args.streaming_chunk_stride,
            args.streaming_chunk_pad_right,
        ]
        self.encoder_chunk_look_back = args.streaming_encoder_lookback
        self.decoder_chunk_look_back = args.streaming_decoder_lookback
        if engine == "streaming":
            model_name = str(
                PARAFORMER_STREAMING_MODEL
                if PARAFORMER_STREAMING_MODEL.exists()
                else "paraformer-zh-streaming"
            )
            print(f"Loading FunASR streaming ASR on {actual_device}: {model_name}", flush=True)
            self.model = AutoModel(
                model=model_name,
                trust_remote_code=True,
                device=actual_device,
                disable_update=True,
            )
            if language not in ("auto", "zh", "cn", "Chinese", "chinese"):
                print(
                    "Warning: paraformer-zh-streaming is optimized for Chinese speech. "
                    "Use --asr-engine sensevoice for multilingual videos.",
                    flush=True,
                )
        else:
            print(f"Loading FunASR SenseVoice on {actual_device} ...", flush=True)
            self.model = AutoModel(
                model=str(SENSEVOICE_MODEL if SENSEVOICE_MODEL.exists() else "iic/SenseVoiceSmall"),
                vad_model=str(VAD_MODEL if VAD_MODEL.exists() else "fsmn-vad"),
                vad_kwargs={"max_single_segment_time": 12000},
                trust_remote_code=True,
                device=actual_device,
                disable_update=True,
            )

    def transcribe(self, wav_path: Path) -> str:
        if self.engine == "streaming":
            samples = read_wav_float32_mono16k(wav_path)
            if samples.size == 0:
                return ""
            result = self.model.generate(
                input=samples,
                cache=self.cache,
                is_final=False,
                chunk_size=self.streaming_chunk_size,
                encoder_chunk_look_back=self.encoder_chunk_look_back,
                decoder_chunk_look_back=self.decoder_chunk_look_back,
                fs=16000,
            )
            return clean_asr_text(extract_text(result))
        result = self.model.generate(
            input=str(wav_path),
            cache={},
            language=self.language,
            use_itn=True,
            batch_size_s=30,
            merge_vad=True,
            merge_length_s=6,
        )
        return clean_asr_text(extract_text(result))

    def close(self) -> None:
        return


class Translator:
    def __init__(self, engine: str, target_language: str, device: str, max_new_tokens: int) -> None:
        self.engine = engine
        self.target_language = target_language
        self.max_new_tokens = max_new_tokens
        self.tokenizer = None
        self.model = None
        if engine == "qwen":
            if not QWEN_MODEL.exists():
                raise RuntimeError(f"Qwen model not found: {QWEN_MODEL}")
            from transformers import AutoModelForCausalLM, AutoTokenizer

            actual_device = resolve_device(device)
            print(f"Loading Qwen translator on {actual_device} ...", flush=True)
            self.tokenizer = AutoTokenizer.from_pretrained(str(QWEN_MODEL), local_files_only=True)
            if "GPTQ" in QWEN_MODEL.name.upper():
                if not actual_device.startswith("cuda"):
                    raise RuntimeError("Qwen GPTQ-Int4 requires CUDA in this build. Set translate-device to cuda:0.")
                self.model = AutoModelForCausalLM.from_pretrained(
                    str(QWEN_MODEL),
                    local_files_only=True,
                    torch_dtype="auto",
                    device_map=actual_device,
                )
            else:
                self.model = AutoModelForCausalLM.from_pretrained(
                    str(QWEN_MODEL),
                    local_files_only=True,
                    torch_dtype="auto",
                )
                if actual_device.startswith("cuda"):
                    self.model = self.model.to(actual_device)
                else:
                    self.model = self.model.to("cpu")
            self.model.generation_config.do_sample = False
            self.model.generation_config.temperature = None
            self.model.generation_config.top_p = None
            self.model.generation_config.top_k = None

    def translate(self, text: str) -> str:
        if not text or self.engine == "none":
            return text
        if self.engine != "qwen":
            raise RuntimeError(f"Unsupported translation engine: {self.engine}")
        assert self.tokenizer is not None and self.model is not None
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a simultaneous interpreter. Translate the user's text into "
                    f"{self.target_language}. Return only the translation. Keep it concise and natural. "
                    "Keep names and numbers accurate. The text may be an imperfect ASR fragment from live audio; "
                    "translate the understandable part directly. Never refuse, never apologize, and never explain "
                    "that context is insufficient. If the text is already in the target language, return it unchanged."
                ),
            },
            {"role": "user", "content": text},
        ]
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.tokenizer([prompt], return_tensors="pt").to(self.model.device)
        output_ids = self.model.generate(
            **inputs,
            max_new_tokens=self.max_new_tokens,
            do_sample=False,
            repetition_penalty=1.05,
        )
        new_ids = output_ids[:, inputs.input_ids.shape[1] :]
        translated = self.tokenizer.batch_decode(new_ids, skip_special_tokens=True)[0].strip()
        if looks_like_translation_refusal(translated):
            return text
        return translated or text


def extract_text(result: object) -> str:
    texts: List[str] = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                value = item.get("text") or item.get("sentence_info") or ""
                if isinstance(value, str):
                    texts.append(value)
                elif isinstance(value, list):
                    texts.extend(str(part.get("text", "")) for part in value if isinstance(part, dict))
    elif isinstance(result, dict):
        value = result.get("text", "")
        if isinstance(value, str):
            texts.append(value)
    return " ".join(part for part in texts if part)


def clean_asr_text(text: str) -> str:
    text = TAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n。,.，")


def looks_like_translation_refusal(text: str) -> bool:
    lowered = text.lower()
    refusal_markers = (
        "无法提供翻译",
        "不能提供翻译",
        "无法翻译",
        "不能翻译",
        "对不起",
        "抱歉",
        "sorry",
        "cannot provide",
        "can't provide",
        "unable to translate",
        "insufficient context",
    )
    return any(marker in lowered for marker in refusal_markers)


def is_valid_wav(path: Path, min_seconds: float) -> bool:
    try:
        size = path.stat().st_size
        if size < 4096:
            return False
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            return rate > 0 and (frames / float(rate)) >= min_seconds
    except Exception:
        return False


def wav_level(path: Path) -> Tuple[float, int]:
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.readframes(wav.getnframes())
            width = wav.getsampwidth()
            if not frames or width <= 0:
                return 0.0, 0
            return float(audioop.rms(frames, width)), int(audioop.max(frames, width))
    except Exception:
        return 0.0, 0


def read_wav_float32_mono16k(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if not frames or width <= 0:
        return np.zeros(0, dtype=np.float32)
    if channels > 1:
        frames = audioop.tomono(frames, width, 1.0 / channels, 1.0 / channels)
        channels = 1
    if rate != 16000:
        frames, _ = audioop.ratecv(frames, width, channels, rate, 16000, None)
        rate = 16000
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return samples


def iter_ready_chunks(chunks_dir: Path, processed: set, min_age: float, min_seconds: float) -> Iterable[Path]:
    now = time.time()
    for path in sorted(chunks_dir.glob("chunk_*.wav")):
        if path in processed:
            continue
        try:
            if now - path.stat().st_mtime < min_age:
                continue
        except FileNotFoundError:
            continue
        if is_valid_wav(path, min_seconds):
            yield path


def start_audio_capture(audio_device: str, chunks_dir: Path, chunk_seconds: float) -> subprocess.Popen:
    if audio_device == LOOPBACK_DEVICE:
        return subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--loopback-capture",
                "--loopback-chunks-dir",
                str(chunks_dir),
                "--loopback-chunk-seconds",
                str(chunk_seconds),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    pattern = str(chunks_dir / "chunk_%06d.wav")
    args = [
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "dshow",
        "-i",
        f"audio={audio_device}",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-reset_timestamps",
        "1",
        pattern,
    ]
    return run_ffmpeg(args)


def start_pcm_audio_capture(audio_device: str) -> subprocess.Popen:
    if audio_device == LOOPBACK_DEVICE:
        return subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--loopback-pcm-stream",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    if not FFMPEG.exists():
        raise RuntimeError(f"FFmpeg not found: {FFMPEG}")
    return subprocess.Popen(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "dshow",
            "-i",
            f"audio={audio_device}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            "-f",
            "s16le",
            "pipe:1",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def test_audio_device(audio_device: str, work_dir: Path, seconds: float = 3.0) -> Tuple[float, int, Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    if audio_device == LOOPBACK_DEVICE:
        record_wasapi_loopback(work_dir, seconds, max_chunks=1)
        output = work_dir / "chunk_000000.wav"
        rms, peak = wav_level(output)
        return rms, peak, output
    output = work_dir / "audio_probe.wav"
    proc = subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "dshow",
            "-t",
            str(seconds),
            "-i",
            f"audio={audio_device}",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(output),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.returncode != 0:
        message = proc.stdout.decode("utf-8", errors="replace")
        raise RuntimeError(message.strip() or f"FFmpeg exited with {proc.returncode}")
    rms, peak = wav_level(output)
    return rms, peak, output


def start_screen_recording(recordings_dir: Path, fps: int) -> subprocess.Popen:
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output = recordings_dir / f"screen_{stamp}.mp4"
    args = [
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "gdigrab",
        "-framerate",
        str(fps),
        "-i",
        "desktop",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(output),
    ]
    print(f"Screen recording: {output}", flush=True)
    return run_ffmpeg(args)


def stop_process(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if proc.stdin:
            proc.stdin.write(b"q")
            proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def monitor_capture_process(
    audio_holder: Dict[str, Optional[subprocess.Popen]],
    args: argparse.Namespace,
    state: State,
    stop_event: threading.Event,
) -> None:
    restart_count = 0
    while not stop_event.is_set():
        proc = audio_holder.get("proc")
        if proc is None:
            time.sleep(0.5)
            continue
        code = proc.poll()
        if code is not None:
            if args.audio_device == LOOPBACK_DEVICE and code == WASAPI_RESTART_EXIT_CODE:
                restart_count += 1
                state.set_status("audio device changed; reconnecting")
                print(
                    f"WASAPI loopback device was invalidated; restarting audio capture "
                    f"(attempt {restart_count}).",
                    flush=True,
                )
                time.sleep(min(3.0, 0.5 * restart_count))
                if stop_event.is_set():
                    return
                try:
                    new_proc = start_audio_capture(args.audio_device, args.chunks_dir, args.chunk_seconds)
                    audio_holder["proc"] = new_proc
                    threading.Thread(target=forward_capture_output, args=(new_proc,), daemon=True).start()
                    state.set_status("listening")
                    continue
                except Exception as exc:
                    state.set_status(f"audio restart failed: {exc}")
                    print(f"Failed to restart audio capture: {exc}", file=sys.stderr, flush=True)
            else:
                state.set_status(f"audio capture stopped: {code}")
            stop_event.set()
            return
        time.sleep(0.5)


def forward_capture_output(proc: subprocess.Popen) -> None:
    if not proc.stdout:
        return
    for line in proc.stdout:
        print(line.rstrip(), flush=True)


def forward_binary_stderr(proc: subprocess.Popen, prefix: str) -> None:
    if not proc.stderr:
        return
    for raw in iter(proc.stderr.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            print(f"[{prefix}] {line}", flush=True)


def handle_cloud_output(
    proc: subprocess.Popen,
    state: State,
    ready: threading.Event,
    connection_state: Dict[str, bool],
    translation_enabled: bool,
    engine_name: str,
) -> None:
    if not proc.stdout:
        return
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(f"[{engine_name}] {line}", flush=True)
            continue
        event = message.get("event")
        if event == "ready":
            connection_state["ok"] = True
            state.set_status(f"listening ({engine_name})")
            ready.set()
            print(f"{engine_name} realtime translation connected.", flush=True)
        elif event == "result":
            sentence_id = message.get("sentence_id")
            chunk = f"{engine_name}-{sentence_id}"
            source = message.get("source") if "source" in message else None
            translated = message.get("translation") if "translation" in message else None
            final = bool(
                message.get("translation_final")
                if translation_enabled
                else message.get("source_final")
            )
            state.upsert_event(
                chunk,
                source=str(source) if source is not None else None,
                translated=str(translated) if translated is not None else None,
                final=final,
            )
            label = "final" if final else "partial"
            text = translated if translated is not None else source
            if text:
                print(f"{engine_name} {label} {sentence_id}: {text}", flush=True)
        elif event == "error":
            error = str(message.get("message") or "unknown error")
            connection_state["ok"] = False
            state.set_status(f"{engine_name} error: {error}")
            ready.set()
            print(f"{engine_name} error: {error}", file=sys.stderr, flush=True)
        elif event == "finished":
            print(f"{engine_name} realtime translation finished.", flush=True)


def cloud_stream_loop(
    args: argparse.Namespace,
    state: State,
    stop_event: threading.Event,
    audio_holder: Dict[str, Optional[subprocess.Popen]],
    cloud_holder: Dict[str, Optional[subprocess.Popen]],
    browser_audio: BrowserAudioInput,
) -> None:
    is_qwen_live = args.asr_engine == "qwen3.5-live"
    engine_name = "Qwen3.5 LiveTranslate" if is_qwen_live else "Gummy"
    worker_path = QWEN_LIVE_WORKER if is_qwen_live else GUMMY_WORKER
    if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        state.set_status("missing Alibaba Cloud API key")
        print("Missing DASHSCOPE_API_KEY. Enter the API key in the launcher.", file=sys.stderr, flush=True)
        stop_event.set()
        return
    if is_qwen_live and not args.aliyun_workspace_id.strip():
        state.set_status("missing Alibaba Cloud Workspace ID")
        print("Missing Alibaba Cloud Workspace ID.", file=sys.stderr, flush=True)
        stop_event.set()
        return
    if not worker_path.exists():
        state.set_status(f"{engine_name} worker missing")
        print(f"{engine_name} worker not found: {worker_path}", file=sys.stderr, flush=True)
        stop_event.set()
        return

    command = [
        sys.executable,
        str(worker_path),
        "--source-language",
        gummy_language_code(args.source_language),
        "--target-language",
        gummy_language_code(args.target_language),
    ]
    if is_qwen_live:
        command.extend(
            [
                "--silence-duration-ms",
                str(args.gummy_max_end_silence),
                "--workspace-id",
                args.aliyun_workspace_id,
                "--region",
                args.aliyun_region,
            ]
        )
        if args.visual_input and args.audio_device == BROWSER_DEVICE:
            command.extend(["--visual-file", str(args.work_dir / "visual_frame.jpg")])
    else:
        command.extend(["--max-end-silence", str(args.gummy_max_end_silence)])
    if not is_qwen_live and args.translation_engine == "none":
        command.append("--no-translation")

    state.set_status(f"connecting to Alibaba Cloud {engine_name}")
    cloud_proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=os.environ.copy(),
    )
    cloud_holder["proc"] = cloud_proc
    ready = threading.Event()
    connection_state = {"ok": False}
    threading.Thread(
        target=handle_cloud_output,
        args=(
            cloud_proc,
            state,
            ready,
            connection_state,
            is_qwen_live or args.translation_engine != "none",
            engine_name,
        ),
        daemon=True,
    ).start()
    threading.Thread(
        target=forward_binary_stderr, args=(cloud_proc, engine_name), daemon=True
    ).start()

    if (
        not ready.wait(timeout=25)
        or not connection_state["ok"]
        or cloud_proc.poll() is not None
    ):
        if cloud_proc.poll() is None:
            state.set_status(f"{engine_name} connection timeout")
        stop_event.set()
        return

    restart_count = 0
    try:
        if args.audio_device == BROWSER_DEVICE:
            state.set_status("waiting for Edge tab audio")
            if not cloud_proc.stdin:
                raise RuntimeError(f"{engine_name} stream pipe is unavailable.")
            while not stop_event.is_set() and cloud_proc.poll() is None:
                frame = browser_audio.read()
                if not frame:
                    continue
                cloud_proc.stdin.write(frame)
                cloud_proc.stdin.flush()
            return
        while not stop_event.is_set() and cloud_proc.poll() is None:
            capture_proc = start_pcm_audio_capture(args.audio_device)
            audio_holder["proc"] = capture_proc
            threading.Thread(
                target=forward_binary_stderr, args=(capture_proc, "audio"), daemon=True
            ).start()
            try:
                if not capture_proc.stdout or not cloud_proc.stdin:
                    raise RuntimeError(f"Audio or {engine_name} stream pipe is unavailable.")
                while not stop_event.is_set() and cloud_proc.poll() is None:
                    frame = capture_proc.stdout.read(3200)
                    if not frame:
                        break
                    cloud_proc.stdin.write(frame)
                    cloud_proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                if cloud_proc.poll() is None and not stop_event.is_set():
                    print(
                        f"{engine_name} audio stream interrupted: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            finally:
                stop_process(capture_proc)
                audio_holder["proc"] = None

            if stop_event.is_set() or cloud_proc.poll() is not None:
                break
            code = capture_proc.poll()
            restart_count += 1
            state.set_status("audio device changed; reconnecting")
            print(
                f"Realtime audio capture stopped with code {code}; reconnecting "
                f"(attempt {restart_count}).",
                flush=True,
            )
            time.sleep(min(3.0, 0.5 * restart_count))
    except Exception as exc:
        state.set_status(f"{engine_name} stream error: {exc}")
        print(f"{engine_name} stream error: {exc}", file=sys.stderr, flush=True)
    finally:
        if cloud_proc.stdin:
            try:
                cloud_proc.stdin.close()
            except OSError:
                pass
        try:
            cloud_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            stop_process(cloud_proc)
        cloud_holder["proc"] = None
        if not stop_event.is_set():
            stop_event.set()


def translation_worker(
    args: argparse.Namespace,
    state: State,
    stop_event: threading.Event,
    jobs: "queue.Queue[Optional[Tuple[str, str]]]",
) -> None:
    translator = Translator(
        args.translation_engine,
        args.target_language,
        args.translate_device,
        args.translation_max_new_tokens,
    )
    while not stop_event.is_set():
        try:
            job = jobs.get(timeout=0.2)
        except queue.Empty:
            continue
        if job is None:
            return
        chunk_name, text = job
        try:
            started = time.monotonic()
            translated = translator.translate(text)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            state.update_event(chunk_name, translated)
            if translated and translated != text:
                print(f"MT {chunk_name}: {elapsed_ms} ms -> {translated}", flush=True)
        except Exception as exc:
            state.set_status(f"translation error: {exc}")
            print(f"Failed to translate {chunk_name}: {exc}", file=sys.stderr, flush=True)


def enqueue_translation(jobs: "queue.Queue[Optional[Tuple[str, str]]]", item: Tuple[str, str]) -> None:
    try:
        jobs.put_nowait(item)
        return
    except queue.Full:
        pass
    try:
        jobs.get_nowait()
    except queue.Empty:
        pass
    try:
        jobs.put_nowait(item)
    except queue.Full:
        pass


def is_chinese_source(language: str) -> bool:
    return language in ("zh", "cn", "Chinese", "chinese")


def normalize_runtime_args(args: argparse.Namespace) -> None:
    if args.asr_engine == "streaming" and not is_chinese_source(args.source_language):
        print(
            f"Streaming ASR is Chinese-only; source language is {args.source_language}. "
            "Using SenseVoice multilingual ASR instead.",
            flush=True,
        )
        args.asr_engine = "sensevoice"

def worker_loop(args: argparse.Namespace, state: State, stop_event: threading.Event) -> None:
    effective_asr_engine = args.asr_engine
    asr: Optional[AsrEngine] = None
    try:
        asr = AsrEngine(effective_asr_engine, args.source_language, args.device, args)
    except Exception as exc:
        if effective_asr_engine != "streaming":
            raise
        print(f"{effective_asr_engine} ASR unavailable: {exc}", file=sys.stderr, flush=True)
        print("Falling back to SenseVoice ASR.", flush=True)
        effective_asr_engine = "sensevoice"
        asr = AsrEngine("sensevoice", args.source_language, args.device, args)
    translation_jobs: "queue.Queue[Optional[Tuple[str, str]]]" = queue.Queue(maxsize=args.translation_queue_size)
    translation_thread = threading.Thread(
        target=translation_worker, args=(args, state, stop_event, translation_jobs), daemon=True
    )
    translation_thread.start()
    processed: set = set()
    quiet_chunks = 0
    min_age = 0.15 if effective_asr_engine == "streaming" else 0.45
    min_seconds = (
        max(0.2, args.chunk_seconds * 0.35)
        if effective_asr_engine == "streaming"
        else max(0.25, args.chunk_seconds * 0.35)
    )
    state.set_status("listening")
    while not stop_event.is_set():
        did_work = False
        for chunk in iter_ready_chunks(args.chunks_dir, processed, min_age=min_age, min_seconds=min_seconds):
            processed.add(chunk)
            did_work = True
            try:
                rms, peak = wav_level(chunk)
                print(f"Audio level {chunk.name}: rms={rms:.1f}, peak={peak}", flush=True)
                if peak < args.min_audio_peak or rms < args.min_audio_rms:
                    quiet_chunks += 1
                    if quiet_chunks == 3 or quiet_chunks % 8 == 0:
                        message = (
                            "No audible input detected. The selected recording device is probably not receiving "
                            "system playback audio. Route Windows/Chrome output to VoiceMeeter Input, then select "
                            "VoiceMeeter Output here."
                        )
                        state.set_status("no audible input")
                        print(message, flush=True)
                    continue
                quiet_chunks = 0
                asr_started = time.monotonic()
                text = asr.transcribe(chunk)
                asr_elapsed_ms = int((time.monotonic() - asr_started) * 1000)
                if not text:
                    continue
                state.add_event(text, PENDING_TRANSLATION, chunk.name)
                print(f"ASR {chunk.name}: {asr_elapsed_ms} ms -> {text}", flush=True)
                if args.translation_engine == "none":
                    state.update_event(chunk.name, text)
                else:
                    enqueue_translation(translation_jobs, (chunk.name, text))
            except Exception as exc:
                state.set_status(f"error: {exc}")
                print(f"Failed to process {chunk}: {exc}", file=sys.stderr, flush=True)
        if not did_work:
            time.sleep(0.05 if effective_asr_engine == "streaming" else 0.12)
    if asr is not None:
        asr.close()
    try:
        translation_jobs.put_nowait(None)
    except queue.Full:
        pass


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live desktop audio interpreter with optional screen recording.")
    parser.add_argument("--list-devices", action="store_true", help="List FFmpeg DirectShow devices and exit.")
    parser.add_argument("--audio-device", default="", help="DirectShow audio device name.")
    parser.add_argument("--loopback-capture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--loopback-pcm-stream", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--loopback-chunks-dir", default="", help=argparse.SUPPRESS)
    parser.add_argument("--loopback-chunk-seconds", type=float, default=1.0, help=argparse.SUPPRESS)
    parser.add_argument("--source-language", default="auto", help="FunASR language: auto, zh, en, ja, ko, yue.")
    parser.add_argument(
        "--asr-engine",
        choices=["qwen3.5-live", "gummy", "streaming", "sensevoice"],
        default="qwen3.5-live",
    )
    parser.add_argument("--target-language", default="Chinese", help="Translation target language.")
    parser.add_argument("--translation-engine", choices=["qwen", "none"], default="qwen")
    parser.add_argument("--device", default="auto", help="ASR device: auto, cpu, cuda:0.")
    parser.add_argument("--translate-device", default="auto", help="Translator device: auto, cpu, cuda:0.")
    parser.add_argument("--chunk-seconds", type=float, default=0.6)
    parser.add_argument("--streaming-chunk-pad-left", type=int, default=0)
    parser.add_argument("--streaming-chunk-stride", type=int, default=10)
    parser.add_argument("--streaming-chunk-pad-right", type=int, default=5)
    parser.add_argument("--streaming-encoder-lookback", type=int, default=4)
    parser.add_argument("--streaming-decoder-lookback", type=int, default=1)
    parser.add_argument("--gummy-max-end-silence", type=int, default=500)
    parser.add_argument("--aliyun-workspace-id", default=os.environ.get("DASHSCOPE_WORKSPACE_ID", ""))
    parser.add_argument("--aliyun-region", choices=["beijing", "singapore"], default="beijing")
    parser.add_argument("--visual-input", dest="visual_input", action="store_true", default=False)
    parser.add_argument("--no-visual-input", dest="visual_input", action="store_false")
    parser.add_argument("--translation-queue-size", type=int, default=6)
    parser.add_argument("--translation-max-new-tokens", type=int, default=72)
    parser.add_argument("--min-audio-rms", type=float, default=20.0)
    parser.add_argument("--min-audio-peak", type=int, default=200)
    parser.add_argument("--test-audio", action="store_true", help="Record a short sample and print input level.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--work-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--record-screen", action="store_true", help="Record the desktop video while interpreting.")
    parser.add_argument("--screen-fps", type=int, default=12)
    parser.add_argument("--overlay", dest="overlay", action="store_true", default=True)
    parser.add_argument("--no-overlay", dest="overlay", action="store_false", help="Disable the draggable overlay window.")
    parser.add_argument(
        "--overlay-geometry",
        type=parse_geometry,
        default=parse_geometry("980x280+240+620"),
        help="Overlay size and position, for example 980x280+240+620.",
    )
    parser.add_argument("--overlay-font", default="Microsoft YaHei UI")
    parser.add_argument("--overlay-font-size", type=int, default=15)
    parser.add_argument("--overlay-hold-seconds", type=float, default=8.0)
    parser.add_argument("--overlay-batch-min-chars", type=int, default=80)
    parser.add_argument("--overlay-batch-max-chars", type=int, default=170)
    parser.add_argument("--overlay-batch-max-events", type=int, default=4)
    parser.add_argument("--overlay-live-mode", dest="overlay_live_mode", action="store_true", default=True)
    parser.add_argument("--overlay-batch-mode", dest="overlay_live_mode", action="store_false")
    parser.add_argument("--overlay-live-events", type=int, default=3)
    parser.add_argument("--overlay-live-max-events", type=int, default=6)
    parser.add_argument("--overlay-live-max-chars", type=int, default=360)
    parser.add_argument("--overlay-color", default="#ffffff")
    parser.add_argument("--overlay-alpha", type=float, default=0.94)
    parser.add_argument(
        "--overlay-show-source",
        action="store_true",
        default=False,
        help="Show recognized source text above the translation in the overlay.",
    )
    parser.add_argument("--overlay-hide-source", dest="overlay_show_source", action="store_false")
    args = parser.parse_args(argv)
    args.work_dir = Path(args.work_dir)
    args.chunks_dir = args.work_dir / "chunks"
    args.recordings_dir = args.work_dir / "recordings"
    return args


def main(argv: Optional[List[str]] = None) -> int:
    configure_environment()
    args = parse_args(argv)
    if args.loopback_capture:
        if not args.loopback_chunks_dir:
            print("Missing --loopback-chunks-dir", file=sys.stderr)
            return 2
        try:
            record_wasapi_loopback(Path(args.loopback_chunks_dir), args.loopback_chunk_seconds)
        except WasapiDeviceInvalidated as exc:
            print(f"WASAPI loopback device invalidated: {exc}", flush=True)
            return WASAPI_RESTART_EXIT_CODE
        return 0
    if args.loopback_pcm_stream:
        try:
            record_wasapi_loopback(None, 0.0, pcm_output=sys.stdout.buffer)
        except WasapiDeviceInvalidated as exc:
            print(f"WASAPI loopback device invalidated: {exc}", file=sys.stderr, flush=True)
            return WASAPI_RESTART_EXIT_CODE
        except BrokenPipeError:
            return 0
        return 0
    if args.list_devices:
        return list_devices()
    if not args.audio_device:
        print("Missing --audio-device. Run --list-devices first.", file=sys.stderr)
        return 2
    if args.test_audio:
        rms, peak, path = test_audio_device(args.audio_device, args.work_dir)
        print(f"Audio test: rms={rms:.1f}, peak={peak}, file={path}", flush=True)
        if peak < args.min_audio_peak or rms < args.min_audio_rms:
            print(
                "No audible system playback was detected on this device. "
                "If a browser video is playing, route Windows output to VoiceMeeter Input "
                "and select VoiceMeeter Output in this app.",
                flush=True,
            )
            return 3
        print("Audio input looks active.", flush=True)
        return 0

    normalize_runtime_args(args)
    args.chunks_dir.mkdir(parents=True, exist_ok=True)
    args.recordings_dir.mkdir(parents=True, exist_ok=True)
    for old_chunk in args.chunks_dir.glob("chunk_*.wav"):
        try:
            old_chunk.unlink()
        except OSError:
            pass

    state = State()
    browser_audio = BrowserAudioInput()
    browser_visual = BrowserVisualInput(
        args.work_dir / "visual_frame.jpg",
        args.visual_input and args.asr_engine == "qwen3.5-live" and args.audio_device == BROWSER_DEVICE,
    )
    stop_event = threading.Event()
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        make_handler(state, browser_audio, stop_event, browser_visual),
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"Subtitle page: http://127.0.0.1:{args.port}", flush=True)

    audio_proc: Optional[subprocess.Popen] = None
    audio_holder: Dict[str, Optional[subprocess.Popen]] = {"proc": None}
    cloud_holder: Dict[str, Optional[subprocess.Popen]] = {"proc": None}
    screen_proc: Optional[subprocess.Popen] = None
    cloud_engines = {"gummy", "qwen3.5-live"}
    if args.asr_engine in cloud_engines:
        worker = threading.Thread(
            target=cloud_stream_loop,
            args=(args, state, stop_event, audio_holder, cloud_holder, browser_audio),
            daemon=True,
        )
    else:
        worker = threading.Thread(target=worker_loop, args=(args, state, stop_event), daemon=True)
    capture_monitor: Optional[threading.Thread] = None

    def request_stop(signum: int, frame: object) -> None:
        stop_event.set()
        state.set_status("stopping")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)

    try:
        if args.record_screen:
            screen_proc = start_screen_recording(args.recordings_dir, args.screen_fps)
        if args.audio_device == BROWSER_DEVICE and args.asr_engine not in cloud_engines:
            raise RuntimeError("Edge tab audio requires a cloud realtime translation engine.")
        if args.asr_engine not in cloud_engines:
            audio_proc = start_audio_capture(args.audio_device, args.chunks_dir, args.chunk_seconds)
            audio_holder["proc"] = audio_proc
            capture_monitor = threading.Thread(
                target=monitor_capture_process, args=(audio_holder, args, state, stop_event), daemon=True
            )
            capture_monitor.start()
            threading.Thread(target=forward_capture_output, args=(audio_proc,), daemon=True).start()
        worker.start()
        if args.overlay:
            print("Overlay window: drag with left mouse button, right-click to exit.", flush=True)
            run_overlay_window(args, state, stop_event)
        else:
            while not stop_event.is_set():
                current_audio_proc = audio_holder.get("proc")
                if current_audio_proc is not None and current_audio_proc.poll() is not None:
                    raise RuntimeError(f"Audio capture stopped with exit code {current_audio_proc.returncode}")
                current_cloud_proc = cloud_holder.get("proc")
                if args.asr_engine in cloud_engines and current_cloud_proc is not None:
                    if current_cloud_proc.poll() is not None:
                        raise RuntimeError(
                            f"Cloud worker stopped with exit code {current_cloud_proc.returncode}"
                        )
                time.sleep(0.5)
    except KeyboardInterrupt:
        stop_event.set()
    except Exception as exc:
        state.set_status(f"error: {exc}")
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        stop_event.set()
        stop_process(audio_holder.get("proc") or audio_proc)
        stop_process(cloud_holder.get("proc"))
        stop_process(screen_proc)
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
