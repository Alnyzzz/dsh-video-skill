#!/usr/bin/env python3
"""video_ops.py - the command-line body of the DSH `video-toolkit` skill.

One entry point for everything the agent needs to do with a video or audio file:
probe metadata, pull frames the model can actually look at, build contact sheets,
cut/convert/compress, extract and normalise audio, detect silence, and write a
self-contained report. Every command drives ffmpeg/ffprobe; nothing here needs the network.

Run `video_ops.py doctor` first when something looks wrong.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from video_common import (  # noqa: E402
    IS_WINDOWS, ToolError, bootstrap_tools, fallback_probe, ffmpeg_run,
    format_timecode, human_size, locate_tools, parse_size, parse_timestamp,
    platform_note, print_json, resolve_tools, run, tool_versions, tools_dir,
    video_home, write_json,
)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".webm", ".m4v", ".ts",
              ".mpg", ".mpeg", ".3gp", ".vob", ".mts", ".m2ts", ".rmvb", ".rm", ".ogv", ".f4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr", ".aiff"}

# Video encoders tried in order when the user did not name one. Pure-software H.264 first
# because it is the most compatible; hardware and legacy fallbacks keep working on builds
# compiled without libx264 (very common in builds bundled with consumer apps).
X264_FALLBACKS = ("libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", "mpeg4")

AUDIO_FALLBACKS = {
    "libmp3lame": ("libmp3lame", "aac", "libopus", "ac3"),
    "mp3": ("libmp3lame", "aac"),
    "libopus": ("libopus", "aac", "libvorbis"),
    "libvorbis": ("libvorbis", "libopus", "aac"),
    "flac": ("flac", "aac"),
    "pcm_s16le": ("pcm_s16le",),
    "copy": ("copy",),
    "aac": ("aac", "libmp3lame", "libopus"),
}
AUDIO_EXT = {"libmp3lame": ".mp3", "mp3": ".mp3", "aac": ".m4a", "libopus": ".opus",
             "pcm_s16le": ".wav", "flac": ".flac", "libvorbis": ".ogg", "copy": ".m4a"}


def _quality_flags(encoder: str, crf: int) -> list:
    """Map a CRF-like quality value onto whatever option the chosen encoder accepts.

    `-crf` only exists on libx264/libx265; NVENC wants `-cq`, QSV `-global_quality`,
    AMF `-qp_i/-qp_p`, Media Foundation `-quality`, and the plain mpeg4 encoder `-q:v`.
    """
    if encoder in {"libx264", "libx265", "libvpx-vp9", "libaom-av1", "libsvtav1"}:
        return ["-crf", str(crf)]
    if encoder in {"h264_nvenc", "hevc_nvenc", "av1_nvenc"}:
        return ["-rc", "vbr", "-cq", str(max(0, min(51, crf)))]
    if encoder in {"h264_qsv", "hevc_qsv", "av1_qsv"}:
        return ["-global_quality", str(max(1, min(51, crf)))]
    if encoder in {"h264_amf", "hevc_amf"}:
        return ["-quality", "quality", "-qp_i", str(crf), "-qp_p", str(crf)]
    if encoder in {"h264_mf", "hevc_mf"}:
        return ["-rate_control", "quality", "-quality", str(max(1, min(100, round(100 - crf * 1.9))))]
    return ["-q:v", str(max(1, min(31, max(2, crf // 2))))]


def _audio_codec(tools: Tools, requested: str) -> str:
    """Best available encoder for the requested audio codec family."""
    available = _encoder_cache(tools)
    for name in AUDIO_FALLBACKS.get(requested, (requested, "aac")):
        if name == "copy" or name in available:
            return name
    return "aac"


def _safe_audio_codec(tools: Tools, args, requested: str) -> tuple[str, str]:
    """Resolve an audio codec and say so when it had to change."""
    resolved = _audio_codec(tools, requested)
    return resolved, ("requested" if resolved == requested else f"fallback from {requested}")


# ======================================================================================
# argument helpers
# ======================================================================================

def add_common(parser: argparse.ArgumentParser, *, bootstrap: bool = True) -> None:
    parser.add_argument("--print-command", action="store_true",
                        help="print the ffmpeg command instead of running it")
    parser.add_argument("--rescan", action="store_true",
                        help="re-walk the disks for ffmpeg/ffprobe instead of using the cached candidate list")
    if bootstrap:
        parser.add_argument("--bootstrap", action="store_true",
                            help="install a private ffmpeg/ffprobe pair if none is usable")
        parser.add_argument("--download", action="store_true",
                            help="allow the bootstrap step to download ffmpeg from the network")
        parser.add_argument("--source", help="local archive/binary/directory to bootstrap from")


def add_outdir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--outdir", "-o", default=None,
                        help="output directory (default: <input>_frames / <input>_out beside the input)")


def emit(payload, args) -> None:
    if getattr(args, "_quiet", False):
        # Commands that call another command internally must not leak its payload,
        # because callers parse exactly one JSON object from stdout.
        return
    if getattr(args, "json_out", None):
        write_json(Path(args.json_out), payload)
        print(f"JSON: {Path(args.json_out).resolve()}")
    else:
        print_json(payload)


# ======================================================================================
# tool plumbing
# ======================================================================================

class Tools:
    def __init__(self, args):
        info = locate_tools(
            bootstrap=getattr(args, "bootstrap", False),
            source=getattr(args, "source", None),
            allow_download=getattr(args, "download", False),
            rescan=getattr(args, "rescan", False),
        )
        self.ffmpeg = info.get("ffmpeg")
        self.ffprobe = info.get("ffprobe")
        self.info = info

    def require_ffmpeg(self) -> Path:
        if not self.ffmpeg:
            raise ToolError(
                "ffmpeg was not found. Run:\n"
                f"  python video_ops.py bootstrap --download\n"
                "or install ffmpeg (winget install Gyan.FFmpeg / brew install ffmpeg / apt install ffmpeg)."
            )
        return self.ffmpeg

    def require_ffprobe(self) -> Path | None:
        return self.ffprobe


def probe_json(tools: Tools, path: Path) -> dict:
    """ffprobe JSON, or a documented pure-Python fallback for ISO-BMFF files."""
    if tools.ffprobe:
        cmd = [tools.ffprobe, "-v", "error", "-hide_banner", "-print_format", "json",
               "-show_format", "-show_streams", "-show_chapters", str(path)]
        proc = run(cmd)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return json.loads(proc.stdout)
        raise ToolError(f"ffprobe failed on {path}:\n{(proc.stderr or '').strip()[-1500:]}")
    if tools.ffmpeg:
        cmd = [tools.ffmpeg, "-hide_banner", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)]
        proc = run(cmd)
        if proc.returncode == 0 and (proc.stdout or "").strip().startswith("{"):
            return json.loads(proc.stdout)
    return fallback_probe(Path(path))


def duration_of(tools: Tools, path: Path) -> float | None:
    try:
        data = probe_json(tools, Path(path))
    except ToolError:
        return None
    try:
        return float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        for stream in data.get("streams", []):
            if stream.get("duration"):
                try:
                    return float(stream["duration"])
                except (TypeError, ValueError):
                    continue
    return None


def video_stream(data: dict) -> dict:
    streams = data.get("streams", [])
    return next((s for s in streams if s.get("codec_type") == "video"), {}) or {}


def audio_streams(data: dict) -> list[dict]:
    return [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]


def encoder_available(tools: Tools, name: str) -> bool:
    if not tools.ffmpeg:
        return False
    try:
        proc = run([tools.ffmpeg, "-hide_banner", "-encoders"], timeout=45)
    except (OSError, Exception):  # noqa: BLE001
        return False
    return name in (proc.stdout or "")


def _encoder_cache(tools: Tools) -> set[str]:
    cached = getattr(tools, "_encoders", None)
    if cached is None:
        try:
            listing = run([tools.ffmpeg, "-hide_banner", "-encoders"], timeout=45).stdout or ""
        except (OSError, Exception):  # noqa: BLE001
            listing = ""
        cached = set(re.findall(r"^\s*[VAS][\w.]{5}\s+(\S+)", listing, re.MULTILINE))
        tools._encoders = cached
    return cached


def pick_lossless_image(tools: Tools, preferred: str = "png") -> str:
    """First available still-image encoder usable for the GIF palette step."""
    for name in (preferred, "png", "bmp", "tiff", "ppm"):
        if name in _encoder_cache(tools):
            return name
    return "bmp"


def pick_encoder(tools: Tools, preferred: str | None, candidates) -> str:
    if preferred:
        return preferred
    for name in candidates:
        if encoder_available(tools, name):
            return name
    return candidates[-1]


PRESET_ENCODERS = {"libx264", "libx265", "libvpx-vp9", "libsvtav1"}
GROP_ENCODERS = {"libx264", "libx265", "h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv"}


def validate_video_encoder(tools: Tools, encoder: str) -> dict:
    """Prove a video encoder can actually encode before a long job starts."""
    if encoder == "mpeg4":
        return {"used": encoder, "note": "legacy mpeg4 encoder: compatible but not H.264"}
    if encoder not in _encoder_cache(tools):
        available = [name for name in X264_FALLBACKS if name in _encoder_cache(tools)]
        return {"used": available[0] if available else encoder,
                "note": f"{encoder} is not compiled into this ffmpeg build"}
    has_preset = encoder in PRESET_ENCODERS or "nvenc" in encoder
    cmd = [tools.ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi",
           "-i", "color=c=black:s=64x64:r=1", "-frames:v", "1",
           "-c:v", encoder] + _quality_flags(encoder, 28)
    if has_preset:
        cmd += ["-preset", "ultrafast" if "nvenc" not in encoder else "p1"]
    cmd += ["-f", "null", "-"]
    proc = run(cmd, timeout=120)
    if proc.returncode == 0:
        return {"used": encoder, "note": "requested" if encoder == "libx264" else f"selected {encoder}"}
    detail = (proc.stderr or "").strip().splitlines()
    return {"used": encoder, "note": f"{encoder} failed its test encode",
            "error": detail[-1] if detail else "unknown", "probe_failed": True}


def finalize_video_flags(tools: Tools, args, *, extra: list | None = None) -> tuple[list, dict]:
    """Build video-stream flags, substituting a working encoder when the first choice fails.

    Long encodes fail on the first frame when an encoder cannot initialise, so the
    fallback chain is walked up front instead of after a multi-minute job.
    """
    chain = list(X264_FALLBACKS) if not getattr(args, "video_encoder", None) else [args.video_encoder]
    note_info: dict = {}
    for index, encoder in enumerate(chain):
        check = validate_video_encoder(tools, encoder)
        if check.get("probe_failed"):
            note_info.setdefault("rejected", []).append({"encoder": encoder, "error": check.get("error")})
            continue
        flags = ["-c:v", check["used"]] + _quality_flags(check["used"], int(getattr(args, "crf", 20) or 20))
        if check["used"] in PRESET_ENCODERS or "nvenc" in check["used"]:
            flags += ["-preset", getattr(args, "preset", "medium")]
        if check["used"] in GROP_ENCODERS:
            flags += ["-g", "120"]
        if extra:
            flags += extra
        note_info["encoder"] = check["used"]
        note_info["encoder_note"] = check["note"]
        return flags, note_info
    encoder = chain[-1]
    flags = ["-c:v", encoder] + _quality_flags(encoder, int(getattr(args, "crf", 20) or 20))
    note_info["encoder"] = encoder
    note_info["encoder_note"] = "no working encoder passed its test encode; last candidate used"
    return flags, note_info


def resolve_output(src: Path, out: str | None, default_suffix: str, outdir: str | None,
                   default_ext: str | None = None) -> Path:
    src = Path(src)
    if out:
        target = Path(out)
        if target.is_dir():
            ext = default_ext or src.suffix
            return target / f"{src.stem}{default_suffix}{ext}"
        return target
    directory = Path(outdir) if outdir else src.parent
    ext = default_ext or src.suffix
    return directory / f"{src.stem}{default_suffix}{ext}"


def prepare_output(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def run_ffmpeg(tools: Tools, cmd: list, args, payload: dict, timeout: int | None = None) -> dict:
    printable = " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)
    payload["command"] = printable
    if getattr(args, "print_command", False):
        print(printable)
        payload["executed"] = False
        return payload
    ffmpeg_run(cmd, timeout=timeout)
    payload["executed"] = True
    return payload


# ======================================================================================
# commands
# ======================================================================================

def cmd_doctor(args) -> int:
    report: dict = {"platform": platform_note(), "tools_dir": str(tools_dir()),
                    "video_home": str(video_home()), "found": {}, "versions": {},
                    "encoders": {}, "python_modules": {}, "warnings": []}

    initial = resolve_tools(rescan=args.rescan)
    report["found"] = {k: (str(v) if v else None) for k, v in initial.items()
                       if k in {"ffmpeg", "ffprobe", "paired", "source"}}
    report["candidates"] = initial.get("candidates")

    if args.bootstrap and not (initial["ffmpeg"] and initial["ffprobe"]):
        try:
            result = bootstrap_tools(source=args.source, allow_download=args.download)
            report["bootstrap"] = {"installed": str(result.get("installed")), "source": result.get("source")}
        except ToolError as exc:
            report["bootstrap"] = {"error": str(exc)}
            report["warnings"].append(str(exc))

    tools = Tools(args)
    report["versions"] = tool_versions(tools.info)
    report["resolved"] = {"ffmpeg": str(tools.ffmpeg) if tools.ffmpeg else None,
                          "ffprobe": str(tools.ffprobe) if tools.ffprobe else None}

    if not tools.ffmpeg:
        report["warnings"].append("no runnable ffmpeg: video work is blocked until one is installed")
    if not tools.ffprobe:
        report["warnings"].append("no runnable ffprobe: metadata falls back to the bundled ISO-BMFF parser")

    if tools.ffmpeg:
        for enc in ("libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", "libx265",
                    "libvpx-vp9", "libmp3lame", "aac", "libopus"):
            report["encoders"][enc] = encoder_available(tools, enc)
        filters = run([tools.ffmpeg, "-hide_banner", "-filters"], timeout=45).stdout or ""
        report["filters"] = {name: (f" {name} " in filters) for name in
                             ("scale", "tile", "select", "drawtext", "palettegen", "silencedetect")}

    for module in ("PIL", "numpy"):
        try:
            __import__(module)
            report["python_modules"][module] = True
        except ImportError:
            report["python_modules"][module] = False
    if not report["python_modules"].get("PIL"):
        report["warnings"].append("Pillow missing: contact sheets fall back to the ffmpeg tile filter (no labels)")

    for binary in ("whisper-cli", "whisper", "whisper.cpp"):
        found = shutil.which(binary)
        if found and Path(found).suffix.lower() in {"", ".exe"}:
            report.setdefault("speech_to_text", {})[binary] = found

    report["ok"] = bool(tools.ffmpeg)
    emit(report, args)
    return 0 if report["ok"] else 3


def cmd_bootstrap(args) -> int:
    try:
        result = bootstrap_tools(source=args.source, allow_download=args.download,
                                 retries=args.retries)
    except ToolError as exc:
        print_json({"ok": False, "error": str(exc),
                    "hint": "slow links: re-run the same command to resume, or pass "
                            "--source <a local ffmpeg folder/archive>"})
        return 3
    tools = Tools(args)
    payload = {
        "ok": True,
        "installed": str(result.get("installed")),
        "ffprobe": str(result["ffprobe"]) if result.get("ffprobe") else None,
        "source": result.get("source"),
        "attempts": result.get("attempts"),
        "versions": tool_versions(tools.info),
    }
    emit(payload, args)
    return 0


def cmd_probe(args) -> int:
    tools = Tools(args)
    path = Path(args.input).expanduser()
    if not path.exists():
        raise ToolError(f"input does not exist: {path}")
    data = probe_json(tools, path)

    fmt = data.get("format", {}) or {}
    streams = []
    for stream in data.get("streams", []):
        entry = {
            "index": stream.get("index"),
            "type": stream.get("codec_type"),
            "codec": stream.get("codec_name"),
            "profile": stream.get("profile"),
            "duration_s": _f(stream.get("duration")),
            "bit_rate": _i(stream.get("bit_rate")),
            "language": (stream.get("tags") or {}).get("language"),
        }
        if stream.get("codec_type") == "video":
            entry.update({
                "width": stream.get("width"), "height": stream.get("height"),
                "pix_fmt": stream.get("pix_fmt"),
                "fps": _fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
                "frames": _i(stream.get("nb_frames")),
                "rotation": _rotation(stream),
                "has_b_frames": stream.get("has_b_frames"),
                "color_range": stream.get("color_range"),
                "sample_aspect_ratio": stream.get("sample_aspect_ratio"),
            })
        elif stream.get("codec_type") == "audio":
            entry.update({
                "sample_rate": _i(stream.get("sample_rate")),
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
            })
        streams.append(entry)

    payload = {
        "file": str(path.resolve()),
        "size": fmt.get("size") and _i(fmt.get("size")),
        "size_human": human_size(_i(fmt.get("size")) or path.stat().st_size),
        "format": fmt.get("format_name"),
        "format_long": fmt.get("format_long_name"),
        "duration_s": _f(fmt.get("duration")),
        "duration_human": format_timecode(_f(fmt.get("duration"))),
        "bit_rate": _i(fmt.get("bit_rate")),
        "tags": fmt.get("tags") or {},
        "chapters": data.get("chapters") or [],
        "streams": streams,
        "stream_count": {"video": sum(1 for s in streams if s["type"] == "video"),
                         "audio": sum(1 for s in streams if s["type"] == "audio"),
                         "subtitle": sum(1 for s in streams if s["type"] == "subtitle"),
                         "data": sum(1 for s in streams if s["type"] == "data")},
    }
    if fmt.get("probe_note"):
        payload["probe_note"] = fmt["probe_note"]
    payload["has_audio"] = payload["stream_count"]["audio"] > 0
    emit(payload, args)
    return 0


def cmd_frames(args) -> int:
    tools = Tools(args)
    src = Path(args.input).expanduser()
    if not src.exists():
        raise ToolError(f"input does not exist: {src}")

    outdir = Path(args.outdir) if args.outdir else src.parent / f"{src.stem}_frames"
    outdir.mkdir(parents=True, exist_ok=True)

    duration = duration_of(tools, src)
    stamps: list[float] = []

    if args.all_frames:
        stamps = []
    elif args.at:
        stamps = [parse_timestamp(v) for v in args.at]
    elif args.count:
        if args.count < 1:
            raise ToolError("--count must be >= 1")
        if duration:
            stamps = [duration * (i + 0.5) / args.count for i in range(args.count)]
        else:
            stamps = [float(i) for i in range(args.count)]
    elif args.every:
        every = parse_timestamp(args.every)
        if every <= 0:
            raise ToolError("--every must be > 0")
        if not duration:
            raise ToolError("cannot sample by interval without a known duration; pass --at or --count")
        stamps = [i * every for i in range(int(math.ceil(duration / every)))]
    else:
        stamps = [0.0]

    ext = args.format.lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    quality = ["-q:v", str(args.quality)] if ext in {"jpeg", "webp"} else []
    scale = ["-vf", f"scale={args.width}:-2"] if args.width else []

    written: list[dict] = []
    ffmpeg = tools.require_ffmpeg()

    if args.all_frames:
        pattern = outdir / f"{src.stem}_%05d.{ext}"
        cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src)]
        if args.fps:
            cmd += ["-vf", f"fps={args.fps},scale={args.width}:-2" if args.width else f"fps={args.fps}"]
        elif scale:
            cmd += scale
        cmd += quality + [str(pattern)]
        payload = {"file": str(src.resolve()), "mode": "all_frames", "outdir": str(outdir.resolve()),
                   "pattern": str(pattern.resolve()), "frames": None}
        run_ffmpeg(tools, cmd, args, payload, timeout=None)
        payload["written"] = len(list(outdir.glob(f"{src.stem}_*.{ext}")))
        emit(payload, args)
        return 0

    for index, stamp in enumerate(stamps):
        target = outdir / f"{src.stem}_{index + 1:03d}_{_safe_time(stamp)}.{ext}"
        cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{max(0.0, stamp):.3f}",
               "-i", str(src), "-frames:v", "1"]
        if scale:
            cmd += scale
        cmd += quality + [str(target)]
        proc = run(cmd)
        if proc.returncode != 0 or not target.exists():
            written.append({"index": index + 1, "at_s": stamp, "at": format_timecode(stamp),
                            "file": None, "error": (proc.stderr or "").strip()[-400:] or "no frame produced"})
        else:
            written.append({"index": index + 1, "at_s": round(stamp, 3), "at": format_timecode(stamp),
                            "file": str(target.resolve()), "bytes": target.stat().st_size})
    payload = {"file": str(src.resolve()), "mode": "timestamps" if args.at else ("even" if args.count else "single"),
               "duration_s": duration, "outdir": str(outdir.resolve()), "frames": written,
               "ok_count": sum(1 for f in written if f.get("file"))}
    emit(payload, args)
    return 0 if payload["ok_count"] else 4


def cmd_sheet(args) -> int:
    tools = Tools(args)
    src = Path(args.input).expanduser()
    if not src.exists():
        raise ToolError(f"input does not exist: {src}")

    outfile = Path(args.out) if args.out else src.parent / f"{src.stem}_sheet.jpg"
    prepare_output(outfile)

    columns = max(1, args.cols)
    rows = max(1, args.rows) if args.rows else max(1, math.ceil(args.count / columns))
    count = args.count if args.rows is None else max(1, columns * rows)

    with tempfile.TemporaryDirectory(prefix="dsh-sheet-") as tmp:
        tmpdir = Path(tmp)
        frame_args = argparse.Namespace(**vars(args))
        frame_args.input = str(src)
        frame_args.outdir = str(tmpdir)
        frame_args.count = count
        frame_args.at = None
        frame_args.every = None
        frame_args.all_frames = False
        frame_args.format = "jpeg"
        frame_args.quality = args.quality
        frame_args.width = args.frame_width
        frame_args.fps = None
        frame_args.print_command = False
        frame_args.bootstrap = False
        frame_args.download = False
        frame_args.source = None
        frame_args.json_out = None
        frame_args._quiet = True
        rc = cmd_frames(frame_args)
        if rc != 0:
            return rc
        images = sorted(tmpdir.glob("*.jpeg")) + sorted(tmpdir.glob("*.jpg"))
        if not images:
            raise ToolError("no frames were extracted for the contact sheet")

        labels = [f"{format_timecode(f['at_s'])}" for f in _list_frames(tools, src, count)][:len(images)]
        composed = _compose_sheet(images, outfile, columns, args.cell_width,
                                  labels if args.labels else None, args.label_size)
        if not composed:
            ffmpeg = tools.require_ffmpeg()
            pattern = str(Path(images[0]).parent / "*.jpeg")
            cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-pattern_type", "glob",
                   "-i", pattern, "-vf", f"scale={args.cell_width}:-2,tile={columns}x{rows}",
                   "-frames:v", "1", str(outfile)]
            payload = {"file": str(src.resolve()), "sheet": str(outfile.resolve()),
                       "engine": "ffmpeg tile filter", "columns": columns, "rows": rows}
            run_ffmpeg(tools, cmd, args, payload)
            emit(payload, args)
            return 0

    payload = {"file": str(src.resolve()), "sheet": str(outfile.resolve()), "engine": "Pillow",
               "columns": columns, "rows": rows, "cells": len(images),
               "bytes": outfile.stat().st_size if outfile.exists() else None,
               "hint": "read this image with the read_image tool; timestamps run left to right, top to bottom"}
    if args.labels:
        payload["labels"] = labels
    emit(payload, args)
    return 0


def _list_frames(tools: Tools, src: Path, count: int) -> list[dict]:
    duration = duration_of(tools, src)
    if not duration:
        return [{"at_s": float(i)} for i in range(count)]
    return [{"at_s": duration * (i + 0.5) / count} for i in range(count)]


def _compose_sheet(images: list[Path], outfile: Path, columns: int, cell_width: int,
                   labels: list[str] | None, label_size: int) -> bool:
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError:
        return False

    thumbs = []
    for path in images:
        try:
            img = Image.open(path).convert("RGB")
        except OSError:
            continue
        ratio = cell_width / float(img.width)
        thumbs.append(img.resize((cell_width, max(1, int(img.height * ratio))), Image.LANCZOS))
    if not thumbs:
        return False

    cell_height = max(t.height for t in thumbs)
    label_band = 0
    font = None
    draw_probe = None
    if labels:
        draw_probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        try:
            from PIL import ImageFont
            font = ImageFont.load_default(size=label_size)
        except Exception:  # noqa: BLE001 - older Pillow has no sized default font
            font = None
        bbox = draw_probe.textbbox((0, 0), "00:00.000", font=font)
        label_band = (bbox[3] - bbox[1]) + 8

    rows = math.ceil(len(thumbs) / columns)
    pad = 6
    sheet_w = columns * cell_width + pad * (columns + 1)
    sheet_h = rows * (cell_height + label_band) + pad * (rows + 1)
    sheet = Image.new("RGB", (sheet_w, sheet_h), (18, 18, 20))
    draw = ImageDraw.Draw(sheet)

    for index, thumb in enumerate(thumbs):
        row, col = divmod(index, columns)
        x = pad + col * (cell_width + pad)
        y = pad + row * (cell_height + label_band + pad)
        sheet.paste(thumb, (x, y))
        if labels and index < len(labels):
            draw.text((x + 2, y + cell_height + 2), labels[index], fill=(240, 240, 240), font=font)

    outfile.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(outfile, quality=88)
    return True


def cmd_scenes(args) -> int:
    tools = Tools(args)
    src = Path(args.input).expanduser()
    ffmpeg = tools.require_ffmpeg()
    cmd = [ffmpeg, "-hide_banner", "-v", "info", "-i", str(src),
           "-filter:v", f"select='gt(scene,{args.threshold})',showinfo", "-an", "-f", "null", "-"]
    proc = run(cmd, timeout=None)
    stamps: list[float] = []
    for line in (proc.stderr or "").splitlines():
        match = re.search(r"pts_time:([0-9.]+)", line)
        if match:
            stamps.append(round(float(match.group(1)), 3))
    duration = duration_of(tools, src)
    payload = {
        "file": str(src.resolve()), "threshold": args.threshold, "duration_s": duration,
        "scene_count": len(stamps),
        "scenes": [{"index": i + 1, "start_s": t,
                    "start": format_timecode(t),
                    "end_s": (stamps[i + 1] if i + 1 < len(stamps) else duration),
                    "end": format_timecode(stamps[i + 1] if i + 1 < len(stamps) else duration)}
                   for i, t in enumerate(stamps)],
    }
    if args.frames:
        frame_ns = argparse.Namespace(**vars(args))
        frame_ns.at = [f"{s['start_s'] + 0.05 * (duration or 1) / max(1, len(stamps) or 1):.3f}" for s in payload["scenes"]]
        frame_ns.count = None
        frame_ns.every = None
        frame_ns.all_frames = False
        frame_ns.outdir = args.frames
        frame_ns.format = "jpeg"
        frame_ns.width = args.width or 960
        frame_ns.quality = 3
        frame_ns.print_command = False
        frame_ns.json_out = None
        cmd_frames(frame_ns)
        payload["frames_dir"] = str(Path(args.frames).resolve())
    emit(payload, args)
    return 0 if stamps else 4


def cmd_clip(args) -> int:
    tools = Tools(args)
    ffmpeg = tools.require_ffmpeg()
    src = Path(args.input).expanduser()
    start = parse_timestamp(args.start)
    end = parse_timestamp(args.end) if args.end else None
    if end is None and args.duration:
        end = start + parse_timestamp(args.duration)
    if end is not None and end <= start:
        raise ToolError("--end must be later than --start")

    out = prepare_output(resolve_output(src, args.out, "_clip", args.outdir))
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-i", str(src)]
    notes: dict = {}
    if args.copy:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        flags, notes = finalize_video_flags(tools, args)
        audio_codec, audio_note = _safe_audio_codec(tools, args, args.audio_encoder or "aac")
        notes["audio_codec"] = audio_codec
        notes["audio_note"] = audio_note
        cmd += flags + ["-c:a", audio_codec, "-b:a", args.audio_bitrate, "-movflags", "+faststart"]
    cmd.append(str(out))
    payload = {"file": str(src.resolve()), "out": str(out.resolve()),
               "start_s": start, "end_s": end, "mode": "stream copy" if args.copy else "re-encode",
               **notes}
    run_ffmpeg(tools, cmd, args, payload)
    _finish_sizes(payload)
    emit(payload, args)
    return 0


def cmd_compress(args) -> int:
    tools = Tools(args)
    ffmpeg = tools.require_ffmpeg()
    src = Path(args.input).expanduser()
    duration = duration_of(tools, src)
    out = prepare_output(resolve_output(src, args.out, "_small", args.outdir))

    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src)]
    notes: dict = {}
    if args.target_mb and duration:
        video_kbps = max(120, int((args.target_mb * 8192) / duration - 128))
        cmd += ["-c:v", pick_encoder(tools, args.video_encoder, X264_FALLBACKS),
                "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k",
                "-bufsize", f"{video_kbps * 2}k"]
    else:
        flags, notes = finalize_video_flags(tools, args)
        cmd += flags
    if args.max_width:
        cmd += ["-vf", f"scale='min({args.max_width},iw)':-2"]
    audio_codec, audio_note = _safe_audio_codec(tools, args, args.audio_encoder or "aac")
    cmd += ["-c:a", audio_codec, "-b:a", args.audio_bitrate,
            "-movflags", "+faststart", str(out)]

    payload = {"file": str(src.resolve()), "out": str(out.resolve()),
               "duration_s": duration, "crf": None if args.target_mb else args.crf,
               "target_mb": args.target_mb, "max_width": args.max_width,
               "audio_codec": audio_codec, "audio_note": audio_note, **notes}
    run_ffmpeg(tools, cmd, args, payload, timeout=None)
    _finish_sizes(payload)
    emit(payload, args)
    return 0


def cmd_convert(args) -> int:
    tools = Tools(args)
    ffmpeg = tools.require_ffmpeg()
    src = Path(args.input).expanduser()
    target_ext = args.to if args.to.startswith(".") else f".{args.to}"
    if not args.out and not args.outdir:
        out = prepare_output(src.parent / f"{src.stem}{target_ext}")
    else:
        out = prepare_output(resolve_output(src, args.out, "", args.outdir, default_ext=target_ext))

    flags, notes = finalize_video_flags(tools, args)
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src)]
    cmd += flags + ["-pix_fmt", "yuv420p"]
    if args.max_width:
        cmd += ["-vf", f"scale='min({args.max_width},iw)':-2"]
    audio_codec, audio_note = _safe_audio_codec(tools, args, args.audio_encoder or "aac")
    if not args.no_audio:
        cmd += ["-c:a", audio_codec, "-b:a", args.audio_bitrate]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(out)]
    payload = {"file": str(src.resolve()), "out": str(out.resolve()), "target": target_ext,
               "audio_codec": audio_codec, "audio_note": audio_note, **notes}
    run_ffmpeg(tools, cmd, args, payload, timeout=None)
    _finish_sizes(payload)
    emit(payload, args)
    return 0


def cmd_scale(args) -> int:
    tools = Tools(args)
    ffmpeg = tools.require_ffmpeg()
    src = Path(args.input).expanduser()
    width, height = parse_size(args.size)
    out = prepare_output(resolve_output(src, args.out, f"_{width}x{height}", args.outdir))

    if args.fit == "pad":
        filter_expr = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
    elif args.fit == "crop":
        filter_expr = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                       f"crop={width}:{height}")
    else:
        filter_expr = f"scale={width}:{height}"
    flags, notes = finalize_video_flags(tools, args)
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src), "-vf", filter_expr]
    cmd += flags + ["-c:a", "copy", "-movflags", "+faststart", str(out)]
    payload = {"file": str(src.resolve()), "out": str(out.resolve()),
               "size": f"{width}x{height}", "fit": args.fit, "filter": filter_expr, **notes}
    run_ffmpeg(tools, cmd, args, payload, timeout=None)
    _finish_sizes(payload)
    emit(payload, args)
    return 0


def cmd_gif(args) -> int:
    tools = Tools(args)
    ffmpeg = tools.require_ffmpeg()
    src = Path(args.input).expanduser()
    out = prepare_output(resolve_output(src, args.out, "_anim", args.outdir, default_ext=".gif"))
    start = parse_timestamp(args.start)
    duration = parse_timestamp(args.duration)

    palette_ext = "." + pick_lossless_image(tools, args.palette_format)
    palette = out.with_name(f"{out.stem}.palette{palette_ext}")
    filters_v = (f"fps={args.fps},scale={args.width}:-2:flags=lanczos"
                 + (f",crop=iw:min(ih\\,{args.max_height})" if args.max_height else ""))
    cmd1 = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", str(src), "-vf", f"{filters_v},palettegen=stats_mode=diff", str(palette)]
    cmd2 = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", str(src), "-i", str(palette),
            "-lavfi", f"{filters_v} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3",
            str(out)]
    payload = {"file": str(src.resolve()), "out": str(out.resolve()), "width": args.width,
               "fps": args.fps, "start_s": start, "duration_s": duration,
               "palette_format": palette_ext.lstrip(".")}
    if args.print_command:
        run_ffmpeg(tools, cmd1, args, payload)
        run_ffmpeg(tools, cmd2, args, payload)
    else:
        try:
            ffmpeg_run(cmd1, timeout=None)
            ffmpeg_run(cmd2, timeout=None)
        finally:
            palette.unlink(missing_ok=True)
        payload["executed"] = True
        payload["commands"] = [
            " ".join(str(c) for c in cmd1),
            " ".join(str(c) for c in cmd2),
        ]
        _finish_sizes(payload)
    emit(payload, args)
    return 0


def cmd_audio(args) -> int:
    tools = Tools(args)
    ffmpeg = tools.require_ffmpeg()
    src = Path(args.input).expanduser()
    codec, note = _safe_audio_codec(tools, args, args.codec)
    ext = AUDIO_EXT.get(codec, AUDIO_EXT.get(args.codec, ".m4a"))
    out = prepare_output(resolve_output(src, args.out, "_audio", args.outdir, default_ext=ext))

    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src), "-vn"]
    if codec == "copy":
        cmd += ["-acodec", "copy"]
    else:
        cmd += ["-c:a", codec, "-b:a", args.bitrate, "-ar", str(args.rate)]
    if args.mono:
        cmd += ["-ac", "1"]
    if args.out and note != "requested":
        # An explicit output name wins over the codec family, so this is a real mismatch.
        raise ToolError(
            f"--out '{args.out}' does not match the available encoder ({note}); the muxer would "
            f"reject the stream. Use --codec {codec} matching that extension, drop --out, or "
            f"bootstrap a build with the requested encoder (bootstrap --download)."
        )
    cmd += [str(out)]

    payload = {"file": str(src.resolve()), "out": str(out.resolve()), "codec": codec,
               "codec_note": note, "rate": args.rate, "mono": bool(args.mono)}
    run_ffmpeg(tools, cmd, args, payload, timeout=None)
    if out.exists():
        data = probe_json(tools, out)
        payload["result"] = {"size": out.stat().st_size, "size_human": human_size(out.stat().st_size),
                             "duration_s": _f((data.get("format") or {}).get("duration"))}
    emit(payload, args)
    return 0


def cmd_normalize(args) -> int:
    tools = Tools(args)
    ffmpeg = tools.require_ffmpeg()
    src = Path(args.input).expanduser()
    codec, note = _safe_audio_codec(tools, args, args.codec)
    ext = AUDIO_EXT.get(codec, ".m4a")
    out = prepare_output(resolve_output(src, args.out, "_norm", args.outdir, default_ext=ext))
    filter_expr = f"loudnorm=I={args.lufs}:TP={args.true_peak}:LRA={args.lra}"
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src), "-vn",
           "-af", filter_expr, "-c:a", codec, "-b:a", args.bitrate, str(out)]
    payload = {"file": str(src.resolve()), "out": str(out.resolve()), "filter": filter_expr,
               "codec": codec, "codec_note": note}
    run_ffmpeg(tools, cmd, args, payload, timeout=None)
    _finish_sizes(payload)
    emit(payload, args)
    return 0


def cmd_silence(args) -> int:
    tools = Tools(args)
    ffmpeg = tools.require_ffmpeg()
    src = Path(args.input).expanduser()
    total = duration_of(tools, src)
    cmd = [ffmpeg, "-hide_banner", "-v", "info", "-i", str(src), "-vn",
           "-af", f"silencedetect=noise={args.noise_db}dB:d={args.min_duration}",
           "-f", "null", "-"]
    proc = run(cmd, timeout=None)
    log = proc.stderr or ""

    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[0-9.]+)", log)]
    silences = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else total
        if end is None:
            continue
        silences.append({"start_s": round(start, 3), "start": format_timecode(start),
                         "end_s": round(end, 3), "end": format_timecode(end),
                         "length_s": round(end - start, 3)})

    speech = []
    cursor = 0.0
    for silence in silences:
        if silence["start_s"] > cursor + 0.05:
            speech.append({"start_s": round(cursor, 3), "start": format_timecode(cursor),
                           "end_s": silence["start_s"], "end": silence["end"]})
        cursor = max(cursor, silence["end_s"])
    if total and cursor < total - 0.05:
        speech.append({"start_s": round(cursor, 3), "start": format_timecode(cursor),
                       "end_s": round(total, 3), "end": format_timecode(total)})
    for segment in speech:
        segment["length_s"] = round(segment["end_s"] - segment["start_s"], 3)

    payload = {"file": str(src.resolve()), "duration_s": total,
               "noise_db": args.noise_db, "min_duration_s": args.min_duration,
               "silence_count": len(silences), "silences": silences,
               "speech_count": len(speech), "speech": speech,
               "silent_total_s": round(sum(s["length_s"] for s in silences), 3)}
    emit(payload, args)
    return 0


def cmd_loudness(args) -> int:
    tools = Tools(args)
    ffmpeg = tools.require_ffmpeg()
    src = Path(args.input).expanduser()
    log = ""
    if args.ebur128:
        cmd = [ffmpeg, "-hide_banner", "-v", "info", "-i", str(src), "-vn",
               "-af", "ebur128=peak=true", "-f", "null", "-"]
        log = run(cmd, timeout=None).stderr or ""
        summary = log.rsplit("Summary:", 1)[-1]
        values = {}
        for key, pattern in (("integrated_lufs", r"I:\s*(-?[0-9.]+)\s*LUFS"),
                             ("loudness_range_lu", r"LRA:\s*(-?[0-9.]+)\s*LU"),
                             ("true_peak_dbfs", r"Peak:\s*(-?[0-9.]+)\s*dBFS")):
            found = re.search(pattern, summary)
            values[key] = float(found.group(1)) if found else None
        payload = {"file": str(src.resolve()), "meter": "ebur128", **values}
        emit(payload, args)
        return 0

    for stream in (0, 1, 2):
        cmd = [ffmpeg, "-hide_banner", "-v", "info", "-i", str(src),
               "-map", f"0:a:{stream}", "-af", "volumedetect", "-f", "null", "-"]
        proc = run(cmd)
        if proc.returncode != 0:
            if stream == 0:
                raise ToolError(f"no usable audio stream in {src}")
            break
        log += proc.stderr or ""
    mean = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", log)
    peak = re.search(r"max_volume:\s*(-?[0-9.]+) dB", log)
    payload = {"file": str(src.resolve()), "meter": "volumedetect",
               "mean_dbfs": float(mean.group(1)) if mean else None,
               "max_dbfs": float(peak.group(1)) if peak else None}
    emit(payload, args)
    return 0


def cmd_report(args) -> int:
    tools = Tools(args)
    src = Path(args.input).expanduser()
    if not src.exists():
        raise ToolError(f"input does not exist: {src}")

    out = Path(args.out) if args.out else src.parent / f"{src.stem}_report.md"
    prepare_output(out)
    assets = out.parent / f"{out.stem}_assets"
    assets.mkdir(parents=True, exist_ok=True)

    data = probe_json(tools, src)
    fmt = data.get("format") or {}
    duration = _f(fmt.get("duration")) or duration_of(tools, src)
    vstream = video_stream(data)
    astream = audio_streams(data)

    frames_dir = assets / "frames"
    sheet_path = assets / "sheet.jpg"
    sheet_ok = False
    try:
        sheet_args = argparse.Namespace(**vars(args))
        sheet_args.input = str(src)
        sheet_args.outdir = None
        sheet_args.out = str(sheet_path)
        sheet_args.count = args.count
        sheet_args.cols = args.cols
        sheet_args.rows = None
        sheet_args.quality = 3
        sheet_args.frame_width = args.frame_width
        sheet_args.cell_width = args.cell_width
        sheet_args.labels = True
        sheet_args.label_size = 12
        sheet_args.print_command = False
        sheet_args.json_out = None
        sheet_args._quiet = True
        sheet_ok = cmd_sheet(sheet_args) == 0 and sheet_path.exists()
    except ToolError:
        sheet_ok = False

    lines: list[str] = []
    lines.append(f"# 视频检查报告 / Video inspection report")
    lines.append("")
    lines.append(f"- 文件 File: `{src.resolve()}`")
    lines.append(f"- 大小 Size: {human_size(src.stat().st_size)}")
    lines.append(f"- 容器 Container: {fmt.get('format_long_name') or fmt.get('format_name')}")
    lines.append(f"- 时长 Duration: {format_timecode(duration)} ({_round(duration)} s)")
    if vstream:
        lines.append(f"- 画面 Video: {vstream.get('codec_name')} {vstream.get('width')}x{vstream.get('height')} "
                     f"@ {_fps(vstream.get('avg_frame_rate') or vstream.get('r_frame_rate'))} fps, "
                     f"{vstream.get('pix_fmt')}, {human_size(_i(vstream.get('bit_rate')) or 0)}/s")
    if astream:
        for stream in astream:
            lines.append(f"- 音频 Audio #{stream.get('index')}: {stream.get('codec_name')} "
                         f"{stream.get('sample_rate')} Hz, {stream.get('channels')} ch, "
                         f"{human_size(_i(stream.get('bit_rate')) or 0)}/s")
    else:
        lines.append("- 音频 Audio: 无 / none")
    lines.append("")
    if sheet_ok:
        lines.append(f"## 关键帧 / Contact sheet")
        lines.append("")
        lines.append(f"![contact sheet]({assets.name}/sheet.jpg)")
        lines.append("")
    lines.append("## 原始元数据 / Raw metadata")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({"format": fmt, "streams": data.get("streams", [])},
                            ensure_ascii=False, indent=2)[:12000])
    lines.append("```")
    if (frames_dir.exists() and any(frames_dir.iterdir())) or (sheet_ok and args.frames):
        lines.append("")
        lines.append(f"关键帧目录 / Frames: `{frames_dir}`")
    out.write_text("\n".join(lines), encoding="utf-8")

    html_path = out.with_suffix(".html")
    _write_html_report(html_path, src, fmt, data, sheet_path if sheet_ok else None)

    payload = {"file": str(src.resolve()), "report": str(out.resolve()),
               "html": str(html_path.resolve()),
               "sheet": str(sheet_path.resolve()) if sheet_ok else None,
               "duration_s": duration,
               "hint": "read the .html with the browser, or the sheet image with read_image"}
    emit(payload, args)
    return 0


def _write_html_report(html_path: Path, src: Path, fmt: dict, data: dict, sheet: Path | None) -> None:
    rows = []
    for stream in data.get("streams", []):
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(str(stream.get("index"))),
                html.escape(str(stream.get("codec_type"))),
                html.escape(str(stream.get("codec_name"))),
                html.escape(f"{stream.get('width', '')}x{stream.get('height', '')}".strip("x")),
                html.escape(str(stream.get("sample_rate") or stream.get("bit_rate") or "")),
            )
        )
    sheet_html = (f'<img src="{html.escape(sheet.name)}" alt="contact sheet" '
                  f'style="max-width:100%;border-radius:8px">' if sheet else "")
    html_path.write_text(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>视频报告 - {html.escape(src.name)}</title>
<style>
 body {{ font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        margin: 32px auto; max-width: 960px; color: #e8e8ea; background: #141416; }}
 h1 {{ font-size: 22px; }} code {{ background: #26262a; padding: 2px 6px; border-radius: 4px; }}
 table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
 th, td {{ border: 1px solid #333; padding: 6px 10px; text-align: left; font-size: 14px; }}
 th {{ background: #1f1f24; }}
</style></head><body>
<h1>视频检查报告</h1>
<p><code>{html.escape(str(src.resolve()))}</code></p>
<ul>
 <li>容器: {html.escape(str(fmt.get('format_long_name') or fmt.get('format_name')))}</li>
 <li>时长: {format_timecode(_f(fmt.get('duration')))}</li>
 <li>码率: {html.escape(human_size(_i(fmt.get('bit_rate')) or 0))}/s</li>
 <li>大小: {html.escape(human_size(_i(fmt.get('size')) or src.stat().st_size))}</li>
</ul>
{sheet_html}
<h2>流 / Streams</h2>
<table><tr><th>#</th><th>类型</th><th>编码</th><th>尺寸</th><th>采样率/码率</th></tr>
{''.join(rows)}
</table>
</body></html>""", encoding="utf-8")


