bump: minor
- `forge-precommit` now refuses to run inside a linked git worktree (exit 2, `--git-common-dir` != `--git-dir`) with an error naming `forge-scratch-repo`, instead of silently resolving hooks and `code_health/` against the worktree; FOUNDATION §11 probing rule now states the scratch-repo-over-worktrees preference (word-neutral swap)
