# Adopting forge — install exactly the layer(s) you want

Forge is **three separable layers**. Each is independently installable and
useful on its own — you do not have to take all three, and the first two
work with **no AI agent at all**. `install-forge-bootstrap` runs all three
in order, but this page lets you follow only the track(s) you want, each
ending in a verification step.

| Layer | What it is | Needs Claude Code? | Standalone installer |
|---|---|---|---|
| **1 — pip CLIs** | `forge-scripts` — verification/generator CLIs + the pre-commit dispatcher | no | `pip install` |
| **2 — git hooks** | `.githooks/*` (pre-commit + post-merge/checkout) | no | `install-forge-githooks` |
| **3 — Claude Code plugin** | agents, slash-command skills, safety hooks | yes | `/plugin install` |

Layer 2 builds on layer 1 (the hooks call the CLIs). Layer 3 is fully
optional — forge's gate is the CLI, never the model.

---

## Track 1 — the CLIs only

The deterministic checks, runnable locally or in CI, no hooks, no plugin.

```bash
# Pin in your pyproject.toml [project.optional-dependencies] (or install directly):
#   forge-scripts @ git+https://github.com/misnaej/forge.git@main
pip install -e ".[dev]"     # your repo's equivalent — never run this from an agent
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

## Track 3 — add the Claude Code plugin

Agents, skills, and safety hooks, in Claude Code only.

```text
/plugin marketplace add misnaej/forge
/plugin install forge@forge
/reload-plugins
```

**Verify:** `forge-doctor` reports `plugin:installed` + populated
`agents/` / `skills/` / `claude-hooks/`. Consumer-specific Claude Code
hooks live under `.claude/hooks/` with `${CLAUDE_PROJECT_DIR}`-rooted
paths — see [`claude-code-plugin.md`](claude-code-plugin.md).

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
| `.badges/DocstringCoverage.svg` | 1 | commit only if you embed it in a README |

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
