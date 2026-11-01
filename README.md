# 电脑画面和声音同声传译工具

**v0.3.2**：安装后默认启用内置热词表，支持自行添加固定译法；自动记忆 API Key、Workspace ID 和字幕设置，并修复插件字幕过期问题。[下载正式版](https://github.com/starry335/live-interpreter/releases/tag/v0.3.2)。

这个目录包含一个电脑同传工具，可以采集电脑音频，实时识别、翻译，并把字幕显示在透明置顶窗口中。v0.3.0 图形界面默认使用阿里云 Paraformer-Realtime-V2 + Qwen-MT-Plus，也保留 Qwen3.5 / Qwen3.8 LiveTranslate、Gummy 和 FunASR 本地识别模式。

v0.3.0 新增 `qwen3.8-live`：在启动器的 ASR 下拉框中选择，填写北京或新加坡地域对应的 API Key 和 Workspace ID。该模式自动识别源语言、自动分句，原文识别始终开启，仅请求文字输出；source 和断句静音控件不适用于此模式。支持 Edge 当前标签页、画面辅助和桌面字幕，仍可切回 `qwen3.5-live` 对比测试。

v0.3.0 新增 `paraformer-mt`：使用 `paraformer-realtime-v2` 识别、默认 `qwen-mt-plus` 翻译，可切换到 `qwen-mt-lite` 等模型。选择北京地域、北京 API Key，source 设为 `ja`、target 设为 `Chinese`，断句静音初始设为 `500` 毫秒。Workspace ID 可选；填写后使用业务空间专属域名，留空使用仍可用的公共域名。支持 Edge 当前标签页和默认电脑声音，仅处理音频，不上传画面；未结束的句子只更新原文，确认句末后只翻译一次，不自动重试付费翻译请求。关闭“启用翻译”时只做识别。

此模式的最终原文、译文、翻译耗时、请求 Token 和每个会话的用量汇总保存到工作目录 `paraformer_mt.jsonl`（2 MB 轮换，保留两个备份）；重连原因仍在 `cloud_recovery.log`。汇总中的 `audio_sent_seconds` 是本机上传量，`asr_billed_seconds` 是服务端最后报告的时长，最终扣费以阿里云账单为准。鉴权或模型配置错误会停止本模式；临时翻译失败会显示原文并记入日志。

v0.3.0 在 `paraformer-mt` 下增加“翻译模型”下拉框：`qwen-mt-lite`、`qwen-mt-flash`、`qwen-mt-plus`、`qwen3.8-flash`、`qwen3.8-max`、`deepseek-v4.1-flash-1017`。默认推荐先测试 `qwen-mt-plus`；所有选项均通过阿里云百炼调用，使用原有北京 API Key，具体模型需在该业务空间具备权限。通用大模型关闭深度思考，仅返回当前句译文，并携带最近两句已确认字幕作为参考（每句原文和译文分别最多 240 字符）；不会累计发送整场历史。更多上下文与不同模型的分词会改变输入用量，不能按 Lite 的实测 Token 直接当作升级后的实际费用。工作进程参数为 `--mt-model`，日志中的 `model`、`translation_model` 记录实际选择。

“翻译模型”旁的“收费对比”可查看已接入模型的输入/输出单价、统一样本的每小时费用估算，并点击“使用选中模型”切换到 Paraformer + 对应翻译模型；运行中需先停止同传。价格为 2026-10-25 北京地域快照，可从窗口打开官方价格页核对。估算未计免费额度或缓存优惠，通用模型的上下文及分词差异会改变实际费用。DeepSeek 的忙闲价格自动按北京时间生效，两行对应同一模型，无法手动选择价格时段。

## OBS / 直播姬透明字幕输出（v0.3.1）

新增“直播透明窗口（免抠像）”：背景像素的 Alpha 为 0，文字和抗锯齿边缘具有各自的 Alpha；使用 Windows 原生逐像素透明窗口，不依赖 Edge 扩展。窗口标题固定为 `Live Interpreter Subtitles`，后台进程是 `LiveInterpreterBackend.exe`，并作为普通应用窗口参与枚举。

1. 音频设备选“默认电脑声音 (WASAPI Loopback)”，勾选“桌面字幕窗口”，字幕模式选“直播透明窗口（免抠像）”。
2. 字号可先调到 32，点击“预览字幕窗口”。预览只是本地中英文样例，不需要 API Key、不采集声音、不调用模型。
3. OBS 添加“窗口捕获”，选 `[LiveInterpreterBackend.exe]: Live Interpreter Subtitles`，捕获方法明确选择 Windows 10 / Windows Graphics Capture（WGC），不要使用旧式 BitBlt 或自动猜测。移除之前的色度键和颜色键滤镜；如所用软件提供“捕获透明度/保留 Alpha”，请启用。是否有该选项以软件版本为准。
4. 在直播预览下方放一张非黑色图片或视频，确认字幕以外的区域能透出底层画面。单独的黑色预览底板无法判断透明是否生效。
5. 配好后填写自己的北京 API Key，点击“启动同传”。预览会关闭，由同名真实字幕窗口接替；如捕获源未自动重连，请重新选择窗口。

透明窗口无标题栏或悬浮按钮；按住文字可移动位置，尺寸和字号在主窗口设置，任务栏关闭窗口或 Alt+F4 会停止该次同传。不要最小化字幕窗口。位置保存到 `alpha_window.json`，与原桌面悬浮窗分开保存。无字幕时输出完全透明，错误信息只显示在主窗口日志。

**直播姬仍出现黑底时：** 其所用的窗口捕获链路可能没有保留 Alpha，不能仅靠输出窗口解决。可点击“复制透明源地址”，先启动同传，再把地址添加到直播软件内置的“浏览器/网页来源”；默认地址为 `http://127.0.0.1:8765/overlay`。这个页面直接输出透明背景和当前字幕，不显示状态、历史列表或等待提示，不需要打开 Edge、不需要安装扩展、也不需要抠像。可按 980×280 设置来源尺寸，或按直播布局调整。若直播姬版本不支持网页来源，可在 OBS 使用该透明来源完成合成。检查实际直播合成画面，而不只是窗口本身。

字幕模式仅保留“桌面透明悬浮窗”和“直播透明窗口（免抠像）”。透明网页来源也可通过“复制透明源地址”使用。

源码窗口预览：`python live_interpreter.py --preview-overlay --overlay-window-mode alpha --overlay-font-size 32`。预览不会启动 HTTP 服务，透明网页来源需在实际同传服务运行时使用。

## Windows 安装版

从 [GitHub 最新发行版](https://github.com/starry335/live-interpreter/releases/latest) 下载 `LiveInterpreter-Setup-v0.3.1.exe` 后直接安装。安装版自带 Python 运行时、透明字幕界面、Edge 扩展和云端连接组件，不需要安装 Conda、Python、FFmpeg 或本地模型。

升级前先停止同传并关闭旧版软件，然后覆盖安装。免安装版请完整解压到新目录，从 `launcher/LiveInterpreter.exe` 启动。Edge 扩展更新后请在扩展管理页重新加载，并刷新视频标签页。默认组合仅支持北京地域；需要自己的百炼 API Key，Workspace ID 可选，模型须具备调用权限。切回 Qwen LiveTranslate 时仍需填写 Workspace ID。

更新公告可在仓库 Releases 查看；也可在 GitHub 点击 Watch → Custom → Releases 订阅发布通知。旧版没有内置推送通道，发布不会自动更新已安装的软件。

`v0.2.2` 支持云端工作进程异常后的自动重连，退避间隔为 1、2、4、8、16、30 秒，稳定运行一分钟后重置退避。重连期间本地 HTTP 服务和字幕窗口持续运行，已有字幕的停留计时暂停；恢复后从当前实时音频继续翻译，不补播断线期间的音频。每次新连接使用独立会话编号，重复句子不会覆盖旧字幕。

桌面窗口拖动和缩放后会自动保存位置与尺寸到工作目录的 `overlay_window.json`，下次启动恢复原位置；Edge 字幕使用扩展本身保存的位置，云端重连不会重新创建页面字幕。重连原因记录在工作目录的 `cloud_recovery.log`，日志达到 512 KB 时轮换，最多保留两个备份。

安装版支持：

- 阿里云 `Qwen3.5 / Qwen3.8 LiveTranslate` 实时文本同传和画面辅助。
- 阿里云 `Gummy Realtime` 实时语音翻译。
- 阿里云 `Paraformer-Realtime-V2` + 六种可选文本翻译模型，附收费对比与模型选择入口。
- Windows 默认电脑声音（WASAPI Loopback）。
- Edge 当前标签页独立音频、网页内字幕和可选画面辅助。
- 桌面透明字幕悬浮窗。

启动后直接填写阿里云百炼 API Key；Qwen LiveTranslate 还需要填写同地域的 Workspace ID。凭证只传给本次后台进程，不写入安装目录或仓库。

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

启用“画面辅助”后，扩展还会把当前标签页画面压缩为最长边 960px 的 JPEG，约每秒上传 1 张，为 Qwen LiveTranslate 提供动画角色、屏幕文字和场景上下文。关闭该选项时扩展不会申请视频轨道，也不会上传截图。画面辅助只在 `qwen3.5-live` 或 `qwen3.8-live` 与 Edge 当前标签页模式组合下生效。

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

## 自动保存配置（v0.3.2）

修改界面设置后会自动保存，正常退出时立即写入；重新打开软件会恢复上次配置，启动后不会自动调用模型。
保存范围包括 API Key、Workspace ID、地域、模型、源/目标语言、音频设备、断句静音、分段秒数、字号、停留秒数、字幕位置、端口、字幕模式、各项开关、工作目录和外部热词表路径。
配置文件为 `%LOCALAPPDATA%\LiveInterpreter\settings.json`，与安装目录分开，覆盖安装不会覆盖该文件。
API Key 使用当前 Windows 用户的 DPAPI 加密保存，文件中不写入明文 Key；换电脑或 Windows 用户后可能需要重新填写。
清空 API Key 后也会保存清空状态；当上次音频设备不可用时会提示并选择可用设备。

## 热词记忆（v0.3.2）

主窗口点击“热词记忆”，每行填写 `原词 = 固定译法`，例如 `山田リョウ = 山田凉`。
安装后默认启用 PR 原有词表，无需选择文件。自行添加的词会叠加到内置词表，同名原词优先采用自定义译法。
编辑窗口只显示自行添加的词；清空自定义词不会清空内置词表。可以单独取消“使用内置 PR 词表”，或关闭全部热词功能。
支持最多 200 条自定义词，可编辑、删除或停用；保存后下次启动同传生效。同传运行时请先停止再保存。
词表保存在当前 Windows 用户的 `%LOCALAPPDATA%\LiveInterpreter\hotwords.json`，重启和更换工作目录后仍保留。
只保存用户确认的词条，不会自动学习字幕，也不保存 API Key。词表按目标语言编写，切换目标语言时请检查译法。

- Qwen 3.5／3.8 LiveTranslate：通过官方 `translation.corpus.phrases` 配置会话热词。
- Paraformer：自动创建并缓存识别热词词库，通过 `vocabulary_id` 使用；创建失败会提示并降级，继续使用翻译术语。
- Paraformer + Qwen-MT：通过官方 `translation_options.terms` 提供当前句子命中的术语，最长匹配优先，单次最多 48 条。
- Paraformer + 普通大模型：把当前句子命中的原词和译法作为术语参考。
- Gummy：外部词表仅用于已配置的禁写词纠正；本地模型暂不使用热词表。

吸收 [PR #1](https://github.com/starry335/live-interpreter/pull/1) 的统一热词链路、示例词库和评测工具。
主窗口的“外部热词表”支持 UTF-8 文本和 `kb-terms/v1` JSON，选择后优先使用外部词表；点击“恢复默认”恢复内置词表与个人记忆设置。
内置词表为 PR 原有的 `hotwords/yumemita_bangdream.txt`，保留识别词、翻译别名和禁写纠正，针对日语直播翻译成中文的场景。

```text
あられ => 阿拉蕾
ユノ
~戦国ユーノ => 由乃
!战国由乃 => 由乃
```

以上分别表示识别与翻译热词、仅识别词、仅翻译别名和输出禁写纠正。识别误写应标记为 `~`，避免注册进 ASR 强化错误。
完整格式、限制及作者的离线评测见 [热词设计说明](docs/hotwords.md)；作者的评测结果未在本地重新调用付费模型验证。

启用后相应热词会发送至所选翻译服务，可能增加输入 Token；不会额外增加翻译请求次数。
热词用于引导译法，无法保证纠正所有语音识别错误或强制每次使用指定译法。
接口依据：[实时同传热词](https://help.aliyun.com/zh/model-studio/qwen3-5-livetranslate-flash-realtime)、[Qwen-MT 术语干预](https://help.aliyun.com/zh/model-studio/machine-translation)。

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
--asr-engine qwen3.8-live   阿里云 Qwen3.8 LiveTranslate 实时文本翻译（源语言自动识别）
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
