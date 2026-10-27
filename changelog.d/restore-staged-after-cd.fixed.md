bump: patch

- `block_git_destructive` no longer refuses `git restore --staged <path>` (the unstage its own refusal recommends) when the command opens with `cd <dir>;` / `cd <dir> &&` or runs under `env -C`: an index-only restore overwrites no working copy wherever it runs.
- A restore or checkout still refused behind a `cd` (e.g. `--ours` / `--theirs`) now says to drop the `cd` and use `git -C <dir>`.
