# Guard helper dependencies are inferred from script text, not declared

- surface: hook_registry
- release: forge 14.6.2.dev3+g8329665e2.d20261120
- seen: 2026-11-20
- branch: feat/coverage-and-monitor-delivery

## Evidence

`src/forge/hook_registry.py:48` (`_SH_REF`) discovers sourced helpers by
regex over shipped script text. Consumed at `hook_registry.py:395`
(`required_scripts`); copied by `src/forge/install_claude.py:338`
(`_publish_hooks`). Pattern matched basenames only; every guard sources
`$(dirname "$0")/lib/command_utils.sh` since #1066. Matched nothing →
helper never in required set → installer never copied it → all 38 guards
exited 2. Repo wedged: Bash and Write both refused, including the repair.
Fixed in #1067 (v14.7.0) by widening pattern + two regression tests.

Blind spots remain. Discovery needs the literal `$(dirname "$0")/` form.
Invisible: `${BASH_SOURCE%/*}`, `$(dirname "${BASH_SOURCE[0]}")`,
single-quoted form, any source through a variable whose assignment is not
that literal. `validate_shipped_scripts` fails closed only on what the
regex SAW; matching nothing is indistinguishable from "no helpers".

## What it costs

Silent mode, not loud. A helper added under a form the regex misses ships
with no file, and the failure surfaces as every guard refusing every
command — after install, in the consumer's repo, with no missing-file
name to blame. Recovery needs a hand-copy, because the guards block the
tools that would repair them.

Each guard also states the path itself (`block_force_push.sh:21,30`), so
the dependency lives in 38 scripts, not in the wiring that owns wiring.

## What would fix it

Declare helpers in `data/hooks.toml` beside the wiring they serve; keep
the text scan as a cross-check that every declared helper really is
sourced, and that no sourced-looking path is undeclared. A typo then
fails validation instead of vanishing.

Cost: 38 guards to migrate, and one fact in two places (DRY), which the
cross-check mitigates rather than removes.

Not filed as an issue: one incident, now fixed, and the remaining gap is
hypothetical (no helper uses an unmatched form today). File if a second
sighting lands.