def cmd_transcribe(args) -> int:
    """Best-effort speech-to-text: proves whether a usable local backend exists."""
    src = Path(args.input).expanduser()
    tools = Tools(args)
    binary = args.binary or shutil.which("whisper-cli") or shutil.which("whisper")
    if not binary:
        payload = {
            "file": str(src.resolve()),
            "ok": False,
            "reason": "no local speech-to-text binary found (whisper.cpp `whisper-cli` or `whisper`)",
            "options": [
                "install whisper.cpp and pass --binary <path-to-whisper-cli> --model <ggml-model.bin>",
                "use the DSH voice-input feature for dictation instead of file transcription",
                "this skill deliberately does not ship a model: ASR models are hundreds of MB",
            ],
        }
        emit(payload, args)
        return 5

    wav = Path(tempfile.gettempdir()) / f"{src.stem}_16k.wav"
    ffmpeg = tools.require_ffmpeg()
    ffmpeg_run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(src),
                "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], timeout=None)
    out_prefix = Path(args.out).with_suffix("") if args.out else src.parent / f"{src.stem}_transcript"
    cmd = [binary, "-f", str(wav), "-l", args.language, "-otxt", "-osrt", "-of", str(out_prefix)]
    if args.model:
        cmd[1:1] = ["-m", args.model]
    proc = run(cmd, timeout=None)
    payload = {"file": str(src.resolve()), "ok": proc.returncode == 0, "binary": binary,
               "text": f"{out_prefix}.txt" if Path(f"{out_prefix}.txt").exists() else None,
               "srt": f"{out_prefix}.srt" if Path(f"{out_prefix}.srt").exists() else None,
               "stdout_tail": (proc.stdout or "")[-800:], "stderr_tail": (proc.stderr or "")[-800:]}
    emit(payload, args)
    return 0 if payload["ok"] else 6


