"""Warm Paraformer standby for the Qwen 3.1 streaming worker.

The 3.1 endpoint occasionally stops sending results while the audio socket
stays open.  The watchdog in :mod:`gummy_realtime_worker` detects that case,
but reconnecting still leaves a long gap in the subtitles.

This module keeps a second worker process connected and idle:

* a PCM ring buffer holds the last ``LIVE_INTERPRETER_FALLBACK_RING_SECONDS``
  (30 s by default) of stream audio;
* the Paraformer worker is started in the background at session start.  It
  opens its WebSocket and registers the hotword vocabulary, but no audio is
  sent, so it is not billed;
* when the watchdog fires, the buffered audio that the 3.1 engine never
  transcribed is replayed into the standby and live audio follows.  The
  standby's results are merged into this worker's JSONL stream with a
  ``pf``-prefixed sentence id so they cannot overwrite 3.1 subtitles;
* after ``LIVE_INTERPRETER_FALLBACK_RECYCLE`` seconds (5 min by default) the
  worker exits cleanly and the backend restarts a fresh 3.1 session, which
  switches the user back to the better engine.

If the standby dies (for example an idle WebSocket timeout) it is restarted
on the next activation; a failed activation simply leaves the normal
watchdog exit path in place.
"""
from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable, Deque, Dict, List, Optional


RING_SECONDS = 30.0
BYTES_PER_MS = 32  # 16 kHz, mono, PCM16
FRAME_BYTES = 3200


class PcmRing:
    """Bounded PCM history that can be replayed from any absolute offset."""

    def __init__(self, seconds: float = RING_SECONDS) -> None:
        self.capacity = max(FRAME_BYTES, int(float(seconds) * 1000 * BYTES_PER_MS))
        self._chunks: Deque[bytes] = deque()
        self._chunk_start: Deque[int] = deque()
        self._total = 0
        self._size = 0
        self._lock = threading.Lock()

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    @property
    def oldest(self) -> int:
        with self._lock:
            return self._total - self._size

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._chunks.append(bytes(data))
            self._chunk_start.append(self._total)
            self._total += len(data)
            self._size += len(data)
            while self._size > self.capacity and len(self._chunks) > 1:
                removed = self._chunks.popleft()
                self._chunk_start.popleft()
                self._size -= len(removed)

    def offset_for_ms(self, milliseconds: float) -> int:
        """Absolute byte offset for a server timestamp, clamped to the buffer."""
        wanted = int(max(0.0, float(milliseconds)) * BYTES_PER_MS)
        return max(self.oldest, min(self._total, wanted))

    def read_from(self, offset: int) -> bytes:
        """Return all buffered audio from ``offset`` to the newest byte."""
        with self._lock:
            start = max(offset, self._total - self._size)
            if start >= self._total:
                return b""
            pieces: List[bytes] = []
            for chunk, chunk_start in zip(self._chunks, self._chunk_start):
                chunk_end = chunk_start + len(chunk)
                if chunk_end <= start:
                    continue
                pieces.append(chunk[max(0, start - chunk_start):])
            return b"".join(pieces)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._chunk_start.clear()
            self._size = 0


def build_paraformer_command(args, work_dir: Path) -> List[str]:
    """Command line for the standby process, using the same settings."""
    arguments = [
        "--source-language", str(getattr(args, "source_language", "ja")),
        "--target-language", str(getattr(args, "target_language", "Chinese")),
        "--mt-model", str(getattr(args, "mt_model", "qwen3.8-max")),
        "--max-end-silence", str(int(getattr(args, "max_end_silence", 500))),
        "--mt-mode", "reference",
        "--log-file", str(work_dir / "paraformer_fallback.jsonl"),
    ]
    workspace = str(getattr(args, "workspace_id", "") or "").strip()
    if workspace:
        arguments.extend(["--workspace-id", workspace])
    hotwords = str(getattr(args, "hotwords", "") or "").strip()
    if hotwords:
        arguments.extend(["--hotwords", hotwords])
    if not bool(getattr(args, "translation_enabled", True)):
        arguments.append("--no-translation")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--paraformer-mt-worker", *arguments]
    script = Path(__file__).resolve().with_name("paraformer_mt_worker.py")
    return [sys.executable, str(script), *arguments]


