#!/usr/bin/env bash
# Block agent-initiated DELETION of the protected remote branch
# (base_branch, default `main`).
#
# Why this exists separately from block_raw_git / block_protected_branches:
#   - block_raw_git blocks raw `git push` from Bash, BUT the
#     forge:git-commit-push agent bypasses it — so a delete-push could
#     still get through that agent. This hook has NO bypass.
#   - block_raw_git is git-only; `gh api -X DELETE .../branches/...` is a
#     gh call it never sees. This hook covers the gh-api delete path too.
#   - Server-side rulesets that "restrict deletions" do NOT stop a
#     privileged (bypass-actor) account — and an agent runs with the
#     user's credentials. The client-side guard is the only thing that
#     reliably catches a base-branch delete before it leaves the machine.
#
# Scope: REMOTE deletion only. Local `git branch -d/-D` is untouched
# (forge-next-prep prunes local [gone] branches with `-d` by design).
#
# Deletion forms caught (per command segment):
#   - git push <remote> --delete|-d <branch>     (and --delete=)
#   - git push <remote> :<branch> / :refs/heads/<branch>
#   - git push <remote> --mirror | --prune       (can delete ANY remote ref)
#   - gh api (-X|--method)[ =]DELETE .../(refs/heads/|branches/)<branch>
#
# No agent_type bypass: there is no legitimate reason for ANY agent to
# delete the base branch. If a human truly intends it, they run the command
# directly with `! ...` (user shell commands do not pass through agent
# PreToolUse hooks).
#
# Reads `[tool.forge]` via an inline python3 heredoc (no forge-scripts
# import) so the hook works without forge installed. Default-deny on a
# recognized delete that targets a protected name; default protected
# branch is `main` on any parse failure.
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$COMMAND" ] && exit 0
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
guard_begin

# Cheap bail: no real git push or gh api at all. Found on the shared
# command-positions view, so a quoted mention never counts and
# `bash -c "git push origin :main"` does.
CMDPOS=$(command_positions "$COMMAND")
if ! echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}push\b" \
    && ! echo "$CMDPOS" | grep -qE "${GH_ANCHOR}api\b"; then
    exit 0
fi

# Split on shell separators so a ':main' / 'DELETE' token in one segment
# (e.g. a commit message) can never trip a push/api in another segment.
# `||` before `|` so a double-pipe collapses to one break. Segments come
# from the --words view: a separator inside a quoted argument never splits
# an invocation, and a quoted refspec or endpoint is still read.
# Redirections are blanked, so the `|` of `>|` splits nothing.
SEGMENTS=$(command_positions --words --no-redir "$COMMAND" | sed -E 's/(&&|\|\||;|\|)/\n/g')

REPO_ROOT=$(echo "$INPUT" | jq -r '.cwd // empty')
if [ -z "$REPO_ROOT" ] || ! echo "$REPO_ROOT" | grep -qE '^/'; then
    REPO_ROOT="."
fi

# Protected branch: base_branch (default main).
protected=$(python3 - "$REPO_ROOT" <<'PY' 2>/dev/null || printf 'main\n'
import sys
from pathlib import Path
try:
    import tomllib
except ImportError:
    print("main"); raise SystemExit(0)
root = Path(sys.argv[1])
pp = root / "pyproject.toml"
if not pp.is_file():
    print("main"); raise SystemExit(0)
try:
    data = tomllib.loads(pp.read_text())
except Exception:
    print("main"); raise SystemExit(0)
sec = data.get("tool", {}).get("forge", {})
print(sec.get("base_branch", "main"))
PY
)

# A push segment: `git push` anywhere in it, or a git invocation the shared
# anchor recognises — which reads past git's global options, so neither
# `git -C x push` nor `git --git-dir=y push` hides the push.
PUSH_RE="git[[:space:]]+push|${GIT_ANCHOR}push([[:space:]]|\$)"
GH_API_RE="gh[[:space:]]+${GH_OPTS}api"

