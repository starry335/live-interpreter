"""Qwen-Audio-ASR-Streaming worker (Bailian realtime ASR) + text-model translation.

Not part of Live Interpreter's shipped engine list: the benchmark needs
``qwen-audio-3.0-asr-flash-streaming`` (instant ``vocabulary`` hotwords) paired
with a general chat model for translation. The protocol is the same DashScope
duplex WebSocket used by the Paraformer worker, so the event surface matches
``--paraformer-mt-worker`` exactly and the same subtitles/telemetry work.

Usage:
  set DASHSCOPE_API_KEY=sk-...
  python qwen_asr_stream_worker.py --hotwords list.txt --mt-model deepseek-v4.1-flash-1017
  # PCM16 16 kHz mono on stdin, 3200-byte frames
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from gummy_realtime_worker import GummyConnection
import hotwords


ASR_MODEL = "qwen-audio-3.0-asr-flash-streaming"
DEFAULT_MT_MODEL = "deepseek-v4.1-flash-1017"
HONORIFICS = ("ちゃん", "さん", "ちん", "たん", "くん", "様", "さま", "氏")
LOGGER = logging.getLogger("qwen-asr-stream")


def emit(payload: Dict[str, object]) -> None:
    line = json.dumps(payload, ensure_ascii=True)
    print(line, flush=True)
    if payload.get("event") != "result" or payload.get("source_final") or payload.get("translation_final"):
        LOGGER.info(json.dumps({"time": dt.datetime.now().isoformat(timespec="milliseconds"), **payload}, ensure_ascii=True))


def load_vocabulary(path: str) -> Dict[str, int]:
    """Bench export: {"term": weight 1..5} JSON."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("vocabulary JSON must be an object of term -> weight")
    result = {}
    for term, weight in document.items():
        if not isinstance(term, str) or not term.strip():
            continue
        try:
            value = int(weight)
        except (TypeError, ValueError):
            value = 5
        result[term.strip()] = max(1, min(50, value))
    return result


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qwen-Audio-ASR-Streaming + chat-model translation worker.")
    parser.add_argument("--model", default=ASR_MODEL)
    parser.add_argument("--source-language", choices=["auto", "ja", "zh", "en", "yue", "ko"], default="ja")
    parser.add_argument("--target-language", default="Chinese")
    parser.add_argument("--mt-model", default=DEFAULT_MT_MODEL)
    parser.add_argument("--max-end-silence", type=int, default=800)
    parser.add_argument("--no-translation", dest="translation_enabled", action="store_false")
    parser.add_argument("--workspace-id", default=os.environ.get("DASHSCOPE_WORKSPACE_ID", ""))
    parser.add_argument("--region", choices=["beijing"], default="beijing")
    parser.add_argument("--log-file", default="")
    parser.add_argument("--hotwords", "--hotwords-file", dest="hotwords", default="",
                        help="Glossary (.txt or kb-terms JSON) for translation terms.")
    parser.add_argument("--vocabulary-file", default="",
                        help="Instant ASR vocabulary JSON {term: weight}; enables parameters.vocabulary.")
    parser.add_argument("--vocabulary-weight", type=int, default=5)
    parser.add_argument("--mt-mode", choices=["force", "reference", "background", "icl", "icl2"], default="icl",
                        help="force: current Live Interpreter wording; reference: names are reference "
                             "spellings and may never be inserted when not spoken.")
    parser.add_argument("--mt-min-chars", type=int, default=0,
                        help="Drop translation terms whose source is shorter than this many characters.")
    parser.add_argument("--mt-drop-aliases", action="store_true",
                        help="Never send ASR-error-form aliases as translation terms.")
    parser.add_argument("--mt-honorific", action="store_true",
                        help="Kana-only short terms are only injected when the sentence has an "
                             "honorific right after them (cuts homophone false triggers).")
    parser.add_argument("--mt-kana-min", type=int, default=4,
                        help="Kana-only terms at least this long skip the honorific requirement.")
    parser.add_argument("--context-turns", type=int, default=0,
                        help="Previous subtitles sent as context; 0 disables it (default).")
    parser.add_argument("--hotword-cards", default="",
                        help="Optional JSON {source: attribute} for ICL name cards.")
    parser.add_argument("--context-text", default="",
                        help="Background context for qwen-audio-3.1 input.context (names, readings, domain).")
    parser.add_argument("--correct-names", action="store_true",
                        help="Snap near-miss kana name spellings onto known readings before MT.")
    args = parser.parse_args(argv)
    if args.workspace_id and not re.fullmatch(r"[A-Za-z0-9-]+", args.workspace_id):
        parser.error("Workspace ID must contain only letters, digits and hyphens.")
    return args


