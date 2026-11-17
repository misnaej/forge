#!/usr/bin/env bash
# Vibe replacement for the PreCompact state write.
#
# Vibe has no PreCompact, so the status panel of .plan/CONTINUATION.md is
# refreshed on every main-agent turn instead (post_agent, match "main").
# Idempotent and cheap; never blocks — post_agent cannot block and strict
# is forbidden on it, so any failure is a silent pass.
set -e
INPUT=$(cat)
AGENT=$(echo "$INPUT" | jq -r '.agent_name // empty')
[ "$AGENT" = "main" ] || exit 0
ROOT="${CLAUDE_PROJECT_DIR:-.}"
[ -f "$ROOT/.plan/CONTINUATION.md" ] || exit 0
command -v forge-continuation >/dev/null 2>&1 || exit 0
cd "$ROOT" && forge-continuation state >/dev/null 2>&1 || true
exit 0
