#!/usr/bin/env bash
# PostToolUse(Bash): after a `git merge` that stopped on conflicts, name the
# command that finishes it.
#
# FOUNDATION §6 — base syncs belong to `forge-update-branch`: it resolves
# generated artifacts (regenerated, never hand-merged), the version slot
# and files the branch already holds, then commits the merge. An agent
# that typed `git merge` itself still lands here with no commit route
# (forge-commit refuses mid-merge), so this is where the instruction
# belongs. Read-only: it never resolves or stages anything.
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
# Found on the command-positions view: a quoted mention merged nothing.
command_positions "$COMMAND" | grep -qE "${GIT_ANCHOR}merge\b" || exit 0

# A merge in progress with unmerged paths is the only case worth a line.
git rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1 || exit 0
[ -n "$(git diff --name-only --diff-filter=U 2>/dev/null)" ] || exit 0
echo "[forge] this merge stopped on conflicts — run \`forge-update-branch --continue\`: it resolves generated files, the version slot and files the branch already holds, then commits the merge (forge-commit refuses mid-merge). Conflicts it cannot resolve go to forge:merge-resolver first. Base syncs normally start with \`forge-update-branch\`, not \`git merge\`."
