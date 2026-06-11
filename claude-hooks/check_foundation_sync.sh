#!/usr/bin/env bash
# Warn at SessionStart if the repo's CLAUDE.md has drifted from the
# installed forge-scripts FOUNDATION.md.
#
# FOUNDATION §2: forge CLIs are required, not optional. If
# install-forge-claude-md is missing, fail loudly so the contributor
# knows to install forge-scripts.
set -euo pipefail

if ! command -v install-forge-claude-md >/dev/null 2>&1; then
    echo "[forge] install-forge-claude-md not on PATH." >&2
    echo "[forge] Run \`pip install -e \".[dev]\"\` (or your repo's equivalent)." >&2
    exit 1
fi

# Note: stderr is NOT redirected — install-forge-claude-md emits the
# channel-aware upstream-version warning (forge-scripts and Claude
# plugin staleness) at WARNING level, and SessionStart is the prime
# moment for the consumer to see it. Suppressing stderr here would
# silently eat that signal.
if ! install-forge-claude-md --check --quiet; then
    echo "[forge] CLAUDE.md is out of sync with installed FOUNDATION.md." >&2
    echo "[forge] Run \`install-forge-claude-md\` to update the managed block." >&2
fi
# SessionStart is warn-only — never block a Claude session on drift.
# The git hooks (post-merge / post-checkout) hard-fail on missing CLI
# because a missed install is a workspace setup issue; here it's just
# information the user should act on at their convenience.
exit 0
