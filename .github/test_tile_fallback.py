"""Prove the tile fallback works without glob support and without Pillow.

Run: python .github/test_tile_fallback.py <skill-dir>
The script hides Pillow from the sheet composition step, runs `sheet`, and asserts that
the ffmpeg tile path produced the image — the path GitHub's Windows runner exposed when
its libavformat reported "globbing is not supported by this libavformat build".
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

skill_dir = pathlib.Path(sys.argv[1]).resolve()
ops = skill_dir / "scripts" / "video_ops.py"
# Scratch inside the bundle's own tree: a confined DSH file policy denies writes elsewhere,
# and the point of this test is the tiling route, not the sandbox.
work = skill_dir / ".tile-fallback-work"
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True, exist_ok=True)
sample = work / "sample.mp4"

doctor = json.loads(subprocess.run([sys.executable, str(ops), "doctor"], capture_output=True,
                                   text=True, encoding="utf-8").stdout)
ffmpeg = doctor["resolved"]["ffmpeg"]
# Not every build has libx264; the point here is a test clip, not a codec check.
encoder = "libx264" if doctor.get("encoders", {}).get("libx264") else (
    "h264_nvenc" if doctor.get("encoders", {}).get("h264_nvenc") else "h264_mf")
subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=6",
                "-c:v", encoder, "-pix_fmt", "yuv420p", str(sample)], check=True)

# Hide Pillow so the composer declines and the tile fallback is the only route left.
blocker = work / "nopillow"
blocker.mkdir(exist_ok=True)
(blocker / "PIL.py").write_text("raise ImportError('hidden for this test')\n", encoding="utf-8")

out = work / "tile_sheet.jpg"
proc = subprocess.run([sys.executable, str(ops), "sheet", str(sample), "--count", "4", "--cols", "2",
                       "--out", str(out)], capture_output=True, text=True, encoding="utf-8",
                      env={**os.environ, "PYTHONPATH": str(blocker)})
payload = json.loads(proc.stdout[proc.stdout.index("{"):]) if "{" in proc.stdout else {}
engine = payload.get("engine")
print("exit:", proc.returncode, "engine:", engine, "sheet exists:", out.exists())
if proc.returncode != 0 or engine != "ffmpeg tile filter" or not out.exists():
    print(proc.stdout[-1500:])
    print(proc.stderr[-1500:])
    shutil.rmtree(work, ignore_errors=True)
    sys.exit(1)
print("tile fallback ok:", out.name, out.stat().st_size, "bytes")
shutil.rmtree(work, ignore_errors=True)
