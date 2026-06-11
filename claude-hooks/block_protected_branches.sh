#!/usr/bin/env bash
# Block direct `git commit` / `git push` on protected branches. Defaults
# to blocking `main`; the protected list can be overridden by setting
# `[tool.forge] base_branch` / `dev_branch` in pyproject.toml (used by
# forge's own repo for its release workflow — most consumers leave it
# alone).
#
# The forge:git-commit-push subagent bypasses via the `agent_type` field
# on the hook payload — keeps the canonical commit/push path working.
#
# Reads `[tool.forge]` via an inline `python3 -c` heredoc rather than
# importing `forge.config` from forge-scripts. The duplication is
# intentional: the hook fires on every `git commit` / `git push` and
# must not require forge-scripts to be installed or importable.
#
# Failure posture: advisory and default-permissive. On any failure
# (missing python3, no pyproject, malformed TOML, tomllib unavailable on
# Python 3.10), the protected list collapses to `["main"]`. Blocking
# every commit because of a parse failure would be more disruptive than
# trusting the contributor to know what branch they're on; GitHub branch
# protection remains the authoritative gate.
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
# Only inspect git commit / git push — checkout, status, log, etc. always allowed.
if ! echo "$COMMAND" | grep -qE '^git (commit|push)'; then
    exit 0
fi
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty')
if [ "$AGENT_TYPE" = "git-commit-push" ] || [ "$AGENT_TYPE" = "forge:git-commit-push" ]; then
    exit 0
fi
REPO_ROOT=$(echo "$INPUT" | jq -r '.cwd // empty')
if [ -n "$REPO_ROOT" ] && ! echo "$REPO_ROOT" | grep -qE '^/'; then
    REPO_ROOT="."
fi
branch=$(git -C "${REPO_ROOT:-.}" branch --show-current 2>/dev/null)

# Read the protected-branch list from pyproject.toml. python3 is already a
# hard dependency of every forge repo (forge IS Python), so this is safe.
# Defaults to "main" when:
#   - pyproject.toml is missing
#   - tomllib is unavailable (Python < 3.11)
#   - [tool.forge] is empty or absent
# Emits one branch per line. Portable to bash 3.2 (macOS default) — no
# mapfile / readarray.
protected=$(python3 - "${REPO_ROOT:-.}" <<'PY' 2>/dev/null || echo main
import sys
from pathlib import Path
try:
    import tomllib
except ImportError:
    print("main")
    raise SystemExit(0)
root = Path(sys.argv[1])
pp = root / "pyproject.toml"
if not pp.is_file():
    print("main")
    raise SystemExit(0)
try:
    data = tomllib.loads(pp.read_text())
except Exception:
    print("main")
    raise SystemExit(0)
section = data.get("tool", {}).get("forge", {})
base = section.get("base_branch", "main")
dev = section.get("dev_branch", "main")
print(base)
if dev != base:
    print(dev)
PY
)

# Membership check via line-anchored grep so a branch literally named
# "main" doesn't match "mainframe" (and vice versa).
if echo "$protected" | grep -qFx "$branch"; then
    echo "BLOCKED: Cannot commit/push on '$branch' (protected branch per [tool.forge] in pyproject.toml). Create a feature branch first, or if intentional, the user can run: ! $COMMAND" >&2
    exit 2
fi
exit 0
