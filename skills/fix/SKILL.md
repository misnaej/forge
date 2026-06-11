---
name: fix
description: Run precommit-fixer to clear every blocking pre-commit failure (lint, docstrings, naming, structure, dep advisories). Use when code needs cleanup before committing.
---

# Fix Code Quality

Run the `precommit-fixer` agent. It runs `forge-precommit`, reads `code_health/*.log`, and dispatches each failure to the right fixer (Edit, `docs-types-checker`, `design-checker`). It takes no file list and no rule selection — its scope is whatever the report flagged.

```
Agent(subagent_type="precommit-fixer", prompt="Clear all pre-commit failures.")
```

If `$ARGUMENTS` is `strict`, run in strict mode (also escalates remaining `pip_audit` advisories as failures):

```
Agent(subagent_type="precommit-fixer", prompt="Clear all pre-commit failures. mode: strict")
```

**Never** invoke `ruff`, `verify-forge-ruff*`, or `.githooks/pre-commit` from this skill or any caller — `precommit-fixer` is the sole entry point.
