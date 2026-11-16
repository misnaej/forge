bump: patch
- `forge-pr-wrapup compose` now quarantines the previous `code_health/pr_wrapup.md` to `pr_wrapup.stale.md` (with a refusal header) on exit-2 refusals, and deletes it on the next successful compose — a refused compose can no longer leave a stale wrap-up postable as-is
