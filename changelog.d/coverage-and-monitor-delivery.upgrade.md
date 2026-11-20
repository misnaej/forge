bump: major

- A squash-merge message must now describe every changelog bullet.
- Claim them per bullet: `--bullet "[1,2] text"`; unclaimed refuses the post.
- Run `forge-pr-squash-comment --dry-run` first: it numbers the bullets.
- Length no longer refuses a squash message — over its ceiling it warns and posts.
- A PR event now reaches the session waiting on it, instead of a log.
- `forge-pr-watch await` blocks until a merge, CI failure, conflict or comment, then exits.
- Start it as a background job: its exit is what delivers the event.
- `forge-pr-watch start`, `list` and `stop` are gone; `digest` stays as the catch-up read.
