---
name: my-skill
description: One sentence describing WHAT this skill does and WHEN to use it. This is the only text the agent sees when deciding whether to load the skill, so make it specific and trigger-rich. e.g. "Convert markdown files into publication-quality PDFs. Use when the user asks to make, export, or generate a PDF."
version: 0.1.0
license: MIT
# Optional: restrict which tools the skill may use. Omit to allow all.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
# Optional: extra trigger phrases (supported by some hosts, ignored by others).
triggers:
  - do the thing
  - run my skill
---

# My Skill

> Replace this file's contents with your own. Everything below is the skill
> *body* — the instructions the agent reads once the skill is invoked. Keep it
> focused: the agent already knows how to code, so tell it the things it can't
> guess (your conventions, the exact commands, the gotchas).

## When to use this skill

Describe the situations that should trigger this skill in plain language.
Mirror the phrasing a user would actually type. Example:

Use when the user asks to "do the thing", "run my skill", or otherwise wants
to <accomplish the goal>. Do **not** use when <out-of-scope case>.

## Instructions

Give the agent a clear, ordered procedure. Numbered steps work well.

1. **Gather inputs.** State what you need from the user or the workspace and
   how to find it (file globs, env vars, CLI flags).
2. **Do the work.** Reference helper scripts by path so the agent can run them
   without re-deriving the logic:
   ```bash
   ./scripts/example.sh "$INPUT"
   ```
3. **Verify.** Never report success without proof — run the test/command that
   demonstrates the result.
4. **Report.** Summarize what changed and where.

## References

Load these only when needed (keeps the main context lean):

- `references/reference.md` — detailed spec / lookup tables / API notes.

## Conventions

- Keep edits minimal and match surrounding style.
- Prefer the host's native file tools over shell `cat`/`sed`.
- Fail loudly: surface errors instead of swallowing them.
