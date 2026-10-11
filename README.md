# dsh-video-skill

A downloadable [DeepSeek Harness](https://github.com/Alnyzzz/dsh-video-skill) (DSH) skill that lets the agent actually work with **video and audio files**: probe them, look at the picture, cut and convert them, and hand back files the user can play.

DSH has no idea what a `.mp4` contains, so the agent used to answer "I cannot process video". With this skill installed, it can:

- read duration, resolution, fps, codecs, audio layout, rotation, chapters;
- pull frames and build a **labeled contact sheet** — then *look* at it with `read_image`;
- cut clips (frame-accurate or lossless stream copy), compress to a size budget, convert containers, resize, make palette-optimised GIFs, detect shot boundaries;
- extract, normalise and measure audio (EBU R128 loudness, silence/speech windows);
- write a Markdown + HTML inspection report with an embedded contact sheet.

Everything is one script driven by `ffmpeg`/`ffprobe`, and the skill installs its own private, verified ffmpeg pair when the machine has none.

## Install

### 1. Get the files

```bash
git clone https://github.com/Alnyzzz/dsh-video-skill.git
cd dsh-video-skill
```

(Or download the ZIP from the green **Code** button and unpack it.)

### 2. Install the skill into DSH

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1            # add -Bootstrap to fetch ffmpeg now
```

```bash
# macOS / Linux
bash scripts/install.sh                                                   # add --bootstrap to fetch ffmpeg now
```

The installer copies `skills/video-toolkit` into `<DSH_HOME>/skills/video-toolkit`, verifies the bundle, and runs a toolchain check. DSH's filesystem skill provider watches that directory, so **no restart is needed** — the skill shows up in the session catalog on the next step.

Manual install (equivalent):

```powershell
# Windows
Copy-Item -Recurse .\skills\video-toolkit "$env:DSH_HOME\skills\video-toolkit"
```

```bash
# macOS / Linux
mkdir -p "${DSH_HOME:-$HOME/.dsh}/skills"
cp -R skills/video-toolkit "${DSH_HOME:-$HOME/.dsh}/skills/video-toolkit"
```

Other valid skill roots, in DSH's priority order: `<project>/.dsh/skills`, `<project>/.agents/skills`, any directory listed in the `dsh-skill-filesystem` plugin's `customSkillDirs`, `<DSH_HOME>/skills`, `~/.agents/skills`.

### 3. Give the agent ffmpeg (usually automatic)

Ask the agent to check the toolchain, or run it yourself:

```bash
python "<skill>/scripts/video_ops.py" doctor
```

If ffmpeg is missing, the skill can install a private pair — it never touches system directories:

```bash
python "<skill>/scripts/video_ops.py" bootstrap --download
# or from a build the machine already has:
python "<skill>/scripts/video_ops.py" bootstrap --source "C:\path\to\ffmpeg\bin"
```

`bootstrap --download` builds a mirror list for the current OS (gyan.dev release-essentials, its GitHub mirror, BtbN win64-gpl, plus public mirrors), downloads **up to three mirrors in parallel**, keeps the first complete archive, verifies that the extracted binary runs, pairs `ffmpeg` with `ffprobe`, and copies them to `<DSH_HOME>/video/tools`. A partial or stalled transfer is retried/resumed. Windows prefers the ~30 MB `.7z` when 7-Zip is present, otherwise the `.zip`.

Already have ffmpeg somewhere unusual? Point the skill at it instead of downloading:

```bash
DSH_FFMPEG_DIR=/opt/ffmpeg/bin          # a folder holding ffmpeg + ffprobe
DSH_FFMPEG_FFMPEG=/usr/local/bin/ffmpeg # an explicit binary
python "<skill>/scripts/video_ops.py" doctor --rescan
```

## Use it

Just ask in chat:

> "Summarize this video: D:\clips\demo.mp4"
> "Cut 00:01:10–00:01:28 into a new mp4 and compress it under 20 MB."
> "What is in this webm? Make me a GIF of the intro."
> "Transcribe the audio of this meeting recording." *(needs a local whisper.cpp)*
> "/video-toolkit report this file"

Or drive the CLI directly:

```bash
S="<skill>/scripts/video_ops.py"
python "$S" doctor
python "$S" probe  video.mp4
python "$S" sheet  video.mp4 --count 12 --cols 4
python "$S" frames video.mp4 --at 00:01:10 --width 1600
python "$S" clip   video.mp4 --start 00:01:10 --duration 18 --out highlight.mp4
python "$S" compress video.mp4 --target-mb 20 --max-width 1280
python "$S" gif    video.mp4 --start 12 --duration 4 --width 720
python "$S" audio  video.mp4 --codec pcm_s16le --rate 16000 --mono --out voice.wav
python "$S" silence video.mp4 --noise-db -35 --min-duration 0.6
python "$S" report video.mp4
```

Every command prints one JSON object; `--print-command` shows the ffmpeg line instead of running it. Full flag and field reference: [`skills/video-toolkit/references/commands.md`](skills/video-toolkit/references/commands.md).

| Command | What it is for |
| --- | --- |
| `doctor` | which ffmpeg/ffprobe, encoders, filters, Python modules |
| `bootstrap` | install a private verified ffmpeg + ffprobe |
| `probe` | duration, streams, codecs, fps, rotation, chapters |
| `frames` | stills by timestamp / count / interval, or every frame |
| `sheet` | one timestamp-labeled contact sheet image |
| `scenes` | shot boundaries, optionally one still per scene |
| `clip` | cut a segment (copy or re-encode) |
| `compress` | CRF or target-size compression |
| `convert` | mp4 / mkv / webm / mov |
| `scale` | resize with stretch / pad / crop |
| `gif` | two-pass palette GIF |
| `audio` | extract or transcode the audio track |
| `normalize` | EBU R128 loudness normalisation |
| `silence` | silent stretches and speech windows |
| `loudness` | integrated LUFS / LRA / true peak, or volumedetect |
| `report` | Markdown + HTML report with an embedded sheet |
| `transcribe` | speech-to-text through a local whisper.cpp build |

## Requirements

- **DSH** with the filesystem skill provider (any recent desktop build).
- **Python 3.9+** — DSH ships one at `<DSH_HOME>/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe`; the DSH Python also bundles Pillow, which is what draws the timestamp labels on contact sheets.
- **ffmpeg + ffprobe** — installed automatically by `bootstrap`, or taken from PATH / WinGet / Scoop / Chocolatey / Homebrew / apt / an app that bundles one.
- No Python packages are required: the scripts are standard-library only. Pillow is optional (contact-sheet labels); without it the sheet falls back to the ffmpeg `tile` filter.

## How it behaves on real machines

- **Finds what is already there.** `doctor` probes environment overrides, the private tools folder, PATH, package-manager locations, and — once, with the result cached — user app-data trees and secondary drives, because media players and editors often ship a full ffmpeg.
- **Never runs a binary it did not verify.** Candidates that fail `-version` are dropped, and only candidates from the private tools folder, environment overrides, PATH, known package locations, or the cached scan list are executed.
- **Adapts to build differences.** `-crf` belongs to libx264/libx265; NVENC wants `-cq`, QSV `-global_quality`, AMF `-qp_i/-qp_p`, Media Foundation `-quality`, mpeg4 `-q:v`. The skill validates the chosen encoder with a one-frame test encode and falls back through the chain, reporting `encoder` and `encoder_note` so the result is never misrepresented. Same for audio (`libmp3lame` missing → AAC, with the extension adjusted).
- **Tells the truth about what it did.** `--copy` (lossless remux) versus re-encode, `engine: Pillow` versus `ffmpeg tile filter`, `probe_note` when the built-in MP4 reader was used instead of ffprobe, exit code 4 when there is genuinely no scene or silence to report, exit 5 when no speech-to-text backend exists.

## Repository layout

```
dsh-video-skill/
├── skills/video-toolkit/            # the skill bundle (this is what DSH loads)
│   ├── SKILL.md                     # English playbook (frontmatter: name + description)
│   ├── SKILL.zh-CN.md               # Chinese playbook
│   ├── references/
│   │   ├── commands.md              # every command, flag and JSON field
│   │   ├── workflows.md             # 12 task recipes
│   │   └── troubleshooting.md       # bootstrap, codecs, sandbox, CJK paths, big files
│   └── scripts/
│       ├── video_ops.py             # the CLI (all commands)
│       └── video_common.py          # tool discovery, bootstrap, helpers
├── scripts/
│   ├── install.ps1                  # Windows installer (copies into <DSH_HOME>/skills)
│   └── install.sh                   # macOS / Linux installer
├── docs/
│   └── SKILL-FORMAT.md              # how a DSH skill bundle is structured
└── .github/workflows/ci.yml         # syntax + bundle validation on every push
```

## Uninstall

```powershell
Remove-Item -Recurse "$env:DSH_HOME\skills\video-toolkit"
Remove-Item -Recurse "$env:DSH_HOME\video"        # optional: the private ffmpeg pair
```

## Notes on privacy and safety

- Nothing here phones home. The only network activity is the explicit `bootstrap --download` (or a `--to` target that needs a codec the build lacks, which never downloads anything).
- Video and audio files are processed locally; no frame, audio sample or transcript leaves the machine because of this skill.
- Outputs are written beside the input unless `--out`/`--outdir` says otherwise; under DSH's workspace-write policy the workspace is a safe destination.

## License

MIT — see [LICENSE](LICENSE). Bundled/downloaded ffmpeg builds keep their own licences (gyan.dev essentials is GPL v3; BtbN win64-gpl is GPL v3). The skill does not redistribute ffmpeg.

---

Author: **Alnyzzz**
