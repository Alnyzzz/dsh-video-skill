#!/usr/bin/env python3
"""Shared tooling for the DSH `video-toolkit` skill.

Responsibilities:
  * locate a usable `ffmpeg` / `ffprobe` pair on this machine;
  * bootstrap a private, verified pair into `$DSH_VIDEO_HOME/tools` when none exists;
  * expose small subprocess / formatting helpers used by `video_ops.py`;
  * fall back to a pure-Python MP4/MOV header parser when no ffprobe exists.

The module is standard-library only, so it runs on the Python that DSH already ships.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
EXE = ".exe" if IS_WINDOWS else ""

# Download sources tried in order when bootstrapping a private ffmpeg pair.
# The 7z "essentials" build is roughly a third of the zip, so it is tried first when
# a 7-Zip binary is available to unpack it.
WINDOWS_7Z_SOURCES = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.7z",
    "https://github.com/GyanD/codexffmpeg/releases/latest/download/ffmpeg-release-essentials.7z",
]
WINDOWS_ZIP_SOURCES = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/GyanD/codexffmpeg/releases/latest/download/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]
MACOS_ZIP_SOURCES = [
    "https://evermeet.cx/ffmpeg/getrelease/zip",
    "https://evermeet.cx/ffprobe/getrelease/zip",
]
LINUX_TARBALL_SOURCES = [
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
]
# Public mirrors for users on slow or restricted links. Tried only after the origin, and
# each one is probed before use; dead mirrors are dropped rather than retried.
WINDOWS_MIRROR_TEMPLATES = [
    "https://mirrors.huaweicloud.com/ffmpeg/{name}",
    "https://mirrors.nju.edu.cn/ffmpeg/{name}",
]


class ToolError(RuntimeError):
    """Raised when a tool cannot be found, installed or executed."""


# --------------------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------------------

def _no_window_flags() -> int:
    if IS_WINDOWS:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def run(cmd, *, text: bool = True, timeout: int | None = None, check: bool = False):
    """Run a command list with a hidden console window; return CompletedProcess."""
    proc = subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=text,
        encoding=None if not text else "utf-8",
        errors=None if not text else "replace",
        timeout=timeout,
        creationflags=_no_window_flags(),
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ToolError(f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}\n{detail}")
    return proc


def ffmpeg_run(cmd, *, timeout: int | None = None, check: bool = True):
    """Run ffmpeg/ffprobe quietly and raise ToolError with its stderr tail on failure."""
    proc = run(cmd, timeout=timeout)
    if check and proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-25:])
        raise ToolError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
    return proc


# --------------------------------------------------------------------------------------
# locating tools
# --------------------------------------------------------------------------------------

def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def video_home() -> Path:
    """Private tool/data directory: $DSH_VIDEO_HOME or <DSH_HOME>/video."""
    env = os.environ.get("DSH_VIDEO_HOME")
    return Path(env) if env else dsh_home() / "video"


def tools_dir() -> Path:
    return video_home() / "tools"


def work_dir(prefix: str = "work") -> Path:
    """Create a scratch directory that a sandboxed run is allowed to use.

    `tempfile` writes to the system temp directory, which DSH's workspace-write file
    policy denies to child processes; and `mkdtemp` applies mode 0700, which the sandbox
    ACL then refuses to let those processes read. So scratch always lives under the
    private per-user directory the skill already owns, created with the parent's ACL.
    """
    directory = video_home() / f".{prefix}-{os.urandom(4).hex()}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def remove_tree(path) -> bool:
    """Best-effort recursive removal that never raises and never fails a command."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 - cleanup is not the operation
        return False
    return not Path(path).exists()


@contextlib.contextmanager
def _scratch():
    """Scratch directory under the private home, removed on the way out."""
    directory = work_dir("ffmpeg")
    try:
        yield directory
    finally:
        remove_tree(directory)


def cache_path() -> Path:
    """Discovered-candidate cache (not a tool-selection cache).

    Only the *candidate list* is cached, never a chosen binary: the tool actually
    executed is always probed and version-checked in the current call, so nothing on
    disk can redirect the skill at a different executable. Delete the file to force a
    fresh scan (`--rescan` does the same).
    """
    return tools_dir() / "candidates.json"


