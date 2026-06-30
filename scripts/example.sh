#!/usr/bin/env bash
#
# example.sh — a sample helper script for the skill.
#
# Skills keep deterministic logic in scripts/ so the agent can run a known-good
# command instead of re-deriving it each time. Replace this with whatever your
# skill actually does.
#
# Usage: ./scripts/example.sh <input>

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <input>" >&2
  exit 64
fi

input="$1"

echo "Running my-skill on: ${input}"
# ... do the real work here ...
echo "Done."
