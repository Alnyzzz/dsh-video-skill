---
name: video-toolkit
description: 用 ffmpeg 检查、理解、剪辑和转换视频与音频文件（mp4、mov、mkv、avi、webm、flv、ts、mp3、wav、m4a）。当视频或音频文件作为输入或需要产出时使用：读取时长/分辨率/编码，抽取关键帧或联系表让模型真正"看到"画面，裁剪、压缩、改分辨率、转换容器、生成 GIF、提取或标准化音频、检测静音、生成检查报告。
whenToUse: 任何需要读取或产出视频文件、帧序列、动图或音轨的任务；用户说 DSH "打不开/看不见" 视频时也用它。
---

# video-toolkit 操作手册（中文版）

我（模型）不能"看"视频，但可以探测它、把画面抽成静帧，再用 `read_image` 工具真正读到这些
静帧。**探测 → 采样 → 观看 → 动手**，这就是这个技能的全部。

所有功能都由本文件旁边的同一个脚本驱动：

```bash
python "<skill-dir>/scripts/video_ops.py" <命令> [参数]
```

Windows 上如果 PATH 里没有 `python`，用 DSH 自带的：

```powershell
& "$env:DSH_HOME\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" `
  "<skill-dir>\skills\video-toolkit\scripts\video_ops.py" <命令> [参数]
