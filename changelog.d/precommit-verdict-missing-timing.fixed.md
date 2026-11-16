bump: patch
- `forge-precommit --verdict` now reports an absent `code_health/precommit_timing.log` as `MISSING precommit_timing (no full run recorded)` with closing cause `missing: precommit_timing`, instead of the wrong `STALE … describes another tree`
