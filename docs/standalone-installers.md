# Standalone installers (reference)

`install-forge-bootstrap` runs every installer + generator listed below
for you. The per-installer documentation here is for users who want to
run a single step manually (e.g. only re-syncing `FOUNDATION.md` after
a forge upgrade).

> **Fork-friendly note.** Each installer below is independently usable
> and idempotent. Forks that disable or replace one of them (e.g. ship
> a different label set) can leave the others untouched.

## Initialize `FOUNDATION.md` + `CLAUDE.md`

```bash
install-forge-claude-md
```

Writes (or updates) **two files** at your repo root:

- **`FOUNDATION.md`** — forge-managed, with START/END markers; do NOT
  edit. Re-running keeps it in sync with the installed forge version.
- **`CLAUDE.md`** — consumer-owned. If absent, a minimal scaffold is
  written:

  ```markdown
  # CLAUDE.md

  @FOUNDATION.md

  ---

  ## Repo-specific rules
  ```

  Claude Code's `@FOUNDATION.md` directive inlines the foundation text
  at session start, so the agent applies foundation rules without you
  copying them.

If `CLAUDE.md` already exists, the CLI leaves it alone but warns when
the `@FOUNDATION.md` include directive is missing.

**Why two files?** The previous (v1.1.2) layout kept the foundation as
an inline managed block at the top of `CLAUDE.md`. That mixed forge
content with consumer content, encouraged duplication, and made the
override boundary invisible. The split layout keeps each file fully
owned by one party.

**Migrating from v1.1.2 (inline block):**

```bash
install-forge-claude-md --migrate
```

This extracts the inline block, writes `FOUNDATION.md` from the shipped
foundation, and replaces the block in `CLAUDE.md` with `@FOUNDATION.md`.
Repo-specific rules below the END marker are preserved verbatim.

## Install the canonical GitHub labels

One time per repo:

```bash
install-forge-labels
```

Creates the canonical label schema (tier-1-critical, tier-2-high, …,
bug, feature, refactor, …) in your GitHub repo. Idempotent.

## Re-wire the git hooks (after a forge upgrade)

```bash
install-forge-githooks           # default — leaves user-customized hooks alone
install-forge-githooks --refresh # rewrite managed hooks regardless of content
install-forge-githooks --force   # rewrite even user-customized hooks (rare)
```

The `post-merge` hook auto-runs `--refresh --quiet` after every `git
pull`, so most users never need to invoke this manually.

## Verify the install

```bash
forge-doctor                       # full check (incl. Claude plugin)
forge-doctor --skip-plugin-checks  # if you don't use Claude Code
```

`forge-doctor` checks that all forge CLIs are on PATH and that `gh` is
authenticated. It also surfaces *under-used capabilities* — INFO-level
advice when a forge CLI is installed but its artifact is missing (e.g.
`forge-gen-api-digest` installed but `docs/api-digest.md` absent →
"run `install-forge-bootstrap`"). Advisory only; does not change the
exit code.

## When running in CI

Each installer is safe to invoke from a CI job. The ones with
dev-loop-only value (`forge-doctor`, `forge-audit-deps`) self-skip
under `install-forge-bootstrap` when
`forge.run_context.is_non_interactive()` returns true — no
`--skip doctor --skip audit-deps` boilerplate. See
[FOUNDATION §15](../FOUNDATION.md#15-runtime-context-awareness)
for the runtime-context contract.

Full pasteable CI workflows are in
[`ci-recipe.md`](ci-recipe.md).
