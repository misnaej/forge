bump: patch

- **Unrelated PRs no longer conflict on the API digest.** `docs/api-digest.md` carried a line counting every module and symbol in the repo, so any branch that added a function anywhere rewrote it, and two unrelated branches collided on that one line at every base sync. The line is gone; the totals are still printed when the digest is generated. Your committed digest loses the line the next time it is regenerated.
