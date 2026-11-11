bump: major
- New `forge-update-branch`: brings a branch up to date with its base in one call. It checkpoints a dirty tree, merges, and resolves the predictable conflicts itself — files the branch already holds (every stacked branch after its parent is squash-merged), generated artifacts and the version slot — then commits and pushes. Conflicts that need judgment stop it (exit 3) with an intake lesson recording the stop; `forge-update-branch --continue` finishes once they are resolved, checking that nothing outside the conflicted files changed and running the pre-commit gate when files were edited by hand. A merge resolved entirely to one side now commits; before, no command could commit it.
- New agent `forge:merge-resolver`, spawned after such a stop: it reads both sides, edits the conflicted files, escalates what it cannot determine, and finishes with `--continue`. It never commits or merges itself.
- **Breaking:** `forge-commit` refuses while a merge is in progress and names `forge-update-branch --continue`; it no longer commits merges. `forge-resync --resolve-conflicts` and `--dry-run` are removed (the command resolves generated artifacts itself). The `warn_generated_conflicts` hook is now `warn_merge_conflicts`. Migration — grep your own docs and rules for each superseded form and replace it:
  - `git merge origin/<base>` (base sync) → `forge-update-branch` (`--base <parent>` on a stacked branch);
  - `forge-resync --resolve-conflicts` → `forge-update-branch` (generated files are resolved for you);
  - `git diff --cached --no-renames --name-only` and "name every staged path" (finishing a merge with `forge-commit`), and "never unstage on a merge" → `forge-update-branch --continue`;
  - `forge-commit` during a merge → refused; `forge-update-branch --continue`;
  - the human `! git commit` for a merge resolved to one side → no longer needed.
  `forge-update-branch` needs no open PR: a stacked branch's first sync, before publication, works the same.
