#!/usr/bin/env bash
# Block force push without explicit user approval.
#
# All five force vectors are caught, not just the long flags:
#   --force / --force-with-lease   long flags (incl. =value forms)
#   -f  /  -uf  /  -fu             a short-flag cluster containing `f`
#   origin +main / +src:dst        a `+`-prefixed (force) refspec token
#   gh api … git/refs …            a ref write asked of GitHub, which needs no
#                                  local git and no push at all: any method
#                                  or body flag on a REST `git/refs` call,
#                                  or a GraphQL updateRef carrying a force
# `git push` short flags never contain `f` except `--force`, and a leading
# `+` on a whitespace-delimited arg is only ever a force refspec, so these
# patterns don't over-block a benign push. A push through a variable in
# the command-word slot (`$G push -f`) is judged the same way: the hook
# cannot tell whether the variable runs git, so force flags there block.
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
# Anchor + rationale live in the shared lib (one home for the whole
# git-guard family — issue #348).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache)
    # must block, not silently disarm the whole guard family — only
    # exit 2 is a block signal in the PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"
# Fail CLOSED on a lib that predates a helper this hook calls.
if ! declare -F guard_begin command_positions invoked guard_help_only guard_gh_api_calls > /dev/null; then
    echo "BLOCKED: the guard anchor lib at $ANCHOR_LIB lacks a helper this hook needs (older or damaged plugin copy) — refusing the command rather than running unguarded." >&2
    exit 2
fi
guard_begin
# Find the push on the command-positions view (quoted mentions are not
# pushes; `bash -c "git push -f"` is), then read its flags from the
# --words view, where a quoted `"--force"` is still the flag git receives.
#
# A variable in the command-word slot (`$G push -f`, GIT_UNKNOWN_ANCHOR
# in git_anchor.sh) may be git, so a push there is judged the same way —
# blocked when the force flags are present, allowed otherwise. The
# accepted false block is a non-git program held in a variable and given
# a `push` verb and a force-shaped flag.
CMDPOS=$(command_positions "$COMMAND")
if [ -z "${GIT_UNKNOWN_ANCHOR:-}" ]; then
    echo "BLOCKED: the guard anchor lib at $ANCHOR_LIB lacks a pattern this hook needs (older or damaged plugin copy) — refusing the command rather than running unguarded." >&2
    exit 2
fi
PUSH_ANCHOR="(${GIT_ANCHOR}|${GIT_UNKNOWN_ANCHOR})push\b"
# The git stage runs only when a push is present, but it must FALL THROUGH
# rather than exit when there is none: the gh stage below covers a command
# that never says `push` at all.
if echo "$CMDPOS" | grep -qE "${PUSH_ANCHOR}" \
    && ! guard_help_only "$CMDPOS" "${PUSH_ANCHOR}"; then
    WORDS=$(command_positions --words --no-redir "$COMMAND")
    # Force-flag detection is scoped to the matched invocation (bounded at
    # the next command separator), so a flag in another segment of a
    # compound command never false-positives.
    # Redirections are blanked (--no-redir), so the `&` of `2>&1` is not
    # taken for that separator.
    if echo "$WORDS" | grep -qE -- \
        "${PUSH_ANCHOR}[^;&|]*(--force|--force-with-lease|[[:space:]]-[a-zA-Z]*f[a-zA-Z]*\b|[[:space:]]\+[^[:space:]]+)"; then
        echo "BLOCKED: Force push is not allowed for agents. Suggest the user run the command themselves with: ! $COMMAND" >&2
        exit 2
    fi
fi

