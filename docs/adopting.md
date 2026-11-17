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
| **3 — agent integration** | agents, slash-command skills, safety guards | yes | `install-forge-claude` |

### Layer 1 is the prerequisite — and what breaks without it

`forge-scripts` must be installed **in the Python environment active when
the hook or agent runs**. Both higher layers shell out to its CLIs:

- **Layer 2 (git hooks)** — the committed `.githooks/*` wrappers run
  `forge-precommit` (and the foundation drift check) on commit / merge. If
  the active env lacks `forge-scripts`, the hook **fails loudly**
  (`forge-precommit: command not found`). *Downside:* a teammate who
  clones the repo but hasn't installed the env has a broken commit gate
  until they install layer 1.
- **Layer 3 (agent integration)** — the agents and skills orchestrate
  the CLIs (`forge-precommit-fixer` → `forge-precommit`,
  `/forge-commit` → `forge-commit`, …), so they are only useful where
  `forge-scripts` is installed. They are **generated into the repo**
  (Track 3) from the package in its own environment, so there is nothing
  to install per machine and no release to keep in step. *Downside:*
  generate them in a repo whose env lacks `forge-scripts` and the agents
  fail the moment they invoke a CLI. The **safety guards** (`block_*`,
  `check_*`) are the exception — bash + jq, they need neither layer 1
  nor 2.

In short: **1 alone is fine; 2 and 3 are inert (or broken) without 1.**

---

## Track 1 — the CLIs only

