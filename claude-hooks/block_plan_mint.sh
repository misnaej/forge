#!/usr/bin/env bash
# Block agents other than issue-triage from minting an executable plan.
#
# `/sentinel` executes an issue unattended when it finds a comment opening
# `[issue-triage] plan-validated:`, the `plan-ready` label and a
# write-access author. Every agent runs with the user's GitHub
# credentials, so without this guard any agent — including one steered by
# text it read in an issue — could produce all three with two ordinary
# `gh` calls. Recording a plan belongs to issue-triage's `record` mode
# (FOUNDATION §14 "Decision trail"); a planner delegates to it.
#
# Rules:
# - A1 label: a caller other than issue-triage may not add `plan-ready` —
#   `gh issue edit --add-label` / `gh issue create --label` naming it (or
#   a value the hook cannot know: `$VAR`, a substitution), `gh label
#   create|edit` naming it, a REST write to an issue's or the repo's
#   labels (whatever the label), a REST issue create or edit
#   (`issues`, `issues/<N>`) carrying a `labels` field or an `--input`
#   body that mentions `labels` (or a field name or body the hook cannot
#   read), a GraphQL label / issue mutation, or any API write whose
#   endpoint or method is built at run time (`$E`, `$(…)`).
# - A2 payload: a caller other than issue-triage may not write an issue
#   comment carrying the plan-validated marker — `gh issue comment`
#   (`--edit-last` included), REST issue-comment writes, GraphQL
#   `addComment` / `updateIssueComment`. The body is read inline, from a
#   heredoc or here-string, or from a readable `--body-file` /
#   `-F body=@file` / `--input` file; a body the hook cannot read (a pipe,
#   `$VAR`, `$(…)`, a missing file) is refused. `gh pr comment` is not an
#   issue comment and is untouched.
# - A3 signature, for every caller (issue-triage included): a
#   plan-validated payload carrying a sign-off claim ("validated by",
#   "approved by", "signed-off-by", "endorsed by", "sign-off:", with a
#   space, hyphen, underscore or nothing between the words) is refused
#   — a human attribution
#   nothing can corroborate (FOUNDATION §14). It is refused, never
#   rewritten. The phrase is matched anywhere in the payload, so a
#   harmless use ("validated by CI") is refused too and is reworded.
#
# Bypass: the PreToolUse payload's `agent_type` (the calling subagent's
# `name:` frontmatter) `issue-triage` or `forge:issue-triage` waives A1
# and A2 only — the same scoped-bypass shape as block_raw_git.sh. The main
# thread carries no agent_type, so it is covered; so is the
# issue-triage-forge wrapper, which only delegates. A human runs the
# command directly with `! <command>`.
#
# Known residuals (a hook reads the command an agent types, nothing
# more): a Python or other interpreter script, or a forge CLI, making the
# call from its own subprocess; a body encoded or computed so the marker
# never appears literally (a JSON `\u00XX` escape in an --input or query
# file is refused; other encodings are not seen), or spliced across
# expansions, including an expansion inside a GraphQL query after its
# first word (the --words view cannot tell it from a GraphQL `$variable`);
# a file rewritten
# after this hook read it (`gen > p.md; gh issue comment 1 -F p.md` reads
# the old p.md); a command name held in a variable (`G=gh; $G issue
# edit …`, as in block_pr_merge.sh); `gh alias`; `gh label clone`; and
# agent_type spoofing
# through a local `.claude/agents/issue-triage.md`. The guard proves which
# agent typed a command, never that a human validated the plan.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
AGENT_TYPE=$(jq -r '.agent_type // empty' <<< "$INPUT")
PAYLOAD_CWD=$(jq -r '.cwd // empty' <<< "$INPUT")
# Trust only an absolute payload cwd (as block_git_destructive.sh does).
case "$PAYLOAD_CWD" in /*) ;; *) PAYLOAD_CWD="." ;; esac
# Anchors and the command-positions views live in the shared lib (one home
# for the whole git-/gh-guard family).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache) must
    # block, not silently disarm the guard — only exit 2 blocks in the
    # PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"
# Fail CLOSED on a lib that predates a helper this hook calls.
if ! declare -F guard_begin command_positions guard_help_only invoked > /dev/null \
    || [ -z "${GUARD_NEUTRAL:-}" ] || [ -z "${GH_ANCHOR:-}" ] || [ -z "${GH_OPTS:-}" ]; then
    echo "BLOCKED: the guard anchor lib at $ANCHOR_LIB lacks a helper this hook needs (older or damaged plugin copy) — refusing the command rather than running unguarded." >&2
    exit 2
fi
guard_begin
# No pathname expansion: segments are split into words below.
set -f

IS_TRIAGE=0
if [ "$AGENT_TYPE" = "issue-triage" ] || [ "$AGENT_TYPE" = "forge:issue-triage" ]; then
    IS_TRIAGE=1
fi

# Commands are found on the command-positions view, values read from the
# --words view (git_anchor.sh): a quoted mention never fires,
# `bash -c "gh issue edit …"` does.
CMDPOS=$(command_positions "$COMMAND")
if ! echo "$CMDPOS" | grep -qE "${GH_ANCHOR}(issue|label|api)\b"; then
    exit 0
fi
WORDS=$(command_positions --words --no-redir "$COMMAND")
N="$GUARD_NEUTRAL"
# The marker, as forge.plan_staleness.PLAN_MARKER spells it, read through
# quoting: in the --words view a quoted blank is GUARD_NEUTRAL.
MARK="\\[issue-triage\\][[:space:]${N}]*plan-validated"
SIGNOFF="(validated|approved|endorsed)[[:space:]${N}_-]*by|sign(ed)?[[:space:]${N}_-]*off[[:space:]${N}_-]*by|sign-off[[:space:]${N}]*:"
LABEL_MUTATIONS="(addLabelsToLabelable|updateIssue|createIssue|updateLabel|createLabel)([^[:alnum:]_]|$)"
COMMENT_MUTATIONS="(addComment|updateIssueComment)([^[:alnum:]_]|$)"
# Any `cd`/`pushd`/`popd` outside quotes moves where a relative body file
# resolves, so such a file is not read (same test as
# block_git_destructive.sh; over-matching only refuses more).
CHDIR=0
if echo "$CMDPOS" | grep -qE '(^|[[:space:];&|(!{])(cd|pushd|popd)([[:space:];&|)]|$)'; then
    CHDIR=1
fi
# A heredoc or here-string in the command: its text is part of the raw
# command, which is scanned whole, so a body read from stdin is known.
HAS_HEREDOC=0
case "$COMMAND" in *'<<'*) HAS_HEREDOC=1 ;; esac

DELEGATE="Delegate instead: Agent(subagent_type='forge:issue-triage', prompt='record mode: …') with the validated plan text and the user's explicit validation — issue-triage checks it mechanically and records it (FOUNDATION §14). A human runs it directly with: ! <command>"

_refuse() {
    echo "BLOCKED: $1 — recording a plan or applying plan-ready belongs to issue-triage. $DELEGATE" >&2
    exit 2
}

# Findings gathered while reading the segments.
LABEL_WRITE=""    # why an A1 label write was seen
COMMENT_WRITE=0   # an issue-comment writer is present
UNKNOWN=""        # why a comment body (or query) cannot be read
FILE_MARK=0       # a body file carries the marker
SIGN_PHRASE=""    # the sign-off phrase found next to the marker

# _signoff_in <text> — record the sign-off phrase when <text> carries
# both the marker and a sign-off claim (A3 judges one unit at a time).
_signoff_in() {
    local phrase
    if [ -z "$SIGN_PHRASE" ] && LC_ALL=C grep -qiE "$MARK" <<< "$1"; then
        phrase=$(LC_ALL=C grep -oiE "$SIGNOFF" <<< "$1" | head -n 1) || true
        if [ -n "$phrase" ]; then
            SIGN_PHRASE=${phrase//"$N"/ }
        fi
    fi
}

# _read_file <path> — read a body or query file into FT. Fails (status 1)
# when the hook cannot know which file the command will read, or cannot
# read it: an expansion or quoted blank in the path, a `~`, a relative
# path after a directory change, a missing, unreadable or oversized file.
_read_file() {
    local p="$1" full size
    case "$p" in "" | *"$N"* | *'$'* | *'('* | *'`'* | '~'*) return 1 ;; esac
    case "$p" in
        /*) full="$p" ;;
        *)
            [ "$CHDIR" = 1 ] && return 1
            full="$PAYLOAD_CWD/$p"
            ;;
    esac
    [ -f "$full" ] && [ -r "$full" ] || return 1
    size=$(wc -c < "$full" 2> /dev/null) || return 1
    [ "$((size))" -le 70000 ] || return 1
    FT=$(LC_ALL=C head -c 70000 "$full" 2> /dev/null) || return 1
    return 0
}

# _encoded <text> — a JSON body carries a `\u00XX` escape, which gh sends
# as-is and GitHub decodes: the marker or a label could hide behind it.
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

# _braced <value> — the value holds an unquoted `{` (a quoted one is the
# neutral byte in the --words view), so bash may brace-expand it
# (`{PATCH,}`) into words the hook never sees. gh's own `{owner}` /
# `{repo}` / `{branch}` placeholders are not expansions.
_braced() {
    local v=${1//\{owner\}/}
    v=${v//\{repo\}/}
    v=${v//\{branch\}/}
    [[ "$v" == *'{'* ]]
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

# _segments <regex> <text> — each call <regex> finds in <text>, one per
# line: from its match to the next gh call in <text>, or the end. No
# bracket is counted: a `)` in a backtick, a case pattern or a `${…}`
# desynchronises any counter. A segment may so run past its call's end,
# which only adds what is judged — options are read as the values of the
# call's own flags, so a trailing `echo plan-ready` is not a label. A gh
# call nested in a substitution (`-f x="$(gh --version)" -f …`) ends a
# segment early, so when <text> holds a `(` and more than one gh call, a
# last line, marked by a leading \002, runs from the first match to the
# end and is judged as one call carrying every endpoint in it.
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
                # A separator never belongs to a word: `plan-ready;` is
                # `plan-ready`. A `(` stays, so a substitution stays visible.
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

# _body_file <path> [json] — a comment body read from <path> (`-` =
# stdin); `json` marks an --input body, where a `\u` escape hides text.
_body_file() {
    case "$1" in
        - | /dev/stdin)
            if [ "$HAS_HEREDOC" != 1 ]; then
                UNKNOWN="the comment body is read from a pipe the hook cannot see"
            elif [ "${2:-}" = json ] && _encoded "$COMMAND"; then
                UNKNOWN="the --input body is JSON-escaped (\\u00XX), so the hook cannot read it"
            fi
            return 0
            ;;
    esac
    if ! _read_file "$1"; then
        UNKNOWN="the comment body file cannot be read by the hook"
        return 0
    fi
    if [ "${2:-}" = json ] && _encoded "$FT"; then
        UNKNOWN="the --input body is JSON-escaped (\\u00XX), so the hook cannot read it"
    fi
    if LC_ALL=C grep -qiE "$MARK" <<< "$FT"; then
        FILE_MARK=1
        _signoff_in "$FT"
    fi
}

# _body_inline <value> — a comment body given inline. Its text is part of
# the command, scanned whole below; only a value built at run time is
# unknowable. `"$(cat <<'EOF' … EOF)"` is the one substitution read: its
# heredoc is in the raw command.
_body_inline() {
    case "$1" in
        *'$'* | *'('* | *'`'*)
            if [[ "$1" == '(cat'* ]] && [ "$HAS_HEREDOC" = 1 ]; then
                return 0
            fi
            UNKNOWN="the comment body is built at run time"
            ;;
    esac
}

# _label_values <value> — true when a label list names plan-ready or
# holds a value the hook cannot know.
_label_values() {
    local v="$1" item
    # An unquoted `{` brace-expands and `[` `*` `?` glob into names the
    # hook never sees (`plan-{ready,x}`, `plan-read[y]`); quoted, the
    # last three look the same here, so a label holding one is refused.
    case "$v" in *'$'* | *'('* | *'`'* | *'{'* | *'['* | *'*'* | *'?'*) return 0 ;; esac
    v=${v//"$N"/,}
    v=${v//\"/}
    v=${v//\'/}
    local IFS=,
    # Matched case-insensitively in the shell itself: GitHub label names
    # ignore case, and a process per word would let a long chain of
    # harmless edits run the guard past its deadline.
    for item in $v; do
        item=${item//[[:space:]]/}
        case "$item" in [Pp][Ll][Aa][Nn]-[Rr][Ee][Aa][Dd][Yy]) return 0 ;; esac
    done
    return 1
}

# --- A1: gh issue edit / create label values ------------------------------
if [ "$IS_TRIAGE" != 1 ] && invoked "${GH_ANCHOR}issue[[:space:]]+${GH_OPTS}(edit|create)\b"; then
    while IFS= read -r seg; do
        read -ra toks <<< "${seg#$'\002'}"
        n=${#toks[@]}
        for ((i = 0; i < n; i++)); do
            t=${toks[i]}
            val=""
            case "$t" in
                --add-label | --label | -l) val=${toks[i + 1]:-} ;;
                --add-label=* | --label=*) val=${t#*=} ;;
                -l?*) val=${t#-l} ;;
                *) continue ;;
            esac
            if [ -n "$val" ] && _label_values "$val"; then
                LABEL_WRITE="adding the plan-ready label (or a label the hook cannot read)"
            fi
        done
    done < <(_segments "${GH_ANCHOR}issue[[:space:]]+${GH_OPTS}(edit|create)\b" "$WORDS")
fi

# --- A1: gh label create / edit naming plan-ready -------------------------
if [ "$IS_TRIAGE" != 1 ] && invoked "${GH_ANCHOR}label[[:space:]]+${GH_OPTS}(create|edit)\b"; then
    # The name is the first plain word after `create` / `edit`, or a
    # `--name` value — never any word the segment happens to hold.
    while IFS= read -r seg; do
        read -ra toks <<< "${seg#$'\002'}"
        n=${#toks[@]}
        want=0
        for ((i = 0; i < n; i++)); do
            t=${toks[i]}
            val=""
            case "$t" in
                create | edit)
                    want=1
                    continue
                    ;;
                --name | -n) val=${toks[i + 1]:-} ;;
                --name=* | -n=*) val=${t#*=} ;;
                -n?*) val=${t#-n} ;;
                -c | --color | -d | --description | -R | --repo)
                    i=$((i + 1))
                    continue
                    ;;
                -*) continue ;;
                *)
                    [ "$want" = 1 ] || continue
                    want=0
                    val=$t
                    ;;
            esac
            if [ -n "$val" ] && _label_values "$val"; then
                LABEL_WRITE="creating or renaming a label to plan-ready"
            fi
        done
    done < <(_segments "${GH_ANCHOR}label[[:space:]]+${GH_OPTS}(create|edit)\b" "$WORDS")
fi

# --- gh issue comment: a comment writer, with its body sources ------------
if invoked "${GH_ANCHOR}issue[[:space:]]+${GH_OPTS}comment\b"; then
    COMMENT_WRITE=1
    while IFS= read -r seg; do
        read -ra toks <<< "${seg#$'\002'}"
        n=${#toks[@]}
        for ((i = 0; i < n; i++)); do
            t=${toks[i]}
            case "$t" in
                -b | --body) _body_inline "${toks[i + 1]:-}" ;;
                --body=*) _body_inline "${t#*=}" ;;
                -b?*) _body_inline "${t#-b}" ;;
                -F | --body-file) _body_file "${toks[i + 1]:-}" ;;
                --body-file=*) _body_file "${t#*=}" ;;
                -F?*) _body_file "${t#-F}" ;;
            esac
        done
    done < <(_segments "${GH_ANCHOR}issue[[:space:]]+${GH_OPTS}comment\b" "$WORDS")
fi

# --- gh api: REST and GraphQL writes --------------------------------------
if invoked "${GH_ANCHOR}api\b"; then
    RE_LABELS="/labels([/?${N}]|$)"
    RE_COMMENTS="issues/[^/${N}]+/comments([/?${N}]|$)|issues/comments/[^/${N}]+"
    RE_ISSUE="/issues(/[^/?${N}]+)?/*([?${N}]|$)"
    while IFS= read -r seg; do
        seg=${seg#$'\002'}
        read -ra toks <<< "$seg"
        n=${#toks[@]}
        # Any write wins: one PATCH/POST/PUT — or a method built at run
        # time — anywhere in the segment makes it a write, and a GET
        # elsewhere never cancels it. A call with no method is a POST
        # once it carries a field or --input, judged per `api` word.
        write=0
        explicit=0
        carries=0
        fields=()
        kinds=()
        inputs=()
        for ((i = 0; i < n; i++)); do
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
                -f?*) carries=1; fields+=("${t#-f}"); kinds+=(raw) ;;
                -F?*) carries=1; fields+=("${t#-F}"); kinds+=(typed) ;;
                --input) carries=1; inputs+=("${toks[i + 1]:-}") ;;
                --input=*) carries=1; inputs+=("${t#*=}") ;;
            esac
            if [ -n "$m" ]; then
                explicit=1
                case "${m^^}" in
                    POST | PUT | PATCH) write=1 ;;
                    *) { _unknowable "$m" || _braced "$m"; } && write=1 ;;
                esac
            fi
        done
        [ "$explicit" = 0 ] && [ "$carries" = 1 ] && write=1
        [ "$write" = 1 ] || continue
        # Every endpoint in the segment (one per `api` word; a merged
        # segment carries several) is judged.
        _endpoints "${toks[@]}"
        ep_unknown=0
        graphql=0
        label_seg=0
        comment_seg=0
        issue_seg=0
        for ep in "${EPS[@]}"; do
            { _unknowable "$ep" || _braced "$ep"; } && ep_unknown=1
            [ "$ep" = graphql ] && graphql=1
            [[ "$ep" =~ $RE_LABELS ]] && label_seg=1
            [[ "$ep" =~ $RE_COMMENTS ]] && comment_seg=1
            [[ "$ep" =~ $RE_ISSUE ]] && issue_seg=1
        done
        # The text this call sends as code: the segment, plus any GraphQL
        # query read from a file.
        text="$seg"
        query_unknown=0
        for ((k = 0; k < ${#fields[@]}; k++)); do
            f=${fields[k]}
            case "$f" in
                query=*)
                    q=${f#query=}
                    if [ "${kinds[k]}" = typed ] && [[ "$q" == @* ]]; then
                        if _read_file "${q#@}" && ! _encoded "$FT"; then
                            text="$text"$'\n'"$FT"
                            if LC_ALL=C grep -qiE "$MARK" <<< "$FT"; then
                                FILE_MARK=1
                                _signoff_in "$FT"
                            fi
                        else
                            query_unknown=1
                        fi
                    elif _unknowable "$q"; then
                        query_unknown=1
                    fi
                    ;;
            esac
        done
        # A GraphQL request sent whole as the --input body: a file is read,
        # stdin is judged by the raw command (a heredoc holds it there).
        if [ "$graphql" = 1 ]; then
            for input in "${inputs[@]}"; do
                case "$input" in
                    - | /dev/stdin)
                        if [ "$HAS_HEREDOC" = 1 ]; then
                            text="$text"$'\n'"$COMMAND"
                        else
                            query_unknown=1
                        fi
                        ;;
                    *)
                        if _read_file "$input" && ! _encoded "$FT"; then
                            text="$text"$'\n'"$FT"
                        else
                            query_unknown=1
                        fi
                        ;;
                esac
            done
        fi
        if [ "$graphql" = 1 ]; then
            LC_ALL=C grep -qE "$LABEL_MUTATIONS" <<< "$text" && label_seg=1
            LC_ALL=C grep -qE "$COMMENT_MUTATIONS" <<< "$text" && comment_seg=1
            if [ "$query_unknown" = 1 ]; then
                label_seg=1
                comment_seg=1
                UNKNOWN="the GraphQL query cannot be read by the hook"
            fi
        fi
        if [ "$label_seg" = 1 ] && [ "$IS_TRIAGE" != 1 ]; then
            LABEL_WRITE="writing issue labels through the API"
        fi
        if [ "$ep_unknown" = 1 ]; then
            comment_seg=1
            [ "$IS_TRIAGE" = 1 ] || LABEL_WRITE="an API write whose endpoint the hook cannot read"
        fi
        # An issue create (`issues`) or edit (`issues/<N>`) sets labels from
        # a `labels` field or body: refused when it carries one, or a field
        # name or body the hook cannot read.
        if [ "$issue_seg" = 1 ] && [ "$IS_TRIAGE" != 1 ]; then
            for ((k = 0; k < ${#fields[@]}; k++)); do
                key=${fields[k]%%=*}
                case "$key" in
                    [Ll][Aa][Bb][Ee][Ll][Ss]*) LABEL_WRITE="setting issue labels through the API" ;;
                    "" | *"$N"* | *'$'* | *'('* | *'`'* | *'{'*)
                        LABEL_WRITE="an API issue write whose field name the hook cannot read"
                        ;;
                esac
            done
            for input in "${inputs[@]}"; do
                case "$input" in
                    - | /dev/stdin)
                        # Raw (a heredoc is literal there) and the --words
                        # view (a here-string's quotes are resolved there).
                        if LC_ALL=C grep -qiE "labels" <<< "$COMMAND"$'\n'"$WORDS"; then
                            LABEL_WRITE="setting issue labels through the API"
                        elif [[ "$COMMAND" == *'<<<'* ]] \
                            && [[ "${COMMAND#*<<<}" == *"''"* || "${COMMAND#*<<<}" == *'""'* ]]; then
                            LABEL_WRITE="an API issue write whose --input here-string splices quotes the hook cannot read"
                        elif [ "$HAS_HEREDOC" != 1 ]; then
                            LABEL_WRITE="an API issue write whose --input body is piped where the hook cannot read it"
                        elif _encoded "$COMMAND"; then
                            LABEL_WRITE="an API issue write whose --input body is JSON-escaped (\\u00XX)"
                        fi
                        ;;
                    *)
                        if ! _read_file "$input"; then
                            LABEL_WRITE="an API issue write whose --input body the hook cannot read"
                        elif LC_ALL=C grep -qiE "labels" <<< "$FT"; then
                            LABEL_WRITE="setting issue labels through the API"
                        elif _encoded "$FT"; then
                            LABEL_WRITE="an API issue write whose --input body is JSON-escaped (\\u00XX)"
                        fi
                        ;;
                esac
            done
        fi
        [ "$comment_seg" = 1 ] || continue
        COMMENT_WRITE=1
        # Every field may carry the body (a GraphQL variable can have any
        # name); `query` was judged above.
        for ((k = 0; k < ${#fields[@]}; k++)); do
            f=${fields[k]}
            case "$f" in query=*) continue ;; esac
            v=${f#*=}
            if [ "${kinds[k]}" = typed ] && [[ "$v" == @* ]]; then
                _body_file "${v#@}"
            else
                _body_inline "$v"
            fi
        done
        for input in "${inputs[@]}"; do
            _body_file "$input" json
        done
    done < <(_segments "${GH_ANCHOR}api\b" "$WORDS")
fi

if [ -n "$LABEL_WRITE" ]; then
    _refuse "$LABEL_WRITE"
fi

[ "$COMMENT_WRITE" = 1 ] || exit 0

MARKED=$FILE_MARK
if LC_ALL=C grep -qiE "$MARK" <<< "$COMMAND"; then
    MARKED=1
    _signoff_in "$COMMAND"
fi
if LC_ALL=C grep -qiE "$MARK" <<< "$WORDS"; then
    MARKED=1
    _signoff_in "$WORDS"
fi

if [ "$IS_TRIAGE" != 1 ]; then
    if [ "$MARKED" = 1 ]; then
        _refuse "posting or editing an [issue-triage] plan-validated payload"
    fi
    if [ -n "$UNKNOWN" ]; then
        _refuse "$UNKNOWN, so it may carry an [issue-triage] plan-validated payload (write the body to a readable file and pass --body-file)"
    fi
    exit 0
fi

# --- A3: issue-triage included — no sign-off claim in a payload -----------
if [ -n "$SIGN_PHRASE" ]; then
    echo "BLOCKED: this plan-validated payload carries a sign-off claim (\"$SIGN_PHRASE\"). A human attribution nothing can corroborate never appears in a recorded plan (FOUNDATION §14 \"Decision trail\") — rephrase or strip it, then post again. The phrase is matched anywhere in the payload, so reword a harmless use too (\"checked by CI\" for \"validated by CI\")." >&2
    exit 2
fi

exit 0
