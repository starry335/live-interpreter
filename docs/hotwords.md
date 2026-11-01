# Hotword pipeline

## Local integration (2026-11-01)

Imported from [PR #1 by HudsonHuang](https://github.com/starry335/live-interpreter/pull/1),
head `d49b7d597663b1a8226d73ec40dbcd3df944ea9b`. The evaluation below is the
contributor's reported result; this local integration has not repeated paid API evaluation.

The PR's original glossary is bundled into both executables and enabled by default,
including its ASR terms, aliases, and forbidden rewrites. Personal terms are prepended
and override built-in entries with the same source. The editor shows only personal
entries and offers separate built-in and overall enable switches; existing custom
files are preserved. Clearing personal entries does not clear the built-in glossary.

The existing “热词记忆” editor and `%LOCALAPPDATA%/LiveInterpreter/hotwords.json`
remain supported through `--hotwords-file`. An optional “外部热词表” takes precedence
through `--hotwords`; choose “恢复默认” to restore the built-in/personal settings.
Both flags use the same hotword engine. The external selector is per launch;
`LIVE_INTERPRETER_HOTWORDS` can supply a persistent external default.

Integration adjustments: general models receive only sentence-matched terms;
the local vocabulary cache is separated by a hash of the API key (no key is stored),
term order does not change cache identity, and the initial vocabulary preparation
has a larger startup allowance. ASCII ASR terms use the provider's seven-word limit;
non-ASCII terms retain the 15-character limit. No vocabulary is created during tests.

This document describes the feature added on the `feat/hotwords` branch and
the offline evaluation used to validate it.

## Problem

The default `paraformer-mt` chain had no hotword support at all:

- `paraformer-realtime-v2` `run-task` had no `vocabulary_id`;
- Qwen-MT requests had only `translation_options.source_lang/target_lang`;
- LiveTranslate sessions had only `translation.language`;
- the shipped `corpus.phrases` was never read by code.

The offline Mugen translator already has the right glossary for the actual
streams: short names and honorifics such as `あられ → 阿拉蕾`,
`ゆのち → 由乃亲`, `ののか → 野乃花`, plus ASR misrecognition aliases such as
`戦国ユーノ → 由乃` and `ラカマチオルレ → 阿拉蕾`.

## Design

One `--hotwords` file drives five stages:

1. **ASR vocabulary** — canonical Japanese surfaces are registered through
   the `speech-biasing` HTTP API (`create_vocabulary`), cached by
   host/model/term-set, and passed as `vocabulary_id` in `run-task`.
2. **Qwen-MT terms** — for each sentence, only the terms that actually occur
   in it are sent (longest match wins, at most 48). Sending all 100+ terms
   made Qwen-MT ignore or misuse them in testing.
3. **LiveTranslate corpus** — the same pairs are sent as
   `translation.corpus.phrases` (translation side only).
4. **General chat models** — a bounded glossary block is added to the system
   prompt.
5. **Output guard** — `!wrong => canonical` lines rewrite forbidden output
   (for example `战国由乃 → 由乃`) after generation.

ASR failure is non-fatal: if vocabulary creation fails, the session continues
with translation terms and the output guard.

Key modules:

- [`hotwords.py`](../hotwords.py) — parsing, limits, term selection,
  vocabulary creation/cache, postprocessing.
- [`paraformer_mt_worker.py`](../paraformer_mt_worker.py) — vocabulary_id,
  `translation_options.terms`, prompt glossary, output guard.
- [`qwen_livetranslate_worker.py`](../qwen_livetranslate_worker.py) —
  `translation.corpus.phrases` and output guard.
- [`gummy_realtime_worker.py`](../gummy_realtime_worker.py) — output guard
  only; Gummy has no hotword API.
- [`live_interpreter_launcher.py`](../live_interpreter_launcher.py) —
  “热词表” picker and `--hotwords` command-line forwarding.

## Data

`hotwords/yumemita_bangdream.txt` is exported from the offline project:

```bash
python tools/export_offline_hotwords.py \
  --offline-root /path/to/new-chat \
  --output hotwords/yumemita_bangdream.txt
```

It contains 71 ASR/translation surfaces, 55 translation-only aliases, and 35
forbidden rewrites. The short names are deliberately included; full names are
not the only surfaces. ASR misrecognitions are marked with `~` and are
**never** registered in the ASR vocabulary (that would reinforce the wrong
surface).

## Offline evaluation

Script: [`tools/eval_hotwords.py`](../tools/eval_hotwords.py).

Data source: the offline project's already-translated cues
(`data/simple/tasks/*/highlight_translation_v1/*/translation.json`,
`lines[].text_ja / text_zh`). We deduplicated cues, removed short/misaligned
fragments, selected 80 name-bearing cues plus 20 general cues, and translated
each source twice with `qwen-mt-plus`:

- **baseline**: no terms, no output guard;
- **treatment**: sentence-matched terms + output guard.

Command:

```bash
python tools/eval_hotwords.py \
  --offline-root /path/to/new-chat \
  --env-file /path/to/new-chat/.env \
  --hotword-limit 80 --general-limit 20 \
  --output runtime/hotword_eval.json \
  --markdown runtime/hotword_eval.md
```

### Result (2026-11-01, 100 cues)

| Metric | Baseline | With hotwords |
|---|---:|---:|
| Person-name accuracy (78 occurrences) | 25.6% | **75.6%** |
| Forbidden-name segments | 5 | **0** |
| Average chrF | 0.2515 | **0.3158** |
| Improved / regressed segments | — | 42 / 12 |
| Segments with an added name | — | 0 |

Representative fixes:

| Source (ASR) | Baseline | Treatment | Reference |
|---|---|---|---|
| `ラカマチオルレですはい` | 我是拉卡马奇奥尔雷 | 我是阿拉蕾 | 我是仲町阿拉蕾 |
| `はい戦国ユーノですは` | 我是战国尤诺 | 我是由乃 | 我是千石由乃 |
| `じゃあ律とあられか` | 那律和霰吗 | 那就是律和阿拉蕾了 | 那就律和阿拉蕾吧 |
| `あられの頭ぐにょぐにょ…` | 艾拉的头… | 阿拉蕾的头… | 阿拉蕾的头… |
| `ののちゃんがいないので` | 因为小诺不在 | 因为没有野乃酱（原为 `のの酱`，已修正为目标不含假名） | 野乃花不在 |

### Reading the regressions

The 12 chrF drops are not all semantic regressions. Manual review:

- `莉酱 → 律酱` is a name fix; chrF drops because the rest of the sentence
  is reworded by Qwen-MT.
- `あられ` / `ゆのち` fixes often keep the correct name but change the
  surrounding sentence, which lowers chrF.
- One real context issue: `バンドリハ` (band rehearsal) matches the term
  `バンドリ` and becomes `BanG Dream!`, while the reference uses “乐队”.
- Truncated cues such as `ビオラにビ` can over-apply a name.

### Remaining misses

Most of the 19 missed name occurrences (17 segments) are source-side limits,
not translation limits:

- the source ASR text does not contain the name (`なんだこの命`,
  `宮永どこだ`, `えりっつが`, `りーちゃんなの`, `新井さん都…`);
- the cue is misaligned with the reference;
- ASR variants not present in the offline alias table (`命` for `ゆのち`,
  `宮永` alone, `荒井/新井` for `あられ`, `りーちゃん/りっつ` for `律`).

Those are exactly what the ASR vocabulary and alias expansion are for; the
translation-only evaluation cannot recover a name that never appears in the
source.

### ASR-side check

A real `paraformer-realtime-v2` run with the exported file successfully:

1. created a 69-term `speech-biasing` vocabulary;
2. passed `vocabulary_id` in `run-task` and received recognition results
   (the worker logged `hotword_vocabulary`, then `ready`).

On a 16-second clip around the introduction, the vocabulary changed the
recognition of `ラカマチオルレ` (baseline `ロこンはち上るです` → hotword
`ロカはジアロルです`), but did not fully recover the name. The clip has
background music and is a hard case; this is not yet evidence of a large ASR
gain. The translation aliases still fix the resulting Chinese subtitle, as
shown in the table above.

## Tests

```bash
python -m unittest -v test_hotwords test_eval_hotwords \
  test_paraformer_hotwords test_paraformer_mt_worker \
  test_qwen_livetranslate_worker
```

`test_hotwords.py` covers parsing, limits, JSON input, sentence-aware term
selection, output guard, cache reuse, and lost-cache recovery with a mocked
API. `test_paraformer_hotwords.py` covers `vocabulary_id`, failure fallback,
Qwen-MT term selection, prompt glossary, forbidden rewrites, bad-file startup,
and launcher command forwarding.
