---
name: weekly
description: Generate a weekly summary of developer GitHub activity - PRs, issues, commits grouped by theme. Use when the user wants a weekly report.
---

# Weekly Summary

Run the `weekly-summary` agent:

```
Agent(subagent_type="weekly-summary", prompt="Generate weekly developer summary. $ARGUMENTS")
```

If no `$ARGUMENTS` provided, default to summarizing activity since last Monday.

If the user specifies a date or time range, pass it through.
