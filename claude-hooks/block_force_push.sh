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
    # One segment per real `gh api` call, bounded at the next separator.
    # A separator INSIDE parentheses is neutralised first: a `|` or `;`
    # within a `$(…)` would otherwise end the segment early and leave the
    # endpoint in a fragment no stage reads. Only the separator characters
    # change, never the text, so an endpoint spelled inside a substitution
    # stays visible. `)` is not itself a bound.
    # The scan and the extraction are separate steps, not one pipeline: in a
    # pipeline only the LAST status survives, so a scan that died would read
    # as "grep found nothing" and the call would run. A scan failure blocks;
    # only a clean scan with no match means there is nothing to judge.
    GH_SCAN=$(command_positions --words --no-redir "$COMMAND" \
        | LC_ALL=C awk '{
    out = ""; depth = 0
    for (i = 1; i <= length($0); i++) {
        c = substr($0, i, 1)
        if (c == "(") depth++
        else if (c == ")" && depth > 0) depth--
        if (depth > 0 && (c == ";" || c == "&" || c == "|")) c = "_"
        out = out c
    }
    print out
}') || {
        echo "BLOCKED: the gh-api check could not read this command, so it is refused rather than run unguarded. If a human truly intends it, they run it: ! $COMMAND" >&2
        exit 2
    }
    GH_API=$(echo "$GH_SCAN" | grep -oE "gh[[:space:]]+${GH_OPTS}api\b[^;&|]*") || GH_API=""
    # Any ref including tags: a force-moved release tag is worse than a
    # moved branch, not better.
    #
    # `git/refs` must sit in a word carrying no `=` before it, i.e. an
    # endpoint rather than a field value — otherwise a PR comment whose
    # body discusses `git/refs` would be refused, and agents write those.
    #
    # `-X` / `--method` set a method; `-f` / `-F` / `--field` /
    # `--raw-field` / `--input` carry a body. Attached forms (`-XPATCH`,
    # `-fforce=true`, `--method=PATCH`) begin with the same characters. The
    # short test allows letters BEFORE the one that counts, since a cluster
    # hides the flag otherwise — the same `-[a-zA-Z]*f` shape the git stage
    # above uses for `-uf` / `-fu`.
    #
    # The verdict is a printed token, not an exit status, so an awk that
    # dies mid-run fails CLOSED (`|| ghapi=…` below) instead of reading as
    # "nothing found". The two cases get different messages: one states a
    # ref write, the other says only that the check could not run.
    ghapi=$(echo "$GH_API" | LC_ALL=C awk '
/(^|[[:space:]])[^[:space:]=]*git\/refs/ {
    if ($0 ~ /(^|[[:space:]])[-][a-zA-Z]*(X|f|F)/) { print "write"; exit }
    if ($0 ~ /(^|[[:space:]])[-][-](method|field|raw-field|input)/) { print "write"; exit }
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
    if echo "$GH_API" | grep -qE "graphql[^;&|]*updateRef" \
        && echo "$GH_API" | grep -qE "updateRef[^;&|]*force|force[^;&|]*updateRef"; then
        echo "BLOCKED: the updateRef mutation with a force rewrites published history exactly as a force push does. Sync a branch with \`forge-update-branch\`; if a human truly intends this, they run it: ! $COMMAND" >&2
        exit 2
    fi
fi
exit 0
