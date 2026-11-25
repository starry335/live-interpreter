from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import threading
import time
import uuid
from typing import Dict, List, Optional

try:
    import audioop  # type: ignore[import-not-found]
except ImportError:  # Python 3.13 without audioop-lts
    audioop = None

import hotwords
import runtime_safety


GUMMY_WEBSOCKET_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def emit(payload: Dict[str, object]) -> None:
    runtime_safety.write_json(payload)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alibaba Cloud Gummy realtime translation worker.")
    parser.add_argument("--source-language", default="auto")
    parser.add_argument("--target-language", default="zh")
    parser.add_argument("--max-end-silence", type=int, default=500)
    parser.add_argument("--transcription-enabled", action="store_true", default=True)
    parser.add_argument("--translation-enabled", action="store_true", default=True)
    parser.add_argument("--no-translation", dest="translation_enabled", action="store_false")
    parser.add_argument("--hotwords", "--hotwords-file", dest="hotwords", default="", help="Hotword glossary file (.txt or JSON).")
    return parser.parse_args(argv)


class GummyConnection:
    websocket_url = GUMMY_WEBSOCKET_URL
    engine_name = "Gummy"

    def __init__(self, args: argparse.Namespace, api_key: str) -> None:
        try:
            import certifi
            import websocket
        except ImportError as exc:
            raise RuntimeError(
                "websocket-client or certifi is not installed in the FunASR Python environment."
            ) from exc

        self.certifi = certifi
        self.websocket = websocket
        self.args = args
        self.api_key = api_key
        self.task_id = uuid.uuid4().hex
        self.started = threading.Event()
        self.finished = threading.Event()
        self.failed = threading.Event()
        self.ws = None
        self.receiver: Optional[threading.Thread] = None
        self.pinger: Optional[threading.Thread] = None
        self.error = ""
        self.heartbeat: Optional[runtime_safety.Heartbeat] = None
        self.close_code: Optional[int] = None
        self.close_reason = ""
        self.hotword_config = getattr(args, "hotword_config", None)
        self.last_result_monotonic = time.monotonic()
        self.last_audio_monotonic = 0.0
        self.last_speech_monotonic = 0.0
        self.watchdog_thread: Optional[threading.Thread] = None
        self.watchdog_fired = False
        self.last_result_end_ms = 0.0
        # Absolute PCM bytes since the worker started.  Server timestamps
        # restart at 0 for every reconnected stream, so the backup replay
        # offset has to be anchored to this counter instead.
        self.audio_total_bytes = 0
        self.stream_start_ms = 0.0
        # Set when the stream is handed to the backup engine; a deliberately
        # closed socket must not be reported as a session error.
        self.handed_over = False
        self.handing_over = False
        self.probation = False
        self.probe_aborted = threading.Event()
        self.stream_epoch = 0
        self.generation = 0
        self._reconnect_lock = threading.Lock()
        self._connect_lock = threading.RLock()
        self.fallback = None
        if bool(getattr(args, "asr_backup", False)):
            try:
                import asr_fallback
                self.fallback = asr_fallback.FallbackController(
                    args, emit, work_dir=getattr(args, "work_dir", None), owner=self)
            except Exception as exc:  # backup is optional
                emit({"event": "warning",
                      "message": f"ASR backup disabled: {type(exc).__name__}: {exc}"})

    # -- connection lifecycle -------------------------------------------------
    def _new_socket(self):
        ws = self.websocket.create_connection(
            self.websocket_url,
            header=[
                f"Authorization: Bearer {self.api_key}",
                "User-Agent: LiveInterpreter/1.0",
            ],
            timeout=10,
            enable_multithread=True,
            sockopt=[(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)],
            sslopt={
                "ca_certs": self.certifi.where(),
                "cert_reqs": ssl.CERT_REQUIRED,
            },
        )
        ws.settimeout(10.0)
        try:
            if os.name == "nt" and ws.sock is not None:
                # 30 s idle / 5 s probe: catches dead NAT entries before the
                # gateway resets a long-lived WebSocket.
                ws.sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30000, 5000))
        except (OSError, AttributeError):
            pass
        return ws

    def _connect(self, first: bool) -> None:
        with self._connect_lock:
            self.generation += 1
            generation = self.generation
            old = self.ws
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            self.started.clear()
            self.failed.clear()
            self.finished.clear()
            self.error = ""
            self.close_code = None
            self.close_reason = ""
            self.task_id = uuid.uuid4().hex
            self.stream_start_ms = self.audio_total_bytes * 1000.0 / 32000.0
            self.ws = self._new_socket()
            ws = self.ws
            self.receiver = threading.Thread(
                target=self._receive_loop, args=(generation, ws), daemon=True)
            self.receiver.start()
            ws.send(json.dumps(self.task_request(), ensure_ascii=False))
            if not self.started.wait(timeout=20) or self.failed.is_set():
                raise RuntimeError(
                    self.error or f"Timed out waiting for {self.engine_name} task-started.")
            self.handed_over = False
            self.handing_over = False
            self.watchdog_fired = False
            self.probation = False
            self.probe_aborted.clear()
            self.last_result_monotonic = time.monotonic()
            self.last_audio_monotonic = 0.0
            self.last_speech_monotonic = 0.0
            if first:
                emit({"event": "ready", "task_id": self.task_id})
            else:
                self.stream_epoch += 1
                emit({"event": "asr_reconnected", "task_id": self.task_id,
                      "epoch": self.stream_epoch, "engine": self.engine_name})
                self._start_watchdog()
                self._start_pinger()

    def reconnect(self) -> bool:
        """Re-open the main engine after a stall (used by the recovery probe)."""
        if not self._reconnect_lock.acquire(blocking=False):
            return False
        try:
            try:
                self._connect(first=False)
                return True
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                try:
                    if self.ws is not None:
                        self.ws.close()
                except Exception:
                    pass
                # A failed probe must not end the worker: the backup is still
                # serving subtitles and the next probe will try again.
                self.failed.clear()
                self.finished.clear()
                self.handed_over = True
                return False
        finally:
            self._reconnect_lock.release()

    def _start_watchdog(self) -> None:
        if self.watchdog_thread is not None and self.watchdog_thread.is_alive():
            return
        self.watchdog_fired = False
        self.watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="asr-watchdog", daemon=True)
        self.watchdog_thread.start()

    def _start_pinger(self) -> None:
        interval = float(os.environ.get("LIVE_INTERPRETER_WS_PING", "20") or 0)
        if interval <= 0:
            return
        if self.pinger is not None and self.pinger.is_alive():
            return
        self.pinger = threading.Thread(target=self._ping_loop, args=(interval,), daemon=True)
        self.pinger.start()

    def open(self) -> None:
        self._connect(first=True)
        self.heartbeat = runtime_safety.Heartbeat(emit)
        self.heartbeat.start()
        self._start_watchdog()
        self._start_pinger()

    @staticmethod
    def _pcm_rms(data: bytes) -> int:
        if len(data) < 2:
            return 0
        if audioop is not None:
            try:
                return int(audioop.rms(data, 2))
            except Exception:
                return 0
        total = 0
        count = len(data) // 2
        for i in range(0, count * 2, 2):
            sample = int.from_bytes(data[i:i + 2], "little", signed=True)
            total += sample * sample
        return int((total / count) ** 0.5) if count else 0

    # -- watchdog and failover ------------------------------------------------
    def _watchdog_loop(self) -> None:
        """Start the backup engine when a half-open ASR socket stops producing text.

        Only fires when audio is still arriving and recent PCM energy says
        someone is speaking; silence/music pauses do not trigger it.
        """
        timeout = float(os.environ.get("LIVE_INTERPRETER_ASR_WATCHDOG", "10") or 0)
        if timeout <= 0:
            return
        rms_threshold = int(os.environ.get("LIVE_INTERPRETER_ASR_WATCHDOG_RMS", "300") or 0)
        while not self.finished.wait(1.0):
            if self.failed.is_set() or self.watchdog_fired:
                return
            now = time.monotonic()
            age = now - self.last_result_monotonic
            audio_recent = (now - self.last_audio_monotonic) <= 2.0
            speech_recent = (now - self.last_speech_monotonic) <= 5.0
            if age < timeout or not audio_recent or not speech_recent:
                continue
            self.watchdog_fired = True
            payload = {
                "event": "asr_stall",
                "engine": self.engine_name,
                "task_id": self.task_id,
                "age_s": round(age, 1),
                "threshold_s": timeout,
                "rms_threshold": rms_threshold,
                "audio_bytes": getattr(self, "audio_bytes", 0),
                "close_code": self.close_code,
            }
            emit(payload)
            runtime_safety.log_disconnect({"kind": "asr_stall", **payload})
            if self.on_stall(payload):
                return
            self.error = f"ASR watchdog: no result for {age:.1f}s while speech audio was arriving."
            self.failed.set()
            self.finished.set()
            try:
                if self.ws is not None:
                    self.ws.close()
            except Exception:
                pass
            return

    def _socket_errors_suppressed(self) -> bool:
        """The main socket is down on purpose while the backup serves."""
        return (self.handed_over or self.handing_over
                or (self.fallback is not None and self.fallback.active))

    def audio_route(self) -> str:
        """main / both (probation) / backup (handover)."""
        if self.handing_over:
            return "backup"
        if self.fallback is not None and self.fallback.active:
            return "both" if self.probation else "backup"
        return "main"

    def on_stall(self, payload: Dict[str, object]) -> bool:
        """Start the public Paraformer backup and probe the main engine."""
        if self.fallback is None:
            return False
        if self.probation:
            self.probe_aborted.set()
            self.probation = False
            try:
                if self.ws is not None:
                    self.ws.close()
            except Exception:
                pass
            return True
        if self.fallback.active:
            return True
        # Buffer audio (and stop writing to the stalled socket) while the
        # public worker is being started.
        self.handing_over = True
        try:
            started = self.fallback.activate(self.last_result_end_ms)
        except Exception as exc:
            self.handing_over = False
            emit({"event": "warning",
                  "message": f"Paraformer backup failed: {type(exc).__name__}: {exc}"})
            return False
        if not started:
            self.handing_over = False
            return False
        self.handing_over = False
        emit({
            "event": "asr_stall_handover",
            "engine": "Paraformer (public)",
            "task_id": self.task_id,
            "age_s": payload.get("age_s"),
            "from_ms": round(self.last_result_end_ms),
        })
        self.handed_over = True
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        threading.Thread(target=self._recovery_loop, name="asr-recovery", daemon=True).start()
        return True

    def _recovery_loop(self) -> None:
        probe = float(os.environ.get("LIVE_INTERPRETER_BACKUP_PROBE", "15") or 15)
        probation_s = float(os.environ.get("LIVE_INTERPRETER_BACKUP_PROBATION", "8") or 8)
        while not self.finished.is_set():
            if self.fallback is None or not self.fallback.active:
                return
            if self.finished.wait(probe):
                return
            if self.fallback is None or not self.fallback.active:
                return
            emit({"event": "asr_recovery_probe", "engine": self.engine_name,
                  "after_s": round(time.monotonic() - self.fallback.started_at)})
            if not self.reconnect():
                continue
            self.probation = True
            self.probe_aborted.clear()
            started = time.monotonic()
            healthy = False
            while time.monotonic() - started < probation_s:
                if self.finished.is_set():
                    return
                if self.probe_aborted.is_set():
                    break
                if self.last_result_monotonic >= started:
                    healthy = True
                    break
                time.sleep(0.2)
            self.probation = False
            if healthy:
                if self.fallback is not None:
                    self.fallback.deactivate()
                self.handed_over = False
                emit({"event": "asr_fallback_done", "reason": "main_recovered",
                      "engine": self.engine_name})
                return
            self.handed_over = True
            try:
                if self.ws is not None:
                    self.ws.close()
            except Exception:
                pass

    def backup_lost(self) -> None:
        """The public backup process died; restart the main worker."""
        self.error = "Paraformer backup exited; restarting the main ASR."
        self.failed.set()
        self.finished.set()

    def _ping_loop(self, interval: float) -> None:
        """Send WebSocket pings so NAT/LB idle timers do not reap the socket."""
        while not self.finished.wait(interval):
            if self.failed.is_set():
                return
            try:
                self.ws.ping("live-interpreter")
            except Exception as exc:
                if self._socket_errors_suppressed():
                    return
                runtime_safety.log_disconnect({
                    "kind": "ping_failed",
                    "engine": self.engine_name,
                    "task_id": self.task_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "close_code": self.close_code,
                })
                return

    def _close_info(self, ws) -> tuple[Optional[int], str]:
        frame = getattr(ws, "close_frame", None)
        if frame is None:
            return self.close_code, self.close_reason
        data = getattr(frame, "data", b"") or b""
        if len(data) >= 2:
            return int.from_bytes(data[:2], "big"), data[2:].decode("utf-8", errors="replace")
        return self.close_code, self.close_reason

    def task_request(self) -> Dict[str, object]:
        return {
            "header": {"action": "run-task", "task_id": self.task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "asr", "function": "recognition",
                "model": "gummy-realtime-v1",
                "parameters": {
                    "format": "pcm", "sample_rate": 16000,
                    "source_language": self.args.source_language,
                    "transcription_enabled": self.args.transcription_enabled,
                    "translation_enabled": self.args.translation_enabled,
                    "translation_target_languages": (
                        [self.args.target_language] if self.args.translation_enabled else []
                    ),
                    "max_end_silence": max(200, min(6000, self.args.max_end_silence)),
                },
                "input": {},
            },
        }

    def _receive_loop(self, generation: int, ws) -> None:
        while not self.finished.is_set():
            if generation != self.generation:
                return
            try:
                raw = ws.recv()
            except self.websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:
                if generation != self.generation:
                    return
                if self._socket_errors_suppressed():
                    return
                if not self.finished.is_set():
                    code, reason = self._close_info(ws)
                    self.close_code, self.close_reason = code, reason
                    self.error = f"{type(exc).__name__}: {exc}"
                    self.failed.set()
                    self.finished.set()
                    self.started.set()
                    emit({"event": "error", "code": code, "reason": reason,
                          "message": self.error})
                    runtime_safety.log_disconnect({
                        "kind": "ws_error", "engine": self.engine_name,
                        "task_id": self.task_id, "code": code,
                        "reason": reason, "error": self.error,
                    })
                return
            if not raw:
                if generation != self.generation:
                    return
                if self._socket_errors_suppressed():
                    return
                if not self.finished.is_set():
                    code, reason = self._close_info(ws)
                    self.close_code, self.close_reason = code, reason
                    self.error = (
                        f"WebSocket closed by remote endpoint"
                        + (f" (code {code} {reason})" if code is not None else "")
                    )
                    self.failed.set()
                    self.finished.set()
                    self.started.set()
                    emit({"event": "error", "code": code, "reason": reason,
                          "message": self.error})
                    runtime_safety.log_disconnect({
                        "kind": "ws_closed", "engine": self.engine_name,
                        "task_id": self.task_id, "code": code,
                        "reason": reason, "error": self.error,
                    })
                return
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if generation != self.generation:
                return
            header = message.get("header") or {}
            event = header.get("event")
            if event == "task-started":
                self.started.set()
            elif event == "result-generated":
                self._emit_result(message)
            elif event == "task-finished":
                if self._socket_errors_suppressed():
                    return
                self.finished.set()
                emit({"event": "finished"})
                return
            elif event == "task-failed":
                if self._socket_errors_suppressed():
                    return
                self.error = str(header.get("error_message") or header.get("error_code") or "Task failed.")
                self.failed.set()
                self.finished.set()
                self.started.set()
                emit(
                    {
                        "event": "error",
                        "code": header.get("error_code"),
                        "message": self.error,
                    }
                )
                return

    def _emit_result(self, message: Dict[str, object]) -> None:
        # Only real text counts as progress: the Qwen task sends heartbeat
        # sentences every few seconds, and treating those as results would
        # keep the watchdog asleep during a real stall.
        self.last_result_monotonic = time.monotonic()
        payload = message.get("payload") or {}
        output = payload.get("output") or {}
        transcription = output.get("transcription") or {}
        translations = output.get("translations") or []
        try:
            end_time = float(transcription.get("end_time") or 0)
        except (TypeError, ValueError):
            end_time = 0.0
        if end_time > 0:
            absolute_end = self.stream_start_ms + end_time
            self.last_result_end_ms = max(self.last_result_end_ms, absolute_end)
        if self.probation:
            return

        if transcription:
            emit(
                {
                    "event": "result",
                    "sentence_id": transcription.get("sentence_id"),
                    "source": transcription.get("text") or "",
                    "source_final": bool(transcription.get("sentence_end")),
                    "begin_time": transcription.get("begin_time"),
                    "end_time": transcription.get("end_time"),
                }
            )

        for translation in translations:
            if not isinstance(translation, dict):
                continue
            if translation.get("lang") != self.args.target_language:
                continue
            text = str(translation.get("text") or "")
            if self.hotword_config:
                text = self.hotword_config.postprocess(text)
            emit(
                {
                    "event": "result",
                    "sentence_id": translation.get("sentence_id"),
                    "translation": text,
                    "translation_final": bool(translation.get("sentence_end")),
                    "begin_time": translation.get("begin_time"),
                    "end_time": translation.get("end_time"),
                }
            )

    def send_audio(self, data: bytes) -> None:
        route = self.audio_route()
        if self.failed.is_set() or self.finished.is_set():
            raise RuntimeError(self.error or f"{self.engine_name} task failed.")
        now = time.monotonic()
        self.audio_total_bytes += len(data)
        if not self.last_audio_monotonic:
            # A connection can sit idle before its first frame; the watchdog
            # must count silence from that frame, not from task-started.
            self.last_result_monotonic = now
        self.last_audio_monotonic = now
        rms_threshold = int(os.environ.get("LIVE_INTERPRETER_ASR_WATCHDOG_RMS", "300") or 0)
        if self._pcm_rms(data) >= rms_threshold:
            resume_gap = float(os.environ.get("LIVE_INTERPRETER_SPEECH_RESUME_GAP", "30") or 30)
            if (self.last_speech_monotonic
                    and now - self.last_speech_monotonic > resume_gap):
                # Speech resumed after a long pause (video paused/muted).  The
                # silence clock is stale; restart it so the watchdog does not
                # instantly declare a stall on the first spoken frame.
                self.last_result_monotonic = now
                self.watchdog_fired = False
            self.last_speech_monotonic = now
        if self.fallback is not None:
            self.fallback.feed(data)
        if route == "backup":
            return
        self.ws.send_binary(data)

    def close(self) -> None:
        if self.fallback is not None:
            self.fallback.close()
        if self.ws is None:
            self.finished.set()
            return
        if self.heartbeat:
            self.heartbeat.stop()
        if self.started.is_set() and not self.failed.is_set() and not self.finished.is_set():
            try:
                self.ws.send(
                    json.dumps(
                        {
                            "header": {
                                "action": "finish-task",
                                "task_id": self.task_id,
                                "streaming": "duplex",
                            },
                            "payload": {"input": {}},
                        }
                    )
                )
                self.finished.wait(timeout=5)
            except Exception:
                pass
        self.finished.set()
        if self.pinger is not None:
            self.pinger.join(timeout=1)
        try:
            self.ws.close()
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    runtime_safety.install("gummy", os.environ.get("LIVE_INTERPRETER_WORK_DIR", ""))
    args.hotword_config = None
    if str(getattr(args, "hotwords", "")).strip():
        try:
            args.hotword_config = hotwords.load(args.hotwords)
        except hotwords.HotwordError as exc:
            emit({"event": "fatal_error", "message": str(exc)})
            return 2
        for warning in args.hotword_config.warnings:
            emit({"event": "warning", "message": warning})
        emit({
            "event": "warning",
            "message": "Gummy has no hotword API; the glossary is only used for output rewrites.",
        })
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        emit({"event": "error", "message": "Missing DASHSCOPE_API_KEY."})
        return 2

    connection = GummyConnection(args, api_key)
    try:
        connection.open()
        while not connection.finished.is_set():
            frame = sys.stdin.buffer.read(3200)
            if not frame:
                break
            connection.send_audio(frame)
        return 1 if connection.failed.is_set() else 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        emit({"event": "error", "message": str(exc)})
        return 1
    finally:
        connection.close()
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(runtime_safety.guard("gummy", main))
