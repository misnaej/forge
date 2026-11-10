bump: minor

- FOUNDATION §1 gains "Authorized work starts in the same turn": once the user has given the go-ahead and an agent says it will do the work, it does it in that turn, or says what blocks it — a turn that ends on an announcement of work not yet begun is a null turn.
- FOUNDATION §6 gains "A rework returns a ready PR to draft": starting a revision round on a published, ready PR (new commits changing what the posted wrap-up describes) converts it to draft until the refreshed wrap-up posts. Answering review comments is not a revision round, so the PR stays ready.
- The after-push stale-wrap-up reminder now adds, for a ready PR, how to mark it draft if the push starts a rework; `forge-pr-wrapup post` notes when the PR it posted to is still draft, so it can be marked ready again.
