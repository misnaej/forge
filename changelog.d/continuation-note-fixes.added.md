bump: minor
- `forge-continuation` gains a `reset` subcommand: replaces the written section with the five §10 headings, regenerates the status panel, never deletes the file. Refuses while the panel shows live work (unpushed commits, dirty tree, open PR) unless `--force`; self-skips in CI.
- A bare `forge-continuation state` run (no `--with-pr`) now reports every PR field as `unknown` instead of carrying the previous run's answer forward under a fresh header — a session can no longer act on a PR that merged hours earlier.
- The documented refresh paths (`block_continuation_delete.sh`, `/next` skill) now name `forge-continuation state --with-pr`.
