bump: minor

- Merges to `main` no longer lose their release tag.
- `forge-pr-watch digest` reports what a monitor saw; `/forge-next` reads it.
- A monitor stops when its pull request is merged or closed.
- A standing condition is reported once per revision, not every minute.
- New comments are reported, including a second on the same revision.
- Monitor text is flattened to one printable line.
- Each report says how long ago it was written.
- Starting a monitor without naming a pull request works again.
- A detached checkout refuses rather than guessing a pull request.
- Review reports label each point: shipped, fixed, verified, deferred, blocked.
- Changelog bullets stay short; an advisory names any that run long.
- A major release must carry upgrade notes.
- Upgrading across a major with no notes says so.
- Syncing a branch with no commits of its own fast-forwards.
- A question no longer stops work when its answer was not needed.
