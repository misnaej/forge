bump: patch

- Text-length budgets now allow a 10% margin over their target: a PR wrap-up (120 words, plus 40 per finding) and a `forge-commit` message (50 words) pass with a warning up to 10% over, and are refused only beyond that. The refusal states the total, the budget, the ceiling and how much to cut, instead of "121 words over a budget of 120", which read as 121 words too many. The squash message keeps its 50/55 limits, now from the same shared margin.