# Every segment is judged in ONE awk pass. A loop that started processes
# per segment let a few hundred harmless segments (`git push origin x;`
# repeated) run this hook past its timeout, and a timed-out hook does not
# block. Prints `<branch><TAB><reason>` for the first deletion found.
#
# A push's words are read the way git reads them: long options by any
# prefix git accepts (`--del`, `--mir`, `--pru`), `--opt=value`, short
# clusters (`-fd` deletes), the value of `-o`/`--repo`/`--receive-pack`/
# `--exec`/`--push-option`, and `--` ending the options. Then:
#   - `--mirror` / `--prune`: can delete any remote ref — blocked;
#   - delete mode (`--delete`/`-d`): ANY positional word naming a
#     protected branch blocks — `--delete origin main` names the remote
#     first;
#   - a refspec with an empty source (`:main`, `+:main`) deletes its
#     destination.
# A branch is compared after dropping a leading `+`, `refs/heads/` or
# `heads/`, so `main` never matches `main-thing`.
verdict=$(printf '%s\n' "$SEGMENTS" | LC_ALL=C awk \
    -v PUSHRE="$PUSH_RE" -v GHRE="$GH_API_RE" -v PROTECTED="$protected" '
function norm(w) {
    sub(/^\+/, "", w); sub(/^refs\/heads\//, "", w); sub(/^heads\//, "", w)
    return w
}
function isprot(w,   i) {
    w = norm(w)
    for (i = 1; i <= NP; i++) if (w != "" && w == P[i]) return P[i]
    return ""
}
function hit(b, why) { printf "%s\t%s\n", b, why; found = 1; exit }
BEGIN { NP = split(PROTECTED, P, /[[:space:]]+/); found = 0 }
{
    # A subshell or substitution bracket is not part of a word: `$(git
    # push origin :main)` names `:main`, not `:main)`.
    seg = $0
    gsub(/[()]/, " ", seg)
    if (match(seg, PUSHRE)) {
        n = split(substr(seg, RSTART + RLENGTH), T, /[[:space:]]+/)
        del = 0; mir = 0; endopt = 0; skip = 0; np = 0
        delete POS
        for (i = 1; i <= n; i++) {
            t = T[i]
            if (t == "") continue
            if (skip) { skip = 0; continue }
            if (!endopt && t == "--") { endopt = 1; continue }
            if (!endopt && substr(t, 1, 2) == "--") {
                name = t; val = ""; hasval = 0
                if (index(t, "=")) { name = substr(t, 1, index(t, "=") - 1); val = substr(t, index(t, "=") + 1); hasval = 1 }
                if (name ~ /^--(de|del|dele|delet|delete)$/) { del = 1; if (val != "") POS[++np] = val; continue }
                if (name ~ /^--(mi|mir|mirr|mirro|mirror|pru|prun|prune)$/) { mir = 1; continue }
                if (!hasval && name ~ /^--(repo|receive-pack|exec|push-option)$/) skip = 1
                continue
            }
            if (!endopt && t ~ /^-[^-]/) {
                if (t ~ /d/) del = 1
                if (t ~ /o$/) skip = 1
                continue
            }
            POS[++np] = t
        }
        if (mir) hit("protected", "git push --mirror/--prune can delete remote branches")
        for (i = 1; i <= np; i++) {
            p = POS[i]
            if (del && (b = isprot(p)) != "") hit(b, "git push deletes \x27" b "\x27")
            c = index(p, ":")
            if (c && (substr(p, 1, c - 1) == "" || substr(p, 1, c - 1) == "+") && (b = isprot(substr(p, c + 1))) != "")
                hit(b, "git push deletes \x27" b "\x27")
        }
    }
    if (match(seg, GHRE) && toupper(seg) ~ /(-X|--METHOD)[[:space:]=]*DELETE/) {
        for (i = 1; i <= NP; i++)
            if (P[i] != "" && seg ~ ("(refs/heads/|/branches/)" P[i] "/?([^A-Za-z0-9._/-]|$)"))
                hit(P[i], "gh api DELETE of \x27" P[i] "\x27")
    }
}') || verdict="protected	the deletion check could not run"
blocked="${verdict%%	*}"
reason="${verdict#*	}"

if [ -n "$blocked" ]; then
    echo "BLOCKED: refusing to delete a protected remote branch (${reason}). Deleting the base branch is irreversible and bypasses server-side rulesets when run with a privileged account (an agent runs with your credentials). If this is genuinely intended, run it yourself: ! ${COMMAND}" >&2
    exit 2
fi
exit 0
