# Mistral Vibe integration (optional)

Forge's CLIs and git hooks are harness-agnostic; the agent surface (guards,
agents, skills, instructions) can also run under [Mistral Vibe](https://mistral.ai/vibe).
Opt in per repo with `[tool.forge.vibe] enabled = true`, then run
`install-forge-bootstrap` (its `vibe` step) or `install-forge-vibe` directly.
In a consumer repo the agent/skill/hook sources come from the installed forge
package (shipped under `src/forge/data/`), so no forge checkout is needed —
`install-forge-vibe` generates `.vibe/agents/*.toml` from them, and the agents
spawn by bare name (see below).

## What lands on disk (all generated, all gitignored)

| Artifact | Produced by | Notes |
|---|---|---|
| `.vibe/hooks.toml` | `forge-gen-vibe-hooks` | One entry per blocking event/matcher, every one `strict = true`, commands route through `forge-hook-dispatch --harness vibe --roster <abs path>`; the two Vibe-only `post_agent` extras (from `src/forge/data/vibe-extra-hooks.toml`) run directly with no `strict` (Vibe forbids strict on post_agent) |
| `.vibe/forge-roster.json` | `install-forge-vibe` | Guard roster baked at generation time; dispatcher contains commands to the repo root and denies a roster claiming `project_root` outside its own location |
| `.vibe/agents/*.toml` + `.vibe/prompts/*.md` | `install-forge-vibe` | The 13 forge subagents in Vibe's schema (`agent_type = "subagent"`, `system_prompt_id`); pinned via `active_model = "forge-<tier>"` using the preset aliases written to `.vibe/config.toml` (an unknown alias would reject the whole agent file, so forge ships the aliases itself) |
| `.agents/skills/*/SKILL.md` | `forge-gen-vibe-agents` | The 19 skills (incl. vendored `/caveman` levels), `CLAUDE.md` references rewritten to `AGENTS.md` |
| `AGENTS.md` | `install-forge-vibe` | Flattened from `FOUNDATION.md` + `CLAUDE.md`; see below |
| `.gitignore` entries | `install-forge-vibe` | Ensures `.vibe/`, `.agents/`, `AGENTS.md` stay untracked |
| `.vibe/config.toml` | `install-forge-vibe` | Appends forge-owned `[[models]]` presets (`forge-haiku`/`forge-sonnet`/`forge-opus`) when absent — never clobbers user models |

### Spawning the forge agents

Under Mistral Vibe, spawn forge agents by their bare name:

```python
agents.spawn(config_name="prior-art")
```

The `forge:<name>` form is the Claude Code plugin namespace and is **not** a valid subagent type under Vibe.

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

The 31 portable guards (Bash/Edit/Write `pre_tool`, bash `post_tool`) run
through `forge-hook-dispatch`, which re-feeds each guard the original stdin
bytes and fails closed: malformed stdin, missing/tampered roster, unknown
event, missing or crashing guard, timeout, or any internal error all emit a
deny verdict (`strict = true` covers the exit code). `claude-hooks/block_vibe_config.sh`
additionally blocks agent edits/deletes of `.vibe/`, `.agents/` and the
root `AGENTS.md` — gitignored files git never restores.

## What Vibe does not provide (and the workaround)

| Claude Code event | Status in Vibe | Workaround |
|---|---|---|
| `SessionStart` (plugin version, foundation sync) | `session_start` (non-blocking) on current Vibe | **shipped**: `session_start` extra `print_loaded_plugin_version.sh` prints the loaded release (match `startup|resume`); foundation sync stays on the post-merge drift check. Older Vibe without `session_start`: `post_agent` `vibe_session_start.sh` (match `main`) injects a once-per-session continuation reminder |
| `PreCompact` state write | no equivalent | **shipped**: `post_agent` `vibe_continuation_state.sh` (match `main`) refreshes the status panel every turn |
| `SubagentStart` | no equivalent | `post_agent` fires per subagent (`match = config_name`) for timing/logging |
| `SubagentStop` blocking gate | post-turn, cannot block | not enforceable; listed as an unenforced protection at install |
| per-agent model tiers | subagents inherit the session model unless `active_model` is set | **shipped**: forge-owned `[[models]]` presets + `active_model = "forge-<tier>"` in generated agent TOMLs |

`install-forge-vibe` prints the unenforced protections on every run.

## How harness compatibility works

Forge's guards are Claude-shaped at the base with per-harness adapters on top today; the target architecture inverts this to a harness-neutral base contract. See [architecture.md — harness compatibility](architecture.md#harness-compatibility-claude-code-mistral-vibe-future).

## Terse output (caveman)

Every generated subagent prompt (`.vibe/prompts/*.md`) carries an
always-on terse-output block, because Vibe has no blocking SessionStart/SubagentStart
hook to enforce a voice on subagents and skills don't inherit into spawned
subagents — the style rule lives in the prompt itself. On top of that:

The `/caveman`, `/ultracave` and `/megacave` voice skills are vendored into
`skills/` (Apache-2.0, attribution in `src/forge/data/VENDORED.md`) and
published to `.agents/skills/`. Vibe's `session_start` extra only prints the
plugin version, so there is no auto-activation — invoke `/caveman` manually per session.

## Checking the install

```bash
forge-doctor                  # auto-detects harnesses; --harness vibe forces vibe-only
forge-doctor --harness vibe   # hooks strict, roster, agents+prompts, skills, AGENTS.md drift, dispatcher, trusted folder
install-forge-vibe --verify   # exit 1 on any missing/stale artifact
```
