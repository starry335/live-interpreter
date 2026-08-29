# Live Interpreter v0.2.0

Windows 桌面与 Edge 当前标签页实时字幕工具。

## 本版变化

- 新增真正的 Windows 安装包，内置 Python 3.11、云端工作进程和透明字幕运行时。
- 安装后可从开始菜单直接启动，并带有标准卸载程序和可选桌面快捷方式。
- 安装版支持 Qwen3.5 LiveTranslate、Gummy Realtime、WASAPI 默认电脑声音和 Edge 当前标签页模式。
- Edge 扩展、网页字幕、透明字幕 UI、原文开关及 Qwen 画面辅助均已包含。
- API Key 和 Workspace ID 可直接在启动器中填写，不写入安装文件。

## 文件

- `LiveInterpreter-Setup-v0.2.0.exe`：推荐下载，Windows 10/11 x64 独立安装包。
- `LiveInterpreter-v0.2.0-portable.zip`：免安装版，两个 EXE 必须放在同一目录。
- `LiveInterpreter_Edge_Extension-v0.2.0.zip`：单独的 Edge Manifest V3 扩展。

安装版不包含本地 FunASR/Nemotron/Qwen 模型和 FFmpeg 屏幕录制功能。云端模式需要用户自己的阿里云百炼 API Key、Qwen Workspace ID 和网络连接，调用费用由阿里云账户产生。
