#!/usr/bin/env bash
# Block dependency-installation commands (FOUNDATION §2: agents must not
# install dependencies — it breaks carefully configured environments).
#
# Covers pip, conda, pipenv, poetry, uv and pixi (install / sync / lock /
# update / add / remove), plus the `<mgr> run pip install ...` wrapper
# forms that slip past a start-anchored `pip` match.
#
# pixi is scoped differently from the other five, deliberately, and is the
# only one governed by an ALLOWLIST. `pixi run` / `pixi shell` /
# `pixi install` materialise a gitignored `.pixi/` inside the checkout from
# the committed lock: deterministic, disposable, and no shared environment
# to damage — not what FOUNDATION §2 protects. Those verbs, plus the
# read-only `list` / `info` / `tree`, are permitted; every other pixi verb
# blocks, including ones pixi has yet to ship. Enumerating verbs to block
# kept missing them (`lock`, `project ... add`, `exec`), so the short,
# stable permitted set is named instead and the guard fails closed.
# One rider: a bare `pixi run` DOES re-solve and rewrite the lock when the
# manifest changed underneath it, so the hook advises `--locked` (which
# still installs, but fails instead of silently rewriting) the first time
# an agent runs without it.
#
# The other four managers remain denylists and share the same structural
# gap — a verb nobody enumerated is allowed. That asymmetry is historical,
# not designed; inverting them is tracked separately.
#
# Every rule treats `(` as a command boundary alongside `;`, `&` and `|`.
# Without that, a subshell hid the command from every rule in this file:
# `(pip install x)` — and the same for conda, uv, poetry, pipenv and
# `python -m pip` — matched nothing and was allowed.
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
#
# `run` and the install verb are NOT required to be adjacent: anything up to
# the next shell separator may sit between them. Requiring adjacency let both
# `conda run -n base pip install` and `pixi run --locked pip install` through —
# the second being the very form this hook's own --locked advisory recommends.
# `python -m pip` is covered here too; the bare-pip rule below understands that
# form but anchors to a command start, which `run` has already consumed.
if echo "$COMMAND" | grep -qE '(^|[;&|(]\s*)(conda|pipenv|uv|poetry|pixi)[[:space:]]+run\b[^;&|]*\b((python[0-9.]*[[:space:]]+-m[[:space:]]+)?pip[0-9.]*[[:space:]]+install|conda[[:space:]]+(install|create|update|env[[:space:]]+(create|update)))\b' &&
    { blocked pip || blocked conda || blocked pipenv || blocked uv || blocked poetry || blocked pixi; }; then
    block
fi

# pixi is an ALLOWLIST, not a denylist — the one manager here that is.
# Enumerating pixi verbs to block kept missing them: `lock` writes the lock,
# `project ... add` writes the manifest, `exec` materialises a temp env of
# arbitrary packages, and the subcommand surface keeps growing. So the verbs
# that only materialise a committed lock are named, and everything else
# blocks — including verbs pixi has not shipped yet. A read-only verb caught
# by that default costs one `!`; a missed install defeats the guard.
#
# Judged PER SEGMENT, before the read-only fast-path below, because that
# fast-path matches anywhere in the command: `pixi list && pixi lock` must
# block on the `lock` despite the `list`.
PIXI_ALLOWED_VERBS='run|shell|install|list|info|tree'

if blocked pixi; then
    while IFS= read -r seg; do
        echo "$seg" | grep -qE '^[[:space:]]*pixi([[:space:]]|$)' || continue
        # The verb is the first token after `pixi` that is not a flag, so
        # `pixi -q list` reads as `list`. A flag that takes a value
        # (`pixi --manifest-path x run …`) leaves that value in the verb
        # slot and therefore blocks: conservative by design, and the `!`
        # escape covers it.
        rest="${seg#*pixi}"
        verb=$(printf '%s' "$rest" | tr -s '[:space:]' '\n' | grep -vE '^(-.*)?$' | head -1)
        # An empty verb is `pixi` alone or `pixi --version`-style: the flag
        # filter above strips dash-prefixed tokens, so those arrive here as
        # "". `help` is the one exempt word that survives extraction.
        case "$verb" in
        "" | help) continue ;;
        esac
        if ! printf '%s' "$verb" | grep -qE "^($PIXI_ALLOWED_VERBS)$"; then
            echo "BLOCKED: \`pixi $verb\` is not on the agent allowlist ($(printf '%s' "$PIXI_ALLOWED_VERBS" | tr '|' ' ')). Unrecognised pixi verbs block by default — most of pixi's surface writes pixi.toml, pixi.lock, or the global tool store. If this one only reads, run it yourself with: ! $COMMAND" >&2
            exit 2
        fi
    done < <(printf '%s\n' "$COMMAND" | tr ';&|()' '\n')
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
    if echo "$COMMAND" | grep -qE '(^|[;&|(]\s*)((python[0-9.]*[[:space:]]+-m[[:space:]]+)?pip[0-9.]*|conda) (install|create|env (create|update)|update)'; then
        block
    fi
fi
if blocked pipenv && echo "$COMMAND" | grep -qE '(^|[;&|(]\s*)pipenv[[:space:]]+(install|sync|lock|update|uninstall)\b'; then
    block
fi
if blocked poetry && echo "$COMMAND" | grep -qE '(^|[;&|(]\s*)poetry[[:space:]]+(add|install|update|lock|remove)\b'; then
    block
fi
if blocked uv && echo "$COMMAND" | grep -qE '(^|[;&|(]\s*)uv[[:space:]]+(add|sync|lock|remove|(pip|tool|python)[[:space:]]+install)\b'; then
    block
fi

# `pixi run` without --locked / --frozen re-solves and rewrites pixi.lock
# when the manifest moved — the one mutation the allow-list above cannot
# rule out statically. Advisory, not a block: the command is legitimate
# (it is how tests run), and an agent that adopts the flag never sees this
# line again.
if blocked pixi && echo "$COMMAND" | grep -qE '(^|[;&|(]\s*)pixi[[:space:]]+(run|shell)\b' &&
    ! echo "$COMMAND" | grep -qE '\-\-(locked|frozen)\b'; then
    echo "NOTE: \`pixi run\` / \`pixi shell\` rewrite pixi.lock when the manifest changed. Pass \`--locked\` — it still installs from the lock, but fails instead of re-solving (FOUNDATION §2)."
fi

# forge-upgrade --apply runs pip install --force-reinstall internally;
# it's an explicit setup-script affordance, not for agents.
if echo "$COMMAND" | grep -qE '(^|[;&|(]\s*)forge-upgrade(\s|$).*--apply\b'; then
    echo "BLOCKED: forge-upgrade --apply runs pip install. Agents must not. Tell the user the exact command to run themselves with: ! $COMMAND" >&2
    exit 2
fi
