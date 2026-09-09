#!/usr/bin/env bash
# Restrict the precommit-fixer agent's Bash to its contract allowlist.
#
# The fixer's contract says the code_health/ logs are its only evidence:
# it runs `forge-precommit` (or one step CLI to refresh a stale log) and
# dispatches from the reports — reconnaissance (git status/diff, tree
# searches, checksums) is never its job, and unenforced prose did not
# stop it. This hook is the inverse of the sibling blockers: instead of
# default-allow with a blocklist, it fires ONLY for the precommit-fixer
# agent and default-DENIES anything off the allowlist. Other agents are
# entirely unaffected.
#
# Also the home of the fixer's three-full-run cap (see the end of the
# file): the fourth bare `forge-precommit` is refused, `--only` refreshes
# are free.
#
# Allowlist: forge-precommit, the six step CLIs, `cd` (navigation), and
# targeted test runs — pytest / python -m pytest with explicit `::`
# node-id selector(s), one or several (the tests being fixed or just
# written). Untargeted pytest (bare, file, directory) stays blocked.
# Every segment of a compound command must pass. Substitution coverage:
# `$(...)` splits at the paren separator; backticks are rejected
# outright (no allowlisted invocation needs one). A jq/parse failure
# fails OPEN by design — the agent cannot be identified then, and
# failing closed would block every agent's Bash (see the hook-family
# hardening issue for the accepted trade-off). Known accepted
# slip-through: redirections on an allowlisted CLI (`< file`, `> file`)
# are not inspected — no step CLI echoes arbitrary stdin back, so the
# recon value is nil; revisit if that changes.
set -e
INPUT=$(cat)
# The command keeps its own jq pass: the segment splitter below relies on
# its real newlines. The identifiers share one pass, one value per line —
# not @tsv, because bash treats tab as IFS whitespace and collapses the
# empty fields, silently shifting a missing agent_id onto the next value.
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
{
    IFS= read -r AGENT_TYPE
    IFS= read -r AGENT_ID
    IFS= read -r SESSION_ID
    IFS= read -r CWD
} <<< "$(jq -r '.agent_type // "", .agent_id // "", .session_id // "", .cwd // "."' <<< "$INPUT")"

# Scoped: only the precommit-fixer is restricted (both name forms, per
# the block_raw_git precedent).
if [ "$AGENT_TYPE" != "precommit-fixer" ] && [ "$AGENT_TYPE" != "forge:precommit-fixer" ]; then
    exit 0
fi

# Backticks smuggle a second command past the segment splitter (bash
# executes the substitution regardless of where it sits in the string);
# no allowlisted invocation needs one, so any backtick blocks outright.
case "$COMMAND" in
    *\`*)
        echo "BLOCKED: precommit-fixer's Bash is limited to forge-precommit, the step CLIs, and targeted pytest node-ids — backtick substitution is never part of that set. Read the code_health/ logs instead." >&2
        exit 2
        ;;
esac

_block() {
    echo "BLOCKED: precommit-fixer's Bash is limited to forge-precommit, the step CLIs, and targeted pytest node-ids — the code_health/ logs are the only evidence (agents/precommit-fixer.md, FOUNDATION §3). '$1' is outside that set; do not run reconnaissance, read the logs." >&2
    exit 2
}

# Trim leading whitespace, then strip VAR=val assignments and
# command/env/exec/builtin/sudo/flag wrappers (the same prefix set the
# sibling hooks anchor past), leaving the real command in STRIPPED.
# Returns non-zero when nothing remains — each caller decides what an
# empty segment means.
_strip_wrapper_prefix() {
    STRIPPED="${1#"${1%%[![:space:]]*}"}"
    while [ -n "$STRIPPED" ]; do
        tok="${STRIPPED%%[[:space:]]*}"
        case "$tok" in
            *=*|command|env|exec|builtin|sudo|-*)
                rest="${STRIPPED#"$tok"}"
                STRIPPED="${rest#"${rest%%[![:space:]]*}"}"
                ;;
            *) break ;;
        esac
    done
    [ -n "$STRIPPED" ]
}

# A full `forge-precommit` run: the bare CLI, not a `--only <step>`
# refresh. Wrapper prefixes are stripped first, so `FORGE_X=1
# forge-precommit` counts too.
_is_full_precommit() {
    _strip_wrapper_prefix "$1" || return 1
    [ "${STRIPPED%%[[:space:]]*}" = "forge-precommit" ] || return 1
    # `--only` as its own word (or `--only=<steps>`), never a substring of
    # some future flag or step name.
    case " $STRIPPED " in *" --only "*|*" --only="*) return 1 ;; esac
    return 0
}

