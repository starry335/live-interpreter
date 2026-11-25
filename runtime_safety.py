"""Crash containment and safe output for the live interpreter processes.

Windows / PyInstaller turns an unhandled exception into exit code -1, which
subprocess reports as 4294967295.  The most common cause in this project is a
worker writing to a stdout pipe after the parent closed it (BrokenPipeError in
a ``finally`` block).  This module:

* wraps stdout/stderr so pipe errors never escape;
* writes unhandled exceptions (main or worker thread) to ``runtime/crash.log``;
* enables ``faulthandler`` so native crashes also leave a stack;
* provides a single place to translate exit codes for the UI.
"""
from __future__ import annotations

import datetime as _dt
import faulthandler
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import IO, Callable, Optional

_CRASH_HANDLE: Optional[IO[str]] = None
_INSTALLED = False
_COMPONENT = "live-interpreter"
_EMIT_LOCK = threading.Lock()
_DAILY_LOCK = threading.Lock()
_LOG_DIR: Optional[Path] = None


class SafeStream:
    """A text stream that swallows broken-pipe / closed-handle errors."""

    def __init__(self, stream: object, name: str = "stdout") -> None:
        self._stream = stream
        self._name = name
        self.broken = False

    def write(self, text: str) -> int:
        if self.broken:
            return len(text)
        try:
            return self._stream.write(text)  # type: ignore[attr-defined]
        except (BrokenPipeError, OSError):
            self.broken = True
            return len(text)

    def flush(self) -> None:
        if self.broken:
            return
        try:
            self._stream.flush()  # type: ignore[attr-defined]
        except (BrokenPipeError, OSError):
            self.broken = True

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())  # type: ignore[attr-defined]
        except Exception:
            return False

    def fileno(self) -> int:
        return self._stream.fileno()  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def log_dir(work_dir: str | Path | None = None) -> Path:
    """Daily, append-only log directory (never rotated over by the 2 MB dump)."""
    global _LOG_DIR
    if _LOG_DIR is not None:
        return _LOG_DIR
    base = Path(
        os.environ.get("LIVE_INTERPRETER_WORK_DIR", "")
        or work_dir
        or (Path(__file__).resolve().parent / "runtime")
    )
    _LOG_DIR = base / "logs" / _dt.date.today().isoformat()
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return _LOG_DIR


def append_daily(kind: str, text: str, work_dir: str | Path | None = None) -> None:
    """Append one line to today's ``<kind>_YYYYMMDD.log``. Read-only failure-safe."""
    try:
        directory = log_dir(work_dir)
        path = directory / f"{kind}_{_dt.date.today().strftime('%Y%m%d')}.log"
        with _DAILY_LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text.rstrip() + "\n")
    except Exception:
        pass


def write_daily_event(payload: dict) -> None:
    """Append the full event stream to today's events_*.jsonl."""
    try:
        directory = log_dir()
        path = directory / f"events_{_dt.date.today().strftime('%Y%m%d')}.jsonl"
        with _DAILY_LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        pass


def log_disconnect(payload: dict) -> None:
    append_daily("disconnect", json.dumps(
        {"at": _dt.datetime.now().isoformat(timespec="milliseconds"), **payload},
        ensure_ascii=False,
    ))


def log_mt_timeout(payload: dict) -> None:
    append_daily("mt_timeout", json.dumps(
        {"at": _dt.datetime.now().isoformat(timespec="milliseconds"), **payload},
        ensure_ascii=False,
    ))


def crash_log_path(work_dir: str | Path | None = None) -> Path:
    explicit = os.environ.get("LIVE_INTERPRETER_CRASH_LOG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = Path(
        os.environ.get("LIVE_INTERPRETER_WORK_DIR", "")
        or work_dir
        or (Path(__file__).resolve().parent / "runtime")
    )
    return base / "crash.log"


def _write_line(text: str) -> None:
    if _CRASH_HANDLE is None:
        return
    try:
        _CRASH_HANDLE.write(text.rstrip() + "\n")
        _CRASH_HANDLE.flush()
    except Exception:
        pass