# Fifth vector: a ref moved on the SERVER through the API, with no local git
# and no push for the stage above to see. Same effect — the ref's old
# commits are left behind — so it belongs to this guard.
#
# The stage reads no flag VALUES, by design. A hook cannot match gh's own
# flag parsing — `-f` takes the whole following word, so `-fforce=true` has
# no `=` split; a short cluster can hide a flag (`-iXPATCH` is `-i` plus
# `-X PATCH`); gh honours the LAST repeated `force=`. So the rule is
# coarse: a call that names `git/refs` in its endpoint and carries ANY
# method or body flag is refused, whatever the values say. A ref write that
# cannot be told apart from a force is treated as one.
#
# Rationale, costs and the routes this cannot see:
# forge-docs/git-safety.md "Ref writes through the API" — the one home for
# them; this header states the rule the code enforces.
#
# A pure READ stays allowed: `gh api repos/o/r/git/refs/heads/x`, with
# `-q` / `--jq` / `--paginate`, carries no method or body flag.
if invoked "${GH_ANCHOR}api\b"; then
    # One row per real `gh api` call comes from the shared parser
    # (git_anchor.sh guard_gh_api_calls): METHOD<TAB>ENDPOINT<TAB>FIELDS,
    # with separators inside a `$(...)` already neutralised there. A parser
    # failure exits non-zero and blocks — a scan that died must never read
    # as "nothing found".
    GH_ROWS=$(guard_gh_api_calls "$COMMAND") || GH_ROWS="__unreadable__"
    if [ "$GH_ROWS" = "__unreadable__" ]; then
        echo "BLOCKED: the gh-api check could not read this command, so it is refused rather than run unguarded. If a human truly intends it, they run it: ! $COMMAND" >&2
        exit 2
    fi
    # Any ref including tags: a force-moved release tag is worse than a
    # moved branch, not better.
    #
    # `git/refs` must sit in a word carrying no `=` before it, i.e. an
    # endpoint rather than a field value — otherwise a PR comment whose
    # body discusses `git/refs` would be refused, and agents write those.
    #
    # The rule stays coarse, as before: a call whose endpoint names
    # `git/refs` and that carries ANY method (-X/--method, in any spelling
    # the parser resolves) or ANY body field (-f/-F/--field/--raw-field/
    # --input) is refused, whatever the values say. A ref write that cannot
    # be told apart from a force is treated as one.
    #
    # The verdict is a printed token, not an exit status, so an awk that
    # dies mid-run fails CLOSED (`|| ghapi=...` below) instead of reading as
    # "nothing found". The two cases get different messages: one states a
    # ref write, the other says only that the check could not run.
    ghapi=$(printf '%s\n' "$GH_ROWS" | LC_ALL=C awk -F'\t' '{
        text = $2 " " $3
        if (text !~ /(^|[[:space:]])[^[:space:]=]*git\/refs/) next
        if ($1 != "" || $3 != "") { print "write"; exit }
    }') || ghapi="unreadable"
    if [ "$ghapi" = "unreadable" ]; then
        echo "BLOCKED: the gh-api ref check could not run, so this command is refused rather than run unguarded. If a human truly intends it, they run it: ! $COMMAND" >&2
        exit 2
    fi
    if [ -n "$ghapi" ]; then
        echo "BLOCKED: this writes a ref through the API — GitHub moves it with no push for the git guards to see, and a hook cannot tell a force from a fast-forward there, so every API ref write is refused. Sync a branch with \`forge-update-branch\`; reading refs is fine. If a human truly intends this, they run it: ! $COMMAND" >&2
        exit 2
    fi
    # GraphQL: `updateRef` carrying a force at all. There is no
    # allow-on-false path: a `force:false` token elsewhere in the command
    # must not disarm it. `refId` is an opaque node id, so this stage can
    # say a force update was requested but never which ref it names.
    if printf '%s\n' "$GH_ROWS" | LC_ALL=C awk -F'\t' '
        { if ($2 ~ /^graphql([[:space:]]|$)/ && $3 ~ /updateRef/ && $3 ~ /force/) { print "gql"; exit } }' \
        | grep -q gql; then
        echo "BLOCKED: the updateRef mutation with a force rewrites published history exactly as a force push does. Sync a branch with \`forge-update-branch\`; if a human truly intends this, they run it: ! $COMMAND" >&2
        exit 2
    fi
fi
exit 0
