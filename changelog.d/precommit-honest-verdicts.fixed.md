bump: minor

- `forge-precommit --verdict` no longer says PASS while untracked files went unchecked: it ends `verdict: FAIL — untracked files not checked by …`.
- `forge-precommit --freshness` reports a log nothing still writes (a removed step's leftover) as `orphaned` instead of fresh.
- A `--skip` run is a partial run, like `--only`: it writes `precommit_only_timing.log` and leaves the continuation note's last-commit-attempt panel alone, so it never replaces full-run evidence.
- The `c4`, `api_digest_check` and `cli_reference_check` gates run their generator from the running forge install, not whichever copy is first on `PATH`.
- Files with non-ASCII names (`café.py`) are no longer dropped from every pre-commit file scope.
- New `[tool.forge.precommit].echo_ci_output` (default `true`): set `false` to stop CI echoing a failing step's output.
