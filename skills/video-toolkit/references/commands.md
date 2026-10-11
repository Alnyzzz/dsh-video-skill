# Command reference

All commands: `python "<skill-dir>/scripts/video_ops.py" <command> [options]`

Shared options on every command:

| Option | Effect |
| --- | --- |
| `--print-command` | print the ffmpeg command line, run nothing |
| `--json-out <file>` | also write the JSON payload to this path |
| `--rescan` | walk the disks again for ffmpeg/ffprobe instead of reusing the candidate cache |
| `--bootstrap` | install a private ffmpeg/ffprobe pair first if none is usable |
| `--download` | allow that install step to download (~100 MB) |
| `--source <path>` | install from a local binary/folder/`.zip`/`.7z`/`.tar.xz` instead |

Output paths default to `<input>_<suffix><ext>` beside the input; `--out` names a file,
`--outdir` names a directory.

Exit codes: `0` success · `3` tool/ffmpeg failure · `4` nothing produced (no frame / no scene /
no silent stretch found) · `5` no speech-to-text backend · `6` transcription failed · `130`
interrupted. Non-zero always comes with a JSON `error` or an explanatory field.

---

## doctor

`doctor [--rescan] [--bootstrap] [--download] [--source PATH]`

Diagnoses the toolchain. Read it before any video task on a new machine.

| Field | Meaning |
| --- | --- |
| `platform`, `video_home`, `tools_dir` | where this skill keeps its private tools |
| `found.{ffmpeg,ffprobe,paired,source}` | the selected executables and how they were found |
| `candidates` | runnable candidates, best first |
| `versions` | first line of `ffmpeg -version` / `ffprobe -version` |
| `encoders`, `filters` | capability flags actually probed on the selected binary |
| `python_modules` | `PIL`, `numpy` availability (affects contact-sheet labels) |
| `speech_to_text` | any `whisper-cli`/`whisper` found on PATH |
| `warnings`, `ok` | what to fix; `ok: false` blocks video work |

## bootstrap

`bootstrap [--download] [--source PATH] [--print-command]`

Installs a verified ffmpeg/ffprobe pair into `<video_home>/tools`. Sources in order:
`--source`, then mirrors (`gyan.dev` release-essentials, its GitHub mirror, BtbN win64-gpl for
Windows; evermeet for macOS; johnvansickle static for Linux). Reports `installed`, `ffprobe`,
`source`, `versions`. Download backends: `curl`, then `urllib`, then PowerShell.

## probe

`probe <input>`

| JSON field | Notes |
| --- | --- |
| `duration_s`, `duration_human` | seconds and `mm:ss.mmm` / `hh:mm:ss.mmm` |
| `size`, `size_human`, `bit_rate` | from the container, not the file listing alone |
| `format`, `format_long`, `tags`, `chapters` | container identity and metadata |
| `streams[]` | `type`, `codec`, `profile`, `bit_rate`, `language`, plus `width`/`height`/`fps`/`pix_fmt`/`frames`/`rotation` for video and `sample_rate`/`channels`/`channel_layout` for audio |
| `stream_count`, `has_audio` | quick gates for later commands |
| `probe_note` | present only when the pure-Python ISO-BMFF reader was used |

## frames

`frames <input> [--at T]... [--count N] [--every SEC] [--all-frames] [--fps F] [--width W] [--format jpeg|png|webp] [--quality 2-31] [--outdir DIR]`

| Selector | Result |
| --- | --- |
| `--at 00:01:10 --at 45.5` | exactly those timestamps |
| `--count 8` | 8 evenly spaced stills |
| `--every 30` | one still every 30 s (needs a known duration) |
| `--all-frames [--fps 2]` | every frame, or `fps`-thinned frames (large) |
| none | the first frame |

Writes `<input>_frames/<stem>_<NNN>_<time>.<ext>` and returns `frames[]` with `at_s`, `at`,
`file`, `bytes` — `file: null` with `error` when a timestamp produced nothing (usually past the
end of the stream). `--width` defaults to 960 and keeps the aspect ratio.

## sheet

`sheet <input> [--out FILE] [--count 12] [--cols 4] [--rows N] [--cell-width 480] [--frame-width 960] [--no-labels] [--label-size 12]`

