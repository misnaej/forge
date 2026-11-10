bump: patch

- `forge-commit` can now commit a renamed file. A rename counts as its old and new path in every check, so naming both commits it; naming only the new path is refused before anything is committed, with a hint to name both. Previously one of its two checks always refused a staged rename, sometimes after the commit already existed.
