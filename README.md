# 电脑画面和声音同声传译工具

这个目录包含一个电脑同传工具，可以采集电脑音频，实时识别、翻译，并把字幕显示在透明置顶窗口中。默认使用阿里云 Qwen3.5 LiveTranslate 实时文本翻译，也保留 Gummy 和 FunASR 本地识别模式。

## Windows 安装版

从 GitHub Releases 下载 `LiveInterpreter-Setup-v0.2.1.exe` 后直接安装。安装版自带 Python 运行时、透明字幕界面、Edge 扩展和云端连接组件，不需要安装 Conda、Python、FFmpeg 或本地模型。

安装版支持：

- 阿里云 `Qwen3.5 LiveTranslate` 实时文本同传和画面辅助。
- 阿里云 `Gummy Realtime` 实时语音翻译。
- Windows 默认电脑声音（WASAPI Loopback）。
- Edge 当前标签页独立音频、网页内字幕和可选画面辅助。
- 桌面透明字幕悬浮窗。

启动后直接填写阿里云百炼 API Key；Qwen3.5 还需要填写同地域的 Workspace ID。凭证只传给本次后台进程，不写入安装目录或仓库。

安装版不包含 FunASR、Nemotron、本地 Qwen 模型和屏幕录制所需的 FFmpeg。这些本地开发功能仍可从源码运行，并通过环境变量指定已有环境：

```powershell
$env:LIVE_INTERPRETER_ENV = "C:\path\to\python-env"
$env:FUNASR_ROOT = "C:\path\to\FunAsr"
```

不要把真实凭证写入仓库；环境变量方式可参考 [`.env.example`](.env.example)。

## 0. 安装并打开

安装后从开始菜单或桌面快捷方式打开：

```text
Live Interpreter
```

图形界面可以选择 Edge 当前标签页或 Windows 默认电脑声音、填写凭证并启动/停止同传。点击“打开字幕页”时，如果后台尚未启动，启动器会先启动服务，等 `127.0.0.1:8765` 可访问后再打开浏览器。

## Edge 当前标签页模式

这个模式只识别当前选择的 Edge 标签页，并把字幕直接显示在网页内，适合直播软件按窗口采集：

1. 在启动器的“音频设备”中选择“Edge 当前标签页 (浏览器扩展)”。
2. 填写阿里云 API Key 和 Workspace ID，并启动同传。
3. 点击“Edge插件目录”。
4. 打开 `edge://extensions`，开启“开发人员模式”，选择“加载解压缩的扩展”，加载该目录。
5. 打开需要翻译的视频标签页，点击扩展，再点击“翻译当前标签页”。

扩展只发送当前标签页的 16 kHz 单声道音频，不会采集桌面或其他软件的声音。直播软件只需选择该 Edge 窗口；字幕已在网页内部，因此无需显示器采集。原生全屏同样支持，画中画窗口不支持页面字幕覆盖。

启用“画面辅助”后，扩展还会把当前标签页画面压缩为最长边 960px 的 JPEG，约每秒上传 1 张，为 Qwen3.5 LiveTranslate 提供动画角色、屏幕文字和场景上下文。关闭该选项时扩展不会申请视频轨道，也不会上传截图。画面辅助只在 `qwen3.5-live` 与 Edge 当前标签页模式组合下生效。

使用 Qwen3.5 LiveTranslate 前，在启动器中填写同一阿里云百炼地域下的 API Key、Workspace ID，并选择 `beijing` 或 `singapore`。凭证只会传入本次运行的子进程，不会保存到项目文件。该模式固定请求纯文本输出，不会生成或播放中文语音。

优先选择音频设备：

```text
默认电脑声音 (WASAPI Loopback)
```

这个模式直接捕获 Windows 当前默认播放设备的声音，不需要 VoiceMeeter。如果“测试音量”显示没有声音，请检查浏览器标签页没有静音，并在 Windows 音量合成器里确认浏览器输出设备是当前默认扬声器/耳机。

## 1. 查看可用音频设备

```powershell
.\run_live_interpreter.bat --list-devices
```

从输出里选择一个 `(audio)` 设备。当前机器检测到的可用设备包括：

- `麦克风阵列 (Realtek(R) Audio)`
- `麦克风阵列 (网易虚拟音频设备)`
- `VoiceMeeter Aux Output (VB-Audio VoiceMeeter AUX VAIO)`
- `VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)`

如果使用 VoiceMeeter/麦克风等 DirectShow 设备，需要把系统声音路由到对应录音设备；否则会录到静音。推荐优先使用 `默认电脑声音 (WASAPI Loopback)`。

## 2. 启动透明字幕窗口

```powershell
.\run_live_interpreter.bat --audio-device "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)"
```

启动后会出现一个透明置顶字幕窗口：

- 字幕窗口是深色半透明面板，默认同时显示原文和译文。
- 左键拖动整块窗口。
- 右键关闭窗口并停止同传，也可以点面板右上角 `×`。
- 浏览器字幕页仍会同时启动，地址是 `http://127.0.0.1:8765`。

