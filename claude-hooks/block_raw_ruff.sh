#!/usr/bin/env bash
# Block raw `ruff check` / `ruff format` invocations from Bash.
# FOUNDATION §2: agents use forge-precommit (which internally calls ruff
# via Python subprocess, not via the Bash tool — this hook doesn't see
# it). No forge agent currently needs to call raw ruff via Bash, so there
# is no bypass list.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
# Anchors and the command-positions views live in the shared lib (one home
# for every guard that locates a command).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache) must
    # block, not silently disarm the guard — only exit 2 blocks in the
    # PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"

# Anchor: a real `ruff check` / `ruff format` invocation, matched on the
# shared command-positions view (git_anchor.sh): quoted bodies (PR
# descriptions, commit messages mentioning "ruff") are blanked there and
# never fire, while `bash -c "ruff check ..."` stays visible and does.
# RUFF_ANCHOR is the shared separator + command-prefix run from
# git_anchor.sh (leading whitespace and words such as `VAR=val`, `sudo`,
# `env`, `time`, `then`, `{`, `!`), then an optional `python[N] -m`, an
# optional path to ruff (`/usr/bin/ruff`, `.venv/bin/ruff`), and a bounded
# run of ruff's global options and redirection words before the
# subcommand (`-q`, `--isolated`, `--config <file>`, `2>&1`). Redirection
# words before the command come with the shared prefix (GUARD_REDIR in
# git_anchor.sh). It has no trailing word boundary, so `ruff checks`
# is caught too. fix-forge-ruff and
# similar one-token wrappers don't match: their `ruff` follows neither a
# separator, a prefix word nor a `/`.
#
# The runner rule covers `<runner> run … ruff check` (uv, pixi, poetry,
# pipenv, conda, pipx; plus `uvx ruff check`): anything up to the next
# separator may sit between the runner and ruff — the shape
# block_install_deps.sh uses for `<mgr> run … pip install`.
#
# A help request (`ruff check --help`) runs no check and stays allowed.
#
# Accepted residuals (documented, deliberately out of scope — a tripwire
# for the routes agents take, not a sandbox):
# - `xargs ruff check` and `find … -exec ruff check`;
# - ruff run from an interpreter subprocess or a script file;
# - variable indirection and `${IFS}` splicing (`$R check`);
# - ruff verbs other than check/format (`ruff rule`, `ruff clean`, …);
# - the shared help exemption reads any `-h` token on the invocation, so a
#   wrapper flag spelled `-h` (`nice -h ruff check`) counts as a help
#   request and is allowed.
# Redirections are blanked (--no-redir), so one among a runner's
# arguments (`uvx 2>&1 ruff check`) cannot end the runner's scan.
CMDPOS=$(command_positions --no-redir "$COMMAND")
# The prefix is taken from the shared anchor, not copied: a copy drifts
# the moment the lib learns a new wrapper word, and this hook would
# silently miss it. If the lib's shape changes so the cut no longer
# applies, refuse everything rather than guard with a broken anchor.
# The cut prefix ends with the anchor's optional directory path
# (GUARD_BINPATH), so a path-qualified wrapper or runner
# (`/usr/bin/env ruff check`, `/usr/bin/uvx ruff check`) is caught too.
RUFF_PREFIX="${GIT_ANCHOR%%git\[\[:space:\]\]+*}"
case "$RUFF_PREFIX" in
    "$GIT_ANCHOR" | "" | *git*) RUFF_PREFIX_OK=0 ;;
    '(^|[;&|(])[[:space:]]*'*) RUFF_PREFIX_OK=1 ;;
    *) RUFF_PREFIX_OK=0 ;;
esac
if [ "$RUFF_PREFIX_OK" != 1 ]; then
    echo "BLOCKED: could not derive the command prefix from GIT_ANCHOR in $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
RUFF_VERB='ruff[[:space:]]+((--config)[[:space:]]+[^[:space:]]+[[:space:]]+|--?[a-zA-Z][^[:space:]]*[[:space:]]+|'"${GUARD_REDIR}"'[[:space:]]+)*(check|format)'
RUFF_ANCHOR="${RUFF_PREFIX}(([^[:space:];&|()]*/)?python[0-9.]*[[:space:]]+-m[[:space:]]+)?([^[:space:];&|()]*/)?${RUFF_VERB}"
RUFF_RUNNER="${RUFF_PREFIX}(uvx|pipx[[:space:]]+run|(uv|pixi|poetry|pipenv|conda)[[:space:]]+run)[[:space:]]([^;&|]*[[:space:]/])?${RUFF_VERB}"
# invoked <regex> — a real invocation matching <regex> that is not only a
# help request.
invoked() {
    echo "$CMDPOS" | grep -qE "$1" && ! guard_help_only "$CMDPOS" "$1"
}
if invoked "$RUFF_ANCHOR" || invoked "$RUFF_RUNNER"; then
    echo "BLOCKED: raw 'ruff' from Bash is forbidden (FOUNDATION §2). Use forge-precommit — it verifies and self-heals (ruff format + ruff check --fix --unsafe-fixes on failure). Agents delegate to the forge:precommit-fixer agent." >&2
    exit 2
fi
