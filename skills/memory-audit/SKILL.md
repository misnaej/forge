---
name: memory-audit
description: Audit the agent's persistent memory against the repo's rule surface - flag contradictions, duplicates of shipped rules, and stale references, then propose reconciliation. Use when the user wants memory checked, cleaned, or reconciled against FOUNDATION / CLAUDE.md / skills.
user-invocable: true
---

# Memory Audit

Agent memory drifts: a remembered convention outlives the code it
described, or a rule later ships into the repo (FOUNDATION §12 "Process
feedback ships into the rule surface") leaving a stale private copy.
This skill reconciles memory against the repo's rules. **Reports first,
confirm-first for every change — never silently edit or delete memory.**

## Step 1: Enumerate memory

Read the memory index (`MEMORY.md` in the agent's memory directory — the
harness names it in the session context) and every memory file it lists.
No memory directory / empty index → report "no memory to audit" and stop.

## Step 2: Load the rule surface

The comparison baseline, in precedence order (consumer wins over
foundation, per the FOUNDATION conflict rule):

1. The repo's `CLAUDE.md`
2. `FOUNDATION.md` (when present)
3. Shipped + local skills (`skills/*/SKILL.md`, `.claude/skills/*/SKILL.md`)
4. Agent docs (`agents/*.md`, `.claude/agents/*.md`)

## Step 3: Classify every memory

One verdict per memory file, with the evidence quoted:

- **CONTRADICTS** — memory says X, a rule says not-X. Quote both texts.
  The repo rule wins by default (§12); the memory is edited or deleted
  unless the user rules otherwise.
- **DUPLICATES** — the memory restates something the rule surface
  already owns. Propose deleting the memory; rules live in the repo.
- **STALE** — the memory names a file, flag, command, or convention
  that no longer exists. Verify by grep before claiming (a live symbol
  is not stale). Propose fix or deletion.
- **PERSONAL** — individual preference or private context that cannot
  ship. Correctly memory-resident; keep.

## Step 4: Report

A table — memory name / verdict / evidence (one line) / proposed
action — followed by the proposals. For a consumer-repo memory that
duplicates or contradicts *foundation-shipped* content, the fix may be
an upstream forge issue instead of a local edit (§12).

## Step 5: Apply (confirm-first)

Only after explicit user confirmation: delete or edit the agreed memory
files and update the `MEMORY.md` index to match. Deletions and edits are
per-file decisions — the user may accept some and keep others.

## Cadence

Run on demand, or when `/next`-style session hygiene notices the memory
index has grown. A natural trigger: right after a working rule ships
into the repo — the memory that motivated it is now a DUPLICATES
candidate.
