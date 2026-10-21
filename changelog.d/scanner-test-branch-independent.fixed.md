bump: patch

The hook test suite no longer fails when run from a checkout on `main` (as CI does after a merge): the scanner's protected-branch rows now test only the push destination, not the branch the checkout happens to be on.