def _application_scan_dirs() -> list[Path]:
    """Common application/package trees, globbed one level deep in phase 1."""
    out: list[Path] = []
    if IS_WINDOWS:
        out += [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
            Path(os.environ.get("USERPROFILE", "")) / "scoop" / "shims",
            Path("C:/ProgramData/chocolatey/bin"),
            Path("C:/ffmpeg/bin"),
            Path("C:/Program Files/ffmpeg/bin"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
            Path(os.environ.get("LOCALAPPDATA", "")),
            Path("C:/Program Files"),
            Path("C:/Program Files (x86)"),
        ]
    elif IS_MACOS:
        out += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/Applications")]
    else:
        out += [Path("/usr/bin"), Path("/usr/local/bin"), Path("/snap/bin"), Path("/usr/lib")]
    return [p for p in out if str(p) and p.exists()]


def _deep_scan_roots() -> list[Path]:
    """Phase-2 roots: user app-data trees and secondary drives with bundled players.

    Every root is walked with a bounded depth so the scan terminates predictably even
    on a drive full of unrelated software.
    """
    roots: list[Path] = []
    if IS_WINDOWS:
        roots += [Path(os.environ.get("APPDATA", "")), Path(os.environ.get("LOCALAPPDATA", ""))]
        roots += [Path("C:/Program Files"), Path("C:/Program Files (x86)")]
        roots += [Path(p) for p in (os.environ.get("DSH_EXTRA_SCAN_ROOTS", "").split(os.pathsep)) if p]
        for letter in "DEFGHIJ":
            drive = Path(f"{letter}:/")
            if drive.exists():
                roots.append(drive)
    elif IS_MACOS:
        roots += [Path("/Applications"), Path.home() / "Library" / "Application Support"]
    else:
        roots += [Path("/opt"), Path.home() / ".local"]
    return [r for r in roots if str(r) and r.exists() and not str(r).lower().startswith("c:\\windows")]


def _bounded_walk(roots: list[Path], names: set[str], *, max_depth: int = 4,
                  seconds: float = 90.0) -> dict[str, list[Path]]:
    """Breadth-first walk of `roots` collecting files whose lower-case name is in `names`.

    Depth- and time-bounded so it terminates predictably on a drive full of unrelated
    software. Returns {lower-case name: [paths]}.
    """
    import time
    found: dict[str, list[Path]] = {n: [] for n in names}
    deadline = time.monotonic() + seconds
    for root in roots:
        frontier = [(root, 0)]
        while frontier:
            if time.monotonic() > deadline:
                return found
            current, depth = frontier.pop(0)
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_file():
                                lowered = entry.name.lower()
                                if lowered in names:
                                    found[lowered].append(Path(entry.path))
                            elif entry.is_dir(follow_symlinks=False) and depth < max_depth:
                                if entry.name.startswith(("$", ".")):
                                    continue
                                frontier.append((Path(entry.path), depth + 1))
                        except OSError:
                            continue
            except (OSError, PermissionError):
                continue
    return found


def _scan_candidates() -> dict[str, list[Path]]:
    """Cheap phase-1 scan of package-manager and well-known application locations."""
    result: dict[str, list[Path]] = {"ffmpeg.exe" if IS_WINDOWS else "ffmpeg": [],
                                     "ffprobe.exe" if IS_WINDOWS else "ffprobe": []}
    for name in list(result):
        binary = name[: -len(EXE)] if EXE else name
        for directory in _application_scan_dirs():
            candidates = [directory / name]
            for pattern in (f"*/{name}", f"{binary}/{name}", f"*/bin/{name}"):
                try:
                    candidates += list(directory.glob(pattern))
                except (OSError, ValueError):
                    continue
            for path in candidates:
                if path.is_file():
                    result[name].append(path)
    return result


def _cache_file() -> Path:
    return tools_dir() / "candidates.json"


def _cached_walk() -> dict[str, list[str]] | None:
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("version") != 2 or not isinstance(data.get("paths"), dict):
        return None
    return data["paths"]


def _store_walk(payload: dict[str, list[Path]]) -> None:
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"version": 2, "scanned": _now(),
             "paths": {k: [str(p) for p in v] for k, v in payload.items()}},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _candidate_executables(name: str, *, deep: bool = False) -> list[Path]:
    """Every plausible executable named ffmpeg/ffprobe, cheapest locations first.

    Phase 1 (always) checks environment overrides, the private tools directory, PATH and
    the package-manager locations. Phase 2 (``deep``, or whenever no usable pair was
    found yet) walks the user app-data trees and secondary drives for bundled copies,
    reusing the on-disk walk result afterwards.
    """
    filename = f"{name}{EXE}"
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if not path or not path.is_file():
            return
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path).lower()
        if key not in seen:
            seen.add(key)
            found.append(path)

    def add_many(paths) -> None:
        for path in paths:
            add(path)

    env = os.environ.get(f"DSH_FFMPEG_{name.upper()}") or os.environ.get("DSH_FFMPEG_DIR")
    if env:
        candidate = Path(env)
        add(candidate if candidate.is_file() else candidate / filename)

    add(tools_dir() / filename)
    add(tools_dir() / "bin" / filename)

    which = shutil.which(name)
    if which:
        add(Path(which))

    scanned = _scan_candidates()
    add_many(scanned.get(filename, []))

    if not deep:
        return found

    cached = _cached_walk()
    if cached is not None:
        add_many(Path(p) for p in cached.get(filename, []))
        return found

    walked = _bounded_walk(_deep_scan_roots(), {"ffmpeg.exe", "ffprobe.exe"} if IS_WINDOWS
                           else {"ffmpeg", "ffprobe"})
    if walked:
        _store_walk({k: v for k, v in walked.items()})
    add_many(walked.get(filename, []))
    return found


