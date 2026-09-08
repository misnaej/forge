#!/usr/bin/env bash
# Block dependency-installation commands (FOUNDATION §2: agents must not
# install dependencies — it breaks carefully configured environments).
#
# Covers pip, conda, pipenv, poetry, uv and pixi (install / sync / lock /
# update / add / remove), plus the `<mgr> run pip install ...` wrapper
# forms that slip past a start-anchored `pip` match.
#
# pixi is scoped differently from the other five, deliberately. `pixi run`
# / `pixi shell` / `pixi install` materialise a gitignored `.pixi/` inside
# the checkout from the committed lock: deterministic, disposable, and no
# shared environment to damage — not what FOUNDATION §2 protects. What is
# blocked is mutation of the manifest or the lock (`add`, `remove`,
# `update`, `upgrade`, `global install`). One rider: a bare `pixi run`
# DOES re-solve and rewrite the lock when the manifest changed underneath
# it, so the hook advises `--locked` (which still installs, but fails
# instead of silently rewriting) the first time an agent runs without it.
#
# Opt-out via [tool.forge.hooks] in the repo's pyproject.toml:
#   block_install_deps = false                 # allow every manager
#   block_install_deps = ["pip", "conda"]      # block only these
#                                              # (names: pip, conda,
#                                              #  pipenv, poetry, uv, pixi)
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

# Wrapper install forms (`<mgr> run pip install …`, `<mgr> run conda install …`)
# are checked FIRST so a read-only `<mgr> run …` allowlist entry below can't
# shadow them. The bare `pip`/`conda` rule below anchors the install verb to a
# command start or shell separator, so the inner manager of a `… run conda
# install` (preceded only by whitespace after `run`) would otherwise slip
# through — this catches it explicitly.
if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)(conda|pipenv|uv|poetry|pixi)[[:space:]]+run[[:space:]]+(pip[[:space:]]+install|conda[[:space:]]+(install|create|update|env[[:space:]]+(create|update)))\b' &&
    { blocked pip || blocked conda || blocked pipenv || blocked uv || blocked poetry || blocked pixi; }; then
    block
fi

# pixi's manifest/lock mutations are checked BEFORE the read-only fast-path
# below: its allowed verbs (`run`, `install`, `shell`) share a prefix with
# its blocked ones, and that fast-path matches anywhere in the command, so
# `pixi list && pixi add numpy` would otherwise slip through. `pixi global
# install` mutates the user's global tool env, unlike a project install.
if blocked pixi && echo "$COMMAND" | grep -qE '(^|[;&|]\s*)pixi[[:space:]]+(add|remove|update|upgrade|global[[:space:]]+(install|remove|update|upgrade))\b'; then
    block
fi

# Read-only commands for every manager stay allowed (coarse fast-path). Note
# `conda run` is intentionally absent — `conda run <non-install>` falls
# through harmlessly (nothing below blocks it), while `conda run pip install`
# is already handled above.
if echo "$COMMAND" | grep -qE '(pip show|pip list|pip audit|pip-audit|conda (list|info|search|activate)|pipenv (--version|graph)|poetry (show|--version)|uv (pip list|--version)|pixi (list|info|tree))'; then
    exit 0
fi

# pip / conda — anchored to command start or after a shell separator so a
# substring inside a quoted body (e.g. an issue body mentioning `pip
# install`) doesn't trigger. (Accepted slip-through: `xargs pip install`.)
if blocked pip || blocked conda; then
    if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)((python[0-9.]*[[:space:]]+-m[[:space:]]+)?pip[0-9.]*|conda) (install|create|env (create|update)|update)'; then
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

# `pixi run` without --locked / --frozen re-solves and rewrites pixi.lock
# when the manifest moved — the one mutation the allow-list above cannot
# rule out statically. Advisory, not a block: the command is legitimate
# (it is how tests run), and an agent that adopts the flag never sees this
# line again.
if blocked pixi && echo "$COMMAND" | grep -qE '(^|[;&|]\s*)pixi[[:space:]]+(run|shell)\b' &&
    ! echo "$COMMAND" | grep -qE '\-\-(locked|frozen)\b'; then
    echo "NOTE: \`pixi run\` / \`pixi shell\` rewrite pixi.lock when the manifest changed. Pass \`--locked\` — it still installs from the lock, but fails instead of re-solving (FOUNDATION §2)."
fi

# forge-upgrade --apply runs pip install --force-reinstall internally;
# it's an explicit setup-script affordance, not for agents.
if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)forge-upgrade(\s|$).*--apply\b'; then
    echo "BLOCKED: forge-upgrade --apply runs pip install. Agents must not. Tell the user the exact command to run themselves with: ! $COMMAND" >&2
    exit 2
fi