One labeled contact-sheet image, default `<input>_sheet.jpg`. Cells run left to right, top to
bottom, each stamped with its timecode. Pillow composes and labels it; if Pillow is missing it
falls back to the ffmpeg `tile` filter and reports `"engine": "ffmpeg tile filter"` without
labels. Read the result with `read_image`.

## scenes

`scenes <input> [--threshold 0.35] [--frames DIR] [--width W]`

Shot-boundary detection. Returns `scene_count` and `scenes[]` with `start_s`/`start`/`end_s`/`end`.
`--frames DIR` also exports one still per scene, which turns a long recording into a readable set.
Exit `4` when no boundary crosses the threshold — that is a normal answer for a single-take video,
not an error to hide.

## clip

`clip <input> --start T [--end T | --duration T] [--out FILE] [--outdir DIR] [--copy] [--crf N] [--preset P] [--video-encoder NAME] [--audio-encoder NAME] [--audio-bitrate 128k]`

`--copy` remuxes without re-encoding: fast and lossless, snapped to keyframes. Without it the
segment is re-encoded and frame-accurate. Returns `mode`, `start_s`, `end_s`, sizes and
`size_ratio`.

## compress

`compress <input> [--crf 26] [--preset medium] [--target-mb N] [--max-width W] [--out FILE]`

H.264 + AAC + `+faststart`. `--crf` is quality-based (lower is better/larger); `--target-mb`
computes a video bitrate from the real duration instead. Returns `out_size_human` and
`size_ratio` so the saving is a fact, not a claim.

## convert

`convert <input> --to mp4|mkv|webm|mov [--max-width W] [--no-audio] [--crf N] [--preset P]`

Re-encodes into another container/codec. `webm` needs VP9/Opus support in the build.

## scale

`scale <input> --size 1280x720|1920:-2|720p|1080p|4k [--fit stretch|pad|crop]`

`-2` keeps the aspect ratio on that axis. `pad` letterboxes, `crop` fills and trims, `stretch`
(default) distorts. Audio is stream-copied.

## gif

`gif <input> [--start 0] [--duration 4] [--width 640] [--max-height H] [--fps 12] [--out FILE]`

Two-pass palette GIF (`palettegen` + `paletteuse`), which looks far better than a naive GIF.
Reports both command lines; the temporary palette file is removed.

## audio

`audio <input> [--codec aac|libmp3lame|libopus|pcm_s16le|flac|libvorbis|copy] [--bitrate 192k] [--rate 44100] [--mono] [--out FILE]`

Extracts/transcodes the audio track (`-vn`). Default extension follows the codec:
`.m4a`, `.mp3`, `.opus`, `.wav`, `.flac`, `.ogg`. `pcm_s16le --rate 16000 --mono --out x.wav` is
the standard hand-off for speech-to-text. Returns `result.size_human` and `result.duration_s`.

## normalize

`normalize <input> [--lufs -16] [--true-peak -1.5] [--lra 11] [--codec aac] [--out FILE]`

Two-pass-free EBU R128 `loudnorm`. `-16` LUFS suits podcasts/social, `-14` streaming, `-23`
broadcast.

## silence

`silence <input> [--noise-db -35] [--min-duration 0.6]`

Returns `silences[]` and the complementary `speech[]` windows, plus `silent_total_s`. Raise
`--noise-db` toward `-25` in noisy rooms; use the speech windows as clip boundaries or
transcription ranges.

## loudness

`loudness <input> [--ebur128]`

`--ebur128` gives `integrated_lufs`, `loudness_range_lu`, `true_peak_dbfs`. Default `volumedetect`
gives `mean_dbfs` and `max_dbfs` (cheaper, also works on old builds).

## report

`report <input> [--out FILE] [--count 12] [--cols 4] [--frame-width 960] [--cell-width 480] [--frames]`

Writes `<input>_report.md`, `<input>_report.html` and `<input>_report_assets/sheet.jpg`.
The HTML is self-contained apart from that image. Delivering `report` in one call is usually
better than chaining `probe` + `sheet` by hand.

## transcribe

`transcribe <input> [--binary PATH] [--model FILE] [--language auto] [--out PREFIX]`

Drives a local `whisper-cli`/`whisper` binary; writes `.txt` and `.srt` beside the input. With no
binary it returns `ok: false` plus the ways to get one, and exit code `5`. It ships no model on
purpose: ASR weights are hundreds of megabytes and belong to the user's machine, not to a skill.
