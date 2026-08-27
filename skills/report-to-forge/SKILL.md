---
name: report-to-forge
description: Turn an observed defect in a forge-shipped process (agent, skill, hook, pre-commit step, CLI) into a filed upstream issue against forge — versions captured mechanically, evidence preserved verbatim, consumer specifics redacted with user confirmation.
user-invocable: true
---

# Report to forge

Consumers must not patch shipped files locally — upgrades overwrite
them (FOUNDATION §12) — so the only durable fix path for a misbehaving
forge process is an issue against the forge repository. This skill
carries that policy out: it captures what reports written from memory
get wrong (versions, evidence) and strips what they must not contain
(consumer specifics, FOUNDATION §2).

## Step 1: Identify the process

Pin the report to exactly one shipped surface: an agent
(`forge:<name>`), a skill, a Claude hook (`claude-hooks/<name>.sh`), a
pre-commit step, or a CLI. A vague target ("commits are weird") is not
reportable — ask the user which shipped thing misbehaved before
continuing, and record the shipped file's path within the plugin or
package so a maintainer lands directly on it.

## Step 2: Capture versions — mechanically, never from memory

```bash
forge-doctor                  # pip version, plugin version, skew report
grep -n "forge" pyproject.toml | grep -i "git+\|forge-scripts"   # the pin
```

Record: the installed `forge-scripts` version, the `pyproject.toml`
pin, and the active plugin version — and **flag any mismatch between
them explicitly**; skew is itself often the bug. `forge-doctor`'s skew
section is the source of truth (FOUNDATION §2: the forge CLI, never a
hand-rolled fallback).

## Step 3: Capture evidence while it still exists

The useful artifacts are transient — collect them now, verbatim, into
the draft:

- relevant `git reflog` lines (rewinds, resets, unexpected history)
- the agent's hand-back text, exactly as returned
- the failing `code_health/*.log` excerpt
- the exact command that was blocked or misfired, and the hook message

Paraphrased evidence is near-worthless; quote it.

## Step 4: Generalize and redact — then confirm

Strip every consumer specific: repo name, org name, branch names,
absolute paths, issue/PR numbers, domain module names. Restate the
scenario in terms any consumer recognizes ("a branch fast-forwarded
onto its base, with uncommitted work"), keeping the forge-side names
(agent/hook/CLI/step) exact. **Show the user the full redacted draft
and get explicit confirmation before filing** — never file on your own
judgment of what counts as private.

## Step 5: Structure the report

Three parts, in order: what happened (observed behaviour + evidence) →
why the current guards do not cover it → concrete request. When one
session produced several findings, tier them by severity — a data-loss
bug is never buried under lint nits; file separate issues when the
findings are independent.

## Step 6: File upstream and report back

```bash
gh issue create --repo <forge upstream> --title "<process>: <symptom>" --body-file <draft>
```

The upstream repo is the canonical constant in `forge.git_utils`
(`_FORGE_GITHUB_REPO`) — never a guessed URL. Return the issue URL to
the user, and append a one-line record to `.plan/CONTINUATION.md`
(FOUNDATION §10).

## Scope

User-invoked only. This skill does not detect reportable moments on its
own, does not edit shipped files, and does not file anything without
the Step 4 confirmation.
