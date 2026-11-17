# Claude Code integration (optional)

Running [Mistral Vibe](vibe.md) instead? The same agents, skills and
guards are generated for it — see [vibe.md](vibe.md).

This layer is optional. The pre-commit hook and every `forge-*` command
work without it.

## Generate it

```bash
install-forge-claude          # write the integration into .claude/
install-forge-claude --check  # report drift, change nothing (exit 1 if stale)
```

`install-forge-bootstrap` runs it for you, and the managed `post-merge`
and `post-checkout` hooks re-run it, so a merge that changes a shipped
agent moves your copy with it.

There is nothing to install, pin, or keep in sync: the files come from
the `forge-scripts` package already in your environment, so the version
you have is the version you run.

## What it writes

| Path | Contents |
|---|---|
| `.claude/agents/forge-<name>.md` | every shipped agent — `forge-pr-manager`, `forge-precommit-fixer`, … |
| `.claude/skills/forge-<name>/SKILL.md` | every shipped skill, so `/forge-commit`, `/forge-pr`, … resolve |
| `.claude/hooks/<script>.sh` | the guard scripts, executable |
| `.claude/settings.json` | forge's guard registrations, merged into whatever else the file holds |
| `.claude/.gitignore` | names the files above as forge's, so they stay untracked |

Agents are addressed by their published name, e.g.
`forge-design-checker`. The `forge-` prefix is part of the file name, so
forge's agents are distinguishable from yours at a glance and cannot
collide with them.

## It never touches your files

Forge shares these directories with your own agents, skills and hooks,
so ownership is explicit:

- An agent or skill is forge's only if forge ships that prefixed name.
- A hook registration is forge's only if **every** command in the group
  names a script forge ships — identified by script name, not by
  directory, because your scripts live in the same directory.
- Anything else in `.claude/` is yours and is left alone.

`settings.json` is the one generated file that stays **tracked**: it
carries your `permissions` beside forge's registrations, so a change to
the guards is reviewed like any other diff.

There is no name to avoid, as long as you do not prefix your own files
with `forge-`. A wrapper around a shipped agent is an ordinary file of
yours — `design-checker-<repo>` — delegating to the prefixed one
(FOUNDATION §16 Pattern A).

## The guards

The registrations come from `src/forge/data/hooks.toml`, the
harness-neutral wiring that says which guard runs on which event behind
which tool matcher. Both the Claude Code and Vibe converters read it, so
neither harness's layout leaks into the other.

Commands are rooted at `${CLAUDE_PROJECT_DIR}`, never relative: a hook
fires with an unspecified working directory.

Some guards `source` a shared helper that the wiring never names
directly. The installer copies those too, discovered by reading the
scripts. This matters more than it sounds: the guards **fail closed**, so
a guard that cannot find its helper refuses the call it was protecting
rather than allowing it.

## Checking it

`forge-doctor` reports one line, `claude:integration`: current, drifted
(naming the files and the command that repairs it), or not generated.

A running session keeps the files it loaded at **startup**. After
regenerating, restart the session — a passing check is evidence about
the filesystem, never about what your session is running.
