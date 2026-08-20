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
# Allowlist: forge-precommit, the six step CLIs, `cd` (navigation), and
# targeted test runs — pytest / python -m pytest with explicit `::`
# node-id selector(s), one or several (the tests being fixed or just
# written). Untargeted pytest (bare, file, directory) stays blocked.
# Every segment of a compound command must pass.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
AGENT_TYPE=$(jq -r '.agent_type // empty' <<< "$INPUT")

# Scoped: only the precommit-fixer is restricted (both name forms, per
# the block_raw_git precedent).
if [ "$AGENT_TYPE" != "precommit-fixer" ] && [ "$AGENT_TYPE" != "forge:precommit-fixer" ]; then
    exit 0
fi

_block() {
    echo "BLOCKED: precommit-fixer's Bash is limited to forge-precommit, the step CLIs, and targeted pytest node-ids — the code_health/ logs are the only evidence (agents/precommit-fixer.md, FOUNDATION §3). '$1' is outside that set; do not run reconnaissance, read the logs." >&2
    exit 2
}

_segment_ok() {
    seg="$1"
    # Trim leading whitespace.
    seg="${seg#"${seg%%[![:space:]]*}"}"
    [ -z "$seg" ] && return 0
    # Strip leading VAR=val assignments and command/env/exec/builtin/
    # sudo/flag wrappers (same prefix set the sibling hooks anchor past).
    while :; do
        tok="${seg%%[[:space:]]*}"
        case "$tok" in
            *=*|command|env|exec|builtin|sudo|-*)
                rest="${seg#"$tok"}"
                seg="${rest#"${rest%%[![:space:]]*}"}"
                [ -z "$seg" ] && return 0
                ;;
            *) break ;;
        esac
    done
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
while IFS= read -r segment; do
    if ! _segment_ok "$segment"; then
        _block "$COMMAND"
    fi
done <<< "$(printf '%s' "$COMMAND" | tr ';&|(' '\n\n\n\n')"
