# 用 gold-v0.3 测试流式翻译

gold-v0.3 是 bangdream-hotword-bench 的人工精标流式集：32 条完整高光、92.4 分钟、
16 kHz WAV + 人工修正中文 SRT。它同时评三件事：

- **回显节奏**：中文第一次出现离参考句结束多远；
- **翻译质量**：中文和人工译文的字符 F1；
- **名字**：人工 SRT 里的名字提及有没有被正确输出，以及输出里有没有无证据的名字。

## 0. 评测范围（2026-11 起）

只把下面两套数据当作质量门槛：

| 集合 | 规模 | 用途 |
|---|---|---|
| gold-v0.3 全量 | 32 条 / 1.54 小时 / 1,525 cue | 质量、名字、节奏、稳定性 |
| 野乃花晾衣服 | 106.5 秒，OCR 人工校对参考 | 名字虚警、稳定性；charF1 仅参考 |

v0.1、v0.4、野乃花魔女裁判是自动整理/未人工校准的数据，**不再作为质量 gate**。
v0.4 的 40:56 音频只用于稳定性 soak，不出现在质量表里。

名字指标定义见 `skill/scripts/name_score.py`：召回按 gold 提及；精确率允许
gold 或强源文证据（全名/敬称/核心 5 人裸名）；虚警/小时只统计无证据的名字和
已知错形（小夜/小泷/乃乃花…）。严格对 gold 的精确率会被人工译文的省略拉低，
只作诊断。

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

## 1.5 全量跑法（推荐）

仓库外层的 `bench/eval/run_profile.py` 负责 32 条并行回放、逐条打分和汇总：

```powershell
python bench/eval/run_profile.py `
  --profile bench/eval/profiles/candidate_precision_31_400_full.json `
  --out bench/runs/gold03_full/candidate_precision_31_400 `
  --workers 4
```

profile 里写 worker、参数、是否 `--reconnect`。全量 32 条 4 路约 25 分钟；
基线 profile 是 `baseline_v032.json`（upstream v0.3.2 + 主线词表 + 2 轮上下文）。
`--reconnect 3` 会在 3.1 会话掉线时从当前采样点重启 worker，避免长 clip 的
尾部被静默丢掉。

汇总报告用：

```powershell
python bench/eval/compare_profiles.py `
  --run baseline=bench/runs/gold03_full/baseline_v032 `
  --run candidate=bench/runs/gold03_full/candidate_precision_31_400 `
  --out bench/reports/gold03_full_compare.md
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
  --offset 120 --start 120 --end 300 --audio-hours 0.05 `
  --label "3.1 reference" --out runs/31_ref/report
```

- `--offset`：SRT 是整条视频的绝对时间，run 从 0 开始；只跑 120–300 秒时传 120。
- `--start/--end`：只统计这个窗口的 cue；跑整条片段时省略。
- `--audio-hours`：虚警/小时的分母；不传则用 cue 跨度。
- `eval_stream.py` 会自动减去 `ready` 事件的时间，把 `_recv_mono` 对齐到「开始喂音频」。
- 产物：`report/metrics.json`、`report/report.md`；`metrics.json` 里有
  `names_v2`（召回/精确/FA/h/错形/逐名字）和 `health`（finals/translated/missing/fallback）。

## 4. 参考结果

### 4.1 全量 32 条（1.54 小时，2026-11-24）

| 配置 | coverage | charF1 | first-zh p50 | 短句率 | 名字召回 | 名字精确 | 虚警/h | 错形 | 漏翻 | 回退 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 主线 v0.3.2：Paraformer + qwen3.8-max + 主线词表 + 2 轮上下文 | 90.8% | 0.378 | 1.93s | 37.9% | 56.1% | 98.9% | 1.30 | 0 | 0 | 59 |
| 稳定档：Paraformer + 3.8-max + strict 词表 + reference | 91.0% | 0.355 | 2.00s | 37.7% | 57.6% | 99.4% | 0.65 | 0 | 0 | 32 |
| 质量档：3.1 + 3.8-max + strict 词表 + 400ms | 89.2% | 0.413 | 0.92s | 45.0% | 76.3% | 99.7% | 0.65 | 1 | 0 | 3 |

结论：

- 精确优先候选把虚警从 2 条降到 1 条（1.30→0.65/h），召回从 56% 提到 76%；
- 3.1 400ms 的首字比基线快约 1 秒，charF1 +0.035，翻译回退从 59 条降到 3 条；
- 3.1 的短句率仍高于基线（45% vs 38%），显示层已做段落合并；如果更在意断句数，可用 500ms（charF1 约回到基线）。
- 严格对 gold 的精确率约 34%，因为人工译文常用“她/好/对对”省略名字；不要把严格精确率当门槛。

### 4.2 2–5 分钟片段（历史参考）

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
