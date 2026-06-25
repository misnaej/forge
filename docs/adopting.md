# Adopting forge — install exactly the layer(s) you want

Forge is **three separable layers**. You can take just layer 1, or 1+2, or
all three — but **layer 1 is the floor**: the other two drive its CLIs, so
neither does anything useful without it. The first two layers work with
**no AI agent at all**. `install-forge-bootstrap` runs all three in order;
this page lets you follow only the track(s) you want, each ending in a
verification step.

| Layer | What it is | Needs Claude Code? | Standalone installer |
|---|---|---|---|
| **1 — pip CLIs** | `forge-scripts` — verification/generator CLIs + the pre-commit dispatcher | no | `pip install` |
| **2 — git hooks** | `.githooks/*` (pre-commit + post-merge/checkout) | no | `install-forge-githooks` |
| **3 — Claude Code plugin** | agents, slash-command skills, safety hooks | yes | `/plugin install` |

### Layer 1 is the prerequisite — and what breaks without it

`forge-scripts` must be installed **in the Python environment active when
the hook or agent runs**. Both higher layers shell out to its CLIs:

- **Layer 2 (git hooks)** — the committed `.githooks/*` wrappers run
  `forge-precommit` (and the foundation drift check) on commit / merge. If
  the active env lacks `forge-scripts`, the hook **fails loudly**
  (`forge-precommit: command not found`). *Downside:* a teammate who
  clones the repo but hasn't installed the env has a broken commit gate
  until they install layer 1.
- **Layer 3 (plugin)** — the agents/skills orchestrate the CLIs
  (`forge:precommit-fixer` → `forge-precommit`, `forge:git-commit-push` →
  the gate, …). The plugin installs **once, globally** in Claude Code
  (`~/.claude`), but it drives CLIs that must exist in **each repo's**
  env. *Downside:* install the plugin, then open a repo whose env lacks
  `forge-scripts`, and the agents fail the moment they invoke a CLI. The
  plugin's pure **safety hooks** (`block_*`, `check_*`) are the exception —
  bash + jq, they need neither layer 1 nor 2.

In short: **1 alone is fine; 2 and 3 are inert (or broken) without 1.**

---

## Track 1 — the CLIs only

The deterministic checks, runnable locally or in CI, no hooks, no plugin.

Add the pin to your repo's dependency table (so the version is tracked),
then install it into your active environment with your repo's normal flow:

```toml
# pyproject.toml — add to [project.optional-dependencies] under your dev extra
dev = [
    "forge-scripts @ git+https://github.com/misnaej/forge.git@main",
]
```

```bash
# Install it yourself (FOUNDATION §2: never from an agent) — your env's flow, e.g.:
pip install "git+https://github.com/misnaej/forge.git@main"
```

**Verify:**

```bash
forge-doctor            # environment diagnostics: CLIs on PATH, gh, optional plugin
forge-precommit         # run the check sequence once, by hand
forge-config --list     # every [tool.forge.*] key forge reads + what to set
```

Configure via `[tool.forge]` in `pyproject.toml` — see
[`configuration.md`](configuration.md). Per-CLI usage:
[`standalone-installers.md`](standalone-installers.md) and the generated
[`cli-reference.md`](cli-reference.md).

## Track 2 — add the git hooks

Wire forge's pre-commit + post-merge/checkout hooks into your repo. They
shell out to the layer-1 CLIs.

```bash
install-forge-githooks      # writes .githooks/* and sets core.hooksPath
```

**Verify:**

```bash
git config core.hooksPath   # → .githooks
.githooks/pre-commit        # dry-run the gate
```

The committed `.githooks/*` wrappers are **managed** — each carries a
`# forge:githook-managed v2 body-sha=<hex>` marker. Forge can refresh them
on upgrade *without* clobbering your edits (a changed body-sha means "you
edited this — leave it"). Add repo-specific steps in
`.githooks/post-merge.d/` / `post-checkout.d/` (a drop-in dir the
installer never touches) or by editing the wrapper directly — see
[`customizing-precommit.md`](customizing-precommit.md).

## Track 3 — add the Claude Code plugin (enable it **per repo**)

Agents, skills, and safety hooks, in Claude Code only. The agents shell
out to the layer-1 CLIs, so the plugin is only useful where
`forge-scripts` is installed — **enable it per repo, not globally**, so it
stays inactive everywhere else.

Let forge write the per-repo enablement for you:

```bash
install-forge-claude-settings        # writes/merges .claude/settings.json
# install-forge-claude-settings --ref dev   # pin the plugin to a channel
```

It's idempotent and **merge-preserving** (your other `.claude/settings.json`
keys are untouched), and `install-forge-bootstrap` runs it automatically.
The marketplace `ref` defaults to your `forge-scripts` pip-pin channel
(so the plugin tracks the package), falling back to `main`; override with
`--ref`. `--check` verifies it without writing (for CI). It writes:

