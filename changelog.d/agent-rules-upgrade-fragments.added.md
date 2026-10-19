bump: minor

- New `upgrade` changelog fragment type (`changelog.d/<slug>.upgrade.md`), assembled first under **⚠️ Upgrade notes** — the lane `forge-upgrade` already reads and the changelog tells consumers to read first, which no fragment could produce until now.
- FOUNDATION §16 Pattern A: a wrapper around a reporter agent delegates in the foreground and returns the base agent's report as its own result.
- The agent template's scope boundaries now say to revise your own edit by editing again, never with a tree-wide restore.
- `/plan-issue` runs `forge:prior-art` for every new file, CLI or symbol a plan proposes and carries the verdict into the plan; `/plan-batch` runs it for drafters that cannot delegate.
- `forge-agent-profile`: the subagent transcript name shape lives in one set of helpers, and an edit receipt that could not be read carries no file map at all, so it cannot be mistaken for a clean one.
