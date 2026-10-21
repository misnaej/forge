#!/usr/bin/env bash
# Block agent-initiated issue closes, reopens, deletes and transfers.
#
# Whether an issue is done is the user's call (FOUNDATION §2): an agent
# that closes one ends the record of work owed, often right after another
# agent declined to. A close is reopenable state, unlike a merge, so this
# is its own hook rather than part of block_pr_merge.sh — but it has no
# bypass either: every agent, issue-triage included, is refused. A PR
# closes the issues it delivers through its `Closes #N` lines when the
# user merges it.
#
# To change an issue's state, the user runs the command themselves (the
# `!` prefix at the Claude Code prompt sends it through the user's shell,
# bypassing agent hooks):
#     ! gh issue close 9
#
# Known residuals: a forge CLI or interpreter script calling gh from its
# own subprocess (the emergency-ledger CLI closes its own ledger issue
# that way, by design); a GraphQL query read from a file
# (`-F query=@q.graphql`) or a REST body piped from another program
# (`… | gh api --input -`); `gh alias`. `gh pr close` is a PR, not an
# issue, and out of scope.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
PAYLOAD_CWD=$(jq -r '.cwd // empty' <<< "$INPUT")
case "$PAYLOAD_CWD" in /*) ;; *) PAYLOAD_CWD="." ;; esac
# Anchors + their rationale live in the shared lib (one home for the
# whole git-/gh-guard family).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache) must
    # block, not silently disarm the guard — only exit 2 blocks in the
    # PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"
guard_begin

_refuse() {
    echo "BLOCKED: agents must not close, reopen, delete or transfer issues${1}. Whether an issue is done is the user's call (FOUNDATION §2). Have the user run: ! $COMMAND" >&2
    exit 2
}

# `gh issue close|reopen|delete|transfer` at a real invocation position,
# found on the command-positions view (git_anchor.sh): a quoted mention
# never fires, `bash -c "gh issue close 1"` does; a help request changes
# nothing.
CMDPOS=$(command_positions "$COMMAND")
if echo "$CMDPOS" | grep -qE "${GH_ANCHOR}issue[[:space:]]+${GH_OPTS}(close|reopen|delete|transfer)\b" \
    && ! guard_help_only "$CMDPOS" "${GH_ANCHOR}issue[[:space:]]+${GH_OPTS}(close|reopen|delete|transfer)\b"; then
    _refuse ""
fi

# The same effect through `gh api`, read per call from the --words view:
# a REST write to an issue endpoint carrying `state` (as a field, or in an
# --input file), a REST DELETE on an issue, or a GraphQL mutation that
# closes, reopens, deletes or transfers one — or sets its state through
# `updateIssue`.
if ! echo "$CMDPOS" | grep -qE "${GH_ANCHOR}api\b"; then
    exit 0
fi
N="$GUARD_NEUTRAL"
ISSUE_TAIL="issues/[^/[:space:]${N}]+([?[:space:]${N}]|$)"
while IFS= read -r seg; do
    if LC_ALL=C grep -qE "(^|[[:space:]])graphql([[:space:]${N}]|$)" <<< "$seg"; then
        if LC_ALL=C grep -qE "(closeIssue|reopenIssue|deleteIssue|transferIssue)([^[:alnum:]_]|$)" <<< "$seg" \
            || { LC_ALL=C grep -qE "updateIssue([^[:alnum:]_]|$)" <<< "$seg" \
                && LC_ALL=C grep -qE "state" <<< "$seg"; }; then
            _refuse " through the GraphQL API"
        fi
        continue
    fi
    LC_ALL=C grep -qE "$ISSUE_TAIL" <<< "$seg" || continue
    if LC_ALL=C grep -qiE "(-X|--method)[[:space:]=]*DELETE([[:space:]]|$)" <<< "$seg"; then
        _refuse " through the REST API"
    fi
    LC_ALL=C grep -qiE "(-X|--method)[[:space:]=]*GET([[:space:]]|$)" <<< "$seg" && continue
    if LC_ALL=C grep -qE "(-f|-F|--field|--raw-field)[[:space:]=]*state=" <<< "$seg"; then
        _refuse " through the REST API"
    fi
    # A request body from --input: a file is read; stdin is judged by the
    # raw command text (a heredoc or here-string holds it there).
    input=$(LC_ALL=C grep -oE -- "--input([[:space:]]+|=)[^[:space:]]+" <<< "$seg" | head -n 1 | sed -E 's/^--input([[:space:]]+|=)//') || true
    [ -n "$input" ] || continue
    case "$input" in
        - | /dev/stdin)
            if LC_ALL=C grep -qE "state" <<< "$COMMAND"; then
                _refuse " through the REST API"
            fi
            continue
            ;;
        *"$N"* | *'$'* | *'('* | *'`'* | '~'*)
            _refuse " (the --input body cannot be read by the hook)"
            ;;
    esac
    case "$input" in
        /*) path="$input" ;;
        *)
            # A directory change moves where a relative file resolves.
            if echo "$CMDPOS" | grep -qE '(^|[[:space:];&|(!{])(cd|pushd|popd)([[:space:];&|)]|$)'; then
                _refuse " (the --input body cannot be read by the hook)"
            fi
            path="$PAYLOAD_CWD/$input"
            ;;
    esac
    if [ ! -f "$path" ] || [ ! -r "$path" ]; then
        _refuse " (the --input body cannot be read by the hook)"
    fi
    if LC_ALL=C head -c 70000 "$path" | LC_ALL=C grep -qE '"state"'; then
        _refuse " through the REST API"
    fi
done < <(LC_ALL=C grep -oE "${GH_ANCHOR}api\b[^;&|)]*" <<< "$(command_positions --words --no-redir "$COMMAND")")

exit 0