The deterministic checks, runnable locally or in CI, no hooks, no agents.

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
forge-doctor            # environment diagnostics: CLIs on PATH, gh, optional integration
forge-precommit         # run the check sequence once, by hand
forge-config --list     # every [tool.forge.*] key forge reads + what to set
```

Configure via `[tool.forge]` in `pyproject.toml` — see
[`configuration.md`](../forge-docs/configuration.md). Per-CLI usage:
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
[`customizing-precommit.md`](customizing-precommit.md). Drop-in scripts
(executable `*.sh` files) run from the repo root in sorted filename order and inherit the shell's
environment; one that fails logs a warning and is skipped.
`install-forge-githooks --force` overwrites edited wrappers and keeps the
old one as `.githooks/<name>.before-forge-vX.Y.Z.bak`.

## Track 3 — add the agent integration (generated, nothing installed)

Agents, skills, and safety guards for Claude Code. They shell out to the
layer-1 CLIs, so this layer is only useful where `forge-scripts` is
installed.

`install-forge-bootstrap` does it for you (its `claude` step):

```bash
install-forge-claude          # write the integration into .claude/
install-forge-claude --check  # report drift only (exit 1 when stale)
```

It writes the agents, skills and guard scripts from the `forge-scripts`
package already in your environment, and registers the guards in
`.claude/settings.json` under `${CLAUDE_PROJECT_DIR}`. The version you
have installed is the version you run — no marketplace, no per-checkout
install, no cache that can go cold. The `post-merge` / `post-checkout`
hooks re-run it, so moving your pin moves the integration; restart the
session afterwards to load it.

It owns only what it generates, listed in `.claude/.gitignore`. Your own
agents, skills and hooks share those directories untouched — just don't
prefix your own files `forge-`, which is what every generated name
carries. `settings.json` stays tracked, because it holds your permissions
too. Details and the guard wiring: [`claude-code.md`](claude-code.md).

Using Mistral Vibe instead of (or alongside) Claude Code? Set
`[tool.forge.vibe] enabled = true` and the bootstrap `vibe` step generates
`.vibe/` hooks + agents, `.agents/skills/` and a flattened `AGENTS.md` —
see [docs/vibe.md](vibe.md).

**Verify:** `forge-doctor` reports `claude:integration` as current. All
hooks live under `.claude/hooks/` with `${CLAUDE_PROJECT_DIR}`-rooted
paths, yours beside forge's.

### Repos that do NOT use forge

Nothing to do. The integration exists only where it was generated, and
nothing is installed machine-wide, so a repo without forge has no forge
agents and no forge guards.

Upgrading from a forge that shipped a plugin? Remove the old install
once (`claude plugin uninstall forge@forge --scope user`, and `--scope
local` in each checkout that had its own). `install-forge-claude` clears
the settings keys that named it.

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
| `forge-docs/` | 1 | **commit** (mirrored forge reference pages + README; never edit — refreshed by upgrades, agent edits hook-blocked) |
| `CLAUDE.md` | 1 | **commit** (yours after the initial scaffold; consumer-owned) |
| `.githooks/*` (wrappers) | 2 | **commit** (byte-stable across version bumps) |
| `.githooks/.forge-hook-version` | 2 | **gitignore** (per-clone version sidecar) |
| `docs/api-digest.md`, `docs/cli-reference.md` | 1 | **commit** (generated; checked for drift) |
| `code_health/*.log`, `audit_deps_tree.log` | 1 | **gitignore** (regenerated each run) |
| `.plan/CONTINUATION.md` | — | **gitignore** (cross-session handoff; its status panel is written by `forge-continuation`, which refuses to write the file unless it is ignored) |
| `.badges/docstring-coverage.svg` | 1 | commit only if you embed it in a README |

---

## How forge detects drift and refreshes

Re-running `install-forge-bootstrap` is safe because every managed
artifact is **drift-aware**, not blindly overwritten:

- **`CLAUDE.md` / `FOUNDATION.md`** — managed-block markers; `install-forge-claude-md --check` (run by the post-merge/checkout hooks) reports when the shipped foundation drifts from yours.
- **`forge-docs/`** — same check reports hand-edits to the mirrored reference pages; the next sync heals them.
- **`.githooks/*`** — the `body-sha=` marker detects consumer edits so a refresh skips wrappers you changed; the gitignored `.forge-hook-version` sidecar records which forge version last wrote them (keeps the committed wrappers byte-stable).
- **Upstream-version staleness** — the hooks' preamble + `SessionStart` warn when the installed forge is behind the latest tag, so you know an upgrade is available. Forge cuts a tag on every merge, so every tag is adoptable (see [Which tags you can adopt](#which-tags-you-can-adopt)).
- **Generated docs** — `forge-gen-*` write `docs/api-digest.md` / `cli-reference.md`; a pre-commit check fails if they drift from the code.

## Upgrading

Pick a channel and let `forge-upgrade` rewrite the pin + re-sync:

```bash
forge-upgrade --channel main          # phase 1: rewrite pin + print the install command
# run the printed command yourself (pip, or the pixi steps in a pixi-locked repo), then:
forge-upgrade --continue              # phase 2: install-forge-bootstrap re-sync
```

> **Moving a pin is not atomic, and the failure mode is sharp.** Your
> installer may remove the installed forge before the new version
> finishes building. If that build fails you are left with **no forge** —
> and in a repo whose commit path runs through forge's pre-commit hook,
> the recovery cannot use the tooling. Keep the previous pin and lock
> file to hand so you can restore them, and prefer upgrading in a branch
> where reverting the pin is one revert. This is tracked, and is not
> specific to any one release.

After a successful upgrade, forge prints the **⚠️ Upgrade notes** for the
new releases (the consumer-action items) — review any newer than your
previous version, review the diff of managed files, and commit it. If
the upgrade regenerated the integration, **restart the Claude Code
session**: a running session keeps the files it loaded at startup
([integration staleness](../forge-docs/integration-staleness.md)). CI
integration: [`ci-recipe.md`](../forge-docs/ci-recipe.md).

A repo that pins through `requirements.txt` or a lockfile runs the
phase-1 install command by hand, then `forge-upgrade --continue`.
`forge-upgrade --apply` does both phases in one call (force-reinstall
plus re-sync) for setup scripts; it is refused in a pixi-locked repo,
and agents never run it, because they never install packages.

### Pick a pin

| Pin | When |
|---|---|
| `@vX.Y.Z` (a release tag) | The default. Upgrades are explicit and comparable. |
| `@main` | Following every merge. pip caches by name and version, and a branch ref does not change the version, so a plain `pip install` silently keeps an old build; `forge-doctor` can look healthy while you are behind. Use `forge-upgrade` to refresh. |

Switching from a branch pin to a tag has a trap: a refresh script that
force-reinstalls only branch-pinned packages stops refreshing forge. So
`forge-upgrade` checks the installed build's recorded revision
(`direct_url.json`) against the pin — phase 1 warns, `--continue`
refuses to regenerate from a stale install, and `forge-doctor` reports
it. A `.devN+gHASH` version is the fingerprint of a branch install.

Forge never rewrites managed files on its own when it drifts: the hooks
warn, and you run the sync and commit the result. The upstream-version
check is cached for 24 hours (`~/.cache/forge/upstream_check.json`),
only ever warns, and prints the upgrade command.

### Which tags you can adopt

**Every `v*` tag is adoptable.** A tag is cut on every merge into the
default branch, from the changelog fragments that merge brought, and the
tag is the only place the version lives. Each one is a commit CI passed.

What a tag does **not** always carry is its own changelog entry. The
prose is assembled afterwards, by a separate pull request, so a recent
tag normally ships a `CHANGELOG.md` whose newest heading is the previous
release. `forge-upgrade` says so and continues — it does not refuse:

```
v14.0.1's own changelog entry is not assembled yet: the changelog it
ships stops at v14.0.0, so the upgrade notes below cover up to there …
```

When that appears, read the repository's `CHANGELOG.md` — or its pending
`changelog.d/` fragments — for what changed in between. Nothing is
hidden; it is written somewhere other than the tag you pinned.

> Tracking "the latest tag" therefore means following every merge. A
> moving pointer at the latest release considered fit to adopt is
> proposed but not built; until then, pin a version deliberately and
> move it in a reviewed change.

### Leaving an older plugin install behind

Forge used to ship as a Claude Code plugin, installed per checkout from
a marketplace. Nothing reads those installs any more. To clean up, move
the `forge-scripts` pin to an adoptable tag, run `install-forge-claude`,
and uninstall the old plugin — `claude plugin uninstall forge@forge
--scope user`, plus `--scope local` in each checkout that had its own.
`install-forge-claude` clears the settings keys that named it.

Then restart the Claude Code session: a running session keeps what it
loaded at startup, and `/reload-plugins` is not enough.
