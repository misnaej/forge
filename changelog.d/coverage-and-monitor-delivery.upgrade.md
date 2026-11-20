bump: major

- If your own scripts or docs call `forge-pr-squash-comment` in a repo that uses changelog fragments, start each `--bullet` with the fragment bullets it covers, e.g. `--bullet "[1,2] text"`. Without that the post is refused until every fragment bullet is covered. Run `forge-pr-squash-comment --dry-run` to see the bullets numbered. The `[1,2]` never appears in the posted message. The forge-shipped agents and skills already do this.
- A squash-merge message over 45 words now only warns and posts; it is no longer refused for length.
- If you call `forge-pr-watch start`, `list` or `stop`, replace them: run `forge-pr-watch await --pr <N>` as a background job. It waits for a merge, CI failure, conflict or review comment, then exits, and that exit hands the event to your session. `forge-pr-watch digest` still shows what earlier waits logged.
