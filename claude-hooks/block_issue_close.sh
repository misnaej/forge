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
# (`… | gh api --input -`); a body or query encoded so `state` or the
# mutation name never appears literally (a JSON `\u00XX` escape is
# refused; other encodings are not seen), or built from an expansion
# after its first word (the --words view cannot tell it from a GraphQL
# `$variable`); a command name held in a variable
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

# _segments <regex> <text> — each call <regex> finds in <text>, one per
# line: from the match to the first `;` `&` `|` `)` or newline outside a
# `(…)` group. The --words view keeps a substitution's `(…)`, so a
# field after `"$(date)"` or `"$(a; b)"` still belongs to the call.
_segments() {
    local offs
    offs=$(LC_ALL=C grep -obE "$1" <<< "$2" | cut -d: -f1 | tr '\n' ' ') || true
    [ -n "$offs" ] || return 0
    LC_ALL=C awk -v offs="$offs" '
        { s = s (NR > 1 ? "\n" : "") $0 }
        END {
            n = split(offs, o, " ")
            len = length(s)
            for (k = 1; k <= n; k++) {
                i = o[k] + 1
                if (substr(s, i, 1) ~ /[;&|(]/) i++
                start = i
                depth = 0
                for (; i <= len; i++) {
                    c = substr(s, i, 1)
                    if (c == "(") depth++
                    else if (c == ")") { if (depth == 0) break; depth-- }
                    else if (depth == 0 && (c == ";" || c == "&" || c == "|" || c == "\n")) break
                }
                out = substr(s, start, i - start)
                gsub(/\n/, " ", out)
                print out
            }
        }' <<< "$2"
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

# _encoded <text> — a JSON body carries a `\u00XX` escape, which gh sends
# as-is and GitHub decodes: `state` or a mutation name could hide behind it.
_encoded() {
    LC_ALL=C grep -qiE '\\u00' <<< "$1"
}

# _unknowable <value> — a value built at run time: an expansion (after
# any quoted blanks), a substitution or a backtick. In the --words view a
# single-quoted `$` is kept too, so only a leading one is judged.
_unknowable() {
    local v="$1"
    while [[ "$v" == "$N"* ]]; do v=${v#"$N"}; done
    case "$v" in '$'* | *'('* | *'`'*) return 0 ;; esac
    return 1
}

# _endpoint <word…> — the call's endpoint: the first positional word
# after `api`, skipping options and the values they take.
_endpoint() {
    local seen=0 skip=0 t
    for t in "$@"; do
        if [ "$skip" = 1 ]; then
            skip=0
            continue
        fi
        if [ "$seen" = 0 ]; then
            [ "$t" = api ] && seen=1
            continue
        fi
        case "$t" in
            -X | --method | -f | --raw-field | -F | --field | -H | --header | --input \
                | -q | --jq | -t | --template | --hostname | --cache | -p | --preview)
                skip=1
                ;;
            -*) ;;
            *)
                printf '%s' "$t"
                return 0
                ;;
        esac
    done
}

# _file_body <path> <what> — read <path> into FT, refusing one the hook
# cannot read or one hiding text behind a JSON escape.
_file_body() {
    _read_file "$1" || _refuse " (the $2 cannot be read by the hook)"
    _encoded "$FT" && _refuse " (the $2 is JSON-escaped, so the hook cannot read it)"
    return 0
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
ISSUE_TAIL="issues/[^/?[:space:]${N}]+/*([?[:space:]${N}]|$)"
while IFS= read -r seg; do
    input=$(_input_of "$seg")
    read -ra toks <<< "$seg"
    # The method (POST once a field or --input is given, GET otherwise),
    # and each field — a method built at run time may be any write.
    method=""
    fields=()
    kinds=()
    for ((i = 0; i < ${#toks[@]}; i++)); do
        t=${toks[i]}
        case "$t" in
            -X | --method) method=${toks[i + 1]:-} ;;
            --method=*) method=${t#*=} ;;
            -X?*) method=${t#-X} ;;
            -f | --raw-field) fields+=("${toks[i + 1]:-}"); kinds+=(raw) ;;
            -F | --field) fields+=("${toks[i + 1]:-}"); kinds+=(typed) ;;
            --raw-field=*) fields+=("${t#*=}"); kinds+=(raw) ;;
            --field=*) fields+=("${t#*=}"); kinds+=(typed) ;;
            -f?*) fields+=("${t:2}"); kinds+=(raw) ;;
            -F?*) fields+=("${t:2}"); kinds+=(typed) ;;
        esac
    done
    method=$(tr '[:lower:]' '[:upper:]' <<< "$method")
    if [ -z "$method" ]; then
        { [ "${#fields[@]}" -gt 0 ] || [ -n "$input" ]; } && method=POST || method=GET
    fi
    _unknowable "$method" && method=UNKNOWN
    [ "$method" = GET ] && continue
    # A write to an endpoint built at run time may be an issue's.
    if _unknowable "$(_endpoint "${toks[@]}")"; then
        _refuse " (an API write whose endpoint the hook cannot read)"
    fi
    if LC_ALL=C grep -qE "(^|[[:space:]])graphql([[:space:]${N}]|$)" <<< "$seg"; then
        _graphql_closes "$seg" && _refuse " through the GraphQL API"
        # A query sent as the --input body: stdin is judged by the raw
        # command (a heredoc or here-string holds it there); a file is
        # read, and one the hook cannot read is refused.
        case "$input" in
            "") ;;
            - | /dev/stdin)
                _graphql_closes "$COMMAND" && _refuse " through the GraphQL API"
                _encoded "$COMMAND" && _refuse " (the --input query is JSON-escaped, so the hook cannot read it)"
                ;;
            *)
                _file_body "$input" "--input query"
                _graphql_closes "$FT" && _refuse " through the GraphQL API"
                ;;
        esac
        # A query read from a file by a typed field (`-F query=@q.graphql`)
        # is read the same way (a raw `-f` field sends `@…` literally); a
        # query built at run time is refused.
        for ((k = 0; k < ${#fields[@]}; k++)); do
            f=${fields[k]}
            case "$f" in query=*) ;; *) continue ;; esac
            q=${f#query=}
            if [ "${kinds[k]}" = typed ] && [[ "$q" == @* ]]; then
                _file_body "${q#@}" "GraphQL query file"
                _graphql_closes "$FT" && _refuse " through the GraphQL API"
            elif _unknowable "$q"; then
                _refuse " (the GraphQL query is built at run time, so the hook cannot read it)"
            fi
        done
        continue
    fi
    LC_ALL=C grep -qE "$ISSUE_TAIL" <<< "$seg" || continue
    case "$method" in
        DELETE) _refuse " through the REST API" ;;
        UNKNOWN) _refuse " (an API call to an issue whose method the hook cannot read)" ;;
    esac
    # Each field's key: `state`, or a key the hook cannot read (an
    # expansion, a substitution, a quoted blank), may close the issue.
    for ((k = 0; k < ${#fields[@]}; k++)); do
        key=${fields[k]%%=*}
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
            _encoded "$COMMAND" && _refuse " (the --input body is JSON-escaped, so the hook cannot read it)"
            ;;
        *)
            _file_body "$input" "--input body"
            LC_ALL=C grep -qE '"state"' <<< "$FT" && _refuse " through the REST API"
            ;;
    esac
done < <(_segments "${GH_ANCHOR}api\b" "$(command_positions --words --no-redir "$COMMAND")")

exit 0
