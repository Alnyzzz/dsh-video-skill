# Troubleshooting

Read the JSON error before retrying anything: every command returns a specific `error` string plus
`ok: false`, and most name the exact fix.

## ffmpeg is missing — `doctor` says `"ok": false`

```bash
python <S> bootstrap --download
```

- No network, or the mirror is unreachable: use a local build —
  `bootstrap --source "C:\path\to\ffmpeg\bin"` (a folder containing `ffmpeg.exe`/`ffprobe.exe`),
  or `--source ffmpeg.zip`, `--source ffmpeg.7z`, or a single `ffmpeg.exe`.
- Download fails on all mirrors: the error lists each URL and each backend tried (curl → urllib →
  Invoke-WebRequest). Proxy-heavy or TLS-restricted networks usually succeed with
  `--source` against a build the user already has (players and editors bundle one).
- `.7z` sources need 7-Zip (`7z`/`7za` on PATH or `C:\Program Files\7-Zip\7z.exe`); `.zip` and
  `.tar.xz` need nothing extra.
- Corporate proxy: set `HTTPS_PROXY`/`HTTP_PROXY`; curl honours them automatically.

## A binary was found but cannot run

`doctor` reports such a candidate as absent, and the payload mentions nothing about it — that is
deliberate, a candidate that fails `-version` is dropped. Common causes:

- a truncated or corrupt bundled `ffmpeg.exe` (players sometimes ship stubs);
- a binary whose sibling DLLs were not copied (the skill copies `*.dll` next to the binary when it
  installs, so reinstall with `--source <that folder>` rather than moving the exe alone);
- a 32-bit/64-bit mismatch or a missing VC++ runtime — install the Microsoft Visual C++
  Redistributable, or bootstrap a static build instead.

## `ffprobe` missing but `ffmpeg` present

`probe` still works for MP4/MOV/M4A/3GP through the bundled pure-Python ISO-BMFF reader; the JSON
carries a `probe_note` saying so. For other containers (MKV, WebM, TS, AVI) install a pair:
`bootstrap --download`. A mixed pair (newer ffmpeg + separate ffprobe) is normal and reported as
`verified-pair`/`mixed-pair`.

## `Unknown encoder 'libx264'`

Not every build is a full build. `doctor` lists what the selected binary actually has.

- The commands fall back automatically to `h264_mf` (Windows Media Foundation), `h264_nvenc`,
  `h264_qsv`, `h264_amf`, or `mpeg4`. Quality and speed then differ from libx264.
- `libmp3lame` missing: use `--codec aac` or `pcm_s16le`.
- `libvpx-vp9` missing: `--to webm` cannot work; convert to `mp4` or `mkv` instead.
- If the output does not need to be small, `--copy` avoids the encoder entirely.

## "Invalid data found when processing input"

The file is not what its extension says, is truncated, or is a still image/multipart stream.
`probe` first and read `format`; if ffprobe cannot identify it either, the file is the problem, not
the tool. Do not loop retries hoping for a different answer.

## CJK, spaces or emoji in paths

Every argument is passed as an array element (never through a shell), so quoting is handled — but
quote paths in your own command line anyway. If a path is denied by the DSH sandbox, write outputs
into the session workspace instead (`--outdir .`), then move them if the user needs them elsewhere.

## The write is denied / "sandbox: file access denied"

The DSH file policy confines writes to the workspace (or the private tool directory
`%DSH_HOME%\video`). Options:

- write into the workspace (`--outdir .`), which is always allowed;
- ask the user to widen the policy for the final destination;
- the private tools directory under `DSH_HOME` is allowed for bootstrap output by design.

## Very large files (multi-GB, long recordings)

- `probe` and `frames` are cheap; `sheet` runs one ffmpeg per cell (12 cells = 12 seeks, seconds).
- Do not use `--all-frames` on anything long; thin it with `--fps 1` or `--every 30` instead.
- `scenes` decodes the whole file; it is the slowest command here. On a 3-hour recording expect
  minutes. Prefer `--every` sampling when the goal is only an overview.
- `compress`/`convert`/`gif` are single passes but write a full-length output; check free space and
  tell the user the expected size before starting.

## Frames come out black or empty

- Past the end of the stream: `probe` the duration and keep timestamps inside it.
- VFR/animated sources: seek by `--at` near the moment, or sample with `--count` (which derives
  timestamps from the real duration).
- Hardware-accelerated decoders on a broken driver: re-run with a *software* ffmpeg (bootstrap
  downloads a software build).
- A file with only an audio stream: `frames` correctly produces nothing; check `has_audio` and
  `stream_count.video` in the probe.

## Contact sheet has no timestamps

Pillow is missing (`doctor` → `python_modules.PIL: false`), so the sheet came from the ffmpeg
`tile` filter. Either use the DSH Python (it bundles Pillow), install Pillow, or read the frame
`at` values from `frames` output instead.

## `silence` finds nothing (exit 4)

The noise floor is above the threshold or the pauses are shorter than `--min-duration`. Raise
`--noise-db` toward `-25`, lower `--min-duration` to `0.3`. Silence detection needs an audio
stream: `probe` will show `has_audio: false` for silent screen recordings.

## `transcribe` returns `ok: false` (exit 5)

By design: no ASR model ships with this skill. Give the user the extracted WAV and one of:

1. install whisper.cpp and pass `--binary` + `--model`;
2. use DSH's own voice-input feature for dictation;
3. paste the audio into any service they already trust.

Never invent a transcript.

## Version differences between ffmpeg builds

Older builds (5.1 and earlier) lack some filter syntax. The skill avoids the fragile forms:
contact sheets are composed in Python when possible, silence/loudness parsing tolerates both old
and new log formats, and `probe` falls back to `ffmpeg -print_format json` when `ffprobe` is absent.
If a specific filter is reported missing in `doctor.filters`, choose another route rather than
repeating the failed command.

## Repeated slowness on every call

The first lookup on a machine walks the disks for bundled copies (a few seconds) and caches the
result in `<video_home>/tools/candidates.json`. Delete that file, or pass `--rescan`, to search
again after installing ffmpeg somewhere new.
