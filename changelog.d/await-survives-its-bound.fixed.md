bump: patch

- Waiting on a pull request no longer stops after one window.
- It re-arms until the PR ends or `--total-bound` is spent.
- `--bound` now sizes one window; `--once` keeps the old single wait.
