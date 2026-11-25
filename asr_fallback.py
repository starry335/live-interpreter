"""On-demand public Paraformer backup for the live ASR workers.

The main ASR (Qwen 3.1, or a workspace Paraformer) sometimes stops sending
results while its socket stays open.  The watchdog detects that case; this
module starts a **public-endpoint** Paraformer worker only at that moment and
replays the audio that never reached the screen:

* a PCM ring buffer holds the last ``LIVE_INTERPRETER_FALLBACK_RING_SECONDS``
  (30 s by default) of stream audio;
* when the watchdog fires, the buffered audio after the last displayed
  subtitle is written to a fresh Paraformer worker, then live audio follows;
* backup results carry a ``pf``-prefixed sentence id and ``fallback: true`` so
  they can never overwrite the main engine's subtitles;
* the main worker keeps probing its own engine; when it produces results
  again, the backup process is stopped and the main engine takes the stream
  back (see ``GummyConnection._recovery_loop``).

The backup is never started before a failure, so it costs nothing while the
main ASR works.  If the main engine is a public Paraformer there is no backup
(the backend disables the flag).
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


def build_paraformer_command(args, work_dir: Optional[Path]) -> List[str]:
    """Public-endpoint Paraformer worker with the same languages and glossary."""
    arguments = [
        "--source-language", str(getattr(args, "source_language", "ja")),
        "--target-language", str(getattr(args, "target_language", "Chinese")),
        "--mt-model", str(getattr(args, "mt_model", "qwen3.8-max")),
        "--max-end-silence", str(int(getattr(args, "max_end_silence", 500))),
        "--mt-mode", "reference",
        # The backup must stay on the public endpoint even though the parent
        # process inherits DASHSCOPE_WORKSPACE_ID from the launcher.
        "--workspace-id", "",
        "--no-asr-backup",
    ]
    if work_dir:
        arguments.extend(["--log-file", str(Path(work_dir) / "paraformer_fallback.jsonl")])
    hotwords = str(getattr(args, "hotwords", "") or "").strip()
    if hotwords:
        arguments.extend(["--hotwords", hotwords])
    if not bool(getattr(args, "translation_enabled", True)):
        arguments.append("--no-translation")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--paraformer-mt-worker", *arguments]
    script = Path(__file__).resolve().with_name("paraformer_mt_worker.py")
    return [sys.executable, str(script), *arguments]


class ParaformerBackup:
    """A public Paraformer worker started on demand."""

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
            fallback_dir = self.work_dir / "fallback"
            env["LIVE_INTERPRETER_WORK_DIR"] = str(fallback_dir)
            env["LIVE_INTERPRETER_CRASH_LOG"] = str(fallback_dir / "crash.log")
            # The backup has no independent fallback and must not exit just
            # because a music segment produced no text.
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
                target=self._pump_stdout, args=(self.proc,), name="paraformer-backup", daemon=True)
            self._stdout_thread.start()
            self._stderr_thread = threading.Thread(
                target=self._pump_stderr, args=(self.proc,), name="paraformer-backup-err", daemon=True)
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
        """Start the public worker, replay missing audio, forward live frames."""
        if not self.start() or not self.wait_ready():
            self.stop()
            return False
        with self._write_lock:
            self.active = True
            payload = ring.read_from(offset)
            wrote = self._write(payload) if payload else True
            if not wrote:
                self.active = False
        if not wrote:
            self.stop()
            return False
        emit_payload = {
            "event": "asr_fallback",
            "engine": "Paraformer (public)",
            "replayed_ms": round(len(payload) / BYTES_PER_MS),
            "ring_from_ms": round(offset / BYTES_PER_MS),
        }
        self.emit(emit_payload)
        return True

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
                       "message": f"Paraformer backup pipe failed: {self._error}"})
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
                # The backup is best-effort; only the controller's process
                # monitor decides when the main worker must be recycled.
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
                    print(f"[backup] {text}", file=sys.stderr, flush=True)
                except (BrokenPipeError, OSError):
                    return


class FallbackController:
    """Start/park the public Paraformer backup and watch its process."""

    def __init__(self, args, emit: Callable[[Dict[str, object]], None],
                 work_dir: Optional[Path] = None,
                 command: Optional[List[str]] = None,
                 owner: object = None) -> None:
        self.args = args
        self.emit = emit
        self.owner = owner
        self.ring = PcmRing(float(os.environ.get("LIVE_INTERPRETER_FALLBACK_RING_SECONDS",
                                                  str(RING_SECONDS)) or RING_SECONDS))
        self.backup = ParaformerBackup(args, emit, work_dir=work_dir, command=command)
        self.active = False
        self.started_at = 0.0
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._monitor: Optional[threading.Thread] = None

    def feed(self, data: bytes) -> None:
        self.ring.append(data)
        if self.active:
            self.backup.send(data)

    def activate(self, last_result_end_ms: float) -> bool:
        with self._lock:
            if self.active:
                return True
            offset = self.ring.offset_for_ms(last_result_end_ms)
            if not self.backup.activate(self.ring, offset):
                self.emit({"event": "warning",
                           "message": "Paraformer backup did not start; restarting the main ASR."})
                return False
            self.active = True
            self.started_at = time.monotonic()
            self._monitor = threading.Thread(target=self._watch, name="backup-monitor", daemon=True)
            self._monitor.start()
        return True

    def deactivate(self) -> None:
        with self._lock:
            self.active = False
        self.backup.stop()

    def _watch(self) -> None:
        while not self._closed.wait(2.0):
            if not self.active:
                return
            if not self.backup.alive():
                self.emit({"event": "warning", "fallback": True,
                           "message": "Paraformer backup exited; restarting the main ASR."})
                owner = self.owner
                if owner is not None and hasattr(owner, "backup_lost"):
                    owner.backup_lost()
                return

    def close(self) -> None:
        self._closed.set()
        self.backup.stop()
