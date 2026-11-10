bump: patch

- `forge-docs/git-safety.md` now gives `git diff --cached --no-renames --name-only` as the command for listing a resolved merge's staged files. The old command paired renames, so it listed only a renamed file's new path and produced the list `forge-commit` refuses. The custom pre-commit example in `docs/customizing-precommit.md` adds the same flag, so a renamed Python file is type-checked instead of skipped.
