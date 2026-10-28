# Claude Code plugin integration (optional)

The Claude Code plugin is a separate layer. You do NOT need it for the
pre-commit hook to work.

> **Fork-friendly note.** A fork can ship its own marketplace pointing
> at the fork's GitHub repo. Consumers who want both can register two
> marketplaces side-by-side; agents live in distinct namespaces
> (`forge:<name>` for canonical, `<fork>:<name>` for the fork).

## Install — one copy per checkout, at the release it pins

If your team uses [Claude Code](https://claude.com/claude-code) and
wants the agents (`pr-manager`, `precommit-fixer`, `git-commit-push`, etc.)
and slash commands (`/commit`, `/pr`, `/next`, …), each checkout installs
forge for itself:

```bash
forge-plugin-sync           # install this checkout's release (no-op when current)
forge-plugin-sync --check   # report only; exit 1 when not on its release
```

**It runs on its own.** `install-forge-bootstrap` runs it as its `plugin`
step (after `claude-settings`), so `forge-upgrade` and forge's own
`dev/setup.sh` do too; the `post-merge` and `post-checkout` git hooks run
it quietly after every pull and branch switch. When it changes the
install it prints *restart the Claude Code session to load it* — the
restart is the only manual step. It skips silently when `claude` is not
installed, and in CI.

**Which release.** A consumer gets the release its `forge-scripts @
git+...@vX.Y.Z` pin names; a pin on a branch names no fixed content, so
the command refuses (exit 2) — pin a release tag. In forge's own repo it
is the version `.claude-plugin/plugin.json` names: the latest tag, except
on a release-assembly commit, which names the release about to be tagged.

**Why per checkout.** Claude Code keeps one marketplace registration per
*name* for the whole machine. While every repo registered forge under the
one name `forge`, repos on different releases overwrote each other's
source, and `/plugin update` fetched whichever release the last repo had
named while reporting it current. `forge-plugin-sync` instead installs
from a marketplace named after the release — `forge-v10-4-0` for 10.4.0
— a one-plugin `marketplace.json` under
`$XDG_CACHE_HOME/forge/marketplaces/<name>/` (default `~/.cache`) that
pins both the tag and its commit. Two repos on different releases use
two different marketplaces and can no longer collide. Every change goes
through Claude Code's own `claude plugin marketplace add … --scope local`
and `claude plugin install forge@<name> --scope local`, after which this
checkout's older per-version install is uninstalled; no Claude Code file
is edited by hand and no cache slot is deleted. Agents keep their
`forge:<name>` names.

**The committed settings turn the shared plugin off.**
`install-forge-claude-settings` (also a bootstrap step) removes forge's
entry from `extraKnownMarketplaces` in the repo's `.claude/settings.json`
and writes:

```jsonc
{ "enabledPlugins": { "forge@forge": false } }
```

Idempotent and merge-preserving; `--check` verifies without writing (CI /
drift).

**Refusals.** Two enabled plugins named `forge` make Claude Code drop
hooks, so `forge-plugin-sync` refuses to install while either exists:

- a forge installed or enabled for your whole user account, from any
  marketplace, which loads in every repo — the refusal names each key to
  remove with `claude plugin uninstall <key> --scope user`
  ([`adopting.md`](adopting.md) Track 3 covers keeping forge out of
  non-forge repos);
- a tracked `.claude/settings.json` enabling any `forge@…` — run
  `install-forge-claude-settings`.

**Where staleness shows:** [`forge-docs/plugin-staleness.md`](../forge-docs/plugin-staleness.md).

### The `/reload-plugins` `0 skills` counter

After `/reload-plugins` runs, its output line reports a **delta** for
that reload pass — not totals:

```
Reloaded: 2 plugins · 0 skills · 17 agents · 16 hooks
```

`0 skills` does NOT mean no skills are loaded. It means no NEW skills
were discovered relative to the prior in-memory state. Skills shipped
by forge (and any other plugin) remain invocable. To confirm skills
are present, invoke one directly (e.g. `/forge:next`) — if the slash
command resolves, the skill is loaded.

That answers whether a skill is *present*, not whether it is
*current*. A skill from a slot several releases old resolves exactly
the same way, and its content is what the session executes. Nothing
distinguishes the two from inside the session — see FOUNDATION §11
"Plugin staleness", which is why a restart, not a reload, is the
terminal step of a plugin-moving upgrade.

## Consumer `CLAUDE.md`

`install-forge-claude-md` writes two files: the foundation lives in
`FOUNDATION.md` (forge-managed); your `CLAUDE.md` carries only the
`@FOUNDATION.md` include directive and your repo-specific rules.
Claude Code inlines the foundation at session start. See [How forge
stays in sync](../README.md#how-forge-stays-in-sync) for upgrade
behavior.

## Consumer-specific Claude Code hooks

`install-forge-claude-md` also creates `.claude/hooks/` with a README
documenting the canonical hook-registration shape, and writes a minimal
`.claude/settings.json` skeleton if one doesn't exist. Consumer hook
scripts go under `.claude/hooks/<name>.sh`.

**Always register hooks with `${CLAUDE_PROJECT_DIR}` paths, never
relative paths.** Relative paths break whenever the hook fires from a
context where the shell's cwd is not the repo root (subagents,
subdirectories, etc.) — you get spurious `/bin/sh: <name>.sh: not found`
errors that look like blocker failures but are non-blocking noise.

```jsonc
// .claude/settings.json — RIGHT
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/your_hook.sh",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

```jsonc
// WRONG — breaks when cwd ≠ repo root
{ "type": "command", "command": ".claude/hooks/your_hook.sh" }
```

Forge's own hooks (`block_raw_git`, `block_raw_ruff`, …) ship via the
plugin manifest at `${CLAUDE_PLUGIN_ROOT}/claude-hooks/...` — you do not
register those. Only consumer-specific hooks live under
`.claude/hooks/`.

## Extending foundation agents

**Don't shadow foundation agents with same-named local files.** Per
FOUNDATION §3, consumer wrappers must use a distinct suffixed name
(e.g. `design-checker-<repo>.md` under
`<consumer_repo>/.claude/agents/`) that delegates to the foundation
agent via the `Task` tool with repo-specific extras in its prompt. A
local file at `<consumer_repo>/.claude/agents/design-checker.md`
would shadow `forge:design-checker` entirely and make direct calls to
the foundation agent unreachable. If you genuinely need to replace
(not extend) a foundation agent, open an issue first — divergence is
what the foundation prevents.
