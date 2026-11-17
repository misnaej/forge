#!/usr/bin/env bash
# PreCompact: refresh .plan/CONTINUATION.md's status panel just before
# Claude Code summarises the conversation.
#
# FOUNDATION §10 — summarising is lossy, and the note is what the next
# context reads back. Pre-commit and `/pr` already rewrite the panel; this
# is the backstop for a session that compacts between those writes, so
# the panel the summary is followed by describes the tree as it is now.
#
# FOUNDATION §2: forge CLIs are required, not optional. A missing
# forge-continuation fails loudly (exit 1 is non-blocking for PreCompact);
# any other failure is a warning — compaction must never be blocked.
set -euo pipefail

if ! command -v forge-continuation >/dev/null 2>&1; then
    echo "[forge] forge-continuation not on PATH." >&2
    echo "[forge] Run \`pip install forge-scripts\` (or your repo's equivalent)." >&2
    exit 1
fi

# The CLI logs to stderr; keep stdout empty so nothing reads as hook output.
if ! (cd "${CLAUDE_PROJECT_DIR:-.}" && forge-continuation state --with-pr) >&2; then
    echo "[forge] forge-continuation state failed; the status panel may be stale." >&2
fi
exit 0