class ParaformerStandby:
    """A connected, idle Paraformer worker that can take over on demand."""

    def __init__(
        self,
        args,
        emit: Callable[[Dict[str, object]], None],
        work_dir: Optional[Path] = None,
        command: Optional[List[str]] = None,
        tag: str = "pf",
    ) -> None:
        self.args = args
        self.emit = emit
        self.work_dir = Path(work_dir or os.environ.get("LIVE_INTERPRETER_WORK_DIR", "")
                             or Path.cwd())
        self.command = command
        self.tag = f"{tag}{int(time.time() * 1000) % 100000}"
        self.proc: Optional[subprocess.Popen] = None
        self.ready = threading.Event()
        self.active = False
        self.last_idle_error = ""
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._error = ""

    # -- process lifecycle ---------------------------------------------------
    def start(self) -> bool:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return True
            if self.proc is not None:
                self.stop()
            command = self.command or build_paraformer_command(self.args, self.work_dir)
            env = os.environ.copy()
            # Keep the standby's crash/daily logs in their own folder so two
            # processes never append to the same JSONL file.
            fallback_dir = self.work_dir / "fallback"
            env["LIVE_INTERPRETER_WORK_DIR"] = str(fallback_dir)
            env["LIVE_INTERPRETER_CRASH_LOG"] = str(fallback_dir / "crash.log")
            # The standby is idle until it is needed; the normal watchdog would
            # treat that silence as a stall.  The parent recycles the worker.
            env["LIVE_INTERPRETER_ASR_WATCHDOG"] = "0"
            env.setdefault("PYTHONIOENCODING", "utf-8")
            try:
                self.proc = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    env=env,
                )
            except OSError as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                self.proc = None
                return False
            self.ready.clear()
            self.active = False
            self._stdout_thread = threading.Thread(
                target=self._pump_stdout, args=(self.proc,), name="paraformer-standby", daemon=True)
            self._stdout_thread.start()
            self._stderr_thread = threading.Thread(
                target=self._pump_stderr, args=(self.proc,), name="paraformer-standby-err", daemon=True)
            self._stderr_thread.start()
            return True

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self, timeout: float = 20.0) -> bool:
        return self.ready.wait(timeout) and self.alive()

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            proc = self.proc
            self.proc = None
            self.active = False
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=1.0)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    # -- audio ---------------------------------------------------------------
    def activate(self, ring: PcmRing, offset: int) -> bool:
        """Replay missing audio and start forwarding live frames."""
        for attempt in range(2):
            if not self.start() or not self.wait_ready():
                continue
            with self._write_lock:
                self.active = True
                payload = ring.read_from(offset)
                wrote = self._write(payload) if payload else True
                if not wrote:
                    self.active = False
            if not wrote:
                # The idle task expired between ready and the first frame;
                # start a fresh standby and replay once more.
                self.stop()
                continue
            if payload:
                self.emit({
                    "event": "asr_fallback",
                    "engine": "Paraformer",
                    "replayed_ms": round(len(payload) / BYTES_PER_MS),
                    "ring_from_ms": round(offset / BYTES_PER_MS),
                })
            return True
        return False

    def keepalive(self, data: bytes = b"\x00\x00" * 1600) -> None:
        """Send a little silence so DashScope does not expire the idle task."""
        if self.active:
            return
        with self._write_lock:
            if not self.active:
                self._write(data)

    def send(self, data: bytes) -> None:
        if not self.active:
            return
        with self._write_lock:
            self._write(data)

    def _write(self, data: bytes) -> bool:
        proc = self.proc
        if proc is None or proc.poll() is not None or not proc.stdin:
            return False
        try:
            for start in range(0, len(data), FRAME_BYTES):
                proc.stdin.write(data[start:start + FRAME_BYTES])
            proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError) as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self.active = False
            self.emit({"event": "warning", "fallback": True,
                       "message": f"Paraformer standby pipe failed: {self._error}"})
            return False

    # -- stdout/stderr -------------------------------------------------------
    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            event = str(message.get("event") or "")
            if event == "ready":
                self.ready.set()
                continue
            if event in {"heartbeat", "finished", "session_config", "asr_config",
                         "hotword_vocabulary", "translation_queued"}:
                continue
            if event in {"error", "fatal_error"}:
                # The standby is best-effort.  An idle task is closed after
                # ~60 s without audio (NO_INPUT_AUDIO_ERROR) and a broken
                # standby must never look like a main-session error: the
                # controller recycles the worker only after the process dies.
                self.last_idle_error = str(message.get("message") or message.get("code") or "")
                continue
            if event == "result" and not self.active:
                continue
            if "sentence_id" in message:
                message["sentence_id"] = f"{self.tag}-{message['sentence_id']}"
            message["fallback"] = True
            self.emit(message)

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                try:
                    print(f"[fallback] {text}", file=sys.stderr, flush=True)
                except (BrokenPipeError, OSError):
                    return


