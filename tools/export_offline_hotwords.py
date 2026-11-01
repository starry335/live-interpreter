#!/usr/bin/env python3
"""Export the offline Mugen glossary into Live Interpreter's hotword format.

The offline project keeps the authoritative JA -> ZH term data in
``glossary/kb_terms.json`` plus deterministic alias tables in
``modules/hotwords.py``.  This tool merges both into the plain-text format
accepted by ``--hotwords`` so the live application can use the same short
names (阿拉蕾 / 由乃亲 / 野乃花 / 律 / 都子) instead of a full-name-only list.

Usage::

    python tools/export_offline_hotwords.py \
        --offline-root /path/to/new-chat \
        --output hotwords/yumemita_bangdream.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def load_offline_module(root: Path):
    sys.path.insert(0, str(root))
    from modules import hotwords as offline  # noqa: WPS433 (intentional late import)
    return offline


def canonical_targets(normal: Dict[str, str]) -> List[str]:
    return sorted({value for value in normal.values() if value}, key=len, reverse=True)


def infer_target(source: str, normal: Dict[str, str], offline) -> str:
    for table in (offline.PERSON_JA_TO_ZH, offline.BRAND_WRONG_ZH, offline.FIXED_ZH):
        value = table.get(source)
        if value:
            return value
    for target in canonical_targets(normal):
        if target in source:
            return target
    return ""


def build(offline_root: Path) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    offline = load_offline_module(offline_root)
    kb_path = offline_root / "glossary" / "kb_terms.json"
    kb = json.loads(kb_path.read_text(encoding="utf-8"))

    # Known ASR misrecognitions / Chinese-only variants must not become
    # speech-biasing terms; they belong in [aliases] for translation only.
    move_to_aliases = {"戦国ユーノ", "宫永野乃花"}

    normal: Dict[str, str] = {}
    for section in ("person", "honorific", "works", "gags"):
        for source, target in (kb.get(section) or {}).items():
            if source in move_to_aliases:
                continue
            normal.setdefault(source, target)
    for source, target in offline.HONORIFIC_JA_TO_ZH.items():
        normal.setdefault(source, target)
    for source, target in offline.FIXED_ZH.items():
        normal.setdefault(source, target)
    # kb_terms has a typo here: "のの酱" leaves Japanese kana in a Chinese
    # subtitle.  The nickname still needs the Chinese given name 野乃.
    if normal.get("ののちゃん") == "のの酱":
        normal["ののちゃん"] = "野乃酱"

    aliases: Dict[str, str] = {}
    for source, target in offline.PERSON_JA_TO_ZH.items():
        if source not in normal:
            aliases.setdefault(source, target)
    for source in move_to_aliases:
        target = offline.PERSON_JA_TO_ZH.get(source) or kb.get("person", {}).get(source)
        if target:
            aliases.setdefault(source, target)
    # Hardcoded postprocess rule in the offline project (not present in the
    # alias tables): a common katakana ASR rendering of 仲町あられ.
    aliases.setdefault("ラカマチオルレ", "阿拉蕾")
    for source, target in offline.BRAND_ASR_JA.items():
        if source not in normal:
            aliases.setdefault(source, target)

    forbidden: Dict[str, str] = {}
    explicit = {"暗属性": "恶属性"}
    for source in offline.FORBIDDEN_ZH:
        if source in forbidden:
            continue
        target = explicit.get(source) or infer_target(source, normal, offline)
        if target and target != source:
            forbidden[source] = target
    for source, target in offline.BRAND_WRONG_ZH.items():
        forbidden.setdefault(source, target)

    return list(normal.items()), list(aliases.items()), list(forbidden.items())


def render(
    normal: List[Tuple[str, str]],
    aliases: List[Tuple[str, str]],
    forbidden: List[Tuple[str, str]],
) -> str:
    lines = [
        "# Live Interpreter hotword list — exported from the offline Mugen translator.",
        "# Source data: glossary/kb_terms.json + modules/hotwords.py (short names and ASR variants).",
        "# Format: source => target (ASR + translation); ~source => target (translation only);",
        "#         !wrong => canonical (rewrite forbidden output). Lines without an arrow are ASR-only.",
        "",
        "[hotwords]",
    ]
    lines.extend(f"{source} => {target}" for source, target in normal)
    lines.append("")
    lines.append("[aliases]")
    lines.extend(f"~{source} => {target}" for source, target in aliases)
    lines.append("")
    lines.append("[forbidden]")
    lines.extend(f"!{source} => {target}" for source, target in forbidden)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export offline glossary to Live Interpreter hotwords.")
    parser.add_argument("--offline-root", required=True, type=Path,
                        help="Path to the offline new-chat project (contains glossary/ and modules/).")
    parser.add_argument("--output", type=Path, default=Path("hotwords/yumemita_bangdream.txt"))
    args = parser.parse_args()

    if not (args.offline_root / "glossary" / "kb_terms.json").exists():
        parser.error(f"not an offline project root: {args.offline_root}")

    normal, aliases, forbidden = build(args.offline_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(normal, aliases, forbidden), encoding="utf-8")
    print(
        f"wrote {args.output}: {len(normal)} hotwords, {len(aliases)} aliases, "
        f"{len(forbidden)} forbidden rewrites"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
