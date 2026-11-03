bump: patch

- A PR that only records or retires intake sightings no longer demands a prior-art report: `forge-pr-plan` and `forge-pr-wrapup compose` treat intake records (an entry under `lessons/` or `findings/`, or the `_LEDGER.md`) like changelog fragments — exempt by exact shape, so any other new file still needs review.
