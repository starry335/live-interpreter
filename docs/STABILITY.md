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

## ASR 停摆看门狗

`GummyConnection` 每分钟检查一次：音频还在到达、最近 5 秒有语音能量（PCM RMS）、
但 8–10 秒没有新的 partial/final，就判定为 `asr_stall`：

- 写 `asr_stall` 到 `events_*.jsonl` 和 `disconnect_*.log`，包含 close code、
  最近结果年龄、已发送音频字节数；
- 3.1 worker 先尝试把流交给预热的 Paraformer 备用进程（见下节）；
- 备用不可用时关闭 socket 让 backend 重启 worker，而不是在屏幕上空等几分钟。

阈值可用环境变量调整：`LIVE_INTERPRETER_ASR_WATCHDOG`（秒，0 关闭）、
`LIVE_INTERPRETER_ASR_WATCHDOG_RMS`（语音能量门限）。

## 公开 Paraformer 备份

主 ASR 正常工作时**不启动**备份进程，因此不占连接、不产生费用。

- 看门狗触发时才拉起一个**公开域名** Paraformer（`dashscope.aliyuncs.com`，显式清空
  Workspace ID），避免和主模型共用同一个专属域名/会话。
- 本地保留最近 30 秒 PCM（`LIVE_INTERPRETER_FALLBACK_RING_SECONDS`）。切换时把
  “最后一条已经上屏的字幕之后”的音频从环形缓冲回填给备份，再把实时音频接上。
- 主 worker 在备份工作期间每 15 秒尝试重连主 ASR（`LIVE_INTERPRETER_BACKUP_PROBE`）；
  重连成功并连续 8 秒出字（`LIVE_INTERPRETER_BACKUP_PROBATION`）后自动停掉备份、
  切回主 ASR。主模型一直不恢复时备份持续工作，不会定时重启。
- 备份结果带 `pf...` 前缀的 `sentence_id` 和 `fallback: true`，不会覆盖主 ASR
  已经上屏的字幕；试运行期间主 ASR 的结果只用于健康判断，不会重复上屏。
- 主模型是公开 Paraformer 时没有备份（同一端点做备份没有意义）；主模型是
  Workspace Paraformer 或 Qwen 时，备份固定走公开 Paraformer。
- launcher 的“主 ASR 掉线时用公开 Paraformer 备份”默认打开；公开 Paraformer 主模型时
  勾选框置灰且保持不勾。
- 备份进程的日志在 `runtime/fallback/` 下，不会和主进程的按天日志混写。

## 3.1 掉线到底是什么

实测到的错误是 **TCP 连接被断开**，不是模型返回 `task-failed`，也不是识别任务被服务端拒绝：

- `websocket-client` 的 `recv()` 返回空，底层是 `WebSocketConnectionClosedException("Connection to remote host was lost.")`；
- 没有 `task-failed` 的 error code，也没有服务端返回的识别错误；
- 同一台机器上 Paraformer 也出现过同样的 “Connection to remote host was lost.”，所以不是 3.1 模型独有的问题。

官方对 `qwen-audio-3.1-asr-flash-streaming` 的说明是：**音频流无时长限制，但受网络稳定性和会话保持策略影响**（[官方样例](https://github.com/aliyun/alibabacloud-bailian-speech-demo)），并明确推荐使用业务空间专属域名
`wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com`，说它“性能更好、稳定性更高”
（[官方 SDK 文档](https://www.alibabacloud.com/help/en/model-studio/qwen-audio-asr-streaming-websocket-sdk-python)）。
不填 Workspace ID 时我们走的是公共地址 `dashscope.aliyuncs.com`；这是目前最可疑的
环境因素，而不是模型本身。

本轮补了三件事：

1. 有 Workspace ID 时走专属域名；没有时在日志里明确提示“公共域名，长连接更容易被重置”。
2. WebSocket 层每 20 秒发一次 ping，并打开 TCP keepalive（30 秒空闲 / 5 秒探测），防止 NAT / LB 空闲回收。
3. 断开时记录 close code / reason（如果有），并保留 `cloud_recovery.log`，下次能区分“服务端主动关闭”“网络 EOF”“代理重置”。

全量 gold-v0.3 的 3.1 400ms 跑法 32 条 0 次 worker 坏退出；300ms 跑法在 16.5 分钟
clip 的 955 秒掉过一次；41 分钟 Paraformer+qwen3.8-flash 是 0 次。**掉线是间歇的，
不是每个会话都发生**。如果换成专属域名 + keepalive 后仍偶发，再看 close code 决定
是否要做“空闲时主动轮换会话”。

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
- `%WORK%\logs\YYYY-MM-DD\`：按天追加、不轮转的诊断日志：
  - `events_YYYYMMDD.jsonl`：完整事件流（含 watchdog、fallback、hotword hit、usage）；
  - `disconnect_YYYYMMDD.log`：WebSocket close code / ping 失败 / `asr_stall`；
  - `crash_YYYYMMDD.log`：未捕获异常；
  - `mt_timeout_YYYYMMDD.log`：8 秒超时与回退；
  - `session_YYYYMMDD.log`：进程启动/结束。
- `%WORK%\qwen_asr.jsonl` / `paraformer_mt.jsonl`：当前会话的滚动事件流。
- `%WORK%\fallback\logs\YYYY-MM-DD\`：Paraformer 热备进程自己的日志。
