#!/usr/bin/env bash
# PostToolUse(Bash): after a `git push`, say when the PR's posted wrap-up
# no longer describes the branch head.
#
# FOUNDATION §6 — `block_unverified_pr_create` proves the wrap-up at
# publication; every later push can silently outdate it. The push is the
# cause, and it happens in this session, so this is where the reminder
# belongs: it reaches the agent that just pushed, not a background
# monitor that may not be running (the §6 monitor's fifth signal covers
# pushes made elsewhere). A Claude Code hook rather than a git pre-push
# hook because the audience is the agent session — git hooks fire for
# any human terminal too, where "re-run /pr" has no reader.
#
# Read-only: prints one line, never posts the refresh (that is `/pr N`).
# Deliberately no `set -e`: every probe here may legitimately fail (no
# PR, no CLI, gh offline) and none are errors — a post-tool hook must
# never turn a working push into a failed one.
set -uo pipefail

INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -n "$COMMAND" ] || exit 0

ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
[ -f "$ANCHOR_LIB" ] || exit 0
# shellcheck source=git_anchor.sh
source "$ANCHOR_LIB"
printf '%s' "$COMMAND" | grep -qE "${GIT_ANCHOR}push\b" || exit 0

command -v forge-pr-plan >/dev/null 2>&1 || exit 0

PR=$(gh pr view --json number --jq .number 2>/dev/null)
[ -n "$PR" ] || exit 0

# A wrap-up already authored for this HEAD is a refresh in flight
# (`/pr` pushes its fix commits before posting) — no reminder needed.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo ".")
HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || true)
# shellcheck source=wrapup_anchor.sh
source "$(dirname "$0")/wrapup_anchor.sh"
if [ -n "$HEAD_SHA" ] && wrapup_names_head "$REPO_ROOT/code_health/pr_wrapup.md" "$HEAD_SHA"; then
    exit 0
fi

VERDICT=$(forge-pr-plan --freshness --pr "$PR" 2>/dev/null) || exit 0
# `tostring`, not `// "null"`: jq's `//` treats false as falsy, which
# would fold the one verdict this hook exists for into "cannot tell".
FRESH=$(printf '%s' "$VERDICT" | jq -r '.fresh | tostring' 2>/dev/null)
# true = current; null = cannot tell (no gh, no wrap-up yet) — never alert.
[ "$FRESH" = "false" ] || exit 0

AT=$(printf '%s' "$VERDICT" | jq -r '.latest_verified_at // "?"')
HEAD7=$(printf '%s' "$VERDICT" | jq -r '.head_oid // "?"' | cut -c1-7)
echo "[forge] wrap-up on PR #$PR was verified at $AT but the head is now $HEAD7 — re-run /pr $PR to post a refreshed wrap-up (FOUNDATION §6)."
