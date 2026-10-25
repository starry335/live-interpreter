# Live Interpreter v0.3.0

实时字幕新增 Paraformer-Realtime-V2 + 文本翻译模式，可在软件内比较费用并选择模型，图形界面默认 Paraformer + Qwen-MT-Plus。

## 新增与改进

- 接入 `qwen-mt-lite`、`qwen-mt-flash`、`qwen-mt-plus`、`qwen3.8-flash`、`qwen3.8-max`、`deepseek-v4.1-flash-1017` 六种翻译模型，统一通过阿里云百炼北京地域调用。
- “翻译模型”旁新增“收费对比”，展示输入/输出单价、翻译费与识别加翻译的小时费用估算；选择模型后直接应用到主界面。运行中请先停止同传再切换。
- 收费表为 2026-10-25 标准价快照，提供官方价格链接；小时费用按统一 Token 样本折算，实际费用以账单为准。DeepSeek 忙闲价格由服务端按北京时间自动生效。
- Paraformer 仅在确认句末后发起翻译，不自动重试付费翻译；通用模型关闭深度思考，仅保留最近两句有限上下文。
- 记录每次翻译的 Token、耗时及会话用量汇总，方便检查消耗。授权或模型配置错误会停止该模式，避免继续产生识别费用。
- 新增 Qwen3.8 LiveTranslate，可与原有 Qwen3.5 / Gummy 模式切换。
- 修正停止云端工作进程的流程，通过音频流结束信号关闭，避免把退出字符写入 PCM 音频。

## 下载与升级

- `LiveInterpreter-Setup-v0.3.0.exe`：推荐，Windows 10/11 x64 安装包，内置 Python 运行时。
- `LiveInterpreter-v0.3.0-portable.zip`：免安装目录版，请完整解压后从 `launcher/LiveInterpreter.exe` 启动。
- `LiveInterpreter_Edge_Extension-v0.3.0.zip`：单独的 Edge 扩展。
- `SHA256SUMS-v0.3.0.txt`：三个下载文件的 SHA-256 校验值。

升级前请先停止同传、关闭旧版，再运行新安装包覆盖安装。Edge 扩展需在管理页重新加载，并刷新视频标签页。免安装版应解压到新目录；不要单独移动 EXE。原版测试日志仍保留在原目录，安装包不会携带这些日志。

## 配置与费用

默认 Paraformer 模式仅支持北京地域，需要自己的阿里云百炼 API Key；Workspace ID 可选。六种模型都需要账号具备相应调用权限，切换不会自动开通模型。Qwen LiveTranslate 仍需要填写同地域 Workspace ID。

收费对比不是实际扣费查询。不同分词、翻译长度、上下文和免费额度会改变费用，请以自己的阿里云账单为准。Qwen-MT-Lite 与 Qwen-MT-Plus 已进行本机实测；其他翻译模型已接入，效果、延迟和权限需结合账号测试。

发行包不含 API Key、本地模型、媒体、字幕记录或运行日志；保留桌面透明字幕、Edge 标签页字幕及默认电脑声音捕获。安装包未进行代码签名。旧版没有内置更新推送或自动升级功能；可在 GitHub Watch → Custom → Releases 订阅后续发行通知。

## 验证

34 项自动化测试通过，覆盖模型请求、句末去重、权限错误停止、停止流程、断线重连、模型选择、收费表与中文系统编码；字幕保留、窗口缩放和排版断言通过。发布前另检查打包后启动与安装结果，本次回归测试不发起付费模型调用。

# Live Interpreter v0.2.2

## 云端断线恢复

- Qwen3.5 和 Gummy 工作进程失败后自动重建会话，按 1、2、4、8、16、30 秒退避。
- 重连期间保持 HTTP 字幕服务、字幕内容和窗口位置，暂停已有字幕的停留计时。
- 桌面字幕的位置和尺寸保存到工作目录，重新启动后可恢复。
- 新会话使用独立句子编号，防止旧字幕被覆盖。
- 保存带时间和错误码的轮换重连日志。

# Live Interpreter v0.2.1

Windows 桌面与 Edge 当前标签页实时字幕工具。

## 陌生电脑兼容性修复

- 修复中文、日文系统代码页不同时字幕出现 `�` 的问题。
- Qwen 与 Gummy 工作进程改用 ASCII 安全 JSON，并强制 UTF-8 标准输入输出。
- 修复 `NO_PROXY=*` 导致企业代理、VPN 网络无法连接阿里云的问题。
- 安装版改为目录式运行时，不再依赖 `%TEMP%` 解压，降低 Defender 和企业安全策略拦截概率。
- 保留 Qwen3.5 LiveTranslate、Gummy Realtime、WASAPI、Edge 标签页字幕、透明字幕 UI 和画面辅助。

## 文件

- `LiveInterpreter-Setup-v0.2.1.exe`：推荐下载，Windows 10/11 x64 独立安装包。
- `LiveInterpreter-v0.2.1-portable.zip`：免安装目录版，必须保留压缩包内的目录结构。
- `LiveInterpreter_Edge_Extension-v0.2.1.zip`：单独的 Edge Manifest V3 扩展。

安装版不包含本地 FunASR/Nemotron/Qwen 模型和 FFmpeg 屏幕录制功能。云端模式需要用户自己的阿里云百炼 API Key、Qwen Workspace ID 和网络连接，调用费用由阿里云账户产生。
