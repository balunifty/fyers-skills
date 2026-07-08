#!/usr/bin/env bash
#
# validate-skill.sh — sanity-check a SKILL.md before importing it.
#
# Verifies the file exists, has YAML frontmatter, and includes the two fields
# every host requires: `name` and `description`. Warns on common issues.
#
# Usage: ./scripts/validate-skill.sh [path/to/SKILL.md]

set -euo pipefail

file="${1:-SKILL.md}"
errors=0
warnings=0

err()  { echo "ERROR: $*" >&2; errors=$((errors + 1)); }
warn() { echo "WARN:  $*" >&2; warnings=$((warnings + 1)); }

[[ -f "$file" ]] || { err "file not found: $file"; exit 1; }

# Frontmatter must be the first thing in the file, delimited by ---.
first_line="$(head -n 1 "$file")"
[[ "$first_line" == "---" ]] || err "must start with a YAML frontmatter block (---)"

# Extract the frontmatter (lines between the first two --- markers).
frontmatter="$(awk 'NR==1 && $0=="---"{f=1; next} f && $0=="---"{exit} f' "$file")"
[[ -n "$frontmatter" ]] || err "frontmatter block is empty or unterminated"

# Required fields.
grep -qE '^name:[[:space:]]*\S' <<<"$frontmatter" || err "missing required field: name"
grep -qE '^description:[[:space:]]*\S' <<<"$frontmatter" || err "missing required field: description"

# Quality nudges.
name="$(grep -E '^name:' <<<"$frontmatter" | head -n1 | sed 's/^name:[[:space:]]*//')"
if [[ -n "$name" ]] && ! grep -qE '^[a-z0-9][a-z0-9-]*$' <<<"$name"; then
  warn "name should be kebab-case (got: '$name')"
fi

desc="$(grep -E '^description:' <<<"$frontmatter" | head -n1 | sed 's/^description:[[:space:]]*//')"
if [[ "$desc" == ">"* || "$desc" == "|"* ]]; then
  # Folded/block scalar — measure the wrapped lines that follow instead.
  desc="$(awk '/^description:[[:space:]]*[>|]/{f=1;next} f&&/^[a-zA-Z_-]+:/{exit} f' <<<"$frontmatter" | tr -d '\n')"
fi
if [[ -n "$desc" && ${#desc} -lt 20 ]]; then
  warn "description is short (${#desc} chars) — make it specific and trigger-rich"
fi

if [[ $errors -gt 0 ]]; then
  echo "FAILED: $errors error(s), $warnings warning(s)" >&2
  exit 1
fi

echo "OK: $file is valid ($warnings warning(s))"
