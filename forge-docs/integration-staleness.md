# Integration staleness

How a running session ends up on old forge agents, how to spot it, how to
recover. Policy: [FOUNDATION §11 "Generated-integration staleness"](../FOUNDATION.md#11-agent-boundary-protocol).

## What goes stale, and what cannot

Forge's agents, skills and guards are **generated into the repo** —
`install-forge-claude` writes `.claude/`, `install-forge-vibe` writes
Vibe's equivalents — from the `forge-scripts` package in the
environment. So there is no install to drift from a release, no
marketplace to fetch from, and no cache to go cold.

Two things can still be out of step:

1. **The files on disk vs. the installed package.** Someone upgraded
   `forge-scripts` without regenerating, or hand-edited a generated
   file.
2. **A running session vs. the files on disk.** The session loaded what
   was there at **startup** and does not follow later writes.

## Loud symptom: missing or misnamed agent

- Symptom: `Agent type '<name>' not found` though the file is on disk.
  The session started before the file existed.
- Recovery: **restart the session.** Reload does not reliably rebind
  skills or monitors.
- Regenerating first is harmless and usually unnecessary: the
  `post-merge` and `post-checkout` hooks already ran
  `install-forge-claude`.

## Quiet symptom: the files no longer match the package

Nothing fails. The session simply runs an older agent definition than the
installed forge ships, so a rule added in the newer release is absent and
nothing says so.

- Detect: `install-forge-claude --check` (exit 1 when stale, naming each
  file), or `forge-doctor`'s `claude:integration` line.
- Recover: `install-forge-claude`, then restart the session.

## What a passing check does and does not prove

`claude:integration` compares the generated files against what forge
ships. That is **filesystem evidence**: it says the next session will
load the current definitions. It says nothing about what *this* session
loaded, and no check can — which is why FOUNDATION §11 makes the restart
the terminal step of a regeneration, and why an agent that regenerated
the integration reports it and stops rather than claiming the new rules
are in force.

## `forge-doctor` lines

- `claude:integration` — current, drifted (naming files and the repair
  command), or not generated.
- Vibe's equivalents are reported separately under `vibe:*`.

## Hand-edited generated files

A generated file edited in place is reported as drift and overwritten on
the next run. That is the intended behaviour: the fix belongs upstream in
the shipped source, not in the copy. A repo's **own** agents, skills and
hooks are never touched — only the paths listed in `.claude/.gitignore`
are forge's.
