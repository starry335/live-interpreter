"""Create tightly timed Japanese-Chinese subtitles from WhisperX JSON."""
import argparse
import json
import os
import re
import time
import urllib.request
from pathlib import Path

FILLER = re.compile(r"^[あぁアァえぇエェうぅウゥおぉオォんンー〜～…、。！？!?っッ\s]+$")


def stamp(seconds):
    total = max(0, round(seconds * 1000))
    hours, total = divmod(total, 3_600_000)
    minutes, total = divmod(total, 60_000)
    seconds, milliseconds = divmod(total, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def build_cues(data):
    words = data["word_segments"]
    cues, current = [], []
    for word in words:
        if word.get("start") is None or word.get("end") is None:
            continue
        text = word["word"].strip()
        if not text:
            continue
        if current:
            previous = current[-1]
            elapsed = word["end"] - current[0]["start"]
            pause = word["start"] - previous["end"]
            if (len("".join(item["word"] for item in current)) >= 8 and pause >= 0.36) or elapsed >= 6.2 or len("".join(item["word"] for item in current)) >= 40:
                cues.append(current)
                current = []
        current.append(word)
        if len(current) >= 8 and text[-1:] in "。！？!?":
            cues.append(current)
            current = []
    if current:
        cues.append(current)
    return [{"id": index, "start": cue[0]["start"], "end": cue[-1]["end"], "ja": "".join(item["word"] for item in cue)} for index, cue in enumerate(cues)]


def is_long_filler(text):
    return len(re.sub(r"\s", "", text)) >= 6 and bool(FILLER.fullmatch(text))


def is_repetitive_artifact(text):
    text = re.sub(r"\s", "", text)
    repeated = re.search(r"(.{1,8})\1{3,}", text)
    return len(text) >= 4 and bool(repeated) and len(text) - len(repeated.group()) <= 7


def skip_translation(text):
    return len(re.sub(r"\s", "", text)) <= 1 or is_long_filler(text) or is_repetitive_artifact(text)


def display_translation(text):
    return text if len(re.sub(r"\s", "", text)) > 1 else ""


def ask_qwen(cues, previous, model, workspace, glossary):
    key = os.environ["DASHSCOPE_API_KEY"]
    payload = {
        "model": model,
        "enable_thinking": False,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Translate Japanese livestream subtitles into natural, concise Simplified Chinese. This is a multi-speaker conversation: use the neighboring lines to resolve who is being addressed, pronouns, turns, names, jokes and tone, but never add speaker labels. Keep listed Japanese proper nouns unchanged when they occur. Never explain. Return only a JSON object mapping each numeric id to its Chinese subtitle."},
            {"role": "system", "content": f"Proper-noun glossary: {glossary}"} if glossary else {"role": "system", "content": ""},
            {"role": "user", "content": json.dumps({"previous_context": previous, "items": [{"id": cue["id"], "ja": cue["ja"]} for cue in cues]}, ensure_ascii=False)},
        ],
    }
    request = urllib.request.Request(
        f"https://{workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions" if workspace else "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = json.loads(response.read())["choices"][0]["message"]["content"].strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
            translations = json.loads(text)
            return {int(key): value.strip() for key, value in translations.items() if str(key).isdigit()}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json", type=Path)
    parser.add_argument("output_srt", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--model", default="qwen3.6-plus")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--glossary", type=Path)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    glossary = "、".join(args.glossary.read_text(encoding="utf-8").splitlines()) if args.glossary else ""
    cues = build_cues(json.loads(args.input_json.read_text(encoding="utf-8")))
    if args.source_only:
        blocks = [f"{index + 1}\n{stamp(cue['start'])} --> {stamp(cue['end'])}\n{cue['ja']}" for index, cue in enumerate(cues)]
        args.output_srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")
        print(f"wrote {len(cues)} source cues: {args.output_srt}")
        return
    cache = json.loads(args.cache.read_text(encoding="utf-8")) if args.cache and args.cache.exists() else {}
    cache.update({str(cue["id"]): "" for cue in cues if skip_translation(cue["ja"])})
    if args.overrides:
        cache.update(json.loads(args.overrides.read_text(encoding="utf-8")))
    for offset in range(0, len(cues), args.batch_size):
        batch = [cue for cue in cues[offset : offset + args.batch_size] if str(cue["id"]) not in cache]
        if not batch:
            continue
        context = "".join(cue["ja"] for cue in cues[max(0, offset - 3) : offset])
        remaining = batch
        for attempt in range(3):
            translated = ask_qwen(remaining, context, args.model, args.workspace, glossary)
            expected = {cue["id"] for cue in remaining}
            received = {key: value for key, value in translated.items() if key in expected and value}
            cache.update({str(key): value for key, value in received.items()})
            if args.cache:
                args.cache.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            remaining = [cue for cue in remaining if cue["id"] not in received]
            if not remaining:
                break
        if remaining:
            raise ValueError(f"Translation response is missing IDs: {[cue['id'] for cue in remaining]}")
        if args.cache:
            args.cache.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"translated {min(offset + args.batch_size, len(cues))}/{len(cues)}", flush=True)
        time.sleep(0.2)
    blocks = []
    for cue in cues:
        translation = display_translation(cache[str(cue["id"])])
        if translation:
            blocks.append(f"{len(blocks) + 1}\n{stamp(cue['start'])} --> {stamp(cue['end'])}\n{cue['ja']}\n{translation}")
    args.output_srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")
    print(f"wrote {len(blocks)} cues: {args.output_srt}")


if __name__ == "__main__":
    main()
