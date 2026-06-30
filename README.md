# Skill Template

A portable **Agent Skill** template that imports cleanly into **Claude Code**,
**Cursor**, and **Open Claw**. A skill is a folder of instructions (and
optional scripts/references) that an AI coding agent loads on demand to perform
a specialized task.

## What's in here

```
.
├── SKILL.md            # The skill itself: frontmatter + instructions (start here)
├── README.md           # This file
├── LICENSE             # MIT
├── scripts/
│   └── example.sh      # Sample helper script the skill can call
├── references/
│   └── reference.md    # Sample reference doc loaded on demand
└── assets/
    └── .gitkeep        # Drop templates, images, fixtures here
```

The contract is `SKILL.md`. It has two parts:

1. **YAML frontmatter** — metadata the host reads to decide *whether* to load
   the skill. The two fields every host respects are `name` and `description`.
   Make `description` specific and trigger-rich; it's all the agent sees when
   choosing the skill.
2. **Body (Markdown)** — the instructions the agent reads *after* the skill is
   invoked. Reference scripts and docs by relative path so they're pulled in
   only when needed.

## Quick start

1. Copy this folder and rename it to your skill (kebab-case, e.g. `make-pdf`).
2. Edit `SKILL.md`: set `name`, write a sharp `description`, replace the body.
3. Put runnable logic in `scripts/`, long lookup material in `references/`.
4. Keep the body short — link out to references instead of inlining everything.

## Importing the skill

### Claude Code

Skills live in a `skills/` directory under a `.claude` folder. The skill's
folder name must match its `name:` field.

```bash
# Per-project (checked into the repo):
mkdir -p .claude/skills
cp -r my-skill .claude/skills/my-skill

# Or per-user (available in every project):
mkdir -p ~/.claude/skills
cp -r my-skill ~/.claude/skills/my-skill
```

Invoke it with `/my-skill`, or just describe the task and let Claude pick it up
from the `description`.

### Cursor

Place the skill folder under `.cursor/skills/` in your project:

```bash
mkdir -p .cursor/skills
cp -r my-skill .cursor/skills/my-skill
```

Cursor reads the same `SKILL.md` frontmatter. Reload the window after copying.

### Open Claw

Open Claw discovers skills from its skills directory:

```bash
mkdir -p ~/.openclaw/skills
cp -r my-skill ~/.openclaw/skills/my-skill
```

> Paths vary by host version. If a host can't find the skill, check its docs
> for the active skills directory and confirm the folder name matches `name:`.

## Authoring tips

- **One job per skill.** Narrow skills are easier for the agent to choose.
- **Description does the routing.** Write it from the user's point of view —
  include the words they'd actually type.
- **Scripts over prose** for deterministic work. Markdown for judgment.
- **Validate before committing:** `./scripts/validate-skill.sh SKILL.md`
- **Version it.** Bump `version:` in frontmatter on meaningful changes.

## License

MIT — see [LICENSE](LICENSE).
