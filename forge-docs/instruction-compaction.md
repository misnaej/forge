# Compacting an always-loaded instruction file

What belongs in a file every agent session loads, and what belongs
elsewhere. Two files in your repo work this way:

- **`FOUNDATION.md`** — forge's, shipped and overwritten on upgrade. You
  never edit it; a rule you want changed goes upstream (FOUNDATION §12).
  It states the rule and links here (§12); this page holds the procedure.
- **`CLAUDE.md`** (or `AGENTS.md`) — **yours**. Same cost per session,
  same growth, and you own every word. This page is for that file too;
  see "Your own CLAUDE.md" below.

## The target is "as short as possible". The budget is a ceiling.

A word count warns; the number is a **ceiling, never a target**. No word
earns its place by fitting, and the shortest prose carrying every rule is
the right length. Forge's check (`tests/test_foundation_budget.py`) warns
and never fails: growth because a real rule arrived is the file doing its
job, growth because prose accumulated is not, and only reading it tells
you which.

## Who pays

An always-loaded file loads into every session of every agent. A rule one
role uses, or one an opted-out repo cannot act on, is paid for by all of
them. Three questions, in order.

### 1. Did this repo opt into the feature the rule serves?

No → it does not belong in an always-loaded file at all. An optional
scheme keeps its rules with the thing that implements it: the selector's
cadence in the selector's spec, a logging convention with the module. A
repo that never enabled a feature must not carry its policy every
session.

### 2. Which role acts on it?

One role → that role's agent doc. Loaded whenever that agent runs,
costing the others nothing. Every role → question 3.

### 3. Would an agent act wrongly before it thought to open the pointer?

- **Yes → it stays.** `NEVER force push` must be resident: you need it in
  order to know you need to look.
- **No → it moves.** The enumeration of every blocked git form can wait;
  by the time an agent needs that list it already knows to look it up.

Second cut for what survives: does the passage **change a decision** or
**inform an execution**? Decisions stay. Executions get a pointer.

## Destinations

| Where | Loaded | Holds |
|---|---|---|
| `FOUNDATION.md` / `CLAUDE.md` | every session, every agent | universal decision rules |
| an agent doc | whenever that agent runs | role-specific rules |
| a referenced page | on demand, possibly never | enumerations, mechanics, rationale |
| a CLI or pre-commit step | every run, mechanically | anything computable from tree and config (§7) |

Prefer the lowest row that fits. A check nobody has to remember beats a
rule everybody must.

## What stays

- Imperatives and prohibitions, in the voice that makes them binding.
- The trigger sentence saying a detail exists and naming where.
- Conflict rules and ownership splits — who owns what, who wins.
- Anything whose omission fails **silently**: no error, no gate, no
  failing test, just a wrong action nobody notices.

## What moves

- Command invocations, flag tables, exit codes.
- Complete enumerations: every blocked form, every mode, every lane.
- Rationale, incident history, worked examples.
- Per-tool mechanics that matter only once the tool is running.

Rationale moves when the rule is unambiguous without it, and stays — one
clause, never a paragraph — when the rule would otherwise be misapplied.
"Recurring bypass means the guard is wrong, never grounds to loosen the
rule" is load-bearing: strip it and the rule reads as negotiable.

## Forbidden

1. **Never move an imperative behind a pointer.** Forge's issue gate once
   stated two conditions in one paragraph, the expensive one inside a
   parenthetical; a run collapsed it to the cheap half and falsely
   labelled 32 issues. A page may never be opened at all.

   Moving an imperative into the agent that executes it is *not* this,
   but only when **exactly one role acts on it and that role always
   loads the doc** — the Q2 case. Two roles, and one of them reaches the
   rule through a pointer, which is this prohibition's own failure mode.
   Then either keep it resident, or make the second role's pointer state
   the rule rather than only its location.
2. **Never merge two rules into one vaguer one** to save words. That is
   how "warns past the ceiling, never refuses" degrades into "has
   limits": shorter, and no longer a rule.
3. **Never renumber a section.** `§7`, `§14` and the rest are cited from
   agents, skills, issues, commit messages and code comments. Retire a
   number rather than reuse it.

## Order of operations

1. **Delete what another file already owns.** Check before moving: a
   passage that names its own spec is usually a duplicate, not a summary.
2. **Route what is left** by the three questions above.
3. **Compress what stays** — §8's terse rule applies to the file itself.
   Same rules, fewer words.
4. **Stop.** A document as short as it can be while carrying every rule
   is finished, over the ceiling or not.

## Referenced pages have budgets too

Give every page you point at its own ceiling, warned the same way —
without one, "move it out" hides growth instead of reducing it, and the
detail decays where nothing measures it. Forge's pattern: a dict of
page → ceiling, warning and never failing.

A referenced page opens with the rule it serves, so a reader arriving
cold knows what the detail is for. The pointer states the rule too —
never only the link — so the page is never load-bearing alone.

## Your own CLAUDE.md

Everything above applies, more sharply: `FOUNDATION.md` is reviewed by
whoever ships forge, while your `CLAUDE.md` is reviewed by nobody but
you, and it grows every time someone corrects an agent.

Run the three questions over it. Two findings are usual:

- **Rules for one role.** A convention only your test-writer or your
  deploy agent needs belongs in that agent's doc (`.claude/agents/…`),
  where it loads on use and costs the others nothing. §16's wrapper
  patterns exist for exactly this.
- **Rules for a tool you no longer run.** Config and process notes for a
  retired tool keep loading forever. Delete them; nothing warns you.

Keep in `CLAUDE.md` what no agent could act correctly without: your
layout, your release and branching rules, your logging entry point
(FOUNDATION §9 requires naming it there if you do not adopt
`common.logging`), your documented exceptions. Push the rest down a row
in the destinations table.

And never paste a shipped rule into it to be safe. A copy drifts from the
original on the next upgrade, and §12's conflict rule means your stale
copy then silently wins.
