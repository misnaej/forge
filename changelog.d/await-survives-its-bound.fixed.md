bump: patch

- Waiting on a pull request no longer stops after one window.
- It re-arms until the PR ends or `--total-bound` is spent, six hours by default.
- `--bound` now sizes one window; `--once` keeps the old single wait.
- `--interval` under 30 seconds is refused, as is a `--bound` below one interval or above `--total-bound`.
