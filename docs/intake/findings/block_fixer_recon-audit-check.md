# block_fixer_recon allowlist and the fixer agent's Allowed-CLIs table can disagree silently

- surface: claude-hooks/block_fixer_recon.sh
- release: forge v15.2.2
- seen: 2026-11-25
- branch: fix/intake-1089-1090-1091

## Evidence

`src/forge/data/claude-hooks/block_fixer_recon.sh:143` (`_segment_ok` case list)
omitted `forge-gen-api-digest` while `src/forge/data/agents/precommit-fixer.md:36`
lists it in the Allowed-CLIs table. `forge-audit-agents` measures the agent file
only, never the allowlist enforced against it, so the disagreement was invisible
until the fixer hit the refusal (#1090, fixed in this branch by adding the CLI).

## What it costs

The only agent the document instructs to run the CLI is the one the guard
refuses, so each drift costs a blocked command plus a wasted full battery.

## What would fix it

A check (audit step or test) asserting the Allowed-CLIs table in
`agents/precommit-fixer.md` and the case list in `block_fixer_recon.sh` name
the same set of CLIs.
