---
name: test
description: Write tests for a target by chaining the forge test agents - test-advisor recommends, test-writer writes, test-advisor reviews, then precommit-fixer cleans. Use when the user wants tests written for a module, path, or feature.
---

# Test-Writing Flow

Write tests for the target in `$ARGUMENTS` (a module, a path, or a feature
description) by chaining the forge test agents in order. This flow only
sequences forge-owned agents + the forge testing-documentation policy —
nothing consumer-specific.

1. **`forge:test-advisor`** — analyze the target and recommend tests:
   ```
   Agent(subagent_type="forge:test-advisor", prompt="Analyze the target and recommend tests for: $ARGUMENTS")
   ```

2. **`forge:test-writer`** — write the tests per those recommendations:
   ```
   Agent(subagent_type="forge:test-writer", prompt="Write the tests test-advisor recommended for: $ARGUMENTS\n\n<paste the advisor's recommendations>")
   ```

3. **`forge:test-advisor`** (review pass) — check the written tests against the
   forge testing-documentation policy:
   ```
   Agent(subagent_type="forge:test-advisor", prompt="Review the tests just written for $ARGUMENTS against the forge testing-documentation policy. Report any violations.")
   ```
   If the review flags issues, loop back to step 2 to address them before
   continuing.

4. **`forge:precommit-fixer`** — clear lint / docstring violations in the new
   test files:
   ```
   Agent(subagent_type="forge:precommit-fixer", prompt="Clear all pre-commit failures.")
   ```

## Notes

- The agents are foundation agents (`forge:` prefix). A consumer that layers
  stricter test rules ships a wrapper agent (e.g. `test-writer-<scope>`) per
  FOUNDATION §3 / §16 and points this flow at it.
- **Does NOT commit.** After the tests pass review, run `/forge:commit` (or the
  `commit` skill) to commit them — same split as every other forge skill.
