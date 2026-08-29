from __future__ import annotations

import argparse
import audioop
import json
import os
import sys
import wave
from pathlib import Path
from typing import Any, Dict, Optional


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


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
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def clean_text(text: str) -> str:
    text = str(text).replace("\ufffd", "")
    return " ".join(text.replace("\n", " ").split()).strip()


def looks_like_garbage(text: str) -> bool:
    text = str(text).strip()
    if not text:
        return False
    if "\ufffd" in text:
        return True
    meaningful = sum(ch.isalnum() or "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in text)
    return len(text) >= 3 and meaningful == 0


class NemotronTranscriber:
    def __init__(self, model: str, language: str, device: str, max_buffer_seconds: float) -> None:
        self.model_name = model
        self.language = language
        self.device = device
        self.max_buffer_seconds = max_buffer_seconds
        self.mode = "direct"
        self.call_style: Optional[str] = None
        self.device_target = "cpu"
        self.processor = None
        self.model = None
        self.pipe = None
        self.torch = None
        self._load()

    def _load(self) -> None:
        import torch
        from transformers import AutoProcessor

        self.torch = torch
        use_cuda = self.device.startswith("cuda") and torch.cuda.is_available()
        dtype = torch.float16 if use_cuda else torch.float32
        self.device_target = self.device if use_cuda else "cpu"
        eprint(f"Loading Nemotron ASR model={self.model_name}, device={self.device_target}, language={self.language}")
        self.processor = AutoProcessor.from_pretrained(self.model_name, trust_remote_code=True)
        try:
            from transformers import AutoModelForRNNT

            self.model = AutoModelForRNNT.from_pretrained(
                self.model_name,
                torch_dtype=dtype,
                trust_remote_code=True,
            ).to(self.device_target)
            self.model.eval()
            self.mode = "direct"
            eprint("Nemotron direct RNNT loader is active.")
            return
        except Exception as exc:
            eprint(f"Direct RNNT loader unavailable, trying pipeline: {exc}")

        from transformers import pipeline

        device_index = 0 if use_cuda else -1
        self.pipe = pipeline(
            "automatic-speech-recognition",
            model=self.model_name,
            device=device_index,
            torch_dtype=dtype if use_cuda else None,
            trust_remote_code=True,
        )
        self.mode = "pipeline"
        eprint("Nemotron ASR pipeline loader is active.")

    def transcribe(self, path: Path, language: Optional[str] = None) -> str:
        language = language or self.language
        audio = read_wav_float32_mono16k(path)
        if audio.size == 0:
            return ""
        if self.mode == "direct":
            return self._transcribe_direct(audio, language)
        return self._transcribe_pipeline(path, audio, language)

    def _transcribe_direct(self, audio, language: str) -> str:
        assert self.processor is not None
        assert self.model is not None
        assert self.torch is not None

        # Keep this explicit because older cached environments may still have
        # torch 2.5; current setup installs torch 2.6 where this is naturally true.
        try:
            import transformers.masking_utils as masking_utils

            masking_utils._is_torch_greater_or_equal_than_2_6 = True
        except Exception:
            pass

        inputs = self.processor(
            audio,
            sampling_rate=16000,
            language=language,
            return_tensors="pt",
        )
        model_dtype = next(self.model.parameters()).dtype
        inputs = {
            key: (
                value.to(device=self.device_target, dtype=model_dtype)
                if hasattr(value, "is_floating_point") and value.is_floating_point()
                else value.to(self.device_target)
                if hasattr(value, "to")
                else value
            )
            for key, value in inputs.items()
        }
        with self.torch.inference_mode():
            generated = self.model.generate(**inputs, max_new_tokens=256)
        sequences = getattr(generated, "sequences", generated)
        durations = getattr(generated, "durations", None)
        if isinstance(sequences, dict):
            sequences = sequences.get("sequences", sequences)
        if durations is not None and hasattr(self.processor, "decode"):
            decoded_with_offsets = self.processor.decode(sequences, durations=durations, skip_special_tokens=True)
            if isinstance(decoded_with_offsets, tuple) and len(decoded_with_offsets) == 2:
                _, timestamp_batches = decoded_with_offsets
                if timestamp_batches:
                    text = clean_text("".join(item.get("token", "") for item in timestamp_batches[0]))
                    if text and not looks_like_garbage(text):
                        return text
        blank_id = getattr(self.processor, "blank_token_id", None)
        pad_id = getattr(getattr(self.processor, "tokenizer", None), "pad_token_id", None)
        if blank_id is not None and hasattr(sequences, "clone"):
            filtered = []
            for token_id in sequences[0].detach().cpu().tolist():
                if token_id not in (blank_id, pad_id):
                    filtered.append(token_id)
            if filtered:
                text = self.processor.tokenizer.decode(filtered, skip_special_tokens=True, group_tokens=False)
                text = clean_text(text)
                if not looks_like_garbage(text):
                    return text
        if hasattr(self.processor, "batch_decode"):
            decoded = self.processor.batch_decode(sequences, skip_special_tokens=True)
        else:
            decoded = self.processor.tokenizer.batch_decode(sequences, skip_special_tokens=True)
        text = clean_text(decoded[0] if decoded else "")
        return "" if looks_like_garbage(text) else text

    def _transcribe_pipeline(self, path: Path, audio, language: str) -> str:
        assert self.pipe is not None
        styles = [self.call_style] if self.call_style else [
            "language_kw",
            "generate_language",
            "generate_target_lang",
            "plain_path",
            "plain_audio",
        ]
        last_error: Optional[Exception] = None
        for style in styles:
            try:
                if style == "language_kw":
                    out = self.pipe(str(path), language=language)
                elif style == "generate_language":
                    out = self.pipe(str(path), generate_kwargs={"language": language})
                elif style == "generate_target_lang":
                    out = self.pipe(str(path), generate_kwargs={"target_lang": language})
                elif style == "plain_audio":
                    out = self.pipe({"array": audio, "sampling_rate": 16000})
                else:
                    out = self.pipe(str(path))
                self.call_style = style
                return self._extract_pipeline_text(out)
            except Exception as exc:
                last_error = exc
                if self.call_style:
                    break
        raise RuntimeError(f"All Nemotron pipeline call styles failed: {last_error}")

    @staticmethod
    def _extract_pipeline_text(out: Any) -> str:
        if isinstance(out, dict):
            return clean_text(out.get("text", ""))
        if isinstance(out, list):
            texts = []
            for item in out:
                if isinstance(item, dict):
                    texts.append(str(item.get("text", "")))
                else:
                    texts.append(str(item))
            return clean_text(" ".join(texts))
        return clean_text(str(out))


def emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persistent Nemotron ASR JSONL worker.")
    parser.add_argument("--model", default="nvidia/nemotron-3.5-asr-streaming-0.6b")
    parser.add_argument("--language", default="ja-JP")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-buffer-seconds", type=float, default=18.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
    try:
        transcriber = NemotronTranscriber(args.model, args.language, args.device, args.max_buffer_seconds)
        emit({"ready": True, "mode": transcriber.mode})
    except Exception as exc:
        emit({"ready": False, "error": str(exc)})
        return 1

    for line in sys.stdin:
        try:
            request = json.loads(line)
            command = request.get("cmd")
            if command == "close":
                emit({"ok": True})
                return 0
            if command != "transcribe":
                emit({"ok": False, "error": f"Unknown command: {command}"})
                continue
            path = Path(str(request.get("path", "")))
            language = str(request.get("language") or args.language)
            text = transcriber.transcribe(path, language=language)
            emit({"ok": True, "text": text})
        except Exception as exc:
            emit({"ok": False, "error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
