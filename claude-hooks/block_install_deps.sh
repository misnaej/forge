#!/usr/bin/env bash
# Block dependency installation commands
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
# Allow read-only pip/conda commands
if echo "$COMMAND" | grep -qE '(pip show|pip list|pip audit|pip-audit|conda list|conda info|conda search|conda run|conda activate)'; then
    exit 0
fi
# Anchor to start-of-string or after a shell separator (;, &&, ||, |, &)
# so substrings inside quoted bodies (e.g. `gh issue create --body '... pip
# install ...'`) don't trigger. The hook fires only when the install is an
# actually-executing command.
#
# Known accepted slip-through: `xargs pip install` is allowed (a plain space
# precedes `pip`, no separator). Acceptable trade-off — narrowing further
# would re-introduce the quoted-body false-positives that prompted this
# tightening. An agent determined to bypass via xargs is misbehaving in
# obvious ways and would be caught by review.
if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)((python[0-9.]*[[:space:]]+-m[[:space:]]+)?pip[0-9.]*|conda) (install|create|env create|update)'; then
    echo "BLOCKED: Agents must not install dependencies. Tell the user the exact command to run themselves with: ! $COMMAND" >&2
    exit 2
fi
# forge-upgrade --apply runs pip install --force-reinstall internally;
# it's an explicit setup-script affordance, not for agents. Same FOUNDATION
# §2 rule.
if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)forge-upgrade(\s|$).*--apply\b'; then
    echo "BLOCKED: forge-upgrade --apply runs pip install. Agents must not. Tell the user the exact command to run themselves with: ! $COMMAND" >&2
    exit 2
fi
