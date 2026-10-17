bump: minor

- `forge-precommit --verdict` no longer says PASS while untracked files went unchecked: it ends `verdict: FAIL — untracked files not checked by …`.
- `forge-precommit --freshness` reports a log nothing still writes (a removed step's leftover) as `orphaned` instead of fresh.
- A `--skip` run writes `precommit_only_timing.log`, like `--only`, so a partial run never replaces the full-run timing record.
- The `c4`, `api_digest_check` and `cli_reference_check` gates run their generator from the running forge install, not whichever copy is first on `PATH`.
- Files with non-ASCII names (`café.py`) are no longer dropped from every pre-commit file scope.
- New `[tool.forge.precommit].echo_ci_output` (default `true`): set `false` to stop CI echoing a failing step's output.
