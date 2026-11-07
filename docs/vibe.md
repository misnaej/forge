# Mistral Vibe integration (optional)

Forge's CLIs and git hooks are harness-agnostic; the agent surface (guards,
agents, skills, instructions) can also run under [Mistral Vibe](https://mistral.ai/vibe).
Opt in per repo with `[tool.forge.vibe] enabled = true`, then run
`install-forge-bootstrap` (its `vibe` step) or `install-forge-vibe` directly.

## What lands on disk (all generated, all gitignored)

| Artifact | Produced by | Notes |
|---|---|---|
| `.vibe/hooks.toml` | `forge-gen-vibe-hooks` | One entry per event/matcher, every entry `strict = true`, all commands route through `forge-hook-dispatch --harness vibe --roster <abs path>` |
| `.vibe/forge-roster.json` | `install-forge-vibe` | Guard roster baked at generation time; dispatcher contains commands to the repo root and denies a roster claiming `project_root` outside its own location |
| `.vibe/agents/*.toml` + `.vibe/prompts/*.md` | `install-forge-vibe` | The 12 forge subagents in Vibe's schema (`agent_type = "subagent"`, `system_prompt_id`); the forge model tier is recorded as a comment — pin it with `active_model` only after defining the alias in Vibe's `[[models]]` (an unknown alias rejects the whole agent file) |
| `.agents/skills/*/SKILL.md` | `forge-gen-vibe-agents` | The 16 skills, `CLAUDE.md` references rewritten to `AGENTS.md` |
| `AGENTS.md` | `install-forge-vibe` | Flattened from `FOUNDATION.md` + `CLAUDE.md`; see below |
| `.gitignore` entries | `install-forge-vibe` | Ensures `.vibe/`, `.agents/`, `AGENTS.md` stay untracked |

## AGENTS.md and `@`-includes

Vibe renders `AGENTS.md` verbatim — it has no `@include` directive (unlike
Claude Code's `CLAUDE.md`). Forge therefore resolves whole-line `@path`
directives **at generation time**, recursively, and writes one flattened
`AGENTS.md`. Markdown links (`[text](path)`) are left as lazy pointers in
both harnesses — the model reads them on demand, so inlining them would
only inflate context. A missing `@path` target or an include cycle is a
hard error at install time. Vibe also layers nested `AGENTS.md` files from
cwd up to the trust root natively, so per-directory rules need no flattening.

Regeneration happens on every `install-forge-vibe` run and on every
merge/checkout via the existing `post-merge` / `post-checkout` git hooks
(`run_vibe_regen`, gated on the opt-in, interactive-only). When an install
actually changed generated artifacts, the installer prints a notice:
**Vibe pins hooks and agents at session start — restart the session (or
`/reload`) to pick them up.** `install-forge-vibe --verify` and
`forge-doctor --harness vibe` both fail on a stale `AGENTS.md`.

## Trust requirement

Vibe loads project `.vibe/` and `AGENTS.md` **only for trusted folders** —
an untrusted checkout gets no guards and no instructions, silently. Trust
the folder in the Vibe CLI once; `forge-doctor` has a `vibe:trusted-folder`
check that reports when it is missing.

## Guards and the dispatcher

The 29 portable guards (Bash/Edit/Write `pre_tool`, bash `post_tool`) run
through `forge-hook-dispatch`, which re-feeds each guard the original stdin
bytes and fails closed: malformed stdin, missing/tampered roster, unknown
event, missing or crashing guard, timeout, or any internal error all emit a
deny verdict (`strict = true` covers the exit code). `claude-hooks/block_vibe_config.sh`
additionally blocks agent edits/deletes of `.vibe/`, `.agents/` and the
root `AGENTS.md` — gitignored files git never restores.

## What Vibe does not provide (and the workaround)

| Claude Code event | Status in Vibe | Workaround |
|---|---|---|
| `SessionStart` (continuation note, caveman, foundation sync) | no equivalent | a `post_agent` hook matched to `main` can inject a once-per-session reminder; foundation sync is covered by the post-merge drift check |
| `PreCompact` state write | no equivalent | write the status panel on every `post_agent` instead — always fresh |
| `SubagentStart` | no equivalent | `post_agent` fires per subagent (`match = config_name`) for timing/logging |
| `SubagentStop` blocking gate | post-turn, cannot block | not enforceable; listed as an unenforced protection at install |
| per-agent model tiers | subagents inherit the session model unless `active_model` is set | ship forge-owned `[[models]]` presets first, then set `active_model = "forge-<tier>"` |

`install-forge-vibe` prints the unenforced protections on every run.

## How harness compatibility works

Forge's guards are Claude-shaped at the base with per-harness adapters on top today; the target architecture inverts this to a harness-neutral base contract. See [architecture.md — harness compatibility](architecture.md#harness-compatibility-claude-code-mistral-vibe-future).

## Checking the install

```bash
forge-doctor                  # auto-detects harnesses; --harness vibe forces vibe-only
forge-doctor --harness vibe   # hooks strict, roster, agents+prompts, skills, AGENTS.md drift, dispatcher, trusted folder
install-forge-vibe --verify   # exit 1 on any missing/stale artifact
```
