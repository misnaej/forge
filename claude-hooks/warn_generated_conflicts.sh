#!/usr/bin/env bash
# PostToolUse(Bash): after a `git merge` that left only forge-generated
# artifacts conflicted, say so and name the mechanical fix.
#
# FOUNDATION §6 — a generated file's correct post-merge content is
# `regenerate()`, never a textual merge of two sides; "resolve with care"
# is the right rule for source and exactly the wrong one here. The merge
# happens in this session, so this is where the instruction belongs. The
# verdict itself is `forge-resync --resolve-conflicts --dry-run` (one
# source of truth for which paths are generated); this hook only relays
# it. Read-only: it never regenerates or stages — the agent runs the
# real command.
#
# Deliberately no `set -e`: a merge that failed on conflicts is the very
# case this hook exists for, and every probe may legitimately fail.
set -uo pipefail

INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -n "$COMMAND" ] || exit 0

ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
[ -f "$ANCHOR_LIB" ] || exit 0
# shellcheck source=git_anchor.sh
source "$ANCHOR_LIB"
printf '%s' "$COMMAND" | grep -qE "${GIT_ANCHOR}merge\b" || exit 0

command -v forge-resync >/dev/null 2>&1 || exit 0

# Exit 0 from the probe means: merge in progress, something conflicted,
# and every conflicted path is a known generated artifact.
forge-resync --resolve-conflicts --dry-run >/dev/null 2>&1 || exit 0
echo "[forge] only forge-generated artifacts conflict in this merge — run \`forge-resync --resolve-conflicts\` (regenerates from the merged tree, verifies, stages), then commit the merge. Never hand-merge a generated file (FOUNDATION §6)."