def _now() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def resolve_tools(*, rescan: bool = False) -> dict:
    """Return the best ffmpeg/ffprobe pair available right now.

    Preference order: a private pair in the tools directory, a same-directory pair
    found elsewhere, then a verified cross-directory mix. The cheap phase-1 locations
    are probed on every call; bundled copies found by an earlier whole-disk walk are
    reused from the on-disk candidate cache. Pass ``rescan=True`` (CLI: ``--rescan``)
    to walk the disks again.
    """
    if rescan:
        _cache_file().unlink(missing_ok=True)
    selected = _select_pair(deep=False)
    if not (selected["ffmpeg"] and selected["ffprobe"]):
        deeper = _select_pair(deep=True)
        if (deeper["ffmpeg"] and deeper["ffprobe"]) or not selected["ffmpeg"]:
            selected = deeper
    return selected


def _select_pair(*, deep: bool) -> dict:
    ffmpegs = [p for p in _candidate_executables("ffmpeg", deep=deep) if _is_runnable(p)]
    probes = [p for p in _candidate_executables("ffprobe", deep=deep) if _is_runnable(p)]

    chosen_ffmpeg = chosen_probe = None
    source = None

    for exe in ffmpegs:
        sibling = exe.parent / f"ffprobe{EXE}"
        if sibling.is_file() and _is_runnable(sibling):
            chosen_ffmpeg, chosen_probe = exe, sibling
            source = "dsh-video-tools" if tools_dir() in exe.parents else "same-directory-pair"
            break

    if not chosen_ffmpeg and ffmpegs:
        chosen_ffmpeg = ffmpegs[0]
    if not chosen_probe and probes:
        chosen_probe = probes[0]
        if chosen_ffmpeg:
            for probe in probes:
                if Path(probe).parent == Path(chosen_ffmpeg).parent:
                    chosen_probe = probe
                    break
    if not source and (chosen_ffmpeg or chosen_probe):
        if chosen_ffmpeg and chosen_probe:
            source = "verified-pair" if _pair_works(chosen_ffmpeg, chosen_probe) else "mixed-pair"
        else:
            source = "partial"

    return {
        "ffmpeg": chosen_ffmpeg,
        "ffprobe": chosen_probe,
        "paired": source in {"dsh-video-tools", "same-directory-pair", "verified-pair", "mixed-pair"},
        "source": source,
        "candidates": {"ffmpeg": [str(p) for p in ffmpegs[:10]], "ffprobe": [str(p) for p in probes[:10]]},
        "cache": str(tools_dir() / "candidates-ffmpeg.json"),
    }


