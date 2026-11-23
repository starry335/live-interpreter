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
import http.client
import io
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
from urllib.parse import urlsplit
from typing import Dict, List, Optional

from gummy_realtime_worker import GummyConnection
import hotwords
import runtime_safety
import warm_names


ASR_MODEL = "qwen-audio-3.0-asr-flash-streaming"
DEFAULT_MT_MODEL = "deepseek-v4.1-flash-1017"
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
MT_ATTEMPTS = 3
HONORIFICS = ("ちゃん", "さん", "ちん", "たん", "くん", "様", "さま", "氏")
LOGGER = logging.getLogger("qwen-asr-stream")


def emit(payload: Dict[str, object]) -> None:
    runtime_safety.write_json(payload)
    if payload.get("event") != "heartbeat" and (
        payload.get("event") != "result" or payload.get("source_final") or payload.get("translation_final")
    ):
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
    parser.add_argument("--asr-vocabulary-limit", type=int, default=300,
                        help="Cap instant ASR vocabulary terms; keeps the task request small "
                             "enough for long 3.1 sessions.")
    parser.add_argument("--mt-mode", choices=["force", "reference", "background", "icl", "icl2"], default="icl",
                        help="force: current Live Interpreter wording; reference: names are reference "
                             "spellings and may never be inserted when not spoken.")
    parser.add_argument("--icl-examples", type=int, default=2,
                        help="Contrastive examples kept in the ICL prompt (0 = none, negative = all).")
    parser.add_argument("--mt-min-chars", type=int, default=0,
                        help="Drop translation terms whose source is shorter than this many characters.")
    parser.add_argument("--mt-drop-aliases", action="store_true",
                        help="Never send ASR-error-form aliases as translation terms.")
    parser.add_argument("--mt-honorific", action=argparse.BooleanOptionalAction, default=True,
                        help="Kana-only short terms are only injected when the sentence has an "
                             "honorific right after them (cuts homophone false triggers).")
    parser.add_argument("--mt-kana-min", type=int, default=4,
                        help="Kana-only terms at least this long skip the honorific requirement.")
    parser.add_argument("--context-turns", type=int, default=0,
                        help="Previous subtitles sent as context; 0 disables it (default).")
    parser.add_argument("--short-context-chars", type=int, default=12,
                        help="Also send one previous subtitle when the source is at most this many chars (0 disables).")
    parser.add_argument("--short-context-turns", type=int, default=1,
                        help="How many previous subtitles to send for a short source.")
    parser.add_argument("--vad-model", default="", choices=["", "near_meeting_16k", "far_field_meeting_16k"],
                        help="3.1 only; empty keeps the server default.")
    parser.add_argument("--speech-noise-threshold", type=float, default=None,
                        help="3.1 only; -1..1, omitted keeps the server default.")
    parser.add_argument("--multi-threshold", action=argparse.BooleanOptionalAction, default=True,
                        help="3.1 only; server multi-threshold VAD (default on).")
    parser.add_argument("--stream-translation", action=argparse.BooleanOptionalAction, default=True,
                        help="Stream the final translation so the first Chinese reaches the overlay sooner.")
    parser.add_argument("--stream-min-interval", type=float, default=0.15,
                        help="Minimum seconds between streamed translation updates.")
    parser.add_argument("--draft-translation", action=argparse.BooleanOptionalAction, default=True,
                        help="Translate a long unfinished sentence provisionally, then replace it with the final translation.")
    parser.add_argument("--draft-after", type=float, default=4.0,
                        help="Start a draft after the sentence has been open this many seconds.")
    parser.add_argument("--draft-after-chars", type=int, default=20,
                        help="Also start a draft once the partial reaches this many characters (0 disables).")
    parser.add_argument("--draft-interval", type=float, default=1.2,
                        help="Minimum seconds between drafts for one sentence.")
    parser.add_argument("--draft-min-chars", type=int, default=12,
                        help="Do not draft sentences shorter than this many characters.")
    parser.add_argument("--draft-growth", type=int, default=6,
                        help="Require at least this many new characters since the previous draft.")
    parser.add_argument("--draft-max", type=int, default=1,
                        help="Maximum drafts per sentence.")
    parser.add_argument("--draft-mt-model", default="deepseek-v4.1-flash-1017",
                        help="Model used only for provisional drafts; empty reuses --mt-model.")
    parser.add_argument("--draft-max-tokens", type=int, default=256,
                        help="Maximum output tokens for a provisional draft.")
    parser.add_argument("--hotword-cards", default="",
                        help="Optional JSON {source: attribute} for ICL name cards.")
    parser.add_argument("--context-text", default="",
                        help="Background context for qwen-audio-3.1 input.context (names, readings, domain).")
    parser.add_argument("--correct-names", action="store_true",
                        help="Snap near-miss kana name spellings onto known readings before MT.")
    parser.add_argument("--warm-names", action=argparse.BooleanOptionalAction, default=False,
                        help="Recall game/anime names by pronunciation for the final MT prompt "
                             "(off by default: precision-first).")
    parser.add_argument("--name-consistency", action=argparse.BooleanOptionalAction, default=False,
                        help="Prefer the neighbouring name when one final uses a lone hotword name "
                             "(off by default: it can invent a different name).")
    parser.add_argument("--asr-warm", action=argparse.BooleanOptionalAction, default=True,
                        help="Send [asr-warm] terms at weight 1; --no-asr-warm keeps the hot list only.")
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
        self.source_history: List[str] = []
        self.pending: queue.Queue = queue.Queue(maxsize=24)
        self.draft_cache: Dict[str, str] = {}
        self.translation_queued = 0
        self.translation_fallbacks = 0
        self.mt_thread: Optional[threading.Thread] = None
        self.mt_stop = threading.Event()
        self.mt_fatal = threading.Event()
        self.draft_thread: Optional[threading.Thread] = None
        self.draft_stop = threading.Event()
        self.draft_queue: queue.Queue = queue.Queue(maxsize=2)
        self.draft_state: Dict[str, Dict[str, object]] = {}
        self.finalized: set = set()
        self.finalizing: set = set()
        self.final_lock = threading.Lock()
        self.draft_requests = 0
        self.draft_failures = 0
        self.draft_input_tokens = 0
        self.draft_output_tokens = 0
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
        self.context_text = str(
            getattr(args, "context_text", "")
            or (self.hotword_config.context if self.hotword_config else "")
        )[:400]
        cards_path = getattr(args, "hotword_cards", "") or str(hotwords.builtin_cards_path())
        self.name_attributes = hotwords.load_attributes(cards_path) if Path(cards_path).exists() else {}
        self.vocabulary: Dict[str, int] = {}
        vocabulary_limit = max(1, int(getattr(args, "asr_vocabulary_limit", 175)))
        if args.vocabulary_file:
            self.vocabulary = dict(list(load_vocabulary(args.vocabulary_file).items())[:vocabulary_limit])
        elif self.hotword_config:
            for term, weight in self.hotword_config.asr_weights(
                args.vocabulary_weight, include_warm=bool(getattr(args, "asr_warm", True))
            ).items():
                if len(self.vocabulary) >= vocabulary_limit:
                    break
                self.vocabulary[term] = weight
        for warning in self.hotword_config.warnings if self.hotword_config else []:
            LOGGER.warning(warning)
            emit({"event": "warning", "message": warning})
        emit({
            "event": "session_config",
            "model": args.model,
            "source_language": args.source_language,
            "language_hints": [] if args.source_language == "auto" else [args.source_language],
            "hotwords": str(getattr(args, "hotwords", "")),
            "cards": cards_path,
            "mt_mode": args.mt_mode,
        })
        emit({
            "event": "hotword_vocabulary",
            "asr_terms": len(self.hotword_config.asr_terms()) if self.hotword_config else 0,
            "mt_terms": len(self.hotword_config.mt_terms()) if self.hotword_config else 0,
            "vocabulary": len(self.vocabulary),
            "asr_term_list": (self.hotword_config.asr_terms()[:30] if self.hotword_config else []),
            "mt_term_list": ([term.source for term in self.hotword_config.mt_terms()][:30]
                             if self.hotword_config else []),
        })

    def task_request(self) -> Dict[str, object]:
        parameters = {
            "format": "pcm",
            "sample_rate": 16000,
            "max_sentence_silence": max(200, min(6000, self.args.max_end_silence)),
            "multi_threshold_mode_enabled": bool(getattr(self.args, "multi_threshold", True)),
            "heartbeat": True,
        }
        if self.args.vad_model:
            parameters["vad_model"] = self.args.vad_model
        if self.args.speech_noise_threshold is not None:
            parameters["speech_noise_threshold"] = self.args.speech_noise_threshold
        if self.args.source_language != "auto":
            # qwen-audio-3.0 speaks language_hints; a plain string is also accepted.
            parameters["language_hints"] = [self.args.source_language]
        if self.vocabulary:
            parameters["vocabulary"] = dict(list(self.vocabulary.items())[:2000])
        task_input = {}
        if self.context_text:
            task_input["context"] = [{
                "role": "user",
                "content": [{"type": "input_text", "text": self.context_text}],
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
        self.draft_thread = threading.Thread(target=self._draft_loop, daemon=True)
        self.draft_thread.start()
        super().open()
        emit({
            "event": "asr_config",
            "model": self.args.model,
            "vocabulary_terms": len(self.vocabulary),
            "vocabulary_limit": getattr(self.args, "asr_vocabulary_limit", 0),
            "mt_model": self.args.mt_model,
            "max_end_silence": self.args.max_end_silence,
            "vad_model": self.args.vad_model or "(server default)",
            "multi_threshold": bool(getattr(self.args, "multi_threshold", True)),
            "stream_translation": bool(getattr(self.args, "stream_translation", True)),
            "draft_translation": bool(getattr(self.args, "draft_translation", True)),
            "draft_after": getattr(self.args, "draft_after", 0),
            "draft_after_chars": getattr(self.args, "draft_after_chars", 0),
            "hotwords": self.hotword_config.summary() if self.hotword_config else None,
        })

    def send_audio(self, data: bytes) -> None:
        if len(data) % 2:
            raise ValueError("PCM16 audio must contain complete 2-byte samples.")
        super().send_audio(data)
        self.audio_bytes += len(data)

    def _correct_name_consistency(self, text: str) -> str:
        """Replace a one-off hotword name with the name used in nearby lines.

        The 3.1 vocabulary treats short member names as a closed class; an
        unknown game character such as ひろちゃん can be written as 律ちゃん.
        If two of the previous three finals use another name and this final is
        the only occurrence of the hotword name, prefer the neighbouring name.
        Real name switches repeat in the window and are left alone.
        """
        groups = {
            "ritsu": ("りつ", "リツ", "律", "りつちゃん", "リツちゃん", "律ちゃん"),
            "hiro": ("ひろ", "ヒロ", "ヒロちゃん"),
            "yuno": ("ゆの", "ユノ", "ゆのちゃん", "ユノちゃん"),
            "nonoka": ("ののか", "ノノカ", "ののちゃん", "ノンちゃん", "のちゃん", "ノちゃん"),
            "miyako": ("みやこ", "ミヤコ", "みやこちゃん", "ミャーコちゃん"),
            "arale": ("あられ", "アラレ", "あられちゃん", "アラレちゃん"),
        }
        window = self.source_history[-3:] + [text]
        counts: Dict[str, int] = {}
        surfaces: Dict[str, str] = {}
        current_group = ""
        for group, variants in groups.items():
            for variant in variants:
                if variant in text:
                    current_group = group
                    break
            count = sum(1 for line in window if any(v in line for v in variants))
            if count:
                counts[group] = count
                for line in window:
                    for variant in variants:
                        if variant in line:
                            surfaces[group] = variant
                            break
        if (
            current_group in ("ritsu", "nonoka")
            and counts.get(current_group, 0) <= 1
            and counts.get("hiro", 0) >= 2
            and surfaces.get("hiro")
        ):
            for variant in groups[current_group]:
                if variant in text:
                    replacement = surfaces["hiro"]
                    if variant.endswith("ちゃん") and not replacement.endswith("ちゃん"):
                        replacement += "ちゃん"
                    text = text.replace(variant, replacement)
                    break
        self.source_history.append(text)
        del self.source_history[:-8]
        return text

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
        if final:
            if bool(getattr(self.args, "name_consistency", True)):
                text = self._correct_name_consistency(text)
            else:
                self.source_history.append(text)
                del self.source_history[:-8]
        result = {
            "event": "result", "sentence_id": sentence_id, "source": text,
            "source_final": final, "begin_time": sentence.get("begin_time"),
            "end_time": sentence.get("end_time"),
        }
        emit(result)
        if final:
            self.seen_sentences.add(sentence_id)
            with self.final_lock:
                self.finalizing.add(sentence_id)
            self.draft_state.pop(sentence_id, None)
            if self.args.translation_enabled:
                try:
                    self.pending.put_nowait(result)
                    self.translation_queued += 1
                    emit({"event": "translation_queued", "sentence_id": sentence_id})
                except queue.Full:
                    self._translation_failed(result, "Translation queue full; showing original text.")
        else:
            self._maybe_draft(sentence_id, text)

    def _term_allowed(self, source: str, text: str) -> bool:
        """Strong written forms always pass; short kana-only readings need an honorific."""
        if any("\u4e00" <= ch <= "\u9fff" for ch in source) or source.isascii():
            return True
        if len(source) >= getattr(self.args, "mt_kana_min", 4):
            return True
        return any(f"{source}{honorific}" in text for honorific in HONORIFICS)

    def _icl_limit(self) -> Optional[int]:
        value = int(getattr(self.args, "icl_examples", 2))
        return None if value < 0 else max(0, value)

    def translate(self, text: str, sentence_id: str = "") -> str:
        if getattr(self.args, "correct_names", False) and self.hotword_config:
            text = self.hotword_config.correct_kana_surfaces(text)
        terms = self.hotword_config.select_mt_terms(text) if self.hotword_config else []
        warm_terms = []
        if bool(getattr(self.args, "warm_names", True)):
            warm_terms = [hotwords.Term(source, target) for source, target in warm_names.retrieve(text)]
        if warm_terms:
            seen_sources = {term.source for term in terms}
            terms = list(terms) + [term for term in warm_terms if term.source not in seen_sources]
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
                self.name_attributes,
                icl_examples_limit=self._icl_limit())
        context_turns = max(0, int(getattr(self.args, "context_turns", 0)))
        context = self.translation_context[-context_turns:] if context_turns else []
        short_limit = max(0, int(getattr(self.args, "short_context_chars", 0)))
        if not context and short_limit and len(text) <= short_limit:
            short_turns = max(1, int(getattr(self.args, "short_context_turns", 1)))
            context = self.translation_context[-short_turns:]
            system_prompt += (
                "\n这是实时字幕里的短句。结合上一句译成自然中文；"
                "不要逐词直译，也不要重复上一句。只输出当前句译文。"
            )
        if context:
            user_content = json.dumps(
                {"previous_subtitles": context, "current_text": text}, ensure_ascii=False
            )
        else:
            # A bare subtitle is a few tokens cheaper than a JSON wrapper and
            # works with the same system prompt.
            user_content = text
        body = {
            "model": self.args.mt_model,
            "enable_thinking": False,
            "temperature": 0.2,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        streaming = bool(getattr(self.args, "stream_translation", True))
        if streaming:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        request = urllib.request.Request(
            self.translation_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        self.mt_requests += 1
        started = time.monotonic()
        if not streaming:
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

        # Streaming: publish the first characters as soon as the model emits
        # them, so the overlay shows Chinese before the full sentence is done.
        # Use http.client directly: urllib's response object buffers SSE lines
        # until the connection closes on Python 3.10 (the frozen backend).
        content: List[str] = []
        usage: Dict[str, object] = {}
        finish_reason = ""
        request_id = ""
        last_emit = 0.0
        split = urlsplit(self.translation_url)
        connection = http.client.HTTPSConnection(
            split.hostname, split.port or 443, timeout=30, context=self.ssl_context
        )
        try:
            connection.request(
                "POST",
                split.path or "/",
                body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            )
            response = connection.getresponse()
            if response.status >= 400:
                detail = response.read(65536)
                raise urllib.error.HTTPError(
                    self.translation_url, response.status, response.reason,
                    response.getheaders(), io.BytesIO(detail),
                )
            while True:
                raw = response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                request_id = str(chunk.get("id") or request_id)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                delta = (choice.get("delta") or {}).get("content") or ""
                if not delta:
                    continue
                content.append(delta)
                now = time.monotonic()
                if now - last_emit >= float(getattr(self.args, "stream_min_interval", 0.06)):
                    emit({
                        "event": "result", "sentence_id": sentence_id,
                        "translation": "".join(content),
                        "translation_final": False, "streaming": True,
                    })
                    last_emit = now
        except Exception as exc:
            partial = "".join(content).strip()
            if partial:
                raise runtime_safety.PartialTranslation(partial) from exc
            raise
        finally:
            connection.close()
        latency_ms = round((time.monotonic() - started) * 1000)
        self.input_tokens += int(usage.get("prompt_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or 0)
        emit({"event": "translation_usage", "model": self.args.mt_model,
              "request_id": request_id, "usage": usage, "latency_ms": latency_ms})
        translated = "".join(content).strip()
        if not translated:
            raise RuntimeError(f"{self.args.mt_model} returned empty translation.")
        if finish_reason == "length":
            raise RuntimeError(f"{self.args.mt_model} output was truncated; showing original text.")
        if self.hotword_config:
            translated = self.hotword_config.postprocess(translated)
        self.translation_context.append({"source": text[:240], "translation": translated[:240]})
        return translated

    def _maybe_draft(self, sentence_id: str, text: str) -> None:
        """Translate a long unfinished sentence provisionally.

        Drafts only exist to fill the silence before the server VAD finalizes a
        long utterance; the final translation always replaces them.
        """
        if not bool(getattr(self.args, "draft_translation", True)):
            return
        if not self.args.translation_enabled:
            return
        with self.final_lock:
            if sentence_id in self.finalizing or sentence_id in self.finalized:
                return
        now = time.monotonic()
        state = self.draft_state.get(sentence_id)
        if state is None:
            state = {"first": now, "last_text": "", "last_at": 0.0, "count": 0}
            self.draft_state[sentence_id] = state
        if now - float(state["first"]) < float(getattr(self.args, "draft_after", 1.8)):
            after_chars = int(getattr(self.args, "draft_after_chars", 0))
            if after_chars <= 0 or len(text) < after_chars:
                return
        if len(text) < int(getattr(self.args, "draft_min_chars", 12)):
            return
        if int(state["count"]) >= int(getattr(self.args, "draft_max", 2)):
            return
        previous = str(state["last_text"])
        if previous and len(text) - len(previous) < int(getattr(self.args, "draft_growth", 6)):
            return
        if now - float(state["last_at"]) < float(getattr(self.args, "draft_interval", 1.0)):
            return
        state["last_text"] = text
        state["last_at"] = now
        state["count"] = int(state["count"]) + 1
        job = (sentence_id, text)
        try:
            self.draft_queue.put_nowait(job)
        except queue.Full:
            try:
                self.draft_queue.get_nowait()
                self.draft_queue.task_done()
            except queue.Empty:
                pass
            try:
                self.draft_queue.put_nowait(job)
            except queue.Full:
                pass

    def translate_draft(self, text: str, sentence_id: str = "") -> str:
        if getattr(self.args, "correct_names", False) and self.hotword_config:
            text = self.hotword_config.correct_kana_surfaces(text)
        terms = self.hotword_config.select_mt_terms(text) if self.hotword_config else []
        if terms and getattr(self.args, "mt_drop_aliases", False):
            alias_sources = {term.source for term in self.hotword_config.aliases}
            terms = [term for term in terms if term.source not in alias_sources]
        if terms and getattr(self.args, "mt_min_chars", 0):
            terms = [term for term in terms if len(term.source) >= self.args.mt_min_chars]
        mode = getattr(self.args, "mt_mode", "force")
        system_prompt = hotwords.build_system_prompt(
            mode, terms, self.args.source_language, self.args.target_language,
            self.name_attributes,
            icl_examples_limit=self._icl_limit())
        system_prompt += (
            "\n这是实时字幕中说话者尚未说完的半句。只翻译已经说出的部分，"
            "保持未完成的状态；不要补全、不要重复、不要解释。只输出中文译文。"
        )
        body = {
            "model": str(getattr(self.args, "draft_mt_model", "") or self.args.mt_model),
            "enable_thinking": False,
            "temperature": 0.2,
            "max_tokens": int(getattr(self.args, "draft_max_tokens", 256)),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
        }
        request = urllib.request.Request(
            self.translation_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        self.draft_requests += 1
        with urllib.request.urlopen(request, timeout=20, context=self.ssl_context) as response:
            data = json.loads(response.read())
        usage = data.get("usage") or {}
        self.draft_input_tokens += int(usage.get("prompt_tokens") or 0)
        self.draft_output_tokens += int(usage.get("completion_tokens") or 0)
        translated = str(data["choices"][0]["message"]["content"] or "").strip()
        if not translated:
            raise RuntimeError("empty draft translation")
        if self.hotword_config:
            translated = self.hotword_config.postprocess(translated)
        return translated

    def _draft_loop(self) -> None:
        while not self.draft_stop.is_set() or not self.draft_queue.empty():
            try:
                sentence_id, text = self.draft_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self.final_lock:
                    if sentence_id in self.finalizing or sentence_id in self.finalized:
                        continue
                translated = self.translate_draft(text, sentence_id)
                with self.final_lock:
                    if sentence_id in self.finalizing or sentence_id in self.finalized:
                        continue
                    self.draft_cache[sentence_id] = translated
                    if len(self.draft_cache) > 64:
                        for old_key in list(self.draft_cache)[:-32]:
                            self.draft_cache.pop(old_key, None)
                    emit({
                        "event": "result", "sentence_id": sentence_id,
                        "translation": translated, "translation_final": False,
                        "draft": True,
                    })
            except Exception as exc:  # drafts are best-effort
                self.draft_failures += 1
                LOGGER.warning("draft skipped: %s", str(exc).replace(self.api_key, "[redacted]")[:300])
            finally:
                self.draft_queue.task_done()

    def _translation_failed(self, result: Dict[str, object], reason: str,
                            partial: str = "") -> None:
        self.mt_failures += 1
        sentence_id = str(result["sentence_id"])
        draft = self.draft_cache.pop(sentence_id, "")
        translated = (partial or draft or str(result["source"] or "")).strip()
        fallback = not partial and not draft
        if fallback:
            self.translation_fallbacks += 1
        with self.final_lock:
            self.finalized.add(sentence_id)
        reason = str(reason).replace(self.api_key, "[redacted]")[:300]
        emit({"event": "warning", "message": f"{self.args.mt_model}: {reason}",
              "sentence_id": result["sentence_id"]})
        emit({"event": "translation_failed", "sentence_id": result["sentence_id"],
              "reason": reason, "fallback": fallback})
        emit({"event": "result", "sentence_id": result["sentence_id"],
              "translation": translated, "translation_final": True,
              "fallback": fallback, "partial": bool(partial)})

    def _translate_loop(self) -> None:
        while not self.mt_stop.is_set() or not self.pending.empty():
            try:
                result = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            sentence_id = str(result["sentence_id"])
            with self.final_lock:
                self.finalizing.add(sentence_id)
            emit({"event": "translation_started", "sentence_id": result["sentence_id"]})
            attempt = 0
            try:
                while attempt < MT_ATTEMPTS:
                    attempt += 1
                    try:
                        translated = self.translate(str(result["source"]), sentence_id)
                        self.draft_cache.pop(sentence_id, None)
                        with self.final_lock:
                            self.finalized.add(sentence_id)
                            emit({"event": "result", "sentence_id": result["sentence_id"],
                                  "translation": translated, "translation_final": True})
                        break
                    except runtime_safety.PartialTranslation as exc:
                        self.draft_cache.pop(sentence_id, None)
                        with self.final_lock:
                            self.finalized.add(sentence_id)
                            emit({"event": "result", "sentence_id": result["sentence_id"],
                                  "translation": exc.text, "translation_final": True,
                                  "partial": True})
                        break
                    except urllib.error.HTTPError as exc:
                        reason = f"HTTP {exc.code}: {exc.read(2048).decode('utf-8', errors='replace')}"
                        if exc.code in {400, 401, 403, 404}:
                            self._translation_failed(result, reason)
                            self.error = (
                                f"{self.args.mt_model} configuration or authorization failed; stopping."
                            )
                            self.mt_fatal.set()
                            emit({"event": "fatal_error", "message": self.error})
                            return
                        if exc.code not in TRANSIENT_HTTP or attempt >= MT_ATTEMPTS:
                            self._translation_failed(result, reason)
                            break
                        time.sleep(0.25 * attempt)
                    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                        reason = f"{type(exc).__name__}: {exc}"
                        if attempt >= MT_ATTEMPTS:
                            self._translation_failed(result, reason)
                            break
                        time.sleep(0.25 * attempt)
                    except Exception as exc:
                        self._translation_failed(result, f"{type(exc).__name__}: {exc}")
                        break
            finally:
                self.pending.task_done()

    def close(self) -> None:
        super().close()
        self.mt_stop.set()
        self.draft_stop.set()
        if self.mt_thread:
            self.mt_thread.join(timeout=10)
        if self.draft_thread:
            self.draft_thread.join(timeout=3)
        emit({
            "event": "usage_summary", "task_id": self.task_id,
            "asr_model": self.args.model, "translation_model": self.args.mt_model,
            "audio_sent_seconds": round(self.audio_bytes / 32000, 3),
            "asr_billed_seconds": self.asr_duration,
            "asr_input_tokens": getattr(self, "asr_input_tokens", 0),
            "asr_output_tokens": getattr(self, "asr_output_tokens", 0),
            "translation_requests": self.mt_requests, "translation_failures": self.mt_failures,
            "translation_queued": self.translation_queued,
            "translation_fallbacks": self.translation_fallbacks,
            "draft_requests": self.draft_requests, "draft_failures": self.draft_failures,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "draft_input_tokens": self.draft_input_tokens,
            "draft_output_tokens": self.draft_output_tokens,
            "vocabulary_terms": len(self.vocabulary),
            "hotwords": self.hotword_config.summary() if self.hotword_config else None,
        })


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    work_dir = Path(args.log_file).expanduser().parent if args.log_file else os.environ.get(
        "LIVE_INTERPRETER_WORK_DIR", ""
    )
    runtime_safety.install("qwen-asr", work_dir)
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
    raise SystemExit(runtime_safety.guard("qwen-asr", main))
