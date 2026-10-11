#!/usr/bin/env python3
"""End-to-end smoke test: build a synthetic clip, run every command that does not need
extra tooling, and assert the JSON contract. Used by CI and runnable locally.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

SKILL = pathlib.Path(__file__).resolve().parent.parent / "skills" / "video-toolkit" / "scripts" / "video_ops.py"
REPO = SKILL.parents[3]

# The child prints UTF-8 JSON; on a Chinese Windows the console code page is GBK, so the
# decode has to be pinned or subprocess.run raises before the assertion can run.
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def run(*args: str, expect: int = 0) -> dict:
    proc = subprocess.run([sys.executable, str(SKILL), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=CHILD_ENV)
    if proc.returncode != expect:
        raise AssertionError(f"{' '.join(args)} exited {proc.returncode}, expected {expect}\n"
                             f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    text = (proc.stdout or "").strip()
    if not text:
        return {}
    start = text.find("{")
    return json.loads(text[start:])


def main() -> int:
    doctor = run("doctor")
    assert doctor["ok"], f"no usable ffmpeg: {doctor}"
    ffmpeg = doctor["resolved"]["ffmpeg"]
    print(f"ffmpeg: {ffmpeg}")
    print(f"ffprobe: {doctor['resolved']['ffprobe']}")
    print(f"encoders: {[k for k, v in doctor['encoders'].items() if v]}")

    # Scratch lives inside the repository, not the system temp directory: under DSH's
    # workspace-write policy a sandboxed run cannot write there (and mkdtemp's 0700 mode
    # is refused by the sandbox ACL), so every command would fail for the wrong reason.
    work = REPO / ".smoke-work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        source = work / "sample.mp4"
        encoder = "libx264" if doctor["encoders"].get("libx264") else "mpeg4"
        subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=6",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                        "-c:v", encoder, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                        str(source)], check=True)

        probe = run("probe", str(source))
        assert probe["duration_s"] and 5.5 < probe["duration_s"] < 6.5, probe
        assert probe["has_audio"], probe
        assert probe["stream_count"]["video"] == 1, probe
        print(f"probe ok: {probe['duration_human']}, {probe['streams'][0]['width']}x{probe['streams'][0]['height']}")

        frames = run("frames", str(source), "--count", "3", "--width", "320")
        assert frames["ok_count"] == 3, frames
        for frame in frames["frames"]:
            assert pathlib.Path(frame["file"]).exists(), frame
        print("frames ok")

        sheet = run("sheet", str(source), "--count", "4", "--cols", "2")
        assert pathlib.Path(sheet["sheet"]).exists(), sheet
        print(f"sheet ok via {sheet['engine']}: {sheet['sheet']}")

        scenes = run("scenes", str(source), "--threshold", "0.4", expect=4)
        assert scenes["scene_count"] == 0, scenes
        print("scenes ok (single-take clip reports no cut, exit 4)")

        clip = run("clip", str(source), "--start", "1", "--duration", "2", "--out", str(work / "cut.mp4"))
        assert pathlib.Path(clip["out"]).exists(), clip
        print(f"clip ok with encoder {clip.get('encoder')}: {clip.get('encoder_note')}")

        copy_clip = run("clip", str(source), "--start", "1", "--duration", "2", "--copy",
                        "--out", str(work / "cut_copy.mp4"))
        assert copy_clip["mode"] == "stream copy", copy_clip
        print("lossless clip ok")

        small = run("compress", str(source), "--crf", "32", "--max-width", "240",
                    "--out", str(work / "small.mp4"))
        assert pathlib.Path(small["out"]).exists(), small
        print(f"compress ok: {small['out_size_human']} (ratio {small.get('size_ratio')})")

        converted = run("convert", str(source), "--to", "mkv", "--out", str(work / "conv.mkv"))
        assert pathlib.Path(converted["out"]).exists(), converted
        print("convert ok")

        scaled = run("scale", str(source), "--size", "480x270", "--fit", "pad",
                     "--out", str(work / "scaled.mp4"))
        assert pathlib.Path(scaled["out"]).exists(), scaled
        print("scale ok")

        gif = run("gif", str(source), "--start", "0", "--duration", "2", "--width", "240", "--fps", "8",
                  "--out", str(work / "anim.gif"))
        assert pathlib.Path(gif["out"]).exists(), gif
        print(f"gif ok: {gif['out_size_human']}")

        audio = run("audio", str(source), "--codec", "pcm_s16le", "--rate", "16000", "--mono",
                    "--out", str(work / "voice.wav"))
        assert pathlib.Path(audio["out"]).exists(), audio
        print(f"audio ok: codec {audio['codec']} ({audio['codec_note']})")

        normalized = run("normalize", str(source), "--lufs", "-16", "--out", str(work / "norm.m4a"))
        assert pathlib.Path(normalized["out"]).exists(), normalized
        print("normalize ok")

        silence = run("silence", str(source), "--noise-db", "-35", "--min-duration", "0.4")
        assert silence["speech_count"] >= 1, silence
        print(f"silence ok: {silence['silence_count']} silent stretches")

        loudness = run("loudness", str(source), "--ebur128")
        assert loudness["integrated_lufs"] is not None, loudness
        print(f"loudness ok: {loudness['integrated_lufs']} LUFS")

        report = run("report", str(source))
        for key in ("report", "html", "sheet"):
            assert pathlib.Path(report[key]).exists(), report
        print("report ok")

        transcribe = run("transcribe", str(source), expect=5)
        assert transcribe["ok"] is False and transcribe["options"], transcribe
        print("transcribe ok (no backend -> exit 5 with options)")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