def log_exception(component: str = "", exc_info=None) -> None:
    kind, error, tb = exc_info or sys.exc_info()
    if kind is None:
        return
    text = (
        f"\n===== {_dt.datetime.now().isoformat(timespec='seconds')} "
        f"{component or _COMPONENT} pid={os.getpid()} =====\n"
        "argv=" + " ".join(sys.argv) + "\n"
        f"thread={threading.current_thread().name} "
        f"daemon={threading.current_thread().daemon}\n"
        + "".join(traceback.format_exception(kind, error, tb))
    )
    _write_line(text)
    append_daily("crash", text)


def write_json(payload: dict) -> None:
    """Write one JSONL event atomically across worker threads.

    The ASR, MT and draft threads all emit; without a shared lock two JSON
    objects can land on the same line and the parent parses neither.
    """
    line = json.dumps(payload, ensure_ascii=True)
    with _EMIT_LOCK:
        try:
            print(line, flush=True)
        except (BrokenPipeError, OSError):
            pass
    write_daily_event(payload)


def _excepthook(kind, error, tb) -> None:
    log_exception(_COMPONENT, (kind, error, tb))


def _thread_excepthook(args) -> None:
    log_exception(f"{_COMPONENT}:{args.thread.name if args.thread else 'thread'}",
                  (args.exc_type, args.exc_value, args.exc_traceback))


def install(component: str = "", work_dir: str | Path | None = None) -> Path:
    """Install safe streams, faulthandler and exception hooks (idempotent)."""
    global _CRASH_HANDLE, _INSTALLED, _COMPONENT, _LOG_DIR
    _COMPONENT = component or _COMPONENT
    path = crash_log_path(work_dir)
    _LOG_DIR = log_dir(work_dir)
    if not _INSTALLED:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _CRASH_HANDLE = path.open("a", encoding="utf-8", buffering=1)
            faulthandler.enable(file=_CRASH_HANDLE, all_threads=True)
        except Exception:
            _CRASH_HANDLE = None
        sys.stdout = SafeStream(sys.stdout, "stdout")  # type: ignore[assignment]
        sys.stderr = SafeStream(sys.stderr, "stderr")  # type: ignore[assignment]
        sys.excepthook = _excepthook
        threading.excepthook = _thread_excepthook
        _INSTALLED = True
        started = (
            f"{_dt.datetime.now().isoformat(timespec='seconds')} "
            f"{_COMPONENT} pid={os.getpid()} started argv={' '.join(sys.argv)}"
        )
        _write_line(started)
        append_daily("session", started)
    return path


def guard(component: str, func, *args, **kwargs) -> int:
    """Run ``func`` and turn any uncaught BaseException into a logged code 2."""
    install(component)
    try:
        result = func(*args, **kwargs)
        return int(result or 0)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        log_exception(component)
        try:
            print('{"event": "fatal_error", "message": "unhandled exception; see runtime/crash.log"}',
                  flush=True)
        except Exception:
            pass
        return 2


def describe_exit_code(code: Optional[int]) -> str:
    if code is None:
        return "still running"
    if code in {-1, 0xFFFFFFFF, 4294967295}:
        return "未捕获异常（不是 API 错误；查看 runtime/crash.log）"
    if code == 2:
        return "配置错误或未捕获异常"
    if code == 0:
        return "正常退出"
    return f"退出码 {code}"


class Heartbeat(threading.Thread):
    """Emit a heartbeat event every N seconds so the parent can detect a hang."""

    def __init__(self, emit: Callable[[dict], None], interval: float = 5.0) -> None:
        super().__init__(name="heartbeat", daemon=True)
        self.emit = emit
        self.interval = max(1.0, float(interval))
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.emit({"event": "heartbeat", "at": round(time.time(), 3)})
            except Exception:
                return

    def stop(self) -> None:
        self.stop_event.set()


class PartialTranslation(Exception):
    """The stream produced some Chinese before the connection failed."""

    def __init__(self, text: str) -> None:
        super().__init__("partial translation")
        self.text = text
