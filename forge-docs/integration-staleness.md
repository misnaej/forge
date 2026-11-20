# Integration staleness

Which generated surfaces a running session picks up, which it does not, and
what a restart settles. Policy: [FOUNDATION §11 "Generated-integration
staleness"](../FOUNDATION.md#11-agent-boundary-protocol).

## Per surface, on Claude Code

| Surface | On a mid-session rewrite |
|---|---|
| Skills (`.claude/skills/`) | **Reloads live.** Claude Code watches the directory; an added, edited or removed skill takes effect in the running session. `/reload-skills` if one does not appear. |
| Subagents (`.claude/agents/`) | **Reloads live.** Watched the same way; the next delegation uses the new definition. Restart still needed for agents under `--add-dir`, and for sessions started with `--disable-slash-commands`. |
| Guard wiring (`.claude/settings.json`, `.claude/hooks/`) | **Unverifiable from inside.** The documentation says settings are watched and reloaded, and bug reports dispute edge cases — a hook added mid-session, a settings file created mid-session. Nothing a session can run reports which hook set is in force. |
| Plugin-provided content | Needs `/reload-plugins`. |
| `CLAUDE.md` | **Not live.** Reloaded only by `/clear`, `/compact` or a restart. |

Two consequences the policy turns into rules:

- An agent may say what a surface does; it may not say what *the session*
  holds. "The session is stale" is unknowable from where it stands, and
  asserting it next to a visible live reload teaches the reader that forge's
  stops are noise.
- A restart settles **guard wiring only**, and the reader will see no visible
  change — the surfaces they can observe had already reloaded. Whoever hands
  over a restart says that, or it looks like it did nothing.

**Vibe is untested.** `install-forge-vibe` writes the equivalent surfaces, and
whether that harness hot-reloads them has not been established. Treat its
reload behaviour as unknown rather than assuming either table row applies.

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
2. **A running session vs. the files on disk.** Per surface, not per
   session — see the table above. Skills and agents follow later writes;
   guard wiring cannot be read from inside at all.

## Loud symptom: missing or misnamed agent

- Symptom: `Agent type '<name>' not found` though the file is on disk.
  The session started before the file existed.
- Recovery: the watcher usually picks a new agent file up within seconds;
  restart if it does not, or if the session was started with
  `--disable-slash-commands` (no watching) or the file sits under
  `--add-dir`.
- Regenerating first is harmless and usually unnecessary: the
  `post-merge` and `post-checkout` hooks already ran
  `install-forge-claude`.

## Quiet symptom: the files no longer match the package

Nothing fails. The files on disk are older than the installed forge ships,
so a rule added in the newer release is absent from them and nothing says
so. This is a disk-vs-package gap, which a check does read — unlike the
session question above.

- Detect: `install-forge-claude --check` (exit 1 when stale, naming each
  file), or `forge-doctor`'s `claude:integration` line.
- Recover: `install-forge-claude`. Skills and agents follow on their own;
  restart only to settle guard wiring, and say that is all it settles.

## What a passing check does and does not prove

`claude:integration` compares the generated files against what forge
ships. That is **filesystem evidence**: it says the next session will
load the current definitions. It says nothing about which guard set this
session enforces, and no check does — which is why §11 forbids the claim
in either direction and keeps the stop for guard wiring, the one surface
no probe reads. An agent that regenerated the integration reports what it
wrote and what a restart would settle, never that the new rules are or
are not in force.

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