_segment_ok() {
    # An empty segment (a bare separator) is nothing to police.
    _strip_wrapper_prefix "$1" || return 0
    seg="$STRIPPED"
    tok="${seg%%[[:space:]]*}"
    case "$tok" in
        cd|forge-precommit|fix-forge-ruff|verify-forge-docstrings|verify-forge-repo-structure|verify-forge-test-naming|verify-forge-manifest|verify-forge-plugin-version)
            return 0 ;;
        pytest)
            case "$seg" in *::*) return 0 ;; esac
            return 1 ;;
        python|python3)
            case "$seg" in *-m\ pytest*::*) return 0 ;; esac
            return 1 ;;
    esac
    return 1
}

# Normalize every command separator (; & | and subshell-opening
# parens) to newlines, then require EVERY segment to pass — a pipe into
# a non-allowlisted tool, or a chained recon command, blocks the whole
# invocation (conservative by intent).
FULL_RUN=0
while IFS= read -r segment; do
    if ! _segment_ok "$segment"; then
        _block "$COMMAND"
    fi
    if _is_full_precommit "$segment"; then
        FULL_RUN=1
    fi
done <<< "$(printf '%s' "$COMMAND" | tr ';&|(' '\n\n\n\n')"

# ---- The three-run cap, made mechanical (agents/precommit-fixer.md) ----
# The fixer's contract allows three full `forge-precommit` runs per
# invocation; as prose it was breached (eleven runs in ten minutes,
# never a STUCK block). Each allowed full run appends one
# `precommit_full_run` line to the agent-timing ledger (the same
# code_health/agent_timing.jsonl `log_agent_timing.sh` writes, keyed by
# the payload's agent_id), and the fourth is refused with the STUCK
# reason the agent must relay. `--only <step>` refreshes are not full
# runs and never count. Fail open without an agent_id (main session or
# an older Claude Code) or without a repo root — the cap cannot be
# keyed then; the recon allowlist above still holds.
[ "$FULL_RUN" = 1 ] && [ -n "$AGENT_ID" ] || exit 0
ROOT=${CLAUDE_PROJECT_DIR:-}
[ -n "$ROOT" ] && [ -e "$ROOT/.git" ] || ROOT=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null) || exit 0
LEDGER="$ROOT/code_health/agent_timing.jsonl"
COUNT=0
if [ -r "$LEDGER" ]; then
    # Self-generated line shape (fixed keys, this script is the only
    # writer of this event), so a fixed-string grep is exact and cheaper
    # than a second jq process.
    NEEDLE=$(jq -rn --arg a "$AGENT_ID" '"\"agent_id\":" + ($a | tojson)' 2>/dev/null) || NEEDLE=""
    [ -n "$NEEDLE" ] && COUNT=$(grep -F '"event":"precommit_full_run"' "$LEDGER" 2>/dev/null \
        | grep -cF "$NEEDLE" || true)
fi
if [ "${COUNT:-0}" -ge 3 ]; then
    echo "BLOCKED: STUCK — this precommit-fixer run already used its three full forge-precommit runs (agents/precommit-fixer.md hard cap). Do not run it again: emit the STUCK block naming the step still failing, what you tried, and hand back to the main agent. A single step CLI or forge-precommit --only <step> may refresh one log." >&2
    exit 2
fi
mkdir -p "$ROOT/code_health" 2>/dev/null || exit 0
# Encoded by jq, like log_agent_timing.sh writing the same ledger: a
# hand-built line would let an id carrying a quote close the field early
# and forge (or evade) the count the grep above performs.
LINE=$(jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg s "$SESSION_ID" \
    --arg a "$AGENT_ID" --arg t "$AGENT_TYPE" \
    '{ts: $ts, event: "precommit_full_run", session_id: $s, agent_id: $a, agent_type: $t}' \
    2>/dev/null) || exit 0
printf '%s\n' "$LINE" >> "$LEDGER" 2>/dev/null || true
exit 0
