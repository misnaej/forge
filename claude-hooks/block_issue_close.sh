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
# line: from its match to the next gh call in <text>, or the end. No
# bracket is counted: a `)` in a backtick, a case pattern or a `${…}`
# desynchronises any counter. A segment may so run past its call's end,
# which only adds what is judged — fields are read as the values of the
# call's own flags, so a trailing `echo state=closed` is not a field. A
# gh call nested in a substitution (`-f x="$(gh --version)" -f …`) ends
# a segment early, so when <text> holds a `(` and more than one gh call,
# a last line, marked by a leading \002, runs from the first match to
# the end and is judged as one call carrying every endpoint in it.
_segments() {
    local kinds anchors
    kinds=$(LC_ALL=C grep -obE "$1" <<< "$2" | cut -d: -f1 | tr '\n' ' ') || true
    [ -n "$kinds" ] || return 0
    anchors=$(LC_ALL=C grep -obE "$GH_ANCHOR" <<< "$2" | cut -d: -f1 | tr '\n' ' ') || true
    LC_ALL=C awk -v kinds="$kinds" -v anchors="$anchors" '
        { s = s (NR > 1 ? " " : "") $0 }
        END {
            nk = split(kinds, k, " ")
            na = split(anchors, a, " ")
            j = 1
            for (x = 1; x <= nk; x++) {
                while (j <= na && a[j] + 0 <= k[x] + 0) j++
                from = k[x] + 1
                to = (j <= na) ? a[j] + 1 : length(s) + 1
                if (substr(s, from, 1) ~ /[;&|(]/) from++
                out = substr(s, from, to - from)
                # A separator never belongs to a word: `state=closed;` is
                # `state=closed`. A `(` stays, so a substitution stays visible.
                gsub(/[;&|)]/, " ", out)
                print out
            }
            if (na > 1 && index(s, "(") > 0) {
                from = k[1] + 1
                if (substr(s, from, 1) ~ /[;&|(]/) from++
                out = substr(s, from)
                gsub(/[;&|)]/, " ", out)
                print "\002" out
            }
        }' <<< "$2"
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

# _endpoints <word…> — set EPS to each call's endpoint: the first
# positional word after each `api`, skipping options and their values.
_endpoints() {
    local seen=0 skip=0 t
    EPS=()
    for t in "$@"; do
        if [ "$skip" = 1 ]; then
            skip=0
            continue
        fi
        if [ "$t" = api ]; then
            seen=1
            continue
        fi
        [ "$seen" = 1 ] || continue
        case "$t" in
            -X | --method | -f | --raw-field | -F | --field | -H | --header | --input \
                | -q | --jq | -t | --template | --hostname | --cache | -p | --preview)
                skip=1
                ;;
            -*) ;;
            *)
                EPS+=("$t")
                seen=0
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
RE_ISSUE="issues/[^/?${N}]+/*([?${N}]|$)"
while IFS= read -r seg; do
    seg=${seg#$'\002'}
    read -ra toks <<< "$seg"
    # Any write wins: one PATCH/POST/PUT/DELETE — or a method built at
    # run time — anywhere in the segment makes it a write, and a GET
    # elsewhere never cancels it. A call with no method is a POST once it
    # carries a field or --input, judged per `api` word.
    write=0
    delete=0
    unknown_method=0
    explicit=0
    carries=0
    fields=()
    kinds=()
    inputs=()
    for ((i = 0; i < ${#toks[@]}; i++)); do
        t=${toks[i]}
        m=""
        case "$t" in
            api)
                [ "$explicit" = 0 ] && [ "$carries" = 1 ] && write=1
                explicit=0
                carries=0
                ;;
            -X | --method) m=${toks[i + 1]:-} ;;
            --method=*) m=${t#*=} ;;
            -X?*) m=${t#-X} ;;
            -f | --raw-field) carries=1; fields+=("${toks[i + 1]:-}"); kinds+=(raw) ;;
            -F | --field) carries=1; fields+=("${toks[i + 1]:-}"); kinds+=(typed) ;;
            --raw-field=*) carries=1; fields+=("${t#*=}"); kinds+=(raw) ;;
            --field=*) carries=1; fields+=("${t#*=}"); kinds+=(typed) ;;
            -f?*) carries=1; fields+=("${t:2}"); kinds+=(raw) ;;
            -F?*) carries=1; fields+=("${t:2}"); kinds+=(typed) ;;
            --input) carries=1; inputs+=("${toks[i + 1]:-}") ;;
            --input=*) carries=1; inputs+=("${t#*=}") ;;
        esac
        if [ -n "$m" ]; then
            explicit=1
            case "${m^^}" in
                POST | PUT | PATCH) write=1 ;;
                DELETE) write=1; delete=1 ;;
                *) _unknowable "$m" && { write=1; unknown_method=1; } ;;
            esac
        fi
    done
    [ "$explicit" = 0 ] && [ "$carries" = 1 ] && write=1
    [ "$write" = 1 ] || continue
    # Every endpoint in the segment (one per `api` word; a merged
    # segment carries several) is judged; one built at run time may be
    # an issue's.
    _endpoints "${toks[@]}"
    graphql=0
    issue=0
    for ep in "${EPS[@]}"; do
        _unknowable "$ep" && _refuse " (an API write whose endpoint the hook cannot read)"
        [ "$ep" = graphql ] && graphql=1
        [[ "$ep" =~ $RE_ISSUE ]] && issue=1
    done
    if [ "$graphql" = 1 ]; then
        _graphql_closes "$seg" && _refuse " through the GraphQL API"
        # A query sent as the --input body: stdin is judged by the raw
        # command (a heredoc or here-string holds it there); a file is
        # read, and one the hook cannot read is refused.
        for input in "${inputs[@]}"; do
            case "$input" in
                - | /dev/stdin)
                    _graphql_closes "$COMMAND" && _refuse " through the GraphQL API"
                    _encoded "$COMMAND" && _refuse " (the --input query is JSON-escaped, so the hook cannot read it)"
                    ;;
                *)
                    _file_body "$input" "--input query"
                    _graphql_closes "$FT" && _refuse " through the GraphQL API"
                    ;;
            esac
        done
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
    fi
    [ "$issue" = 1 ] || continue
    [ "$delete" = 1 ] && _refuse " through the REST API"
    [ "$unknown_method" = 1 ] && _refuse " (an API call to an issue whose method the hook cannot read)"
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
    for input in "${inputs[@]}"; do
        case "$input" in
            - | /dev/stdin)
                LC_ALL=C grep -qE "state" <<< "$COMMAND" && _refuse " through the REST API"
                _encoded "$COMMAND" && _refuse " (the --input body is JSON-escaped, so the hook cannot read it)"
                ;;
            *)
                _file_body "$input" "--input body"
                LC_ALL=C grep -qE '"state"' <<< "$FT" && _refuse " through the REST API"
                ;;
        esac
    done
done < <(_segments "${GH_ANCHOR}api\b" "$(command_positions --words --no-redir "$COMMAND")")

exit 0
