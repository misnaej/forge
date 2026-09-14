---
name: commit
description: Run the standard commit flow - precommit-fixer, then the forge-commit CLI. Use when the user wants to commit changes.
user-invocable: true
---

# Commit Flow

Run the standard commit workflow:

1. **Run `precommit-fixer`** (no file list — it scopes off `code_health/`):
   ```
   Agent(subagent_type="forge:precommit-fixer", prompt="Clear all pre-commit failures.")
   ```

2. **Commit with `forge-commit`** — one direct Bash call, no subagent:
   ```bash
   forge-commit -m "<message>" --all      # or name the paths instead of --all
   ```
   - **Message**: `$ARGUMENTS` minus any push flags (below). When it gives
     none, write the conventional commit message yourself (FOUNDATION §6
     "Commit messages"). For a multi-line message, or one that names a git
     command (text-matching hooks read the whole Bash line), write it to a
     scratch file and pass `-F <file>`.
   - **Selection**: `--all`, or the paths the user named.
   - **Finishing a merge**: omit `-m` and paths — git's prepared merge
     message is used.
   - What the CLI checks before committing: `forge-commit --help`. Do not
     pre-check by hand.

3. **Act on the exit code:**
   - `0` — committed, pushed (unless `--no-push`), and recorded; the CLI
     appends the `.plan/CONTINUATION.md` activity line itself.
   - `2` with `refused — <reason>` — nothing committed; do what the reason
     names.
   - `2` with the pre-commit hook's report — the hook blocked; go back to
     step 1, then commit again.
   - `1` — the commit landed, but the push or a record step failed; the
     output says which. After fixing a failed push, run
     `forge-commit --push-only`.

4. **Continue into verification when the branch's work is done.** If this
   commit completes the branch's planned implementation, do NOT stop here —
   go straight into the `/pr` flow's verification steps (FOUNDATION §6
   "Verification starts itself"): the reviews are read-only and need no
   permission. Skip only when the user said the work continues (more commits
   coming) or explicitly deferred finalization.

## Push behavior

**Push by default** after the commit succeeds. Pass `--no-push` only when:
- `$ARGUMENTS` contains `--no-push` or `--local-only`
- The user explicitly says "commit only" / "no push"

`forge-commit` refuses to commit on the base branch itself, and sets the
upstream on the first push of a branch that has none.
