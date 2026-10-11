---
name: video-toolkit
description: Inspect, understand, edit, and convert video and audio files (mp4, mov, mkv, avi, webm, flv, ts, mp3, wav, m4a) with ffmpeg. Use whenever a video or audio file is an input or a requested deliverable: reading duration/resolution/codecs, extracting frames or contact sheets so the model can actually see the picture, cutting, compressing, resizing, converting containers, making GIFs, extracting or normalising audio, finding silence, or writing an inspection report.
whenToUse: Any task that reads or produces a video file, a frame sequence, an animated GIF, or an audio track. Also use it when the user says DSH "cannot open/see" a video.
---

# video-toolkit playbook

You cannot watch a video. You **can** probe it, pull stills out of it, and read those stills
with the `read_image` tool. That loop — probe, sample, look, then act — is the whole skill.

Everything runs through one script beside this file:

```bash
python "<skill-dir>/scripts/video_ops.py" <command> [args]      # any OS
```

On Windows use the DSH Python when `python` is not on PATH:

```powershell
& "$env:DSH_HOME\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" `
  "<skill-dir>\skills\video-toolkit\scripts\video_ops.py" <command> [args]
```

Every command prints one JSON object on stdout and exits `0` on success. `--print-command`
shows the exact ffmpeg line without running it. `--json-out <file>` also writes the payload
to disk when it is large.

Replace `<skill-dir>` with this skill directory. Quote paths: they often contain spaces or CJK.

## 0. Establish the toolchain once per session

```bash
python video_ops.py doctor
```

Read the JSON. You need `"ok": true`, and you want `"found": {"paired": true}`. Key fields:

| Field | Meaning |
| --- | --- |
| `found.ffmpeg` / `found.ffprobe` | executable actually selected |
| `found.source` | `dsh-video-tools`, `same-directory-pair`, `verified-pair`, `mixed-pair`, `partial` |
| `versions` | what each binary reports |
| `encoders` | which of `libx264`, `h264_nvenc`, `h264_qsv`, `h264_amf`, `h264_mf`, `libx265`, `libvpx-vp9`, `libmp3lame`, `aac`, `libopus` exist |
| `filters` | `scale`, `tile`, `select`, `drawtext`, `palettegen`, `silencedetect` |
| `python_modules` | `PIL` (contact-sheet labels), `numpy` |
| `warnings` | act on these before blaming a command |

If ffmpeg is missing (`"ok": false`), install a private verified pair **without asking** — it is
the skill's own dependency, not a change to the user's project:

```bash
python video_ops.py bootstrap --download          # ~100 MB from a public ffmpeg mirror
python video_ops.py doctor                        # confirm, then continue with the task
```

`bootstrap` also accepts a local route when the machine has no network or already has a build:
`--source <ffmpeg.exe|folder|.zip|.7z|.tar.xz>`. It verifies that the installed binary actually
runs before reporting success. Files land in `%DSH_HOME%\video\tools` (`--source`/env
`DSH_FFMPEG_DIR`/`DSH_FFMPEG_FFMPEG`/`DSH_FFMPEG_FFPROBE` override discovery).

Not every ffmpeg build has every codec. A build without `libx264` still encodes through
`h264_mf`/`h264_nvenc`/`h264_amf`, and the commands below pick a working encoder automatically.
When quality matters and `libx264` is absent, say so instead of pretending the output is optimal.

## 1. Probe before you ask

```bash
python video_ops.py probe "video.mp4" --json-out probe.json
```

Returns duration, container, bitrate, size, and per-stream codec, resolution, fps, pixel format,
rotation, sample rate, channel layout, chapters and language tags. Cheap; always run it first.

If `ffprobe` is genuinely unavailable, the same command answers for MP4/MOV/M4A/3GP from a
bundled pure-Python ISO-BMFF reader and marks the payload with `"probe_note"`.

The act-now facts you must decide from the probe, not from the filename:

- **duration** — drives sampling counts and clip bounds;
- **has_audio** — task may be audio-only; audio tools reject silent files loudly;
- **resolution / rotation** — a 1920x1080 stream tagged `rotate: 90` is portrait to the viewer;
- **container** — `.webm` as a target needs VP9/Opus; `.mp4` with H.264 is the safe default.

If the user asked a question about the *content* ("what happens in this clip?"), no probe
substitutes for looking. Go to step 3.

## 2. Never invent metadata

Every number you report (duration, fps, resolution, bitrate, silence window, loudness) must come
from a command output in this session. Do not estimate from file size, and do not trust a filename.
If a command fails, relay its JSON `error` and the fix it names.

## 3. Look at the picture: frames and contact sheets

```bash
python video_ops.py sheet "video.mp4" --count 12 --cols 4        # one labeled grid image
python video_ops.py frames "video.mp4" --at 00:00:03 --at 45.5 --width 1280
python video_ops.py frames "video.mp4" --count 8                 # evenly spaced stills
python video_ops.py frames "video.mp4" --every 30                # every 30 s
```

`sheet` is the fast path for "what is in this video": one image, cells left to right, top to
bottom, each stamped with its timecode. Read it with `read_image`.

`frames` writes individual stills into `<input>_frames/`. Use it when a specific moment matters
(long enough to need full resolution) or when a sheet's cells are too small.

Then zoom in on anything interesting:

```bash
python video_ops.py clip "video.mp4" --start 00:01:10 --duration 6 --out look_here.mp4
python video_ops.py frames look_here.mp4 --count 6 --width 1440
```

Practical guidance:

- 6–12 cells is the readable range for a contact sheet; 100 cells is a thumbnail wall, not evidence.
- `--at` accepts `12.5`, `00:01:10`, `01:02:03.500`. Use the times the probe or a scene list gave you.
- Reading frames is the expensive step. Extract the smallest set that answers the question, then
  look once.
- Text on screen (subtitles, slides, UI) needs full resolution: `--width 1600` and a tight time.

## 4. Cut, compress, convert, resize, GIF

```bash
python video_ops.py clip "video.mp4" --start 00:02:00 --end 00:02:30 --out part.mp4
python video_ops.py clip "video.mp4" --start 10 --duration 5 --copy     # fast, keyframe aligned
python video_ops.py compress "video.mp4" --crf 26 --max-width 1280 --out small.mp4
python video_ops.py compress "video.mp4" --target-mb 25                 # aim at a size budget
python video_ops.py convert "video.mkv" --to mp4                        # container/codec change
python video_ops.py scale "video.mp4" --size 1080p --fit pad --out hd.mp4
python video_ops.py gif "video.mp4" --start 12 --duration 4 --width 720 --fps 12
python video_ops.py scenes "video.mp4" --threshold 0.35 --frames shots/   # shot boundaries
```

- `--copy` is stream copy: near-instant and lossless, but it starts on the nearest keyframe, so
  the useful range can drift by a second or two. Use a re-encode when the exact frame matters.
- `compress` writes H.264 + AAC with `+faststart`, which plays everywhere. `--crf` 23 (light) to
  30 (aggressive); `--target-mb` computes a bitrate from the real duration.
- `convert --to webm` needs VP9/Opus in the build; check `doctor` before promising it.
- `scale --fit pad|crop` deals with aspect changes; a bare `--size` stretches.
- Re-encoding is always slower than copying and always changes quality. State which one you did.

## 5. Audio: extract, normalise, find silence

```bash
python video_ops.py audio "video.mp4" --codec libmp3lame --bitrate 192k --out track.mp3
python video_ops.py audio "video.mp4" --codec pcm_s16le --rate 16000 --mono --out voice.wav
python video_ops.py normalize "voice.wav" --lufs -16 --out voice_norm.m4a
python video_ops.py loudness "video.mp4" --ebur128           # integrated LUFS, LRA, true peak
python video_ops.py silence "video.mp4" --noise-db -35 --min-duration 0.6
```

`silence` returns both `silences` and the complementary `speech` windows; those windows are the
natural cut points for a highlight reel or the ranges worth transcribing. 16 kHz mono PCM is the
right hand-off format for any speech-to-text tool.

`transcribe` exists but deliberately ships no model: it drives a local `whisper-cli` when one is
present (`--binary`, `--model`) and otherwise reports how to get one. Never claim a transcript you
did not produce.

## 6. Reports

```bash
python video_ops.py report "video.mp4"        # <input>_report.md + .html + _assets/sheet.jpg
```

Markdown plus a self-contained HTML page (metadata table, stream list, embedded contact sheet).
Deliver the `.html` for human review and read `_assets/sheet.jpg` yourself. `present` the video
artifact itself when the user asked for a file they can play.

## 7. Answering "DSH cannot handle video"

DSH does not decode video for the model. That is why this skill exists. The answer is never
"unsupported": probe the file, pull a contact sheet, read it, then act, and hand back a file
the user can open. When the user's request is only about *seeing* the video, a contact sheet plus
targeted full-resolution stills is the honest, complete answer.

## Rules

- Probe first; every reported number comes from a command in this session.
- Look with `read_image` before describing content. A sheet is evidence; a filename is not.
- Quote paths — they contain spaces and CJK characters.
- Report absolute output paths, plus size and duration after a re-encode.
- Prefer stream copy when lossless is acceptable; say clearly when you re-encoded.
- Do not raise resolution or bitrate to "improve" quality; that only inflates the file.
- Keep the source file untouched. Write outputs beside it or into `--outdir`.
- If a build lacks a codec, say so and use the fallback instead of silently degrading.
- Never claim to have watched, heard, or transcribed media you only probed.

## Read when

- `references/commands.md` — every command with its flags and JSON fields.
- `references/workflows.md` — task recipes: summarize a video, cut highlights, compress for chat, subtitle prep, batch triage, screen-recording review.
- `references/troubleshooting.md` — bootstrap failures, missing codecs, permission/sandbox denials, CJK paths, huge files, ffmpeg version differences.
- `SKILL.zh-CN.md` — the same playbook in Chinese.
