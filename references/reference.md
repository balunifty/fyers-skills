# Reference

This file is a stand-in for detailed material the skill loads **on demand** —
keep it out of `SKILL.md` so the agent only pays the context cost when it
actually needs the detail.

Good candidates for a reference file:

- API endpoint tables, schemas, or response shapes
- Long enumerations (error codes, supported flags, config keys)
- Worked examples and edge-case walkthroughs
- Domain background the agent won't have

In `SKILL.md`, point to it like:

> See `references/reference.md` for the full field list.

The agent reads it only when the body tells it to.