class QwenASRStreamConnection(GummyConnection):
    engine_name = "Qwen-Audio-ASR-Streaming"

    def __init__(self, args: argparse.Namespace, api_key: str) -> None:
        super().__init__(args, api_key)
        host = (f"{args.workspace_id}.cn-beijing.maas.aliyuncs.com" if args.workspace_id
                else "dashscope.aliyuncs.com")
        self.websocket_url = f"wss://{host}/api-ws/v1/inference"
        self.translation_url = f"https://{host}/compatible-mode/v1/chat/completions"
        self.ssl_context = ssl.create_default_context(cafile=self.certifi.where())
        self.translation_context = []
        self.pending: queue.Queue = queue.Queue(maxsize=6)
        self.mt_thread: Optional[threading.Thread] = None
        self.mt_stop = threading.Event()
        self.mt_fatal = threading.Event()
        self.seen_sentences = set()
        self.audio_bytes = 0
        self.asr_duration = 0.0
        self.mt_requests = 0
        self.mt_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.hotword_config = (
            hotwords.load(args.hotwords) if str(getattr(args, "hotwords", "")).strip() else None
        )
        cards_path = getattr(args, "hotword_cards", "") or str(hotwords.builtin_cards_path())
        self.name_attributes = hotwords.load_attributes(cards_path) if Path(cards_path).exists() else {}
        self.vocabulary: Dict[str, int] = {}
        if args.vocabulary_file:
            self.vocabulary = load_vocabulary(args.vocabulary_file)
        elif self.hotword_config:
            for term in self.hotword_config.asr_terms():
                self.vocabulary[term] = args.vocabulary_weight
        for warning in self.hotword_config.warnings if self.hotword_config else []:
            LOGGER.warning(warning)
            emit({"event": "warning", "message": warning})
        emit({
            "event": "hotword_vocabulary",
            "asr_terms": len(self.hotword_config.asr_terms()) if self.hotword_config else 0,
            "mt_terms": len(self.hotword_config.mt_terms()) if self.hotword_config else 0,
            "vocabulary": len(self.vocabulary),
            "asr_term_list": (self.hotword_config.asr_terms()[:300] if self.hotword_config else []),
            "mt_term_list": ([term.source for term in self.hotword_config.mt_terms()][:300]
                             if self.hotword_config else []),
        })

    def task_request(self) -> Dict[str, object]:
        parameters = {
            "format": "pcm",
            "sample_rate": 16000,
            "max_sentence_silence": max(200, min(6000, self.args.max_end_silence)),
            "multi_threshold_mode_enabled": True,
            "heartbeat": True,
        }
        if self.args.source_language != "auto":
            # qwen-audio-3.0 speaks language_hints; a plain string is also accepted.
            parameters["language_hints"] = [self.args.source_language]
        if self.vocabulary:
            parameters["vocabulary"] = dict(list(self.vocabulary.items())[:2000])
        task_input = {}
        if str(getattr(self.args, "context_text", "")).strip():
            task_input["context"] = [{
                "role": "user",
                "content": [{"type": "input_text", "text": str(self.args.context_text)[:400]}],
            }]
        return {
            "header": {"action": "run-task", "task_id": self.task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "asr", "function": "recognition",
                "model": self.args.model, "parameters": parameters, "input": task_input,
            },
        }

    def open(self) -> None:
        self.mt_thread = threading.Thread(target=self._translate_loop, daemon=True)
        self.mt_thread.start()
        super().open()
        emit({
            "event": "asr_config",
            "model": self.args.model,
            "vocabulary_terms": len(self.vocabulary),
            "mt_model": self.args.mt_model,
            "hotwords": self.hotword_config.summary() if self.hotword_config else None,
        })

    def send_audio(self, data: bytes) -> None:
        if len(data) % 2:
            raise ValueError("PCM16 audio must contain complete 2-byte samples.")
        super().send_audio(data)
        self.audio_bytes += len(data)

    def _emit_result(self, message: Dict[str, object]) -> None:
        payload = message.get("payload") or {}
        output = payload.get("output") or {}
        usage = payload.get("usage") or output.get("usage") or {}
        if usage.get("duration"):
            self.asr_duration = max(self.asr_duration, float(usage["duration"]))
        if usage.get("input_tokens"):
            self.asr_input_tokens = max(getattr(self, "asr_input_tokens", 0), int(usage["input_tokens"]))
        if usage.get("output_tokens"):
            self.asr_output_tokens = max(getattr(self, "asr_output_tokens", 0), int(usage["output_tokens"]))
        sentence = output.get("sentence") or {}
        if sentence.get("heartbeat"):
            return
        text = str(sentence.get("text") or "").strip()
        if not text:
            return
        sentence_id = str(sentence.get("begin_time", len(self.seen_sentences)))
        final = bool(sentence.get("sentence_end"))
        if final and sentence_id in self.seen_sentences:
            return
        result = {
            "event": "result", "sentence_id": sentence_id, "source": text,
            "source_final": final, "begin_time": sentence.get("begin_time"),
            "end_time": sentence.get("end_time"),
        }
        emit(result)
        if final:
            self.seen_sentences.add(sentence_id)
            if self.args.translation_enabled:
                try:
                    self.pending.put_nowait(result)
                except queue.Full:
                    self._translation_failed(result, "Translation queue full; showing original text.")

    def _term_allowed(self, source: str, text: str) -> bool:
        """Strong written forms always pass; short kana-only readings need an honorific."""
        if any("\u4e00" <= ch <= "\u9fff" for ch in source) or source.isascii():
            return True
        if len(source) >= getattr(self.args, "mt_kana_min", 4):
            return True
        return any(f"{source}{honorific}" in text for honorific in HONORIFICS)

    def translate(self, text: str, sentence_id: str = "") -> str:
        if getattr(self.args, "correct_names", False) and self.hotword_config:
            text = self.hotword_config.correct_kana_surfaces(text)
        terms = self.hotword_config.select_mt_terms(text) if self.hotword_config else []
        if terms:
            emit({
                "event": "hotword_hit",
                "sentence_id": sentence_id,
                "source": text[:120],
                "terms": [{"source": term.source, "target": term.target} for term in terms],
            })
        if terms and getattr(self.args, "mt_drop_aliases", False):
            alias_sources = {term.source for term in self.hotword_config.aliases}
            terms = [term for term in terms if term.source not in alias_sources]
        if terms and getattr(self.args, "mt_min_chars", 0):
            terms = [term for term in terms if len(term.source) >= self.args.mt_min_chars]
        if terms and getattr(self.args, "mt_honorific", False):
            terms = [term for term in terms if self._term_allowed(term.source, text)]
        mode = getattr(self.args, "mt_mode", "force")
        if mode == "background" and self.hotword_config:
            background_pairs = "; ".join(
                f"{term.source}=>{term.target}" for term in self.hotword_config.mt_terms()[:200])
            system_prompt = hotwords.build_system_prompt(
                "reference", [], self.args.source_language, self.args.target_language)
            system_prompt += f"\nKnown name translations in this stream: {background_pairs}"
        else:
            system_prompt = hotwords.build_system_prompt(
                mode, terms, self.args.source_language, self.args.target_language,
                self.name_attributes)
        context = self.translation_context[-max(0, int(getattr(self.args, "context_turns", 0))):]
        user_payload = {"current_text": text}
        if context:
            user_payload = {"previous_subtitles": context, "current_text": text}
        body = {
            "model": self.args.mt_model,
            "enable_thinking": False,
            "temperature": 0.2,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        request = urllib.request.Request(
            self.translation_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        self.mt_requests += 1
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=20, context=self.ssl_context) as response:
            data = json.loads(response.read())
        usage = data.get("usage") or {}
        self.input_tokens += int(usage.get("prompt_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or 0)
        emit({"event": "translation_usage", "model": self.args.mt_model,
              "request_id": data.get("id"), "usage": usage,
              "latency_ms": round((time.monotonic() - started) * 1000)})
        translated = str(data["choices"][0]["message"]["content"] or "").strip()
        if not translated:
            raise RuntimeError(f"{self.args.mt_model} returned empty translation.")
        if data["choices"][0].get("finish_reason") == "length":
            raise RuntimeError(f"{self.args.mt_model} output was truncated; showing original text.")
        if self.hotword_config:
            translated = self.hotword_config.postprocess(translated)
        self.translation_context.append({"source": text[:240], "translation": translated[:240]})
        return translated

    def _translation_failed(self, result: Dict[str, object], reason: str) -> None:
        self.mt_failures += 1
        emit({"event": "warning", "message": f"{self.args.mt_model}: {reason}",
              "sentence_id": result["sentence_id"]})
        emit({"event": "result", "sentence_id": result["sentence_id"],
              "translation": result["source"], "translation_final": True})

    def _translate_loop(self) -> None:
        while not self.mt_stop.is_set() or not self.pending.empty():
            try:
                result = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                translated = self.translate(str(result["source"]), str(result["sentence_id"]))
                emit({"event": "result", "sentence_id": result["sentence_id"],
                      "translation": translated, "translation_final": True})
            except urllib.error.HTTPError as exc:
                reason = f"HTTP {exc.code}: {exc.read(2048).decode('utf-8', errors='replace')}"
                self._translation_failed(result, reason.replace(self.api_key, "[redacted]"))
                if exc.code in {400, 401, 403, 404}:
                    self.error = f"{self.args.mt_model} configuration or authorization failed; stopping."
                    self.mt_fatal.set()
                    emit({"event": "fatal_error", "message": self.error})
                    return
            except Exception as exc:
                self._translation_failed(result, str(exc).replace(self.api_key, "[redacted]"))
            finally:
                self.pending.task_done()

    def close(self) -> None:
        super().close()
        self.mt_stop.set()
        if self.mt_thread:
            self.mt_thread.join(timeout=3)
        emit({
            "event": "usage_summary", "task_id": self.task_id,
            "asr_model": self.args.model, "translation_model": self.args.mt_model,
            "audio_sent_seconds": round(self.audio_bytes / 32000, 3),
            "asr_billed_seconds": self.asr_duration,
            "asr_input_tokens": getattr(self, "asr_input_tokens", 0),
            "asr_output_tokens": getattr(self, "asr_output_tokens", 0),
            "translation_requests": self.mt_requests, "translation_failures": self.mt_failures,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "vocabulary_terms": len(self.vocabulary),
            "hotwords": self.hotword_config.summary() if self.hotword_config else None,
        })


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    handler = None
    if args.log_file:
        path = Path(args.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        LOGGER.propagate = False
    connection = None
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    try:
        if not api_key:
            emit({"event": "fatal_error", "message": "Missing DASHSCOPE_API_KEY."})
            return 2
        connection = QwenASRStreamConnection(args, api_key)
        connection.open()
        while not connection.failed.is_set() and not connection.mt_fatal.is_set() and not connection.finished.is_set():
            frame = sys.stdin.buffer.read(3200)
            if not frame:
                break
            connection.send_audio(frame)
    except KeyboardInterrupt:
        pass
    except hotwords.HotwordError as exc:
        message = str(exc).replace(api_key, "[redacted]") if api_key else str(exc)
        emit({"event": "fatal_error", "message": message})
        return 2
    except Exception as exc:
        message = str(exc).replace(api_key, "[redacted]") if api_key else str(exc)
        fatal = getattr(exc, "status_code", None) in {400, 401, 403, 404}
        emit({"event": "fatal_error" if fatal else "error", "message": message})
        return 1
    finally:
        if connection:
            connection.close()
        if handler:
            handler.close()
            LOGGER.removeHandler(handler)
    return 1 if connection and (connection.failed.is_set() or connection.mt_fatal.is_set()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
