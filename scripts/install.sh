#!/usr/bin/env bash
# Install the video-toolkit skill into DeepSeek Harness (macOS / Linux).
#
#   bash scripts/install.sh [--destination DIR] [--bootstrap] [--force]
#
# Copies skills/video-toolkit into <DSH_HOME>/skills/video-toolkit, verifies the
# bundle, and reports which ffmpeg the skill can use.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$repo_root/skills/video-toolkit"

destination=""
bootstrap=0
force=0
while [ $# -gt 0 ]; do
  case "$1" in
    --destination) destination="${2:-}"; shift 2 ;;
    --bootstrap) bootstrap=1; shift ;;
    --force) force=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ ! -d "$source_dir" ]; then
  echo "skill source not found: $source_dir" >&2
  exit 1
fi

if [ -z "$destination" ]; then
  dsh_home="${DSH_HOME:-$HOME/.dsh}"
  destination="$dsh_home/skills"
fi
target="$destination/video-toolkit"

echo "==> Installing video-toolkit"
echo "     from: $source_dir"
echo "     to:   $target"

if [ -e "$target" ]; then
  if [ "$force" -eq 0 ]; then
    echo "  !!  already installed; re-run with --force to overwrite"
  fi
  rm -rf "$target"
fi

mkdir -p "$target"
cp "$source_dir/SKILL.md" "$source_dir/SKILL.zh-CN.md" "$target/"
for dir in scripts references assets; do
  [ -d "$source_dir/$dir" ] && cp -R "$source_dir/$dir" "$target/"
done
echo "  ok  files copied"

if ! head -n 12 "$target/SKILL.md" | grep -q '^name: video-toolkit'; then
  echo "SKILL.md frontmatter looks wrong: $target/SKILL.md" >&2
  exit 1
fi
echo "  ok  SKILL.md frontmatter verified"

python_bin="$(command -v python3 || command -v python || true)"
if [ -z "$python_bin" ]; then
  echo "  !!  no python found; run 'python <skill>/scripts/video_ops.py doctor' manually"
else
  echo "==> Checking the toolchain (video_ops.py doctor)"
  args=(doctor)
  [ "$bootstrap" -eq 1 ] && args=(doctor --bootstrap --download)
  if ! "$python_bin" "$target/scripts/video_ops.py" "${args[@]}"; then
    echo "  !!  toolchain check reported a problem (see JSON above)"
  fi
fi

echo "==> Done"
echo "Skill root : $destination"
echo "Skill name : video-toolkit"
echo "Try saying: 'summarize the video at <path>'"
