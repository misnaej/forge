bump: patch
`forge-commit` no longer reports a resolved merge commit as refused after creating it: it now checks the commit against its first parent, so a merge naming every staged path commits and pushes normally. The git-safety guide and the `/pr` skill now say to finish a conflicted merge with `forge-commit`.
