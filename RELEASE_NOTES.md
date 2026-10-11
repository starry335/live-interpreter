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
