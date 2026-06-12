#!/usr/bin/env bash
# Block agent-initiated DELETION of the protected remote branches
# (base_branch + dev_branch, default `main` + `dev`).
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
#     reliably catches a main/dev delete before it leaves the machine.
#
# Scope: REMOTE deletion only. Local `git branch -d/-D` is untouched
# (forge-next-prep prunes local [gone] branches with `-d` by design).
#
# No agent_type bypass: there is no legitimate reason for ANY agent to
# delete main/dev. If a human truly intends it, they run the command
# directly with `! ...` (user shell commands do not pass through agent
# PreToolUse hooks).
#
# Reads `[tool.forge]` via an inline python3 heredoc (no forge-scripts
# import) so the hook works without forge installed. Default-deny on a
# recognized delete that targets a protected name; default protected set
# is `main` + `dev` on any parse failure.
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$COMMAND" ] && exit 0

# Fast path: only git-push and gh-api commands can delete a remote branch.
is_git_push=false
is_gh_delete=false
echo "$COMMAND" | grep -qE '(^|[;&|]|&&)[[:space:]]*git[[:space:]]+push' && is_git_push=true
if echo "$COMMAND" | grep -qE 'gh[[:space:]]+api' \
    && echo "$COMMAND" | grep -qiE '(-X|--method)[[:space:]]+DELETE'; then
    is_gh_delete=true
fi
[ "$is_git_push" = false ] && [ "$is_gh_delete" = false ] && exit 0

# For git push, only a deletion form is relevant (--delete / -d / :refspec).
git_delete=false
if [ "$is_git_push" = true ] && echo "$COMMAND" \
    | grep -qE '(--delete([[:space:]=]|$)|[[:space:]]-d([[:space:]]|$)|[[:space:]]:)'; then
    git_delete=true
fi
[ "$git_delete" = false ] && [ "$is_gh_delete" = false ] && exit 0

REPO_ROOT=$(echo "$INPUT" | jq -r '.cwd // empty')
if [ -z "$REPO_ROOT" ] || ! echo "$REPO_ROOT" | grep -qE '^/'; then
    REPO_ROOT="."
fi

# Protected branches: base_branch + dev_branch (default main + dev).
protected=$(python3 - "$REPO_ROOT" <<'PY' 2>/dev/null || printf 'main\ndev\n'
import sys
from pathlib import Path
try:
    import tomllib
except ImportError:
    print("main"); print("dev"); raise SystemExit(0)
root = Path(sys.argv[1])
pp = root / "pyproject.toml"
if not pp.is_file():
    print("main"); print("dev"); raise SystemExit(0)
try:
    data = tomllib.loads(pp.read_text())
except Exception:
    print("main"); print("dev"); raise SystemExit(0)
sec = data.get("tool", {}).get("forge", {})
base = sec.get("base_branch", "main")
dev = sec.get("dev_branch", "dev")
print(base)
if dev != base:
    print(dev)
PY
)

# Trailing word-boundary so "main" does not match "main-thing"/"mainframe".
bound='([^A-Za-z0-9._/-]|$)'
blocked=""
for b in $protected; do
    if [ "$git_delete" = true ] && echo "$COMMAND" | grep -qE \
        "((--delete[[:space:]=]+|[[:space:]]-d[[:space:]]+)(refs/heads/)?${b}${bound}|[[:space:]]:(refs/heads/)?${b}${bound})"; then
        blocked="$b"; break
    fi
    if [ "$is_gh_delete" = true ] && echo "$COMMAND" | grep -qE \
        "(refs/heads/|/branches/)${b}${bound}"; then
        blocked="$b"; break
    fi
done

if [ -n "$blocked" ]; then
    echo "BLOCKED: refusing to delete the protected remote branch '${blocked}'. Deleting main/dev is irreversible and bypasses server-side rulesets when run with a privileged account (an agent runs with your credentials). If this is genuinely intended, run it yourself: ! ${COMMAND}" >&2
    exit 2
fi
exit 0