```

每个命令都会在 stdout 输出一个 JSON 对象，成功时退出码为 `0`。`--print-command` 只打印
ffmpeg 命令而不执行；`--json-out <文件>` 额外把结果写到磁盘。路径要加引号（常有空格和中文）。

## 0. 每个会话先确认工具链

```bash
python video_ops.py doctor
```

需要 `"ok": true`，并尽量让 `"found": {"paired": true}`。重点字段：`found.*`（选中的
ffmpeg/ffprobe 与来源）、`versions`、`encoders`（有哪些编码器）、`filters`（有哪些滤镜）、
`python_modules`（PIL 决定联系表能否带时间码）、`warnings`。

如果 ffmpeg 缺失，**不要问用户**，直接安装技能自己的依赖：

```bash
python video_ops.py bootstrap --download     # 从公共镜像下载约 100 MB
python video_ops.py doctor                   # 确认后继续任务
```

没有网络时用本地已有构建：`--source <ffmpeg.exe|目录|.zip|.7z|.tar.xz>`。安装前会先验证二进制
真的能运行。文件落在 `%DSH_HOME%\video\tools`。

## 1. 先探测，再提问

```bash
python video_ops.py probe "视频.mp4" --json-out probe.json
```

返回时长、容器、码率、体积、每条流的编码/分辨率/帧率/像素格式/旋转、采样率、声道、章节与语言
标签。**便宜且必须**，任何视频任务都先跑它。MP4/MOV/M4A/3GP 即使没有 ffprobe 也能用内置的
纯 Python ISO-BMFF 解析器读出结果（会带 `probe_note` 字段）。

要靠探测结果（不是文件名）判断：`duration`、`has_audio`、分辨率与 `rotation`（带
`rotate: 90` 的 1920x1080 对观众是竖屏）、容器格式（目标 `.webm` 需要 VP9/Opus）。

## 2. 不许编造元数据

报告的每个数字都必须来自本会话中某个命令的输出。不要用文件大小估算，也不要相信文件名。

## 3. 看画面：抽帧与联系表

```bash
python video_ops.py sheet "视频.mp4" --count 12 --cols 4        # 一张带时间码的棋盘图
python video_ops.py frames "视频.mp4" --at 00:00:03 --at 45.5 --width 1280
python video_ops.py frames "视频.mp4" --count 8                 # 均匀采样
python video_ops.py frames "视频.mp4" --every 30                # 每 30 秒一帧
```

`sheet` 是"这个视频里有什么"的最快路径：一张图，从左到右、从上到下，每格标着时间码，用
`read_image` 读它。需要某个时刻的全分辨率画面时用 `frames`，输出到 `<输入>_frames/`。

要点：联系表 6–12 格最易读；`--at` 支持 `12.5`、`00:01:10`、`01:02:03.500`；屏幕文字/字幕需要
全分辨率（`--width 1600`）并精确到秒；读图是昂贵操作，只抽能回答问题的最小集合。

## 4. 剪辑、压缩、转换、缩放、动图

```bash
python video_ops.py clip "视频.mp4" --start 00:02:00 --end 00:02:30 --out part.mp4
python video_ops.py clip "视频.mp4" --start 10 --duration 5 --copy   # 极快，但按关键帧对齐
python video_ops.py compress "视频.mp4" --crf 26 --max-width 1280 --out small.mp4
python video_ops.py compress "视频.mp4" --target-mb 25                # 按目标体积
python video_ops.py convert "视频.mkv" --to mp4
python video_ops.py scale "视频.mp4" --size 1080p --fit pad --out hd.mp4
python video_ops.py gif "视频.mp4" --start 12 --duration 4 --width 720 --fps 12
python video_ops.py scenes "视频.mp4" --threshold 0.35 --frames shots/
```

`--copy` 是无损流复制（快，但起点会漂到最近关键帧）；需要精确到帧就不要加。`compress` 输出
H.264 + AAC + `+faststart`，兼容性最好；`--crf` 23（轻）到 30（狠）。转换/压缩都比复制慢，
而且要说明自己做了哪一种。

## 5. 音频：提取、标准化、找静音

```bash
python video_ops.py audio "视频.mp4" --codec libmp3lame --bitrate 192k --out track.mp3
python video_ops.py audio "视频.mp4" --codec pcm_s16le --rate 16000 --mono --out voice.wav
python video_ops.py normalize voice.wav --lufs -16 --out voice_norm.m4a
python video_ops.py loudness "视频.mp4" --ebur128
python video_ops.py silence "视频.mp4" --noise-db -35 --min-duration 0.6
```

`silence` 同时给出 `silences` 与互补的 `speech` 窗口——这些窗口就是剪辑点或值得转写的区间。
16 kHz 单声道 PCM 是交给语音识别工具的通用格式。

`transcribe` 刻意不内置模型：本机有 `whisper-cli` 时通过 `--binary`/`--model` 使用，否则如实报告
获取方式。**绝不允许编造转写结果。**

## 6. 报告

```bash
python video_ops.py report "视频.mp4"   # 生成 <输入>_report.md、.html 和 _assets/sheet.jpg
```

Markdown + 自包含 HTML（元数据表、流列表、内嵌联系表）。HTML 交给用户看，`sheet.jpg` 自己读。
用户要的是可播放文件时，用 `present` 把视频交付出去。

## 7. 回答"DSH 处理不了视频"

DSH 不会把视频解码给模型看——这正是本技能存在的原因。答案永远不是"不支持"：探测、抽
联系表、读图、动手，最后交付一个用户能打开的文件。如果用户只是想"看看"，那么联系表加几张
精确的全分辨率静帧就是完整且诚实的答案。

## 规则

- 先探测；所有数字都来自本会话的命令输出。
- 描述内容前先用 `read_image` 看画面。文件名不是证据。
- 路径加引号；把源文件保持原样，输出写到它旁边或用 `--outdir`。
- 报告输出的绝对路径、体积与时长；说明是流复制还是重编码。
- 不要为了"提高质量"而升分辨率或升码率，那只会让文件变大。
- 构建缺少某个编码器时如实说明并走回退方案，不要悄悄降级。
- 绝不声称看过、听过或转写过你只探测过的媒体。

## 需要时再读

- `references/commands.md` — 全部命令、参数与 JSON 字段。
- `references/workflows.md` — 任务配方：总结长视频、剪精彩片段、压到能发聊天、转写准备、批量分诊、录屏复现。
- `references/troubleshooting.md` — 引导安装失败、缺编码器、沙箱拒绝、中文路径、超大文件、版本差异。
- `SKILL.md` — 同一份手册的英文版。