```jsonc
{
  "extraKnownMarketplaces": {
    "forge": { "source": { "source": "github", "repo": "misnaej/forge", "ref": "main" } }
  },
  "enabledPlugins": { "forge@forge": true }
}
```

Claude Code prompts to trust + install on first session in that repo; the
plugin then loads **only here**.

> **Avoid the global `/plugin install forge@forge`.** A global install is
> active in **every** repo (opt-out), so its agents then error in repos
> that lack `forge-scripts`. Per-repo enablement is opt-in — the model
> below depends on it.

**Verify:** `forge-doctor` reports `plugin:installed` + populated
`agents/` / `skills/` / `claude-hooks/`. Consumer-specific Claude Code
hooks live under `.claude/hooks/` with `${CLAUDE_PROJECT_DIR}`-rooted
paths — see [`claude-code-plugin.md`](claude-code-plugin.md).

### Repos that do NOT use forge

With the per-repo model above you do **nothing** — the plugin was never
installed globally, so it simply isn't present (no agents offered, no
errors). The problem only arises from a **global** install. If forge was
installed globally on your machine and you hit agent errors in a non-forge
repo, pick one:

- **Switch to the per-repo model** — uninstall the global plugin
  (`/plugin uninstall forge@forge`); afterwards only repos that carry the
  `enabledPlugins` block above load it.
- **Disable it for that one repo** — commit to its `.claude/settings.json`:
  ```jsonc
  { "enabledPlugins": { "forge@forge": false } }
  ```
  Project settings override user settings, so this wins over a global
  enable.

Even when the plugin *is* active in a forge-less repo, nothing is silently
broken: the **agents fail loudly** with a `forge-scripts not installed`
message (FOUNDATION §2), and the **safety hooks** (`block_*` / `check_*`)
are pure bash + jq — they need no env and are harmless everywhere.

## All three at once

```bash
install-forge-bootstrap     # CLAUDE.md scaffold + githooks + labels + gen docs + doctor
```

Idempotent and re-run-safe — it's also the upgrade re-sync step (below).

---

## What lands on disk — commit or gitignore?

| Artifact | Layer | Commit? |
|---|---|---|
| `FOUNDATION.md` | 1 | **commit** (shared engineering baseline) |
| `CLAUDE.md` | 1 | **commit** (yours after the initial scaffold; consumer-owned) |
| `.githooks/*` (wrappers) | 2 | **commit** (byte-stable across version bumps) |
| `.githooks/.forge-hook-version` | 2 | **gitignore** (per-clone version sidecar) |
| `docs/api-digest.md`, `docs/cli-reference.md` | 1 | **commit** (generated; checked for drift) |
| `code_health/*.log`, `audit_deps_tree.log` | 1 | **gitignore** (regenerated each run) |
| `.plan/CONTINUATION.md` | — | **gitignore** (cross-session handoff) |
| `.badges/docstring-coverage.svg` | 1 | commit only if you embed it in a README |

---

## How forge detects drift and refreshes

Re-running `install-forge-bootstrap` is safe because every managed
artifact is **drift-aware**, not blindly overwritten:

- **`CLAUDE.md` / `FOUNDATION.md`** — managed-block markers; `install-forge-claude-md --check` (run by the post-merge/checkout hooks) reports when the shipped foundation drifts from yours.
- **`.githooks/*`** — the `body-sha=` marker detects consumer edits so a refresh skips wrappers you changed; the gitignored `.forge-hook-version` sidecar records which forge version last wrote them (keeps the committed wrappers byte-stable).
- **Upstream-version staleness** — the hooks' preamble + `SessionStart` warn when the installed forge is behind the latest tag (and the cached Claude Code plugin is behind), so you know an upgrade is available.
- **Generated docs** — `forge-gen-*` write `docs/api-digest.md` / `cli-reference.md`; a pre-commit check fails if they drift from the code.

## Upgrading

Pick a channel and let `forge-upgrade` rewrite the pin + re-sync:

```bash
forge-upgrade --channel main          # phase 1: rewrite pin + print the pip command
# run the printed pip command yourself, then:
forge-upgrade --continue              # phase 2: install-forge-bootstrap re-sync
```

After a successful upgrade, forge prints the **⚠️ Upgrade notes** for the
new releases (the consumer-action items) — review any newer than your
previous version. Channel choice (`@main` minors-only vs `@dev` every
version vs a `@vX.Y.Z` pin) and the full flow:
[`Upgrading forge`](../README.md#upgrading-forge-in-your-repo). CI
integration: [`ci-recipe.md`](ci-recipe.md).
