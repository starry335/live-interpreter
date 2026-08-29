# Live Interpreter v0.1.3

Windows 桌面与 Edge 当前标签页实时字幕工具。

## 运行要求

- Windows 10/11 x64。
- 外部 Python 环境；通过 `LIVE_INTERPRETER_ENV` 指向该环境目录。
- 云端模式需要阿里云百炼 API Key、Workspace ID 和网络连接。
- `qwen3.5-live` 画面辅助只在 Edge 当前标签页模式下生效，会每秒上传约一张压缩截图。

## 文件

- `LiveInterpreter.exe`：轻量启动器，不包含本地模型和完整 Python 运行环境。
- `LiveInterpreter_Edge_Extension.zip`：Edge Manifest V3 扩展。
- `README.md`：安装、配置和使用说明。

API Key、Workspace ID、本地模型、媒体文件和运行日志均未包含在发行包中。
