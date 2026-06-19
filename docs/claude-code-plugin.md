# Claude Code plugin integration (optional)

The Claude Code plugin is a separate layer. You do NOT need it for the
pre-commit hook to work.

> **Fork-friendly note.** A fork can ship its own marketplace pointing
> at the fork's GitHub repo. Consumers who want both can register two
> marketplaces side-by-side; agents live in distinct namespaces
> (`forge:<name>` for canonical, `<fork>:<name>` for the fork).

## Install — enable it **per repo** (recommended)

If your team uses [Claude Code](https://claude.com/claude-code) and
wants the agents (`pr-manager`, `precommit-fixer`, `git-commit-push`, etc.)
and slash commands (`/commit`, `/pr`, `/next`, …):

Enable it in the **consumer repo's** `.claude/settings.json`, so the plugin
loads only in that repo. Its agents shell out to `forge-scripts`, so it is
only useful where that's installed — a per-repo enable keeps it inactive
(and error-free) everywhere else.

The simplest way is to let forge write/verify the block (idempotent,
merge-preserving; also run by `install-forge-bootstrap`):

```bash
install-forge-claude-settings        # marketplace ref tracks your pip pin, else main
install-forge-claude-settings --ref dev    # or pin a channel explicitly
install-forge-claude-settings --check      # verify only (CI / drift)
```

It writes:

```jsonc
{
  "extraKnownMarketplaces": {
    "forge": {
      "source": { "source": "github", "repo": "misnaej/forge", "ref": "main" }
    }
  },
  "enabledPlugins": { "forge@forge": true }
}
```

Claude Code prompts to trust + install on first session in that repo. (The
one-liner `/plugin install forge@forge --scope project` writes an
equivalent block by hand.)

> **Don't install globally.** `/plugin install forge@forge` without
> `--scope project` installs into `~/.claude` and is active in **every**
> repo (opt-out), so the agents then error in repos that lack
> `forge-scripts`. Scope it per repo instead — [`adopting.md`](adopting.md)
> Track 3 covers the rationale and how to keep it out of (or disabled in)
> non-forge repos.

To pin a specific plugin version (recommended):

```jsonc
// ~/.claude/installed_plugins.json
{ "forge@forge": { "version": "v1.2.5" } }
```

Keep two version pins aligned: the pip `forge-scripts @ ...@vX.Y.Z` dep
and the Claude plugin version. Releases bump both together.

## Switching channels (dev ↔ main)

To move between forge release channels — `main` (tagged releases) and
`dev` (rolling-next, where new work lands first) — editing the `ref`
field in `~/.claude/settings.json` and running `/plugin marketplace
update forge` is **not enough**. Claude Code keeps the cached
marketplace `ref` frozen at the value it was first registered with.
The verified workaround is a five-command sequence; the order matters
and skipping the install step silently leaves the consumer with zero
forge skills loaded.

```
/plugin marketplace remove forge
/plugin marketplace add misnaej/forge
```

Then edit `~/.claude/settings.json` by hand: in the `forge.source`
block, add or change `"ref": "dev"` (or `"main"`). This is the step
that actually changes channels — the slash commands alone cannot
update an already-cached marketplace `ref`.

```
/plugin marketplace update forge
/plugin install forge@forge        # CRITICAL — marketplace remove also
                                   # uninstalls the plugin
/reload-plugins
```

The `/plugin install forge@forge` step is the footgun. `/plugin marketplace remove` evicts the
plugin from `~/.claude/installed_plugins.json` along with the
marketplace entry. Without `/plugin install forge@forge` after the
re-add, `/reload-plugins` finds nothing to load and returns silently —
no error, no warning. The agent ecosystem just stops working.

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

The underlying caching behaviour is an upstream Claude Code issue.
Forge tracks it in issue #71; this section will be removed when the
upstream fix ships.

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
