---
name: triage
description: Triage GitHub issues - prioritize backlog, recommend next work items. Use when the user wants to plan what to work on next.
---

# Issue Triage

Run the `issue-triage` agent to manage the issue backlog:

```
Agent(subagent_type="issue-triage", prompt="$ARGUMENTS")
```

If no `$ARGUMENTS` provided, default to: "Run triage mode: walk all open issues, apply tier labels where missing, and regenerate the 📋 Backlog Index issue."

The agent maintains a single pinned `📋 Backlog Index` GitHub issue per repo,
rebuilt from live `gh` data on every `triage` run (FOUNDATION §14). There is no
markdown backlog file — GitHub is the canonical backlog.
