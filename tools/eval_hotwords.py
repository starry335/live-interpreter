#!/usr/bin/env python3
"""Offline before/after evaluation for hotword-aware subtitle translation.

It reads the offline Mugen project's already-translated cues
(``translation.json`` -> ``lines[].text_ja/text_zh``), translates the
Japanese source twice with Qwen-MT (baseline vs. hotwords), and reports
person-name accuracy, forbidden-name hits, chrF, and regression examples.

Example::

    python tools/eval_hotwords.py \
      --offline-root /path/to/new-chat \
      --hotwords hotwords/yumemita_bangdream.txt \
      --env-file /path/to/new-chat/.env \
      --hotword-limit 60 --general-limit 20 \
      --output runtime/hotword_eval.json

The script is intentionally dependency-free (stdlib only) and works on
Windows, macOS, and Linux.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hotwords  # noqa: E402


NAME_ALIASES: Dict[str, Tuple[str, ...]] = {
    "阿拉蕾": ("阿拉蕾", "仲町阿拉蕾"),
    "由乃": ("由乃", "千石由乃"),
    "由乃亲": ("由乃亲",),
    "野乃花": ("野乃花", "宮永野乃花", "宫永野乃花", "野乃酱"),
    "律": ("律",),
    "都子": ("都子",),
}

EXTRA_FORBIDDEN = (
    "战国由乃", "戦国由乃", "拉卡町奥露蕾", "拉卡马奇奥尔雷",
    "野乃香", "野乃美", "乃乃花", "莉津", "理津", "美代子",
    "优乃", "悠乃", "柚乃", "由乃民", "暗属性",
)


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_endpoint(env: Dict[str, str]) -> str:
    workspace = env.get("DASHSCOPE_WORKSPACE_ID", "").strip()
    base = env.get("DASHSCOPE_BASE_URL", "").strip()
    if base:
        host = re.sub(r"^https?://", "", base).split("/", 1)[0]
    elif workspace:
        host = f"{workspace}.cn-beijing.maas.aliyuncs.com"
    else:
        host = "dashscope.aliyuncs.com"
    return f"https://{host}/compatible-mode/v1/chat/completions"


def translate(
    text: str, api_key: str, url: str, model: str,
    terms: Iterable[hotwords.Term], ssl_context, timeout: float = 30.0,
) -> str:
    options: Dict[str, object] = {"source_lang": "ja", "target_lang": "zh"}
    term_list = [{"source": term.source, "target": term.target} for term in terms]
    if term_list:
        options["terms"] = term_list
    body = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "translation_options": options,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
                data = json.loads(response.read())
            return str(data["choices"][0]["message"]["content"] or "").strip()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(2.0 * (attempt + 1))
                continue
            detail = exc.read(512).decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    raise RuntimeError("translation retries exhausted")


def chrf(hypothesis: str, reference: str, max_n: int = 4, beta: float = 2.0) -> float:
    """Character n-gram F-score (chrF), dependency-free."""
    hyp = re.sub(r"\s+", "", hypothesis or "")
    ref = re.sub(r"\s+", "", reference or "")
    if not hyp or not ref:
        return 0.0
    precisions = []
    recalls = []
    for n in range(1, max_n + 1):
        hyp_grams = Counter(hyp[i:i + n] for i in range(max(0, len(hyp) - n + 1)))
        ref_grams = Counter(ref[i:i + n] for i in range(max(0, len(ref) - n + 1)))
        overlap = sum((hyp_grams & ref_grams).values())
        total_hyp = max(1, sum(hyp_grams.values()))
        total_ref = max(1, sum(ref_grams.values()))
        precisions.append(overlap / total_hyp)
        recalls.append(overlap / total_ref)
    if not precisions:
        return 0.0
    precision = sum(precisions) / len(precisions)
    recall = sum(recalls) / len(recalls)
    if precision == 0.0 and recall == 0.0:
        return 0.0
    beta_sq = beta * beta
    return (1 + beta_sq) * precision * recall / (beta_sq * precision + recall)


def expected_names(reference: str) -> List[str]:
    names = []
    for name in NAME_ALIASES:
        if name not in (reference or ""):
            continue
        # 由乃亲 contains 由乃; count the honorific once, not both.
        if name == "由乃" and "由乃亲" in reference:
            continue
        names.append(name)
    return names


def name_hit(name: str, text: str) -> bool:
    return any(alias in (text or "") for alias in NAME_ALIASES[name])


def source_name_map(config: hotwords.HotwordConfig) -> Dict[str, str]:
    mapping = {}
    for term in config.mt_terms():
        for canonical, aliases in NAME_ALIASES.items():
            if term.target in aliases:
                mapping[term.source.casefold()] = canonical
    return mapping


def names_in_source(source: str, mapping: Dict[str, str]) -> List[str]:
    lowered = (source or "").casefold()
    return sorted({name for surface, name in mapping.items() if surface and surface in lowered})


def usable_cue(source: str, reference: str) -> bool:
    if len(source) < 4 or len(reference) < 2:
        return False
    # Some offline cues are misaligned (a 1-5 char ASR fragment paired with a
    # whole sentence); they cannot be scored fairly.
    return len(reference) <= max(30, len(source) * 4)


def collect_lines(offline_root: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    seen = set()
    pattern = "data/simple/tasks/*/highlight_translation_v1/*/translation.json"
    for path in sorted(offline_root.glob(pattern)):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for line in document.get("lines") or []:
            source = str(line.get("text_ja") or "").strip()
            reference = str(line.get("text_zh") or "").strip()
            if not usable_cue(source, reference):
                continue
            key = (source, reference)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "id": f"{path.parent.parent.parent.name}/{path.parent.name}/{line.get('cue_id') or line.get('index')}",
                "source": source,
                "reference": reference,
                "speaker": line.get("speaker"),
                "human_gold": bool(line.get("human_gold")),
            })
    return rows


def select_rows(
    rows: List[Dict[str, object]], config: hotwords.HotwordConfig,
    hotword_limit: int, general_limit: int, seed: int,
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    mapping = source_name_map(config)
    surfaces = [
        term.source.casefold() for term in config.mt_terms()
        if len(term.source.strip()) >= 2
    ]
    named: List[Dict[str, object]] = []
    general: List[Dict[str, object]] = []
    for row in rows:
        source = str(row["source"])
        reference = str(row["reference"])
        if not usable_cue(source, reference):
            continue
        lowered = source.casefold()
        has_surface = any(surface and surface in lowered for surface in surfaces)
        has_reference_name = any(name in reference for name in NAME_ALIASES)
        if has_surface or has_reference_name:
            named.append(row)
        else:
            general.append(row)
    rng = random.Random(seed)
    rng.shuffle(named)
    named.sort(key=lambda item: (not item["human_gold"],))
    named = named[:hotword_limit]
    rng.shuffle(general)
    general = general[:general_limit]
    return named, general


def evaluate(
    rows: List[Dict[str, object]], config: hotwords.HotwordConfig,
    api_key: str, url: str, model: str, sleep: float,
) -> List[Dict[str, object]]:
    ssl_context = ssl.create_default_context()
    forbidden = [term.source for term in config.forbidden] + list(EXTRA_FORBIDDEN)
    mapping = source_name_map(config)
    results = []
    for index, row in enumerate(rows, 1):
        source = str(row["source"])
        reference = str(row["reference"])
        terms = config.select_mt_terms(source)
        baseline = translate(source, api_key, url, model, [], ssl_context)
        if terms:
            time.sleep(sleep)
            treatment_raw = translate(source, api_key, url, model, terms, ssl_context)
            treatment = config.postprocess(treatment_raw)
        else:
            # No term can match: reuse the baseline and only exercise the
            # deterministic output guard. Avoids paying twice for noise.
            treatment_raw = baseline
            treatment = config.postprocess(baseline)
        expected = expected_names(reference)
        record = {
            **row,
            "terms": [term.source for term in terms],
            "baseline": baseline,
            "treatment": treatment,
            "treatment_raw": treatment_raw,
            "expected_names": expected,
            "names_baseline": {name: name_hit(name, baseline) for name in expected},
            "names_treatment": {name: name_hit(name, treatment) for name in expected},
            "forbidden_baseline": sorted({bad for bad in forbidden if bad and bad in baseline}),
            "forbidden_treatment": sorted({bad for bad in forbidden if bad and bad in treatment}),
            "chrf_baseline": round(chrf(baseline, reference), 4),
            "chrf_treatment": round(chrf(treatment, reference), 4),
            "source_names": names_in_source(source, mapping),
            "added_names": sorted({
                name for name in NAME_ALIASES
                if name_hit(name, treatment)
                and not name_hit(name, reference)
                and name not in names_in_source(source, mapping)
            }),
        }
        record["delta"] = round(record["chrf_treatment"] - record["chrf_baseline"], 4)
        results.append(record)
        print(
            f"[{index}/{len(rows)}] {row['id']} chrF {record['chrf_baseline']:.3f} -> "
            f"{record['chrf_treatment']:.3f} | {source[:40]}",
            flush=True,
        )
        time.sleep(sleep)
    return results


def summarize(results: List[Dict[str, object]]) -> Dict[str, object]:
    named = [row for row in results if row["expected_names"]]
    total_names = sum(len(row["expected_names"]) for row in named)
    base_hits = sum(sum(row["names_baseline"].values()) for row in named)
    treat_hits = sum(sum(row["names_treatment"].values()) for row in named)
    improved = sum(1 for row in results if row["delta"] > 0.005)
    regressed = sum(1 for row in results if row["delta"] < -0.005)
    return {
        "segments": len(results),
        "named_segments": len(named),
        "expected_name_occurrences": total_names,
        "name_accuracy_baseline": round(base_hits / total_names, 4) if total_names else None,
        "name_accuracy_treatment": round(treat_hits / total_names, 4) if total_names else None,
        "forbidden_baseline": sum(1 for row in results if row["forbidden_baseline"]),
        "forbidden_treatment": sum(1 for row in results if row["forbidden_treatment"]),
        "avg_chrf_baseline": round(sum(row["chrf_baseline"] for row in results) / max(1, len(results)), 4),
        "avg_chrf_treatment": round(sum(row["chrf_treatment"] for row in results) / max(1, len(results)), 4),
        "improved_segments": improved,
        "regressed_segments": regressed,
        "added_name_segments": sum(1 for row in results if row["added_names"]),
    }


def examples(results: List[Dict[str, object]]) -> Dict[str, List[Dict[str, object]]]:
    name_fixes = []
    for row in results:
        before = row["names_baseline"]
        after = row["names_treatment"]
        fixed = [name for name in before if not before[name] and after.get(name)]
        if fixed:
            name_fixes.append({**row, "fixed_names": fixed})
    regressions = sorted(
        [row for row in results if row["delta"] < -0.02],
        key=lambda row: row["delta"],
    )[:10]
    added = [row for row in results if row["added_names"]][:10]
    general_low = sorted(
        [
            row for row in results
            if not row["expected_names"] and row["chrf_treatment"] < 0.5
        ],
        key=lambda row: row["chrf_treatment"],
    )[:10]
    return {
        "name_fixes": name_fixes[:15],
        "regressions": regressions,
        "added_names": added,
        "general_low": general_low,
    }


def markdown_report(summary: Dict[str, object], results: List[Dict[str, object]], config_path: str) -> str:
    payload = examples(results)
    lines = [
        "# Hotword translation evaluation",
        "",
        f"- hotwords: `{config_path}`",
        f"- segments: {summary['segments']}",
        f"- name accuracy: {summary['name_accuracy_baseline']} -> {summary['name_accuracy_treatment']} "
        f"({summary['expected_name_occurrences']} occurrences in {summary['named_segments']} segments)",
        f"- forbidden hits: {summary['forbidden_baseline']} -> {summary['forbidden_treatment']}",
        f"- avg chrF: {summary['avg_chrf_baseline']} -> {summary['avg_chrf_treatment']}",
        f"- improved / regressed: {summary['improved_segments']} / {summary['regressed_segments']}",
        f"- segments with an added name: {summary['added_name_segments']}",
        "",
        "## Name fixes",
        "",
    ]
    for row in payload["name_fixes"]:
        lines.extend([
            f"### {row['id']} ({', '.join(row['fixed_names'])})",
            f"- source: {row['source']}",
            f"- reference: {row['reference']}",
            f"- baseline: {row['baseline']}",
            f"- treatment: {row['treatment']}",
            "",
        ])
    lines.extend(["## Regressions (chrF delta < -0.02)", ""])
    for row in payload["regressions"]:
        lines.extend([
            f"### {row['id']} ({row['delta']})",
            f"- source: {row['source']}",
            f"- reference: {row['reference']}",
            f"- baseline: {row['baseline']}",
            f"- treatment: {row['treatment']}",
            "",
        ])
    lines.extend(["## Possible added names", ""])
    for row in payload["added_names"]:
        lines.extend([
            f"### {row['id']} ({', '.join(row['added_names'])})",
            f"- source: {row['source']}",
            f"- reference: {row['reference']}",
            f"- treatment: {row['treatment']}",
            "",
        ])
    lines.extend(["## General low-chrF samples (manual review)", ""])
    for row in payload["general_low"]:
        lines.extend([
            f"### {row['id']} (chrF {row['chrf_treatment']})",
            f"- source: {row['source']}",
            f"- reference: {row['reference']}",
            f"- output: {row['treatment']}",
            "",
        ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate hotword-aware translation on offline cues.")
    parser.add_argument("--offline-root", type=Path, help="Offline new-chat project root.")
    parser.add_argument("--dataset", type=Path, help="Pre-built JSONL with source/reference rows.")
    parser.add_argument("--hotwords", type=Path, default=ROOT / "hotwords" / "yumemita_bangdream.txt")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model", default="qwen-mt-plus")
    parser.add_argument("--hotword-limit", type=int, default=60)
    parser.add_argument("--general-limit", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261101)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--output", type=Path, default=ROOT / "runtime" / "hotword_eval.json")
    parser.add_argument("--markdown", type=Path, default=ROOT / "runtime" / "hotword_eval.md")
    args = parser.parse_args()

    if args.env_file:
        load_env(args.env_file)
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        parser.error("DASHSCOPE_API_KEY is not set; pass --env-file or export it")
    env = {key: os.environ.get(key, "") for key in ("DASHSCOPE_WORKSPACE_ID", "DASHSCOPE_BASE_URL")}
    url = api_endpoint(env)

    if args.dataset:
        rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif args.offline_root:
        rows = collect_lines(args.offline_root)
    else:
        parser.error("pass --offline-root or --dataset")
    if not rows:
        parser.error("no translation cues found")

    config = hotwords.load(args.hotwords)
    for warning in config.warnings:
        print(f"hotword warning: {warning}", file=sys.stderr)
    named, general = select_rows(rows, config, args.hotword_limit, args.general_limit, args.seed)
    selected = named + general
    print(
        f"selected {len(named)} named + {len(general)} general segments "
        f"({len(named) * 2 + len(general) * 2} paid requests)",
        flush=True,
    )
    results = evaluate(selected, config, api_key, url, args.model, args.sleep)
    summary = summarize(results)
    payload = {
        "summary": summary,
        "hotwords": config.summary(),
        "model": args.model,
        "examples": examples(results),
        "segments": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(markdown_report(summary, results, str(args.hotwords)), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
    print(f"wrote {args.output} and {args.markdown}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
