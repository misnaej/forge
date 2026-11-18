# The Vibe reference report lists block markers as forge references

- surface: gen-vibe-agents
- release: forge 14.3.0
- seen: 2026-11-18
- branch: fix/marker-not-a-reference

## Evidence

`src/forge/gen_vibe_agents.py:155` uses `PLUGIN_REFERENCE_RE` directly
(`.search` + `.findall`) with no marker exclusion, so a managed-block
marker (`forge:c4:start`, `forge:state:begin`, `forge:config:end`,
`forge:badges:start`) is reported as a `forge:` reference.

Both reviewers raised it independently while reviewing the marker fix
(`match_is_marker`, `src/forge/agent_harness.py`). It did not fire in
practice: the helper reports only, gates nothing, rewrites nothing.

## What it costs

A false line in a reference report. No behaviour change — the mapper and
the two leak checks all route the decision through `match_is_marker`.

## What would fix it

`match_is_marker(match)` at that call site, same as the leak checks. One
import, one condition; the colon rule stays in one place.

NOT done here deliberately: `gen_vibe_agents.py` is owned by the
concurrent #1044 implementation (pruning stale unprefixed Vibe skills
and prompts). Touching it risked a merge conflict for a cosmetic
reporting defect. File it only if it reproduces as a real false leak,
per FOUNDATION §1 — otherwise fold the one-line guard into whatever
next edits that file.
