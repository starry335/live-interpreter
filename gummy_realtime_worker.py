from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import threading
import time
import uuid
from typing import Dict, List, Optional


GUMMY_WEBSOCKET_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def emit(payload: Dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alibaba Cloud Gummy realtime translation worker.")
    parser.add_argument("--source-language", default="auto")
    parser.add_argument("--target-language", default="zh")
    parser.add_argument("--max-end-silence", type=int, default=500)
    parser.add_argument("--transcription-enabled", action="store_true", default=True)
    parser.add_argument("--translation-enabled", action="store_true", default=True)
    parser.add_argument("--no-translation", dest="translation_enabled", action="store_false")
    return parser.parse_args(argv)


class GummyConnection:
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
        self.error = ""

    def open(self) -> None:
        self.ws = self.websocket.create_connection(
            GUMMY_WEBSOCKET_URL,
            header=[
                f"Authorization: Bearer {self.api_key}",
                "User-Agent: LiveInterpreter/1.0",
            ],
            timeout=10,
            enable_multithread=True,
            sslopt={
                "ca_certs": self.certifi.where(),
                "cert_reqs": ssl.CERT_REQUIRED,
            },
        )
        self.ws.settimeout(1.0)
        self.receiver = threading.Thread(target=self._receive_loop, daemon=True)
        self.receiver.start()
        self.ws.send(
            json.dumps(
                {
                    "header": {
                        "action": "run-task",
                        "task_id": self.task_id,
                        "streaming": "duplex",
                    },
                    "payload": {
                        "task_group": "audio",
                        "task": "asr",
                        "function": "recognition",
                        "model": "gummy-realtime-v1",
                        "parameters": {
                            "format": "pcm",
                            "sample_rate": 16000,
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
                },
                ensure_ascii=False,
            )
        )
        if not self.started.wait(timeout=20):
            raise RuntimeError(self.error or "Timed out waiting for Gummy task-started.")
        emit({"event": "ready", "task_id": self.task_id})

    def _receive_loop(self) -> None:
        while not self.finished.is_set():
            try:
                raw = self.ws.recv()
            except self.websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:
                if not self.finished.is_set():
                    self.error = str(exc)
                    self.failed.set()
                    emit({"event": "error", "message": self.error})
                return
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            header = message.get("header") or {}
            event = header.get("event")
            if event == "task-started":
                self.started.set()
            elif event == "result-generated":
                self._emit_result(message)
            elif event == "task-finished":
                self.finished.set()
                emit({"event": "finished"})
                return
            elif event == "task-failed":
                self.error = str(header.get("error_message") or header.get("error_code") or "Task failed.")
                self.failed.set()
                self.finished.set()
                emit(
                    {
                        "event": "error",
                        "code": header.get("error_code"),
                        "message": self.error,
                    }
                )
                return

    def _emit_result(self, message: Dict[str, object]) -> None:
        payload = message.get("payload") or {}
        output = payload.get("output") or {}
        transcription = output.get("transcription") or {}
        translations = output.get("translations") or []

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
            emit(
                {
                    "event": "result",
                    "sentence_id": translation.get("sentence_id"),
                    "translation": translation.get("text") or "",
                    "translation_final": bool(translation.get("sentence_end")),
                    "begin_time": translation.get("begin_time"),
                    "end_time": translation.get("end_time"),
                }
            )

    def send_audio(self, data: bytes) -> None:
        if self.failed.is_set():
            raise RuntimeError(self.error or "Gummy task failed.")
        self.ws.send_binary(data)

    def close(self) -> None:
        if self.ws is None:
            return
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
        try:
            self.ws.close()
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        emit({"event": "error", "message": "Missing DASHSCOPE_API_KEY."})
        return 2

    connection = GummyConnection(args, api_key)
    try:
        connection.open()
        while not connection.failed.is_set():
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
    raise SystemExit(main())
