#!/usr/bin/env python3
"""Validate the video-toolkit skill bundle (used by CI and by hand).

Checks the layout DSH's filesystem skill provider expects, the SKILL.md frontmatter
contract, and that every reference the playbook tells the model to read actually exists.
Exits non-zero with a readable list of problems.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skills" / "video-toolkit"
REQUIRED = [
    "SKILL.md",
    "scripts/video_ops.py",
    "scripts/video_common.py",
    "references/commands.md",
    "references/workflows.md",
    "references/troubleshooting.md",
]
CONTEXT_BUDGET = 20000


def main() -> int:
    problems: list[str] = []

    if not SKILL_DIR.is_dir():
        print(f"FAIL: skill directory missing: {SKILL_DIR}")
        return 1

    # Discovery contract: the bundle must be exactly one level below the skill root.
    if not (SKILL_DIR / "SKILL.md").is_file():
        problems.append("SKILL.md must sit at the top of the bundle (<root>/<name>/SKILL.md)")

    for relative in REQUIRED:
        if not (SKILL_DIR / relative).is_file():
            problems.append(f"missing file: skills/video-toolkit/{relative}")

    for unexpected in sorted(SKILL_DIR.glob("*/SKILL.md")):
        problems.append(f"nested bundle is not discovered by DSH: {unexpected.relative_to(ROOT)}")

    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8") if (SKILL_DIR / "SKILL.md").is_file() else ""
    front = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not front:
        problems.append("SKILL.md must start with YAML frontmatter delimited by ---")
    else:
        block = front.group(1)
        name = re.search(r"^name:\s*(\S+)\s*$", block, re.M)
        description = re.search(r"^description:\s*(.+)$", block, re.M)
        if not name or name.group(1) != "video-toolkit":
            problems.append("frontmatter name must be 'video-toolkit' (kebab-case, matches the directory)")
        if not description or len(description.group(1).strip()) < 40:
            problems.append("frontmatter description is required and must describe when to use the skill")
        if name and name.group(1) != SKILL_DIR.name:
            problems.append(f"frontmatter name '{name.group(1)}' does not match directory '{SKILL_DIR.name}'")

    if len(text) > CONTEXT_BUDGET:
        problems.append(f"SKILL.md is {len(text)} chars; keep the playbook under {CONTEXT_BUDGET} "
                        "and move detail into references/")

    # Every references/<file>.md the playbook mentions must exist.
    for mention in set(re.findall(r"references/([A-Za-z0-9._-]+\.md)", text)):
        if not (SKILL_DIR / "references" / mention).is_file():
            problems.append(f"SKILL.md references references/{mention}, which does not exist")

    for script in sorted((SKILL_DIR / "scripts").glob("*.py")):
        try:
            compile(script.read_text(encoding="utf-8"), str(script), "exec")
        except SyntaxError as exc:
            problems.append(f"python syntax error in {script.relative_to(ROOT)}: {exc}")

    for shell_script in sorted((ROOT / "scripts").glob("*")):
        if shell_script.suffix not in {".ps1", ".sh"}:
            problems.append(f"unexpected file in scripts/: {shell_script.name}")

    if problems:
        print("bundle validation FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("bundle validation ok:")
    print(f"  - {len(REQUIRED)} required files present")
    print(f"  - SKILL.md frontmatter valid, {len(text)} chars of playbook")
    print("  - scripts compile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
