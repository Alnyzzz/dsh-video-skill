# Task recipes

Each recipe is a closed loop: probe, look, act, verify, report. Replace `<input>` with a quoted
real path. `<S>` is `scripts/video_ops.py` from this skill directory.

## 1. "What is this video?" (the most common request)

```bash
python <S> probe "<input>"                       # duration, resolution, fps, audio?
python <S> sheet "<input>" --count 12 --cols 4   # one labeled grid
```

Read the sheet with `read_image`. If a cell shows something the user will care about, go get that
moment at full resolution before describing it:

```bash
python <S> frames "<input>" --at 00:01:32 --width 1600
```

Then answer with: what it is, how long, the resolution/fps, whether it has audio, and the
content read from the stills. Report only numbers you saw in the probe output.

## 2. Summarize a long recording (lecture, stream, meeting, gameplay)

```bash
python <S> probe "<input>"
python <S> scenes "<input>" --threshold 0.3 --frames shots/    # if it has cuts
python <S> sheet "<input>" --count 20 --cols 5                 # if it is one continuous take
python <S> audio "<input>" --codec pcm_s16le --rate 16000 --mono --out voice.wav
```

`scenes` gives the chapter skeleton for edited material; the 20-cell sheet is the honest overview
for a single-take recording. `voice.wav` is the hand-off for transcription; if no ASR backend is
installed, say so and summarize from the stills instead of guessing at speech.

## 3. Cut highlights or a specific moment

```bash
python <S> silence "<input>" --noise-db -35 --min-duration 0.8   # speech windows
python <S> clip "<input>" --start 00:04:12 --duration 18 --out highlight.mp4
python <S> frames highlight.mp4 --count 4 --width 1280           # verify what you cut
```

Verify by looking at the cut, not by trusting the timestamps. Re-encode (omit `--copy`) whenever
the boundary matters; `--copy` drifts to the nearest keyframe.

## 4. Shrink a file for chat, email, or a slide

```bash
python <S> compress "<input>" --target-mb 20 --max-width 1280 --out small.mp4
python <S> probe small.mp4           # confirm it still plays and has audio
```

`--target-mb` computes the bitrate from the real duration; `--max-width 1280` is usually enough
for review. Report both sizes and the ratio. If the source is already smaller than the target do
not "compress" it — say so.

## 5. Format conversion that must play everywhere

```bash
python <S> convert "<input>" --to mp4 --max-width 1920 --out universal.mp4
python <S> probe universal.mp4
```

MP4 + H.264 + AAC + `+faststart` is the most compatible combination. Check `doctor` for
`h264_mf`/`h264_nvenc` when `libx264` is absent; never promise a codec the build lacks.

## 6. Animated GIF from a moment

```bash
python <S> sheet "<input>" --count 12 --cols 4     # find the moment
python <S> gif "<input>" --start 00:00:42 --duration 3 --width 720 --fps 12 --out clip.gif
```

Look at the GIF's source range first; a GIF of the wrong three seconds is worse than none.

## 7. Speech-to-text preparation

```bash
python <S> audio "<input>" --codec pcm_s16le --rate 16000 --mono --out voice.wav
python <S> normalize voice.wav --lufs -16 --out voice_norm.m4a
python <S> silence voice_norm.m4a --noise-db -35 --min-duration 0.5
python <S> transcribe voice.wav --binary <path-to-whisper-cli> --model <ggml-model.bin>
```

16 kHz mono PCM is what ASR engines want; loudness normalisation improves accuracy in quiet
recordings. If `transcribe` reports `ok: false`, hand the user the WAV and the options it printed.

## 8. Audio-only job (podcast, voice memo, music)

```bash
python <S> probe "<input>"
python <S> loudness "<input>" --ebur128
python <S> normalize "<input>" --lufs -16 --out episode_norm.m4a
python <S> silence "<input>"
```

Watch for `has_audio: false` — then nothing in this section applies and you should say the file
has no audio track.

## 9. Screen-recording review (UI demo, bug repro)

```bash
python <S> probe "<input>"
python <S> frames "<input>" --every 5 --width 1600 --outdir steps/
```

Text-heavy frames need full width. Read the stills in order, then describe the UI flow as a
sequence of steps. For a specific complaint ("it breaks around 40 s") sample tightly:

```bash
python <S> frames "<input>" --at 00:00:38 --at 00:00:40 --at 00:00:42 --width 1600
```

## 10. Batch triage of a folder

```bash
python <S> doctor
python <S> report "<A>"      # per file: markdown + html + sheet
python <S> report "<B>"
```

Run `probe` first across the set when you only need an inventory, and `report` only for the files
the user actually asked about. Contact sheets are images: reading twenty of them costs a lot of
context. Triage with `probe`, then look at the two or three files that matter.

## 11. Vertical / social reformat

```bash
python <S> scale "<input>" --size 1080x1920 --fit pad --out vertical.mp4
python <S> gif vertical.mp4 --start 0 --duration 2 --width 540 --out teaser.gif
```

`pad` letterboxes (keeps the whole frame, safe), `crop` fills the frame (loses the edges, more
native-looking). Say which one you chose and why.

## 12. Extract one still as an image deliverable

```bash
python <S> frames "<input>" --at 00:00:12.4 --width 1920 --format png --outdir stills/
```

Returns the absolute `file` path; hand that to the user, or `present` it if a separate file card
helps.
