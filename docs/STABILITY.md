# 稳定性与诊断

## 退出码 4294967295 是什么

Windows 进程退出码 `4294967295` 等于 `-1`。PyInstaller 打包的程序遇到**未捕获异常**时
会把退出码写成 `-1`，`subprocess` 读出来就是 4294967295。它不是 API 错误，也不是
“翻译失败”的业务错误，而是某个 Python 异常没有被兜住。

本项目现在做了三层兜底：

1. `runtime_safety.SafeStream` 包住 stdout/stderr；父进程关掉管道后继续 `print`
   不会抛 `BrokenPipeError`。
2. `runtime_safety.write_json` 用全局锁写 JSONL；ASR/MT/草稿线程不会再把两条事件
   拼到同一行。
3. `runtime_safety.guard` 捕获 `main` 的 `BaseException`，写 `runtime/crash.log`
   （含 `faulthandler` 线程栈、argv、pid），返回稳定退出码 2；launcher 把
   `-1/4294967295` 显示成“未捕获异常”，不再只显示数字。

## 翻译不掉的三层设计

- **生命周期**：每个终句有 `translation_queued` / `started` / `failed` / `final`；
  `usage_summary` 里有 `translation_queued`、`translation_fallbacks`、`translation_failures`。
- **重试**：timeout、429、5xx、连接断最多重试 2 次；400/401/403/404 不重试。
- **兜底**：失败时优先用已有草稿收尾；没有草稿才显示日文原文并标 `fallback`。
  关闭时 MT 队列最多等 10 秒，避免最后几句被进程退出截掉。

## 断线重连不丢音

`PcmStream` 在 worker 重启期间保持采集进程和 5 秒环形队列；新 worker 从当前采样点
继续，不会把重连那几秒的语音静默丢掉。Edge 标签页音频也不再在每次重连时
`discard_pending()`。

`tools/replay_stream.py --reconnect N` 用同样的方式回放：worker 提前退出时从当前
采样点起新会话，后续事件的 `sentence_id` 加 `s{n}-` 前缀，`begin_time/end_time`
加上已喂音频的偏移，评测时间轴保持连续。

## 引擎选择

launcher 和 backend 共用一套云端引擎集合；`qwen3.1-audio` 在 Edge 标签页音频下
**不会**再被静默改成 `qwen3.5-live`。非法组合会直接报错并保留用户选择。

## 本轮实测

| 场景 | 结果 |
|---|---|
| Paraformer + qwen3.8-flash，40:56 连续音频 | 659 finals / 659 translated / 0 missing / 0 error / 0 crash / 回退 18 |
| 3.1 + qwen3.8-max，16.5 分钟 clip | 400ms 档完整跑完，0 drop；300ms 档曾在 955s 掉线，由回放 supervisor 兜底 |
| 全量 gold-v0.3（32 条） | 3.1 400ms：0 missing / 3 fallback / 0 worker 坏退出；Paraformer 基线：0 missing / 59 fallback |

## 出问题看哪里

- `%WORK%\crash.log`：未捕获异常/原生崩溃栈。
- `%WORK%\cloud_recovery.log`：重连原因、重试间隔、最后一条字幕。
- `%WORK%\qwen_asr.jsonl` / `paraformer_mt.jsonl`：完整事件流（含 hotword hit、
  translation_usage、usage_summary）。
