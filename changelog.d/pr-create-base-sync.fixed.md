bump: patch

- `forge-pr-create` now refuses a branch that is behind or conflicting with its base, before the pull request exists. The base often moves while verification runs, and the wrap-up post already refused such a branch — but only after the PR was public and reviewers notified, leaving it with no description. Same rule as the post: an emergency publication may be behind, never conflicting; an unreadable base is a warning.
