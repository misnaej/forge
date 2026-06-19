#!/usr/bin/env bash
# Block dependency-installation commands (FOUNDATION §2: agents must not
# install dependencies — it breaks carefully configured environments).
#
# Covers pip, conda, pipenv, poetry, and uv (install / sync / lock /
# update / add / remove), plus the `<mgr> run pip install ...` wrapper
# forms that slip past a start-anchored `pip` match.
#
# Opt-out via [tool.forge.hooks] in the repo's pyproject.toml:
#   block_install_deps = false                 # allow every manager
#   block_install_deps = ["pip", "conda"]      # block only these
#   (unset / true)                             # block all (safe default)
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Resolve the opt-out config. Prints ALL, NONE, or a comma-list of
# managers. Defaults to ALL when pyproject / python / the key is absent —
# the safe baseline matches FOUNDATION §2.
BLOCKED=$(
    python3 - "${CLAUDE_PROJECT_DIR:-.}/pyproject.toml" 2>/dev/null <<'PY' || echo ALL
import sys, tomllib

try:
    with open(sys.argv[1], "rb") as fh:
        val = (
            tomllib.load(fh)
            .get("tool", {})
            .get("forge", {})
            .get("hooks", {})
            .get("block_install_deps", True)
        )
except (OSError, tomllib.TOMLDecodeError):
    val = True
if val is False:
    print("NONE")
elif isinstance(val, list):
    print(",".join(str(v).lower() for v in val))
else:
    print("ALL")
PY
)
[ "$BLOCKED" = "NONE" ] && exit 0

# blocked <manager> — true when this manager is in the blocked set.
blocked() {
    [ "$BLOCKED" = "ALL" ] || printf ',%s,' "$BLOCKED" | grep -q ",$1,"
}

block() {
    echo "BLOCKED: Agents must not install dependencies. Tell the user the exact command to run themselves with: ! $COMMAND" >&2
    exit 2
}

# Wrapper install forms (`<mgr> run pip install ...`) are checked FIRST so a
# read-only `<mgr> run …` allowlist entry below can't shadow them — and so
# `conda run pip install` is caught (its bare `conda` rule only matches
# `conda install|create|update`).
if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)(conda|pipenv|uv|poetry)[[:space:]]+run[[:space:]]+pip[[:space:]]+install\b' &&
    { blocked pip || blocked conda || blocked pipenv || blocked uv || blocked poetry; }; then
    block
fi

# Read-only commands for every manager stay allowed (coarse fast-path). Note
# `conda run` is intentionally absent — `conda run <non-install>` falls
# through harmlessly (nothing below blocks it), while `conda run pip install`
# is already handled above.
if echo "$COMMAND" | grep -qE '(pip show|pip list|pip audit|pip-audit|conda (list|info|search|activate)|pipenv (--version|graph)|poetry (show|--version)|uv (pip list|--version))'; then
    exit 0
fi

# pip / conda — anchored to command start or after a shell separator so a
# substring inside a quoted body (e.g. an issue body mentioning `pip
# install`) doesn't trigger. (Accepted slip-through: `xargs pip install`.)
if blocked pip || blocked conda; then
    if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)((python[0-9.]*[[:space:]]+-m[[:space:]]+)?pip[0-9.]*|conda) (install|create|env create|update)'; then
        block
    fi
fi
if blocked pipenv && echo "$COMMAND" | grep -qE '(^|[;&|]\s*)pipenv[[:space:]]+(install|sync|lock|update|uninstall)\b'; then
    block
fi
if blocked poetry && echo "$COMMAND" | grep -qE '(^|[;&|]\s*)poetry[[:space:]]+(add|install|update|lock|remove)\b'; then
    block
fi
if blocked uv && echo "$COMMAND" | grep -qE '(^|[;&|]\s*)uv[[:space:]]+(add|sync|lock|remove|pip[[:space:]]+install)\b'; then
    block
fi
# Wrapper forms: `<mgr> run pip install ...` — a space precedes `pip`, so
# the start-anchored pip rule above misses it. Block when any involved
# manager is blocked.
if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)(pipenv|uv|poetry)[[:space:]]+run[[:space:]]+pip[[:space:]]+install\b' &&
    { blocked pip || blocked pipenv || blocked uv || blocked poetry; }; then
    block
fi

# forge-upgrade --apply runs pip install --force-reinstall internally;
# it's an explicit setup-script affordance, not for agents.
if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)forge-upgrade(\s|$).*--apply\b'; then
    echo "BLOCKED: forge-upgrade --apply runs pip install. Agents must not. Tell the user the exact command to run themselves with: ! $COMMAND" >&2
    exit 2
fi
