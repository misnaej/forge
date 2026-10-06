---
name: memory-audit
description: Audit the agent's persistent memory against the repo's rule surface - flag contradictions, duplicates of shipped rules, and stale references, route true-and-shareable lessons to the repo's lessons file or upstream, then propose reconciliation. Use when the user wants memory checked, cleaned, or reconciled against FOUNDATION / CLAUDE.md / skills, or when /next or /pr offers it.
user-invocable: true
---

# Memory Audit

Agent memory drifts: a remembered convention outlives the code it
described, or a rule later ships into the repo (FOUNDATION §12 "Process
feedback ships into the rule surface") leaving a stale private copy. And
some memories are simply right — a lesson worth sharing that never left
one agent's notes. This skill reconciles memory against the repo's rules
and moves shareable lessons to where others can find them. **Reports
first, confirm-first for every change — never silently edit or delete
memory.**

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
5. The repo's lessons file (`forge-memory-audit status` prints its path;
   `[tool.forge.memory_audit].lessons_file`, default `docs/lessons.md`)

## Step 3: Classify every memory

One verdict per memory file, with the evidence quoted:

- **DUPLICATES** — the memory restates something the rule surface or
  the lessons file already owns. Propose deleting the memory. If it
  matches a lessons-file entry, propose raising that entry's count (see
  Step 5).
- **STALE** — the memory names a file, flag, command, or convention
  that no longer exists. Verify by grep before claiming (a live symbol
  is not stale). Propose fix or deletion.
- **CONTRADICTS** — memory says X, a rule says not-X. Quote both and
  **ask the user which is right** — never assume the rule wins. If the
  rule is right, the memory is edited or deleted. If the memory is
  right, it becomes **SHOULD-SHIP**, and the rule is what needs fixing.
- **SHOULD-SHIP** — true, and useful to others beyond this agent. It
  goes to the intake (FOUNDATION §12), never straight into an
  always-loaded doc:
  - a lesson about this repo → a new entry in the lessons file,
    `occurrences: 1`;
  - a lesson about forge itself (a shipped agent, skill, hook, CLI or
    FOUNDATION rule) → `/report-to-forge`.
- **UNOWNED** — true, but nothing in the repo can own it (harness or
  model behaviour, a third-party tool's quirk). Keep it, labelled
  UNOWNED, so the next audit does not re-litigate it.
- **PERSONAL** — individual preference or private context that cannot
  ship. Correctly memory-resident; keep.

## Step 4: Report

A table — memory name / verdict / evidence (one line) / proposed
action — followed by the proposals. Lessons-file entries that reached
two occurrences are listed as promotion candidates (Step 5).

## Step 5: Apply (confirm-first)

Only after explicit user confirmation, per file — the user may accept
some proposals and keep others:

1. **SHOULD-SHIP first, delete last.** Write the lessons-file entry (or
   file the forge report) and confirm it landed — the file write
   succeeded, or the issue URL came back — before deleting the memory.
   A failed write keeps the memory.
2. **A lesson seen again** raises its entry's `occurrences` count. At
   **2**, propose promoting it into `CLAUDE.md`, a skill or an agent doc
   — the place it would have prevented the mistake — and remove the
   entry once the user confirms and the promotion lands.
3. Edit or delete the other agreed memory files and update the
   `MEMORY.md` index to match.
4. Run `forge-memory-audit stamp --memory-dir <memory dir>` so the next
   `status` counts only memories added after this audit.

Lessons-file entry shape (one `##` section per lesson):

```markdown
## <one-line lesson>
- occurrences: 1
- first seen: <YYYY-MM-DD>
- where it applies: <file, skill or workflow>

<two or three sentences: what happened, what to do instead>
```

## Cadence

- **On demand.**
- **`/next`** runs `forge-memory-audit status` and offers this audit once
  enough new memories have built up (`[tool.forge.memory_audit].threshold`,
  default 5).
- **`/pr`** offers it when the PR edits the rule surface (`forge-pr-plan`
  reports `rule_surface: true`) — the memory that motivated a rule change
  is now a DUPLICATES candidate.

Offered, never run silently.