class FallbackController:
    """Glue between the Qwen connection, the ring and the standby process."""

    def __init__(self, args, emit: Callable[[Dict[str, object]], None],
                 work_dir: Optional[Path] = None,
                 command: Optional[List[str]] = None) -> None:
        self.args = args
        self.emit = emit
        self.ring = PcmRing(float(os.environ.get("LIVE_INTERPRETER_FALLBACK_RING_SECONDS",
                                                  str(RING_SECONDS)) or RING_SECONDS))
        self.standby = ParaformerStandby(args, emit, work_dir=work_dir, command=command)
        self.active = False
        self.started_at = 0.0
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def warm_up(self) -> None:
        """Keep the standby connected without blocking the ASR session."""
        self._warned_unavailable = False
        threading.Thread(target=self._keep_warm, name="fallback-warmup", daemon=True).start()

    def _keep_warm(self) -> None:
        """Keep the idle task alive and reconnect it if the server drops it."""
        last_keepalive = 0.0
        while not self._closed.wait(1.0):
            if self.active:
                last_keepalive = 0.0
                continue
            if not self.standby.alive():
                try:
                    started = self.standby.start()
                except Exception as exc:  # standby is optional
                    started = False
                    if not self._warned_unavailable:
                        self.emit({"event": "warning",
                                   "message": f"Paraformer standby unavailable: "
                                              f"{type(exc).__name__}: {exc}"})
                        self._warned_unavailable = True
                last_keepalive = 0.0
                if not started:
                    self._closed.wait(10.0)
                continue
            if self.standby.ready.is_set() and (
                    not last_keepalive or time.monotonic() - last_keepalive >= 40.0):
                self.standby.keepalive()
                last_keepalive = time.monotonic()

    def feed(self, data: bytes) -> None:
        self.ring.append(data)
        if self.active:
            self.standby.send(data)

    def activate(self, last_result_end_ms: float) -> bool:
        with self._lock:
            if self.active:
                return True
            offset = self.ring.offset_for_ms(last_result_end_ms)
            if not self.standby.activate(self.ring, offset):
                self.emit({"event": "warning",
                           "message": "Paraformer standby did not become ready; reconnecting 3.1 only."})
                return False
            self.active = True
            self.started_at = time.monotonic()
        threading.Thread(target=self._recycle_when_due, name="fallback-recycle", daemon=True).start()
        return True

    def _recycle_when_due(self) -> None:
        limit = float(os.environ.get("LIVE_INTERPRETER_FALLBACK_RECYCLE", "300") or 0)
        while not self._closed.wait(2.0):
            if not self.standby.alive():
                self.emit({"event": "warning", "fallback": True,
                           "message": "Paraformer standby exited; recycling the 3.1 worker."})
                self._recycle(1)
                return
            if limit > 0 and time.monotonic() - self.started_at >= limit:
                self.emit({"event": "asr_fallback_done", "engine": "Paraformer",
                           "after_s": round(time.monotonic() - self.started_at)})
                self._recycle(0)
                return

    def _recycle(self, code: int) -> None:
        """Exit so the backend starts a fresh 3.1 session (and a new standby)."""
        if self._closed.is_set():
            return
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(code)

    def close(self) -> None:
        self._closed.set()
        self.standby.stop()
