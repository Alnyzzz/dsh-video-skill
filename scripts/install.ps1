<#
.SYNOPSIS
    Install the video-toolkit skill into DeepSeek Harness.

.DESCRIPTION
    Copies skills/video-toolkit into <DSH_HOME>\skills\video-toolkit, where the DSH
    filesystem skill provider discovers it automatically (no restart needed), then
    verifies the bundle and reports which ffmpeg the skill can use.

    Also honours $env:DSH_AGENTS_HOME\skills and a custom -Destination.

.PARAMETER Destination
    Skill root to install into. Default: <DSH_HOME>\skills (or ~\.dsh\skills).

.PARAMETER Bootstrap
    Download a verified ffmpeg/ffprobe pair into <DSH_HOME>\video\tools when the
    machine has no usable one. Adds ~100 MB of download on first install.

.PARAMETER Force
    Overwrite an existing installation.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Bootstrap
#>
[CmdletBinding()]
param(
    [string]$Destination,
    [switch]$Bootstrap,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "  ok  $message" -ForegroundColor Green }
function Write-Warn2($message) { Write-Host "  !!  $message" -ForegroundColor Yellow }

$repoRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repoRoot 'skills\video-toolkit'
if (-not (Test-Path $source)) { throw "skill source not found: $source" }

if (-not $Destination) {
    $dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $HOME '.dsh' }
    $Destination = Join-Path $dshHome 'skills'
}
$target = Join-Path $Destination 'video-toolkit'

Write-Step "Installing video-toolkit"
Write-Host "     from: $source"
Write-Host "     to:   $target"

if (Test-Path $target) {
    if (-not $Force) {
        Write-Warn2 "already installed; re-run with -Force to overwrite"
    }
    Remove-Item $target -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $target | Out-Null

foreach ($item in @('SKILL.md', 'SKILL.zh-CN.md')) {
    Copy-Item (Join-Path $source $item) -Destination $target -Force
}
foreach ($dir in @('scripts', 'references', 'assets')) {
    $from = Join-Path $source $dir
    if (Test-Path $from) { Copy-Item $from -Destination $target -Recurse -Force }
}
Write-Ok "files copied"

$skillFile = Join-Path $target 'SKILL.md'
$head = (Get-Content -Path $skillFile -Raw -Encoding UTF8)
if ($head -notmatch '(?m)^name:\s*video-toolkit\s*$') {
    throw "SKILL.md frontmatter looks wrong (name must be video-toolkit): $skillFile"
}
if ($head -notmatch '(?m)^description:\s*\S') {
    throw "SKILL.md frontmatter is missing a description: $skillFile"
}
Write-Ok "SKILL.md frontmatter verified"

function Test-Python([string]$exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    # The Microsoft Store stub lives in WindowsApps and answers nothing useful.
    if ($exe -like '*\WindowsApps\*') { return $false }
    try {
        $out = & $exe --version 2>&1 | Out-String
        return ($out -match 'Python 3')
    } catch { return $false }
}

$dshHomeGuess = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $HOME '.dsh' }
$bundledPython = Join-Path $dshHomeGuess 'dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
$pythonCandidates = @(
    (Get-Command python -ErrorAction SilentlyContinue).Source
    (Get-Command python3 -ErrorAction SilentlyContinue).Source
    $bundledPython
)

$pythonExe = $null
foreach ($candidate in $pythonCandidates) {
    if (Test-Python $candidate) { $pythonExe = $candidate; break }
}

if (-not $pythonExe) {
    Write-Warn2 "no working python 3 found; run 'python <skill>\scripts\video_ops.py doctor' manually to check ffmpeg"
} else {
    $ops = Join-Path $target 'scripts\video_ops.py'
    Write-Step "Checking the toolchain (video_ops.py doctor)"
    $doctorArgs = @($ops, 'doctor')
    if ($Bootstrap) { $doctorArgs += @('--bootstrap', '--download') }
    $json = & $pythonExe @doctorArgs 2>&1 | Out-String
    try {
        $report = $json | ConvertFrom-Json
        if ($report.ok) {
            Write-Ok "ffmpeg : $($report.resolved.ffmpeg)"
            if ($report.resolved.ffprobe) { Write-Ok "ffprobe: $($report.resolved.ffprobe)" }
            else { Write-Warn2 "no ffprobe: metadata falls back to the built-in MP4 reader" }
        } else {
            Write-Warn2 "ffmpeg not found yet. Install it with:"
            Write-Host "      & `"$pythonExe`" `"$ops`" bootstrap --download"
        }
        if ($report.warnings) { $report.warnings | ForEach-Object { Write-Warn2 $_ } }
    } catch {
        Write-Warn2 "could not parse doctor output:"
        Write-Host $json
    }
}

Write-Step "Done"
Write-Host "Skill root : $Destination"
Write-Host "Skill name : video-toolkit"
Write-Host "Try saying: 'summarize the video at <path>' or '/video-toolkit what is in this mp4?'"
