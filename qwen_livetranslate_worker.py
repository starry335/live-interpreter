from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import threading
import time
import uuid
from typing import Dict, List, Optional


MODEL = "qwen3.5-livetranslate-flash-realtime"
REGION_HOSTS = {
    "beijing": "cn-beijing.maas.aliyuncs.com",
    "singapore": "ap-southeast-1.maas.aliyuncs.com",
}


def emit(payload: Dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qwen3.5 LiveTranslate realtime text worker.")
    parser.add_argument("--source-language", default="ja")
    parser.add_argument("--target-language", default="zh")
    parser.add_argument("--silence-duration-ms", type=int, default=500)
    parser.add_argument("--workspace-id", default=os.environ.get("DASHSCOPE_WORKSPACE_ID", ""))
    parser.add_argument("--region", choices=sorted(REGION_HOSTS), default="beijing")
    parser.add_argument("--visual-file", default="")
    return parser.parse_args(argv)


def event_text(message: Dict[str, object]) -> str:
    """Combine confirmed text and the provider's replaceable prediction stash."""
    text = str(message.get("text") or message.get("transcript") or "")
    stash = str(message.get("stash") or "")
    return text + stash


def image_event(data: bytes) -> Dict[str, object]:
    return {
        "event_id": uuid.uuid4().hex,
        "type": "input_image_buffer.append",
        "image": base64.b64encode(data).decode("ascii"),
    }


def session_config(args: argparse.Namespace) -> Dict[str, object]:
    transcription: Dict[str, object] = {"model": "qwen3-asr-flash-realtime"}
    if args.source_language != "auto":
        transcription["language"] = args.source_language
    return {
        "modalities": ["text"],
        "sample_rate": 16000,
        "input_audio_format": "pcm",
        "input_audio_transcription": transcription,
        "translation": {"language": args.target_language},
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.2,
            "silence_duration_ms": max(200, min(6000, args.silence_duration_ms)),
        },
    }


class ResultNormalizer:
    def __init__(self) -> None:
        self.sequence = 0
        self.current_sentence = "0"
        self.response_sentences: Dict[str, str] = {}
        self.item_sentences: Dict[str, str] = {}

    def _new_sentence(self) -> str:
        self.sequence += 1
        self.current_sentence = str(self.sequence)
        return self.current_sentence

    def _current(self) -> str:
        if self.current_sentence == "0":
            return self._new_sentence()
        return self.current_sentence

    def handle(self, message: Dict[str, object]) -> List[Dict[str, object]]:
        event_type = str(message.get("type") or "")
        if event_type == "input_audio_buffer.speech_started":
            self._new_sentence()
            return []

        if event_type.startswith("conversation.item.input_audio_transcription."):
            item_id = str(message.get("item_id") or "")
            sentence_id = self.item_sentences.setdefault(item_id, self._current())
            text = event_text(message)
            if not text:
                return []
            return [
                {
                    "event": "result",
                    "sentence_id": sentence_id,
                    "source": text,
                    "source_final": event_type.endswith(".completed"),
                }
            ]

        if event_type == "response.created":
            response = message.get("response") or {}
            if isinstance(response, dict):
                response_id = str(response.get("id") or "")
                if response_id:
                    self.response_sentences[response_id] = self._current()
            return []

        if event_type in {"response.text.text", "response.text.delta", "response.text.done"}:
            response_id = str(message.get("response_id") or "")
            sentence_id = self.response_sentences.setdefault(response_id, self._current())
            text = event_text(message)
            if not text:
                return []
            return [
                {
                    "event": "result",
                    "sentence_id": sentence_id,
                    "translation": text,
                    "translation_final": event_type == "response.text.done",
                }
            ]
        return []