def _pair_works(ffmpeg: Path, ffprobe: Path) -> bool:
    """Cheap interop check: ffprobe must at least answer a version query."""
    try:
        proc = run([ffprobe, "-hide_banner", "-version"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    try:
        proc = run([ffmpeg, "-hide_banner", "-version"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def locate_tools(*, bootstrap: bool = False, source: str | None = None,
                 allow_download: bool = False, rescan: bool = False) -> dict:
    """Resolve ffmpeg/ffprobe, optionally installing a private pair when missing."""
    info = resolve_tools(rescan=rescan)
    if bootstrap and not (info["ffmpeg"] and info["ffprobe"]):
        install = bootstrap_tools(source=source, allow_download=allow_download)
        if install.get("installed"):
            info = resolve_tools()
            info["installed"] = install["installed"]
            info["source"] = install["source"]
    return info


def _is_runnable(exe: Path, timeout: int = 20) -> bool:
    try:
        proc = run([exe, "-hide_banner", "-version"], timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "version" in (proc.stdout or "").lower()


def tool_versions(info: dict) -> dict:
    out = {}
    for key in ("ffmpeg", "ffprobe"):
        exe = info.get(key)
        if not exe:
            out[key] = None
            continue
        try:
            first = (run([exe, "-hide_banner", "-version"], timeout=30).stdout or "").splitlines()
        except (OSError, subprocess.SubprocessError):
            first = []
        out[key] = first[0].strip() if first else "unknown"
    return out


# --------------------------------------------------------------------------------------
# bootstrap: install a private, verified ffmpeg pair
# --------------------------------------------------------------------------------------

def _walk_for_pair(root: Path) -> tuple[Path, Path] | None:
    ffmpeg_hits = sorted(root.rglob(f"ffmpeg{EXE}"))
    for exe in ffmpeg_hits:
        probe = exe.parent / f"ffprobe{EXE}"
        if probe.is_file():
            return exe, probe
    if ffmpeg_hits:
        return ffmpeg_hits[0], None
    return None


# Sibling files that a private ffmpeg pair may need at run time. Copying every DLL from a
# host application directory would drag in unrelated megabytes (OBS, Qt, game engines).
SIDECAR_PATTERNS = ("avcodec*.dll", "avformat*.dll", "avutil*.dll", "avfilter*.dll",
                    "avdevice*.dll", "swscale*.dll", "swresample*.dll", "postproc*.dll",
                    "libx264*.dll", "libx265*.dll", "libvpx*.dll", "libmp3lame*.dll",
                    "libopus*.dll", "libvorbis*.dll", "libogg*.dll", "libass*.dll",
                    "libfreetype*.dll", "libharfbuzz*.dll", "libfribidi*.dll", "zlib*.dll",
                    "libwinpthread*.dll", "libgcc*.dll", "libstdc++*.dll",
                    "msvcp*.dll", "vcruntime*.dll", "concrt*.dll", "vcomp*.dll")
SIDECAR_SUFFIXES = {".dll", ".txt", ".md", ".license", ".rtf"}


def _install_pair(ffmpeg: Path, ffprobe: Path | None, dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    target_ffmpeg = dest / f"ffmpeg{EXE}"
    if ffmpeg.resolve() != target_ffmpeg.resolve():
        shutil.copy2(ffmpeg, target_ffmpeg)
    if ffprobe is not None:
        shutil.copy2(ffprobe, dest / f"ffprobe{EXE}")

    # Private builds (shared DLLs) need their siblings; static builds need none.
    for pattern in SIDECAR_PATTERNS:
        for sibling in ffmpeg.parent.glob(pattern):
            if sibling.is_file():
                try:
                    shutil.copy2(sibling, dest / sibling.name)
                except OSError:
                    pass
    for sibling in ffmpeg.parent.iterdir():
        if sibling.is_file() and sibling.suffix.lower() in SIDECAR_SUFFIXES and not sibling.suffix.lower() == ".dll":
            try:
                shutil.copy2(sibling, dest / sibling.name)
            except OSError:
                pass

    if not _is_runnable(target_ffmpeg):
        raise ToolError(f"installed ffmpeg is not runnable: {target_ffmpeg}")
    installed_probe = dest / f"ffprobe{EXE}" if ffprobe else None
    if installed_probe and not _is_runnable(installed_probe):
        installed_probe = None
    return {"ffmpeg": target_ffmpeg, "ffprobe": installed_probe}


def _curl() -> str | None:
    return shutil.which("curl") or shutil.which("curl.exe")


def _headers(url: str) -> dict:
    """HEAD/range probe: follow redirects, return final status, length and range support."""
    curl = _curl()
    if not curl:
        return {}
    proc = run([curl, "--location", "--silent", "--show-error", "--head",
                "--connect-timeout", "20", "--max-time", "60", url], timeout=90)
    text = proc.stdout or ""
    info: dict = {"status": 0, "length": 0, "ranges": False, "url": url}
    for line in text.splitlines():
        lowered = line.lower()
        if lowered.startswith("http/"):
            match = re.search(r"http/[\d.]+\s+(\d+)", line)
            if match:
                info["status"] = int(match.group(1))
        elif lowered.startswith("content-length:"):
            try:
                info["length"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif lowered.startswith("accept-ranges:") and "bytes" in lowered:
            info["ranges"] = True
    return info


DOWNLOAD_CHUNKS = 4
# A transfer moving slower than this for a full minute is treated as stalled and retried;
# the same flag makes curl give up on a connection that dies mid-transfer.
STALL_SECONDS = 60
STALL_LIMIT = "3072"


def _build_download_plan(sources: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if not IS_WINDOWS:
        return sources
    expanded: list[tuple[str, str]] = []
    for url, filename in sources:
        expanded.append((url, filename))
        base = filename.rsplit(".", 1)[0]
        for template in WINDOWS_MIRROR_TEMPLATES:
            for ext in (".7z", ".zip"):
                if filename.endswith(ext):
                    expanded.append((template.format(name=base + ext), filename))
    return expanded


def _download_racing(plan: list[tuple[str, str]], dest_dir: Path, expected: dict) -> tuple[Path, str]:
    """Download the same artifact from several mirrors at once; first complete file wins.

    Every mirror writes to its own path so a slow-but-alive source can be abandoned
    without corrupting progress elsewhere. Total wall time becomes the fastest mirror's
    time instead of the sum of every failed attempt. Only usable mirrors are raced: a
    mirror answering with an HTML error page (no length, no range support) is dropped.
    """
    import time
    usable = [(url, filename) for url, filename in plan
              if expected.get(url, 0) > 1024 * 1024 or not expected]
    if not usable:
        raise ToolError("no mirror reported a usable archive size")

    active: list[dict] = []
    for url, filename in usable[:3]:
        suffix = Path(filename).suffix
        target = dest_dir / f"{Path(filename).stem}.{len(active)}{suffix}"
        process = subprocess.Popen(
            [_curl(), "--fail", "--location", "--silent", "--show-error",
             "--connect-timeout", "25",
             "--speed-limit", STALL_LIMIT, "--speed-time", str(STALL_SECONDS),
             "--output", str(target), url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_no_window_flags(),
        )
        active.append({"url": url, "target": target, "proc": process,
                       "expected": expected.get(url, 0)})

    deadline = time.monotonic() + 3600
    result: tuple[Path, str] | None = None
    while active and result is None and time.monotonic() < deadline:
        time.sleep(2)
        for worker in list(active):
            target: Path = worker["target"]
            if target.exists() and target.stat().st_size > 1024:
                size = target.stat().st_size
                if (worker["expected"] and size >= worker["expected"]) or _looks_complete(target):
                    result = (target, worker["url"])
                    break
            if worker["proc"].poll() is not None:
                if result is None and target.exists() and target.stat().st_size > 1024:
                    result = (target, worker["url"])
                    break
                active.remove(worker)
        if result is not None:
            break

    for worker in active:
        try:
            worker["proc"].terminate()
        except OSError:
            pass
    if result is None:
        raise ToolError("every racing mirror failed or stalled: "
                        + ", ".join(w["url"] for w in active))
    return result


def _looks_complete(path: Path) -> bool:
    """Cheap sanity check for a finished archive when the server sent no length."""
    try:
        with open(path, "rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - 512))
            tail = handle.read()
    except OSError:
        return False
    return b"PK\x05\x06" in tail or tail.strip().endswith((b"7z\xbc\xaf\x27\x1c", b"\x00\x00"))


def _download_single(url: str, dest: Path, *, label: str = "") -> bool:
    """Resumable single-stream download with a stall guard."""
    proc = run([_curl(), "--fail", "--location", "--silent", "--show-error",
                "--retry", "3", "--retry-delay", "3", "--connect-timeout", "25",
                "--speed-limit", STALL_LIMIT, "--speed-time", str(STALL_SECONDS),
                "--continue-at", "-", "--output", str(dest), url], timeout=5400)
    return proc.returncode == 0 and dest.exists() and dest.stat().st_size >= 1024


def _download(url: str, dest: Path) -> Path:
    """Fetch a URL with the most reliable backend available on this machine.

    curl is preferred (Windows 10+, macOS, Linux all ship it) because it handles
    redirects, proxies and TLS far more consistently than a bare urllib call. A
    partial download is resumed rather than restarted, and a stalled transfer is
    retried. urllib and PowerShell's Invoke-WebRequest are fallbacks.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    name = dest.name

    curl = _curl()
    if curl:
        info = _headers(url)
        declared = info.get("length", 0)
        if dest.exists() and declared and dest.stat().st_size == declared:
            return dest
        if _download_single(url, dest, label=name):
            return dest
        errors.append(f"curl: {name} not fetched")

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "dsh-video-toolkit/1.0"})
        with urllib.request.urlopen(request, timeout=180) as response, open(dest, "wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 512)
        if dest.stat().st_size >= 1024:
            return dest
        errors.append("urllib: download too small")
    except Exception as exc:  # noqa: BLE001 - report and fall through to the next backend
        errors.append(f"urllib: {exc}")

    if IS_WINDOWS:
        script = (f"$ProgressPreference='SilentlyContinue';"
                  f"Invoke-WebRequest -Uri '{url}' -OutFile '{dest}' -UseBasicParsing -TimeoutSec 600")
        proc = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=1800)
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size >= 1024:
            return dest
        errors.append(f"Invoke-WebRequest: {(proc.stderr or '').strip()[:200]}")

    raise ToolError("download failed: " + " | ".join(errors))

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "dsh-video-toolkit/1.0"})
        with urllib.request.urlopen(request, timeout=180) as response, open(dest, "wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 512)
        if dest.stat().st_size >= 1024:
            return dest
        errors.append("urllib: download too small")
    except Exception as exc:  # noqa: BLE001 - report and fall through to the next backend
        errors.append(f"urllib: {exc}")

    if IS_WINDOWS:
        script = (f"$ProgressPreference='SilentlyContinue';"
                  f"Invoke-WebRequest -Uri '{url}' -OutFile '{dest}' -UseBasicParsing -TimeoutSec 600")
        proc = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=1800)
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size >= 1024:
            return dest
        errors.append(f"Invoke-WebRequest: {(proc.stderr or '').strip()[:200]}")

    raise ToolError("download failed: " + " | ".join(errors))


def _extract(archive: Path, workdir: Path) -> Path:
    suffix = archive.name.lower()
    workdir.mkdir(parents=True, exist_ok=True)
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(workdir)
        return workdir
    if suffix.endswith((".tar.xz", ".tar.gz", ".tgz")):
        run(["tar", "-xf", str(archive), "-C", str(workdir)], check=True)
        return workdir
    if suffix.endswith(".7z"):
        seven = next((p for p in (shutil.which("7z"), shutil.which("7za"),
                                  Path("C:/Program Files/7-Zip/7z.exe")) if p), None)
        if not seven:
            raise ToolError("7-Zip is required to unpack .7z archives; install 7-Zip or use a .zip source")
        run([seven, "x", "-y", f"-o{workdir}", str(archive)], check=True)
        return workdir
    if suffix.endswith(".exe"):
        return archive
    raise ToolError(f"unsupported archive type: {archive.name}")


def bootstrap_tools(*, source: str | None = None, allow_download: bool = False,
                    retries: int = 3) -> dict:
    """Install ffmpeg/ffprobe into the private tools dir.

    Precedence: explicit ``source`` (a binary, a directory, or an archive) > network
    download (``allow_download``). Slow links are expected: the racing downloader resumes
    partial files and ``retries`` bounds how many rounds it will attempt before giving up.
    """
    dest = tools_dir()
    result = {"installed": None, "ffprobe": None, "source": None}

    if source:
        candidate = Path(source)
        if not candidate.exists():
            raise ToolError(f"--source path does not exist: {candidate}")
        if candidate.is_file() and candidate.suffix.lower() in {".zip", ".7z", ".xz", ".gz", ".tgz"}:
            with _scratch() as tmp:
                root = _extract(candidate, Path(tmp))
                pair = _walk_for_pair(root)
                if not pair:
                    raise ToolError(f"no ffmpeg inside {candidate}")
                installed = _install_pair(pair[0], pair[1], dest)
            result.update(installed=installed["ffmpeg"], ffprobe=installed["ffprobe"], source=str(candidate))
            return result
        if candidate.is_file():
            installed = _install_pair(candidate, candidate.parent / f"ffprobe{EXE}", dest)
            result.update(installed=installed["ffmpeg"], ffprobe=installed["ffprobe"], source=str(candidate))
            return result
        pair = _walk_for_pair(candidate)
        if not pair:
            raise ToolError(f"no ffmpeg under {candidate}")
        installed = _install_pair(pair[0], pair[1], dest)
        result.update(installed=installed["ffmpeg"], ffprobe=installed["ffprobe"], source=str(candidate))
        return result

    if not allow_download:
        raise ToolError(
            "no ffmpeg/ffprobe pair found. Re-run with --download to fetch one, or pass "
            "--source <archive|binary|directory>."
        )

    sources: list[tuple[str, str]] = []   # (url, archive filename)
    if IS_WINDOWS:
        if _seven_zip():
            sources += [(url, "ffmpeg-release-essentials.7z") for url in WINDOWS_7Z_SOURCES]
        sources += [(url, "ffmpeg-release-essentials.zip") for url in WINDOWS_ZIP_SOURCES]
    elif IS_MACOS:
        sources += [(url, "ffmpeg.zip") for url in MACOS_ZIP_SOURCES]
    else:
        sources += [(url, "ffmpeg-release-static.tar.xz") for url in LINUX_TARBALL_SOURCES]

    if IS_WINDOWS:
        for template in WINDOWS_MIRROR_TEMPLATES:
            sources.append((template.format(name="ffmpeg-release-essentials.7z"),
                            "ffmpeg-release-essentials.7z"))
            sources.append((template.format(name="ffmpeg-release-essentials.zip"),
                            "ffmpeg-release-essentials.zip"))

    errors: list[str] = []
    plan = _build_download_plan(sources)
    expected: dict[str, int] = {}
    if _curl():
        for url, _ in plan:
            info = _headers(url)
            if info.get("length"):
                expected[url] = info["length"]
        for attempt in range(1, max(1, retries) + 1):
            try:
                with _scratch() as tmp:
                    tmp_path = Path(tmp)
                    archive, source_url = _download_racing(plan, tmp_path, expected)
                    root = _extract(archive, tmp_path / "x")
                    pair = _walk_for_pair(root)
                    if not pair:
                        raise ToolError("archive contained no ffmpeg")
                    installed = _install_pair(pair[0], pair[1] if len(pair) > 1 else None, dest)
                result.update(installed=installed["ffmpeg"], ffprobe=installed["ffprobe"],
                              source=source_url, attempts=attempt)
                return result
            except Exception as exc:  # noqa: BLE001 - retry, then fall through to the sequential loop
                errors.append(f"racing mirrors (attempt {attempt}/{retries}): {exc}")

    for url, filename in plan:
        try:
            with _scratch() as tmp:
                tmp_path = Path(tmp)
                archive = _download(url, tmp_path / filename)
                root = _extract(archive, tmp_path / "x")
                if root.is_file() and root.suffix.lower() == ".exe":
                    installed = _install_pair(root, None, dest)
                else:
                    pair = _walk_for_pair(root)
                    if not pair:
                        raise ToolError("archive contained no ffmpeg")
                    installed = _install_pair(pair[0], pair[1] if len(pair) > 1 else None, dest)
            result.update(installed=installed["ffmpeg"], ffprobe=installed["ffprobe"], source=url)
            return result
        except Exception as exc:  # noqa: BLE001 - try every mirror before failing
            errors.append(f"{url} -> {exc}")

    raise ToolError("could not download ffmpeg from any mirror:\n  " + "\n  ".join(errors))


def _seven_zip() -> Path | None:
    candidates = [shutil.which("7z"), shutil.which("7za"), shutil.which("7zr"),
                  Path("C:/Program Files/7-Zip/7z.exe"), Path("C:/Program Files (x86)/7-Zip/7z.exe")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


# --------------------------------------------------------------------------------------
# tiny pure-Python fallback for basic container facts (used when ffprobe is absent)
# --------------------------------------------------------------------------------------

class _Mp4Reader:
    """Minimal ISO-BMFF (MP4/MOV/M4A/3GP) header reader: duration, timescale, tracks."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _boxes(self, handle, end: int):
        while handle.tell() < end:
            start = handle.tell()
            header = handle.read(8)
            if len(header) < 8:
                return
            size, kind = struct.unpack(">I4s", header)
            header_size = 8
            if size == 1:
                size = struct.unpack(">Q", handle.read(8))[0]
                header_size = 16
            elif size == 0:
                size = end - start
            kind = kind.decode("latin-1")
            yield kind, start + header_size, start + size
            handle.seek(start + size)

    def _read(self, handle, limit: int) -> bytes:
        return handle.read(limit)

    def summary(self) -> dict:
        info: dict = {"format": {}, "streams": [], "reader": "pure-python"}
        with open(self.path, "rb") as handle:
            size = self.path.stat().st_size
            for kind, start, end in self._boxes(handle, size):
                if kind != "moov":
                    continue
                for sub, sstart, send in self._boxes(handle, end):
                    if sub == "mvhd":
                        handle.seek(sstart)
                        payload = self._read(handle, send - sstart)
                        version = payload[0]
                        if version == 1:
                            timescale = struct.unpack(">I", payload[20:24])[0]
                            duration = struct.unpack(">Q", payload[24:32])[0]
                        else:
                            timescale = struct.unpack(">I", payload[12:16])[0]
                            duration = struct.unpack(">I", payload[16:20])[0]
                        if timescale:
                            info["format"]["duration"] = duration / timescale
                            info["format"]["timescale"] = timescale
                    elif sub == "trak":
                        track: dict = {}
                        for leaf, lstart, lend in self._boxes(handle, send):
                            handle.seek(lstart)
                            payload = self._read(handle, min(lend - lstart, 256))
                            if leaf == "hdlr" and len(payload) > 12:
                                track["codec_type"] = {
                                    "vide": "video", "soun": "audio", "subt": "subtitle",
                                    "text": "subtitle", "meta": "data",
                                }.get(payload[8:12].decode("latin-1", "replace"), "unknown")
                            elif leaf == "mdhd":
                                version = payload[0]
                                if version == 1 and len(payload) >= 32:
                                    ts = struct.unpack(">I", payload[20:24])[0]
                                    dur = struct.unpack(">Q", payload[24:32])[0]
                                elif len(payload) >= 20:
                                    ts = struct.unpack(">I", payload[12:16])[0]
                                    dur = struct.unpack(">I", payload[16:20])[0]
                                else:
                                    ts = dur = 0
                                if ts:
                                    track["duration"] = dur / ts
                        if track.get("codec_type"):
                            info["streams"].append(track)
                break
        fmt = info["format"]
        fmt["format_name"] = self.path.suffix.lstrip(".").lower()
        fmt["size"] = self.path.stat().st_size
        fmt["probe_note"] = "ffprobe unavailable; read with the bundled ISO-BMFF header parser"
        return info


def fallback_probe(path: Path) -> dict:
    if Path(path).suffix.lower() in {".mp4", ".m4v", ".mov", ".m4a", ".3gp", ".3g2"}:
        return _Mp4Reader(path).summary()
    raise ToolError("no ffprobe available and the container is not ISO-BMFF; run bootstrap --download")


# --------------------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------------------

def parse_timestamp(value: str | float) -> float:
    """Accept 12.5, '12.5', '01:02:03.500', '02:03'."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    if ":" not in text:
        return float(text)
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad timestamp: {value}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def format_timecode(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours >= 1:
        return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"
    return f"{int(minutes):02d}:{secs:06.3f}"


def parse_size(value: str) -> tuple[int, int]:
    """'1280x720', '1280:-2', 'hd720'."""
    presets = {"480p": "854x480", "540p": "960x540", "hd": "1280x720", "720p": "1280x720",
               "1080p": "1920x1080", "1440p": "2560x1440", "4k": "3840x2160", "2160p": "3840x2160"}
    text = presets.get(str(value).strip().lower(), str(value).strip().lower())
    match = re.fullmatch(r"(\d+|-?\d+)[x:](\d+|-?\d+)", text)
    if not match:
        raise ValueError(f"bad size: {value} (use WIDTHxHEIGHT, e.g. 1280x720 or 1280:-2)")
    return int(match.group(1)), int(match.group(2))


def human_size(num_bytes: int | float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def write_json(path: Path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_json(payload) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:  # legacy console code pages
        sys.stdout.buffer.write(text.encode("utf-8", "replace"))
        print()


def platform_note() -> str:
    return f"{platform.system()} {platform.release()} ({platform.machine()}) python {platform.python_version()}"
