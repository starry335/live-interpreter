# 用 gold-v0.3 测试流式翻译

gold-v0.3 是 bangdream-hotword-bench 的人工精标流式集：32 条完整高光、92.4 分钟、
16 kHz WAV + 人工修正中文 SRT。它同时评三件事：

- **回显节奏**：中文第一次出现离参考句结束多远；
- **翻译质量**：中文和人工译文的字符 F1；
- **热词**：参考句里的人名/乐队名有没有出现在中文里。

数据和评测脚本都在
[bangdream-hotword-bench](https://github.com/HudsonHuang/bangdream-hotword-bench)，
音频/字幕在
[Hugging Face](https://huggingface.co/datasets/zhongyihuang/bangdream-hotword-bench/tree/main/gold-v0.3)。

## 1. 下载数据

```bash
hf download zhongyihuang/bangdream-hotword-bench --repo-type dataset \
  --include "gold-v0.3/*" "data/gold_v03.jsonl" --local-dir data
```

目录：

```text
data/gold-v0.3/audio/*.wav    # 32 条，16 kHz mono
data/gold-v0.3/srt/*.srt      # 人工中文 SRT，cue 带 [speaker=...]
data/gold_v03.jsonl           # 32 条清单
```

## 2. 跑一段

`tools/replay_stream.py` 是仓库自带的实时回放入口：它把 WAV 按 1x 喂给
`qwen_asr_stream_worker.py`，并把每个 stdout 事件记上 `_recv_mono`。

以 2–5 分钟那段为例：

```powershell
$env:DASHSCOPE_API_KEY = "sk-..."

python tools/replay_stream.py `
  --wav "data/gold-v0.3/audio/9u5-VQG61So_hl04_11天热量结算：1律≈4由乃.wav" `
  --start 120 --seconds 180 --out runs/31_ref `
  -- --source-language ja --target-language zh `
     --model qwen-audio-3.1-asr-flash-streaming `
     --max-end-silence 200 --vad-model near_meeting_16k `
     --mt-model qwen3.8-max --mt-mode reference `
     --hotwords hotwords/yumemita_bangdream.txt
```

跑完整条片段时去掉 `--start/--seconds`。`--mt-mode` 可选：

| 模式 | 说明 | 2–5 分钟实测 |
|---|---|---|
| `reference` | 只给术语表（默认，成本最低） | p50 1.34 s / charF1 0.445 / 人名 92.3% / 2.74 元/h |
| `icl --icl-examples 2 --context-turns 1` | 小抄 + 1 轮上下文（质量优先） | p50 1.42 s / charF1 0.456 / 人名 84.6% / 3.68 元/h |

Paraformer 基线用：

```powershell
python tools/replay_stream.py --wav ... --out runs/para -- `
  --source-language ja --target-language zh --max-end-silence 300 `
  --mt-model qwen3.8-max --mt-mode icl
```

## 3. 打分

```powershell
python <bench>/skill/scripts/eval_stream.py `
  --run runs/31_ref/events.jsonl `
  --srt "data/gold-v0.3/srt/9u5-VQG61So_hl04_11天热量结算：1律≈4由乃.srt" `
  --offset 120 --start 120 --end 300 `
  --label "3.1 reference" --out runs/31_ref/report
```

- `--offset`：SRT 是整条视频的绝对时间，run 从 0 开始；只跑 120–300 秒时传 120。
- `--start/--end`：只统计这个窗口的 cue；跑整条片段时省略。
- `eval_stream.py` 会自动减去 `ready` 事件的时间，把 `_recv_mono` 对齐到「开始喂音频」。
- 产物：`report/metrics.json`、`report/report.md`。

## 4. 参考结果

9u5-VQG61So_hl04 的 120–300 秒，关闭额外上下文，温度 0.2：

| 配置 | coverage | first-zh p50 | mean | charF1 | 人名召回 | 成本 |
|---|---:|---:|---:|---:|---:|---:|
| Paraformer + qwen3.8-max | 95.7% | 1.77 s | 1.39 s | 0.430 | 76.9% | 2.71 元/h |
| 3.1 + qwen3.8-max reference | 91.5% | 1.34 s | 0.72 s | 0.445 | 92.3% | 2.74 元/h |
| 3.1 + qwen3.8-max ICL + 1 轮上下文 | 91.5% | 1.42 s | 0.76 s | 0.456 | 84.6% | 3.68 元/h |

完整 32 条和口径说明见
[`skill/docs/GOLD03.md`](https://github.com/HudsonHuang/bangdream-hotword-bench/blob/main/skill/docs/GOLD03.md)
与 [`results/gold03_results.md`](https://github.com/HudsonHuang/bangdream-hotword-bench/blob/main/results/gold03_results.md)。

## 5. 注意

- gold-v0.3 的 SRT 是中文人工译文，不是日文金标；它评中文可懂度/热词/节奏，不评 ASR WER。
- 人名召回是保守下界：人工译文常用「她」省略名字，没出现不等于模型漏译。
- 32 条都是已发布高光，版权归原权利人；仅限非商业研究与评测。
