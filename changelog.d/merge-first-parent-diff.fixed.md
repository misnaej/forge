bump: patch

- `forge-commit` no longer refuses a resolved merge commit whose branch changed files the base did not touch: the merge is now read against its first parent only, not against both parents.
