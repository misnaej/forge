#!/usr/bin/env bash
# Session-start line: print the forge plugin release this session loaded.
#
# A hook is invoked from the loaded slot itself, so dirname "$0"/.. is
# that slot's root and its plugin.json carries the version the session
# actually loaded — the one fact no installed-copy check can give
# (FOUNDATION §11: a green check is filesystem evidence, never session
# proof). Fail-open: a session must never be blocked on this line.
# Used by the Claude SessionStart hook (check_foundation_sync.sh) and by
# the Vibe session_start extra entry in vibe-extra-hooks.toml.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PJ="$ROOT/.claude-plugin/plugin.json"
[ -f "$PJ" ] || PJ="$ROOT/plugin.json"
[ -f "$PJ" ] || exit 0
VERSION="$(jq -r '.version // empty' "$PJ" 2>/dev/null || true)"
if [ -z "$VERSION" ]; then
    VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version", ""))' "$PJ" 2>/dev/null || true)"
fi
[ -n "$VERSION" ] || exit 0
echo "forge plugin loaded: $VERSION (live slot; /reload-plugins re-reads it)"
exit 0
