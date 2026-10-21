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
# that way, by design); a query or body piped from another program
# (`… | gh api --input -`); a command name held in a variable
# (`G=gh; $G issue close 5`, as in block_pr_merge.sh); `gh alias`.
# `gh pr close` is a PR, not an issue, and out of scope.
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
# Fail CLOSED on a lib that predates a helper this hook calls: an
# undefined function returns 127, which reads as "no match" and would let
# the command through.
if ! declare -F guard_begin command_positions guard_help_only > /dev/null \
    || [ -z "${GUARD_NEUTRAL:-}" ] || [ -z "${GH_ANCHOR:-}" ] || [ -z "${GH_OPTS:-}" ]; then
    echo "BLOCKED: the guard anchor lib at $ANCHOR_LIB lacks a helper this hook needs (older or damaged plugin copy) — refusing the command rather than running unguarded." >&2
    exit 2
fi
guard_begin
# No pathname expansion: segments are split into words below.
set -f

_refuse() {
    echo "BLOCKED: agents must not close, reopen, delete or transfer issues${1}. Whether an issue is done is the user's call (FOUNDATION §2) — a human runs the same command with \`!\`." >&2
    exit 2
}

# Any `cd`/`pushd`/`popd` outside quotes moves where a relative file
# resolves, so such a file is not read.
CHDIR_RE='(^|[[:space:];&|(!{])(cd|pushd|popd)([[:space:];&|)]|$)'

# _read_file <path> — read an --input file into FT. Fails (status 1) when
# the hook cannot know which file the command reads, or cannot read it:
# an expansion or quoted blank in the path, a `~`, a relative path after a
# directory change, a missing, unreadable or oversized file.
_read_file() {
    local p="$1" full size
    case "$p" in "" | *"$N"* | *'$'* | *'('* | *'`'* | '~'*) return 1 ;; esac
    case "$p" in
        /*) full="$p" ;;
        *)
            echo "$CMDPOS" | grep -qE "$CHDIR_RE" && return 1
            full="$PAYLOAD_CWD/$p"
            ;;
    esac
    [ -f "$full" ] && [ -r "$full" ] || return 1
    size=$(wc -c < "$full" 2> /dev/null) || return 1
    [ "$((size))" -le 70000 ] || return 1
    FT=$(LC_ALL=C head -c 70000 "$full" 2> /dev/null) || return 1
    return 0
}

# _input_of <segment> — the segment's --input value (empty when none).
_input_of() {
    LC_ALL=C grep -oE -- "--input([[:space:]]+|=)[^[:space:]]+" <<< "$1" | head -n 1 | sed -E 's/^--input([[:space:]]+|=)//' || true
}

# _graphql_closes <text> — the text names a mutation that changes an
# issue's state.
_graphql_closes() {
    LC_ALL=C grep -qE "(closeIssue|reopenIssue|deleteIssue|transferIssue)([^[:alnum:]_]|$)" <<< "$1" \
        || { LC_ALL=C grep -qE "updateIssue([^[:alnum:]_]|$)" <<< "$1" \
            && LC_ALL=C grep -qE "state" <<< "$1"; }
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
ISSUE_TAIL="issues/[^/?[:space:]${N}]+/?([?[:space:]${N}]|$)"
while IFS= read -r seg; do
    input=$(_input_of "$seg")
    if LC_ALL=C grep -qE "(^|[[:space:]])graphql([[:space:]${N}]|$)" <<< "$seg"; then
        _graphql_closes "$seg" && _refuse " through the GraphQL API"
        # A query sent as the --input body: stdin is judged by the raw
        # command (a heredoc or here-string holds it there); a file is
        # read, and one the hook cannot read is refused.
        case "$input" in
            "") ;;
            - | /dev/stdin)
                _graphql_closes "$COMMAND" && _refuse " through the GraphQL API"
                ;;
            *)
                _read_file "$input" || _refuse " (the --input query cannot be read by the hook)"
                _graphql_closes "$FT" && _refuse " through the GraphQL API"
                ;;
        esac
        # A query read from a file by a typed field (`-F query=@q.graphql`)
        # is read the same way; a raw `-f` field sends `@…` literally.
        read -ra toks <<< "$seg"
        for ((i = 0; i < ${#toks[@]}; i++)); do
            t=${toks[i]}
            case "$t" in
                -F | --field) f=${toks[i + 1]:-} ;;
                --field=*) f=${t#*=} ;;
                -F?*) f=${t:2} ;;
                *) continue ;;
            esac
            case "$f" in
                query=@*)
                    _read_file "${f#query=@}" || _refuse " (the GraphQL query file cannot be read by the hook)"
                    _graphql_closes "$FT" && _refuse " through the GraphQL API"
                    ;;
            esac
        done
        continue
    fi
    LC_ALL=C grep -qE "$ISSUE_TAIL" <<< "$seg" || continue
    if LC_ALL=C grep -qiE "(-X|--method)[[:space:]=]*DELETE([[:space:]]|$)" <<< "$seg"; then
        _refuse " through the REST API"
    fi
    LC_ALL=C grep -qiE "(-X|--method)[[:space:]=]*GET([[:space:]]|$)" <<< "$seg" && continue
    # Each field's key: `state`, or a key the hook cannot read (an
    # expansion, a substitution, a quoted blank), may close the issue.
    read -ra toks <<< "$seg"
    for ((i = 0; i < ${#toks[@]}; i++)); do
        t=${toks[i]}
        case "$t" in
            -f | -F | --field | --raw-field) f=${toks[i + 1]:-} ;;
            --field=* | --raw-field=*) f=${t#*=} ;;
            -f?* | -F?*) f=${t:2} ;;
            *) continue ;;
        esac
        key=${f%%=*}
        case "$key" in
            state) _refuse " through the REST API" ;;
            "" | *"$N"* | *'$'* | *'('* | *'`'*)
                _refuse " (a field name the hook cannot read may set the state)"
                ;;
        esac
    done
    # A request body from --input: a file is read; stdin is judged by the
    # raw command text (a heredoc or here-string holds it there).
    case "$input" in
        "") ;;
        - | /dev/stdin)
            LC_ALL=C grep -qE "state" <<< "$COMMAND" && _refuse " through the REST API"
            ;;
        *)
            _read_file "$input" || _refuse " (the --input body cannot be read by the hook)"
            LC_ALL=C grep -qE '"state"' <<< "$FT" && _refuse " through the REST API"
            ;;
    esac
done < <(LC_ALL=C grep -oE "${GH_ANCHOR}api\b[^;&|)]*" <<< "$(command_positions --words --no-redir "$COMMAND")")

exit 0