Qwen3.5 LiveTranslate 和 Gummy 模式会持续发送 16 kHz 单声道 PCM，并直接显示云端返回的中间翻译和最终翻译。FunASR/Qwen 本地模式首次加载模型会比较慢。

## 3. 原文显示

新版默认显示原文。如果要隐藏原文：

```powershell
.\run_live_interpreter.bat --audio-device "__wasapi_loopback__" --overlay-hide-source
```

## 4. 调整透明窗口

```powershell
.\run_live_interpreter.bat --audio-device "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)" --overlay-geometry 1200x260+80+680 --overlay-font-size 40
```

常用窗口参数：

```text
--overlay-geometry 980x280+240+620    宽x高+左上角X+左上角Y
--overlay-font-size 15                字幕字号
--overlay-hold-seconds 8              实时字幕和批量字幕的最短停留秒数
--overlay-batch-min-chars 80          累积到接近两行后再刷新
--overlay-batch-max-chars 170         单批字幕最大字符数
--overlay-color "#ffffff"             字幕颜色
--overlay-alpha 0.94                  窗口整体透明度
--overlay-show-source                 同时显示识别原文
--no-overlay                          不显示透明窗，只保留浏览器字幕页
```

## 5. 同时录制屏幕

此功能仅供源码开发模式使用，需要 FFmpeg；独立安装版中该选项为禁用状态。

```powershell
.\run_live_interpreter.bat --audio-device "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)" --record-screen
```

屏幕录制会保存到 `runtime\recordings`。音频识别分段会保存到 `runtime\chunks`。

## 6. 校准双语 SRT 时间轴（WhisperX）

已有“原文在第一行、译文在第二行”的 SRT 时，用原声重新强制对齐原文，再保留译文：

```powershell
.\make_aligned_bilingual_srt.ps1 -Audio .\video.mp4 -SourceSrt .\subtitle.srt -Language ja
```

结果在 `outputs\subtitle.aligned.srt`。模型保存在 `models\whisperx`；日语对齐模型已预置在 `align-ja-ivy`，后续运行不需要联网下载。

```powershell
.\.whisperenv\Scripts\python.exe -m pip install whisperx
```

## 其他参数

```text
--source-language auto      源语言，支持 auto/zh/en/ja/ko/yue
--target-language Chinese   翻译目标语言
--asr-engine qwen3.5-live   阿里云 Qwen3.5 LiveTranslate 实时文本翻译（默认）
--aliyun-workspace-id xxx   阿里云百炼 Workspace ID
--aliyun-region beijing     服务地域，可选 beijing/singapore
--visual-input              启用 Edge 当前标签页低频画面辅助
--no-visual-input           禁用画面上传
--asr-engine gummy          阿里云 Gummy 实时识别和翻译
--gummy-max-end-silence 500 云端 VAD 句末静音阈值，范围 200-6000 ms
--asr-engine sensevoice     使用本地多语言 SenseVoice
--translation-engine qwen   使用本地 Qwen 翻译；可设为 none 只显示识别文本
--chunk-seconds 1           仅本地模型使用；云端实时模式不按该秒数分段
--translation-max-new-tokens 72  限制单次翻译输出长度，降低等待时间
--overlay-live-mode         低延迟显示：先显示原文和“翻译中”，翻译完成后原地更新
--overlay-live-events 3     至少保留最近三句
--overlay-live-max-events 6 最多同时显示六句，防止窗口无限堆积
--overlay-batch-mode        批量停留显示：更稳定但延迟更高
--device auto               FunASR 设备，auto/cpu/cuda:0
--translate-device auto     Qwen 设备，auto/cpu/cuda:0
--port 8765                 浏览器字幕页端口
```

## 依赖说明

独立安装版自带 Python 3.11 运行时、`websocket-client`、`certifi` 和 Tcl/Tk，只使用云端实时模型，不要求显卡。

`v0.2.1` 起安装版使用目录式运行时，不再在每次启动时把程序解压到 `%TEMP%`，更适合未安装开发环境、启用 Defender 或限制临时目录的电脑。云端工作进程使用 ASCII 安全的 JSON 传输字幕，因此不受 Windows GBK、UTF-8 等系统代码页影响；系统代理仍可用于访问阿里云，仅本机 `127.0.0.1` 字幕服务绕过代理。

源码开发模式可以继续复用本机环境和模型：

- Python：`E:\ANACONDA\envs\funasr_py38\python.exe`
- FFmpeg：`E:\ANACONDA\envs\funasr_py38\Library\bin\ffmpeg.exe`
- ASR：`E:\FunAsr\models\models\iic\SenseVoiceSmall`
- 本地翻译：`E:\FunAsr\models\Qwen\Qwen2___5-3B-Instruct-GPTQ-Int4`
- 云端实时翻译：阿里云百炼 `qwen3.5-livetranslate-flash-realtime` 或 `gummy-realtime-v1`
- 云端连接：`websocket-client 1.8.0`

云端实时模式需要联网并按阿里云百炼实际用量计费；本地模式不消耗云端调用额度。