# ======================================================================================
# small value helpers
# ======================================================================================

def _finish_sizes(payload: dict) -> None:
    out = payload.get("out")
    if out and Path(out).exists():
        size = Path(out).stat().st_size
        payload["out_size"] = size
        payload["out_size_human"] = human_size(size)
        src = payload.get("file")
        if src and Path(src).exists():
            before = Path(src).stat().st_size
            if before:
                payload["size_ratio"] = round(size / before, 3)
        payload["ok"] = True
    elif payload.get("executed", True):
        payload["ok"] = False


def _safe_time(seconds: float) -> str:
    return f"{max(0.0, seconds):08.2f}".replace(".", "s")


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _round(value, digits: int = 3):
    return round(value, digits) if isinstance(value, (int, float)) else None


def _fps(rate: str | None):
    if not rate or "/" not in str(rate):
        return _f(rate)
    num, _, den = str(rate).partition("/")
    try:
        den_f = float(den)
        return round(float(num) / den_f, 3) if den_f else None
    except (TypeError, ValueError):
        return None


def _rotation(stream: dict):
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            return int(tags["rotate"])
        except (TypeError, ValueError):
            return None
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(side["rotation"])
            except (TypeError, ValueError):
                return None
    return None


# ======================================================================================
# parser
# ======================================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video_ops.py",
        description="Video/audio operations for the DSH video-toolkit skill (ffmpeg backed).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Every command accepts --bootstrap/--download when ffmpeg is missing, "
               "--print-command to show the ffmpeg line without running it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="diagnose tools, encoders, filters and Python modules")
    add_common(p)
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("bootstrap", help="install a private ffmpeg/ffprobe pair")
    add_common(p, bootstrap=False)
    p.add_argument("--download", action="store_true",
                   help="download ffmpeg from a public mirror when no local copy is available")
    p.add_argument("--source", help="local archive/binary/directory to install from instead")
    p.add_argument("--retries", type=int, default=3,
                   help="how many download rounds to attempt before giving up (default 3)")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("probe", help="metadata as JSON (duration, streams, codecs, fps)")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("frames", help="extract frames as images the model can read")
    add_common(p)
    p.add_argument("input")
    add_outdir(p)
    p.add_argument("--at", action="append", help="timestamp, repeatable (12.5 / 00:01:02.5)")
    p.add_argument("--count", type=int, help="evenly spaced sample count")
    p.add_argument("--every", help="sample every N seconds")
    p.add_argument("--all-frames", action="store_true", help="export every frame (big)")
    p.add_argument("--fps", type=float, help="frame rate when sampling all frames")
    p.add_argument("--width", type=int, default=960, help="frame width, -2 keeps aspect (default 960)")
    p.add_argument("--format", default="jpeg", help="jpeg|png|webp")
    p.add_argument("--quality", type=int, default=3, help="jpeg/webp quality, 2 best .. 31 worst")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("sheet", help="build one labeled contact sheet image")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--out", help="output image path (default <input>_sheet.jpg)")
    p.add_argument("--count", type=int, default=12, help="cell count (default 12)")
    p.add_argument("--cols", type=int, default=4, help="columns (default 4)")
    p.add_argument("--rows", type=int, default=None, help="rows (overrides count)")
    p.add_argument("--cell-width", type=int, default=480)
    p.add_argument("--frame-width", type=int, default=960)
    p.add_argument("--labels", action="store_true", default=True, help="draw timestamps (Pillow)")
    p.add_argument("--no-labels", dest="labels", action="store_false")
    p.add_argument("--label-size", type=int, default=12)
    p.add_argument("--quality", type=int, default=3)
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("scenes", help="detect shot boundaries")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--threshold", type=float, default=0.35, help="scene score 0..1 (default 0.35)")
    p.add_argument("--frames", help="also export one frame per scene into this directory")
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_scenes)

    p = sub.add_parser("clip", help="cut a segment")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--start", required=True, help="start timestamp")
    p.add_argument("--end", help="end timestamp")
    p.add_argument("--duration", help="length instead of --end")
    p.add_argument("--out")
    add_outdir(p)
    p.add_argument("--copy", action="store_true", help="stream copy (fast, keyframe aligned)")
    p.add_argument("--video-encoder")
    p.add_argument("--audio-encoder")
    p.add_argument("--audio-bitrate", default="128k")
    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--preset", default="medium")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_clip)

    p = sub.add_parser("compress", help="shrink a file (CRF or target size)")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--out")
    add_outdir(p)
    p.add_argument("--crf", type=int, default=26)
    p.add_argument("--preset", default="medium")
    p.add_argument("--target-mb", type=float, help="aim for this many megabytes")
    p.add_argument("--max-width", type=int, help="downscale so width <= N")
    p.add_argument("--video-encoder")
    p.add_argument("--audio-encoder")
    p.add_argument("--audio-bitrate", default="128k")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_compress)

    p = sub.add_parser("convert", help="change container/codec (mp4, mkv, webm, mov)")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--to", required=True, help="target extension, e.g. mp4 / mkv / webm")
    p.add_argument("--out")
    add_outdir(p)
    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--preset", default="medium")
    p.add_argument("--max-width", type=int)
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--video-encoder")
    p.add_argument("--audio-encoder")
    p.add_argument("--audio-bitrate", default="128k")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("scale", help="resize")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--size", required=True, help="1280x720, 1920:-2, or 720p/1080p/4k")
    p.add_argument("--fit", choices=["stretch", "pad", "crop"], default="stretch")
    p.add_argument("--out")
    add_outdir(p)
    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--preset", default="medium")
    p.add_argument("--video-encoder")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_scale)

    p = sub.add_parser("gif", help="animated GIF with an optimized palette")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--out")
    add_outdir(p)
    p.add_argument("--start", default="0")
    p.add_argument("--duration", default="4")
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--max-height", type=int, default=None)
    p.add_argument("--fps", type=int, default=12)
    p.add_argument("--palette-format", default="png",
                   help="still-image format for the temporary palette (png/bmp/tiff/ppm)")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_gif)

    p = sub.add_parser("audio", help="extract / transcode the audio track")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--out")
    add_outdir(p)
    p.add_argument("--codec", default="aac",
                   help="aac|libmp3lame|libopus|pcm_s16le|flac|libvorbis|copy")
    p.add_argument("--bitrate", default="192k")
    p.add_argument("--rate", type=int, default=44100)
    p.add_argument("--mono", action="store_true")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_audio)

    p = sub.add_parser("normalize", help="EBU R128 loudness normalization")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--out")
    add_outdir(p)
    p.add_argument("--lufs", type=float, default=-16.0)
    p.add_argument("--true-peak", type=float, default=-1.5)
    p.add_argument("--lra", type=float, default=11.0)
    p.add_argument("--codec", default="aac")
    p.add_argument("--bitrate", default="192k")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_normalize)

    p = sub.add_parser("silence", help="find silent stretches / speech windows")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--noise-db", type=float, default=-35.0)
    p.add_argument("--min-duration", type=float, default=0.6)
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_silence)

    p = sub.add_parser("loudness", help="measure loudness (EBU R128 or volumedetect)")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--ebur128", action="store_true", help="use ebur128 instead of volumedetect")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_loudness)

    p = sub.add_parser("report", help="write a Markdown + HTML inspection report")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--out", help="report path (default <input>_report.md)")
    p.add_argument("--count", type=int, default=12)
    p.add_argument("--cols", type=int, default=4)
    p.add_argument("--frame-width", type=int, default=960)
    p.add_argument("--cell-width", type=int, default=480)
    p.add_argument("--frames", action="store_true", help="note the frames directory in the report")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("transcribe", help="speech to text through a local whisper.cpp build")
    add_common(p)
    p.add_argument("input")
    p.add_argument("--binary", help="path to whisper-cli / main")
    p.add_argument("--model", help="ggml model path")
    p.add_argument("--language", default="auto")
    p.add_argument("--out", help="output prefix (writes .txt and .srt)")
    p.add_argument("--json-out")
    p.set_defaults(func=cmd_transcribe)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ToolError as exc:
        print_json({"ok": False, "error": str(exc)})
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
