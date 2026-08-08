---
name: commit
description: Run the standard commit flow - precommit-fixer then git-commit-push. Use when the user wants to commit changes.
---

# Commit Flow

Run the standard commit workflow:

1. **Run `precommit-fixer`** (no file list — it scopes off `code_health/`):
   ```
   Agent(subagent_type="precommit-fixer", prompt="Clear all pre-commit failures.")
   ```

2. **Run `git-commit-push`** to stage and commit:
   ```
   Agent(subagent_type="git-commit-push", prompt="Commit changes with message: $ARGUMENTS")
   ```
   If no message provided via `$ARGUMENTS`, the agent generates an appropriate conventional commit message.

3. If pre-commit hook fails, go back to step 1.

4. **Update CONTINUATION state** for significant commits:
   - `git-commit-push` agent appends a one-line activity record to `.plan/CONTINUATION.md` automatically (gitignored).

5. **Continue into verification when the branch's work is done.** If this
   commit completes the branch's planned implementation, do NOT stop here —
   go straight into the `/pr` flow's verification steps (FOUNDATION §6
   "Verification starts itself"): the reviews are read-only and need no
   permission. Skip only when the user said the work continues (more commits
   coming) or explicitly deferred finalization.

## Push behavior

**Push by default** after commit succeeds. Skip the push only when:
- `$ARGUMENTS` contains `--no-push` or `--local-only`
- The current branch is `main` (the `block_protected_branches` hook already prevents this case for agents).
- The user explicitly says "commit only" / "no push"

The default push goes to the current branch's tracking remote (or sets up the
remote with `git push -u origin <branch>` if the branch isn't tracked yet).
