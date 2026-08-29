import json
import re
import sys
import unicodedata
from bisect import bisect_left
from pathlib import Path

from rapidfuzz.distance import Levenshtein


STAMP = re.compile(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)")


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(c for c in text if c.isalnum() or "\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff")


def milliseconds(groups) -> int:
    h, m, s, ms = map(int, groups)
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def stamp(seconds: float) -> str:
    value = max(0, round(seconds * 1000))
    h, value = divmod(value, 3_600_000)
    m, value = divmod(value, 60_000)
    s, ms = divmod(value, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def read_srt(path: Path):
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip()):
        lines = block.splitlines()
        match = STAMP.fullmatch(lines[1].strip())
        if len(lines) < 4 or not match:
            raise ValueError(f"Invalid bilingual SRT block: {block[:80]!r}")
        cues.append(
            {
                "source": lines[2].strip(),
                "translation": " ".join(line.strip() for line in lines[3:] if line.strip()),
                "old_start": milliseconds(match.groups()[:4]) / 1000,
                "old_end": milliseconds(match.groups()[4:]) / 1000,
            }
        )
    return cues


def build_stream(data):
    words = []
    chars = []
    char_words = []
    for segment in data["segments"]:
        segment_words = segment.get("words") or [
            {"start": segment["start"], "end": segment["end"], "word": segment["text"]}
        ]
        for item in segment_words:
            text = normalized(item["word"])
            if (
                not text
                or item.get("start") is None
                or item.get("end") is None
                or float(item["end"]) <= float(item["start"])
            ):
                continue
            index = len(words)
            start_char = len(chars)
            chars.extend(text)
            char_words.extend([index] * len(text))
            words.append(
                {
                    "start": float(item["start"]),
                    "end": float(item["end"]),
                    "text": item["word"],
                    "start_char": start_char,
                    "end_char": len(chars),
                }
            )
    return words, "".join(chars), char_words


def align(cues, words, stream, char_words):
    old_chars = []
    cue_ranges = []
    for cue in cues:
        start = len(old_chars)
        old_chars.extend(normalized(cue["source"]))
        cue_ranges.append((start, len(old_chars)))
    old_stream = "".join(old_chars)
    mapping = [0] * len(old_stream)
    exact = [False] * len(old_stream)
    for opcode in Levenshtein.opcodes(old_stream, stream):
        tag, src_start, src_end, dst_start, dst_end = opcode
        src_size = src_end - src_start
        dst_size = dst_end - dst_start
        for offset in range(src_size):
            if dst_size:
                destination = dst_start + min(dst_size - 1, offset * dst_size // max(1, src_size))
            else:
                destination = min(dst_start, len(stream) - 1)
            mapping[src_start + offset] = destination
            exact[src_start + offset] = tag == "equal"

    results = []
    for index, (cue, (src_start, src_end)) in enumerate(zip(cues, cue_ranges)):
        if index < 3 or src_end == src_start:
            results.append({**cue, "start": cue["old_start"], "end": cue["old_end"], "score": 0, "heard": cue["source"]})
            continue

        first_word = char_words[min(mapping[src_start], len(char_words) - 1)]
        last_word = char_words[min(mapping[src_end - 1], len(char_words) - 1)]
        if last_word < first_word:
            last_word = first_word
        selected = words[first_word : last_word + 1]
        results.append(
            {
                **cue,
                "start": selected[0]["start"],
                "end": selected[-1]["end"],
                "score": round(100 * sum(exact[src_start:src_end]) / (src_end - src_start), 1),
                "heard": "".join(word["text"] for word in selected).strip(),
            }
        )

    for previous, current in zip(results[2:], results[3:]):
        if current["start"] < previous["end"]:
            boundary = (current["start"] + previous["end"]) / 2
            previous["end"] = max(previous["start"] + 0.08, boundary)
            current["start"] = min(current["end"] - 0.08, boundary + 0.001)
    return results


def write_srt(results, path: Path) -> None:
    anchors = []
    for item in results:
        if item["score"] >= 55:
            old_mid = (item["old_start"] + item["old_end"]) / 2
            new_mid = (item["start"] + item["end"]) / 2
            anchors.append((old_mid, new_mid - old_mid))
    anchor_times = [item[0] for item in anchors]

    def shift_at(value: float) -> float:
        index = bisect_left(anchor_times, value)
        if index == 0:
            return anchors[0][1]
        if index == len(anchors):
            return anchors[-1][1]
        left_time, left_shift = anchors[index - 1]
        right_time, right_shift = anchors[index]
        weight = (value - left_time) / (right_time - left_time)
        return left_shift + (right_shift - left_shift) * weight

    for index, item in enumerate(results):
        if index >= 3 and item["score"] < 55:
            item["start"] = item["old_start"] + shift_at(item["old_start"])
            item["end"] = item["old_end"] + shift_at(item["old_end"])

    for previous, current in zip(results, results[1:]):
        if current["start"] < previous["end"]:
            boundary = (current["start"] + previous["end"]) / 2
            previous["end"] = max(previous["start"] + 0.08, boundary)
            current["start"] = max(previous["end"] + 0.001, min(current["end"] - 0.08, boundary + 0.001))
        if current["end"] <= current["start"]:
            current["end"] = current["start"] + 0.08
    results[-1]["end"] = 2180.150

    replacements = {
        "無限大ニュータイプ": "夢限大みゅーたいぷ",
        "無限大ミュータイプ": "夢限大みゅーたいぷ",
        "夢見たラジオ": "ゆめ∞みたラジオ",
        "ミネツキリツ": "峰月律",
        "藤宮子": "藤都子",
        "フジミヤコ": "藤都子",
        "バンドリー": "BanG Dream!",
    }
    blocks = []
    for number, item in enumerate(results, 1):
        source = item["source"]
        for old, new in replacements.items():
            source = source.replace(old, new)
        source = re.sub(r"\s+", " ", source).strip()
        blocks.append(
            f"{number}\n{stamp(item['start'])} --> {stamp(item['end'])}\n{source}\n{item['translation']}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")


def main() -> None:
    source, transcript, output = map(Path, sys.argv[1:4])
    cues = read_srt(source)
    words, stream, char_words = build_stream(json.loads(transcript.read_text(encoding="utf-8")))
    results = align(cues, words, stream, char_words)
    assert len(results) == len(cues) and all(item["end"] > item["start"] for item in results)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if len(sys.argv) > 4:
        write_srt(results, Path(sys.argv[4]))
    scores = sorted(item["score"] for item in results[3:])
    print(f"cues={len(results)} median={scores[len(scores)//2]:.1f} below60={sum(s < 60 for s in scores)}")


if __name__ == "__main__":
    main()
