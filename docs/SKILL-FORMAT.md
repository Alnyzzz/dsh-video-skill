# How a DSH skill bundle is structured

This file documents the conventions the `video-toolkit` bundle follows, so the repository
can be copied or forked without guessing. It reflects DSH's filesystem skill provider.

## Where skills are loaded from

DSH's `dsh-skill-filesystem` provider scans a ranked list of roots and watches them, so
new, renamed or deleted skills appear in the session catalog without a restart:

| Rank | Source | Path |
| --- | --- | --- |
| 100 | `project-dsh` | `<projectRoot>/.dsh/skills` |
| 200 | `project-agents` | `<projectRoot>/.agents/skills` |
| 300 | `custom` | the plugin's `customSkillDirs` |
| 400 | `user-dsh` | `<DSH_HOME>/skills` (`DSH_HOME` defaults to `~/.dsh`) |
| 500 | `user-agents` | `<AGENTS_HOME>/skills` (`~/.agents`) |
| 600 | `bundled` | `$DSH_BUNDLED_SKILL_DIR` when configured |

The project root is the nearest ancestor containing `.git`; without one, the current working
directory is used. `.system` under the user DSH root is skipped.

## Discovery is one level deep

A skill is exactly one of:

- a **directory bundle**: `<root>/<name>/SKILL.md`
- a **flat file**: `<root>/<name>.md`

Nested trees (`<root>/<name>/sub/SKILL.md`) are deliberately **not** discovered. Files under
`references/`, `scripts/` and `assets/` are resources of the bundle, not skills; changing them
does not invalidate the catalog, so they can be edited freely while a session runs.

## SKILL.md frontmatter

The file starts with YAML frontmatter:

```yaml
---
name: video-toolkit            # required, kebab-case, must match the directory name
description: ...               # required, one line, describes when to use it
whenToUse: ...                 # optional extra routing hint
metadata: {}                   # optional
disable-model-invocation: false  # optional
user-invocable: true             # optional
---
```

Rules that matter in practice:

- `name` must be kebab-case and is what users type (`/video-toolkit`).
- `description` is what the model sees when choosing a skill, so it must name the file types
  and the tasks, not the implementation.
- Everything after the frontmatter is the playbook. It is rendered into the model's context
  when the skill is loaded, so keep it under roughly 8 KB and push detail into `references/`
  files that the playbook tells the model to read on demand.

## Bundle layout used here

```
video-toolkit/
├── SKILL.md            # playbook (loaded into context)
├── SKILL.zh-CN.md      # Chinese playbook, same content, human-facing
├── references/         # read on demand; not loaded automatically
│   ├── commands.md
│   ├── workflows.md
│   └── troubleshooting.md
└── scripts/            # executables the playbook tells the agent to run
    ├── video_ops.py
    └── video_common.py
```

## Script conventions that keep a skill portable

1. **Never hard-code the Python path.** The playbook shows both `python` and the DSH-bundled
   interpreter, because `python` may be absent from PATH on Windows.
2. **Resolve sibling scripts from `__file__`**, so the bundle works from any root:
   `sys.path.insert(0, str(Path(__file__).resolve().parent))`.
3. **Print one JSON object per run** and exit `0` on success. Structured output survives
   quoting, code pages and truncation far better than prose, and any non-zero exit should be
   explained *inside* the JSON.
4. **Accept that tools may be missing.** Detect, install into a private directory, or degrade
   with an explicit note — never crash with a traceback the model has to interpret.
5. **Quote paths in the playbook's examples.** Real user paths contain spaces and CJK text.
6. **Keep the network opt-in.** A skill that silently downloads on first use is a surprise;
   `bootstrap --download` is explicit, and every other command works offline.
