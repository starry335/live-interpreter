# Hotword files

`--hotwords` accepts a UTF-8 text file or a `kb-terms/v1` JSON file. The
launcher exposes the same file picker as “热词表”.

## Text format

```text
# full-line comments only (# at the start of the line)
あられ => 阿拉蕾        # ASR term + translation term
ゆのち => 由乃亲
ユノ                    # ASR-only term
~戦国ユーノ => 由乃      # translation-only alias (ASR skips it)
!战国由乃 => 由乃        # rewrite forbidden output to the canonical name
```

Optional sections (`[hotwords]`, `[aliases]`, `[asr]`, `[forbidden]`) make
long lists easier to read. `=>`, `→`, `->`, and a tab are accepted as the
separator.

## Routing and limits

| Stage | Input | Limit |
|---|---|---|
| Paraformer `speech-biasing` | `[hotwords]` + `[asr]` surfaces | 500 terms; ≤15 chars per non-ASCII term |
| Qwen-MT `translation_options.terms` | terms found in the current sentence | 48 terms per request |
| LiveTranslate `translation.corpus.phrases` | `[hotwords]` + `[aliases]` | 200 source->target pairs |
| General chat models | prompt glossary block | 2400 chars |
| Output guard | `[forbidden]` rewrites | — |

ASR vocabulary creation is cached under `%LOCALAPPDATA%\LiveInterpreter`
(Windows) or `~/.cache/live-interpreter` (macOS/Linux), keyed by host,
target model, and the exact term set. A lost cache is recovered by listing
an existing vocabulary with the same prefix before creating a new one.

## Example data

`yumemita_bangdream.txt` is exported from the offline Mugen translation
project (`glossary/kb_terms.json` + `modules/hotwords.py`). It deliberately
uses the short names used in actual streams (阿拉蕾, 由乃亲, 野乃花, 律,
都子) and keeps full names only where the offline glossary needs them.
Regenerate with:

```bash
python tools/export_offline_hotwords.py \
  --offline-root /path/to/new-chat \
  --output hotwords/yumemita_bangdream.txt
```

The `~` aliases include ASR misrecognitions such as `戦国ユーノ` and
`ラカマチオルレ`; they must never be registered as ASR hotwords, because
speech-biasing would then reinforce the wrong surface.
