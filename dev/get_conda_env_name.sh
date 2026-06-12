#!/usr/bin/env bash
# Resolve the conda env name for this forge working copy.
#
# Lets you run several forge clones in parallel (dev-0, dev-1, dev-2, ...),
# each bound to its own conda env, by dropping a `.conda_env_name` file at
# the clone root. Without that file the default is used, so a single-clone
# setup needs no configuration.
#
# Precedence (first hit wins):
#   1. $CONDA_ENV_NAME              — explicit env-var override
#   2. .conda_env_name (repo root) — first non-comment, non-blank line
#   3. the default passed by the caller (forge's setup.sh passes "forge")
#
# Usage:
#   source dev/get_conda_env_name.sh          # sets $ENV_NAME (default "forge")
#   name="$(resolve_conda_env_name myproj)"   # or call directly with a default
#
# Conda env names must use letters, digits, and underscores only — NEVER
# dashes: conda reads a leading dash as a flag, and dashed names break
# activation. The resolver warns on stderr when the resolved name contains
# a dash (it does not rewrite it — that is the caller's call).
#
# Kept dependency-free (pure bash + sed/grep) on purpose: env-name
# resolution must run BEFORE the conda env exists, i.e. before forge-scripts
# is installed, so it cannot rely on any forge CLI.

# Resolve and echo the env name. Arg 1 = default when nothing else matches.
resolve_conda_env_name() {
    local default_name="${1:-forge}"
    local repo_root name=""

    if [ -n "${CONDA_ENV_NAME:-}" ]; then
        name="$CONDA_ENV_NAME"
    else
        # Anchor on the git repo root so the lookup works from any subdir
        # and whether the script is sourced or executed. A forge working
        # copy is always a git tree; if git can't resolve a root, there is
        # no repo to hold .conda_env_name, so skip the lookup and use the
        # default.
        repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"
        if [ -n "$repo_root" ] && [ -f "$repo_root/.conda_env_name" ]; then
            # Strip inline comments + surrounding whitespace, take the first
            # remaining non-blank line.
            name="$(sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
                "$repo_root/.conda_env_name" | grep -m1 -vE '^$' | tr -d '\r')"
        fi
        name="${name:-$default_name}"
    fi

    case "$name" in
    *-*)
        printf '[forge] warning: conda env name "%s" contains a dash; conda may misread it as a flag — use underscores.\n' \
            "$name" >&2
        ;;
    esac

    printf '%s\n' "$name"
}

# When sourced, expose $ENV_NAME for the caller. Default "forge"; override
# the default via $CONDA_ENV_NAME_DEFAULT before sourcing.
ENV_NAME="$(resolve_conda_env_name "${CONDA_ENV_NAME_DEFAULT:-forge}")"
