bump: patch
- **One-fragment-per-PR gate no longer blocks conflicted base merges** — a fragment now counts as branch-added only when it is both added since the fork point and absent from the base branch's tip tree, so the fragments a base merge brings in (pre-commit fires while `HEAD` is still the pre-merge commit) are never mistaken for the PR's own.