class QwenLiveConnection:
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
        self.updated = threading.Event()
        self.finished = threading.Event()
        self.failed = threading.Event()
        self.ws = None
        self.receiver: Optional[threading.Thread] = None
        self.visual_sender: Optional[threading.Thread] = None
        self.error = ""
        self.normalizer = ResultNormalizer()
        self.send_lock = threading.Lock()
        self.audio_started = threading.Event()

    def open(self) -> None:
        workspace_id = self.args.workspace_id.strip()
        if not workspace_id:
            raise RuntimeError("Missing Alibaba Cloud Workspace ID.")
        host = REGION_HOSTS[self.args.region]
        url = f"wss://{workspace_id}.{host}/api-ws/v1/realtime?model={MODEL}"
        self.ws = self.websocket.create_connection(
            url,
            header=[
                f"Authorization: Bearer {self.api_key}",
                "User-Agent: LiveInterpreter/1.0",
            ],
            timeout=10,
            enable_multithread=True,
            sslopt={"ca_certs": self.certifi.where(), "cert_reqs": ssl.CERT_REQUIRED},
        )
        self.ws.settimeout(1.0)
        self.receiver = threading.Thread(target=self._receive_loop, daemon=True)
        self.receiver.start()
        self._send(
            {
                "event_id": uuid.uuid4().hex,
                "type": "session.update",
                "session": session_config(self.args),
            }
        )
        if not self.updated.wait(timeout=20):
            raise RuntimeError(self.error or "Timed out waiting for Qwen session.updated.")
        if self.args.visual_file:
            self.visual_sender = threading.Thread(target=self._visual_loop, daemon=True)
            self.visual_sender.start()
        emit({"event": "ready", "model": MODEL})

    def _send(self, payload: Dict[str, object]) -> None:
        with self.send_lock:
            self.ws.send(json.dumps(payload, ensure_ascii=False))

    def _visual_loop(self) -> None:
        path = self.args.visual_file
        last_modified = 0
        while not self.finished.wait(0.25):
            if not self.audio_started.is_set():
                continue
            try:
                modified = os.stat(path).st_mtime_ns
                if modified == last_modified:
                    continue
                with open(path, "rb") as stream:
                    data = stream.read()
                if (
                    not data.startswith(b"\xff\xd8")
                    or not data.endswith(b"\xff\xd9")
                    or len(data) > 512000
                ):
                    continue
                self._send(image_event(data))
                last_modified = modified
            except FileNotFoundError:
                continue
            except Exception as exc:
                if not self.finished.is_set():
                    print(f"Visual frame skipped: {exc}", file=sys.stderr, flush=True)

    def _receive_loop(self) -> None:
        while not self.finished.is_set():
            try:
                raw = self.ws.recv()
            except self.websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:
                if not self.finished.is_set():
                    self._fail(str(exc))
                return
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            event_type = str(message.get("type") or "")
            if event_type == "session.updated":
                self.updated.set()
            elif event_type == "session.finished":
                self.finished.set()
                emit({"event": "finished"})
                return
            elif event_type == "error":
                error = message.get("error") or {}
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("code")
                else:
                    detail = error
                self._fail(str(detail or "Qwen realtime request failed."))
                return
            else:
                for payload in self.normalizer.handle(message):
                    emit(payload)

    def _fail(self, message: str) -> None:
        self.error = message
        self.failed.set()
        self.finished.set()
        self.updated.set()
        emit({"event": "error", "message": message})

    def send_audio(self, data: bytes) -> None:
        if self.failed.is_set():
            raise RuntimeError(self.error or "Qwen realtime session failed.")
        self._send(
            {
                "event_id": uuid.uuid4().hex,
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(data).decode("ascii"),
            }
        )
        self.audio_started.set()

    def close(self) -> None:
        if self.ws is None:
            return
        if self.updated.is_set() and not self.failed.is_set() and not self.finished.is_set():
            try:
                self._send({"event_id": uuid.uuid4().hex, "type": "session.finish"})
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

    connection = QwenLiveConnection(args, api_key)
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
