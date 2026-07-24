# Cutting releases in a consumer repo (single-track, tag-versioned)

How a consumer repo whose version is **derived from `v*` git tags**
(setuptools-scm — no manual `version =` in `pyproject.toml`, no
`.claude-plugin/plugin.json`) cuts `vX.Y.Z` releases with forge instead
of hand-rolling the flow.

This is the single-track counterpart to forge's own release process
([`release-process.md`](release-process.md), forge-only): one trunk
(`base_branch`, default `main`), no dev→main promotion, the tag **is**
the release.

## `forge-release`

```bash
forge-release --bump patch|minor|major [--dry-run]
```

Run it on your base branch after the release-worthy work has merged. It
enforces, in order — all failures reported at once, exit `1`:

1. **Clean working tree.**
2. **On `base_branch`** (`[tool.forge].base_branch`, default `main`).
3. **Single-track release model** — refuses on a dual-track repo
   (`dev_branch != base_branch`) and on a manifest-versioned repo
   (`.claude-plugin/plugin.json` present → use `forge-next-prep --tag`).
4. **CHANGELOG gate** — when `CHANGELOG.md` exists, it must already
   carry a `## vX.Y.Z` heading for the tag being cut. A repo with no
   CHANGELOG gets a warning and proceeds.

Then it computes the tag (`next_version(latest_v_tag(...), bump)`),
creates it annotated on `HEAD`, and pushes it to `origin`. `--dry-run`
reports the tag that would be cut and stops.

The full release recipe — including which steps an agent may run —
is under ["Cutting a release — agent/human boundary"](#cutting-a-release--agenthuman-boundary)
below.

No config is required: a repo without `[tool.forge]` defaults to
single-track on `main`.

## Changelog convention

`forge-release` works with or without a `CHANGELOG.md`, but a repo that
keeps one should follow **one** convention — this one — so the CHANGELOG
gate, the release recipe, and any local tooling all agree on what the
file means. It follows [Keep a Changelog](https://keepachangelog.com/)
in spirit; the deliberate divergences are listed at the end.

### Format

- One level-2 heading per release: `## vX.Y.Z — YYYY-MM-DD`. The date
  may be added when the release is cut; `release_headings` recognizes
  the heading with or without it.
- Under each release heading, group entries as `### Added` /
  `### Changed` / `### Fixed` / `### Removed` — create a group when its
  first entry appears; omit empty groups.
- **No `## Unreleased` section.** The top heading always names the
  version **about to be released** — the CHANGELOG *declares* the next
  version, and the tag `forge-release` cuts *confirms* it. This is the
  single-track analogue of forge's own rolling-next invariant
  ([`release-process.md`](release-process.md), forge-only). Right after
  a release is cut, top heading and latest tag are **equal** — that
  window is valid; the **first PR after a tag opens the next
  `## vX.Y.Z` heading** (and carries its own entries under it).

### Per-PR rule

Every PR with a user-facing effect adds its bullet under the top
`## vX.Y.Z` heading **in that same PR** — never batched into a later
"release PR". The CHANGELOG is release-ready at all times: cutting a
release requires no CHANGELOG-only commit beyond (possibly) stamping
the date. Released headings below the top one are history — never edit
them.

### Cutting a release — agent/human boundary

```bash
git switch main && git pull --ff-only
# top `## vX.Y.Z` heading already matches the bump you intend
# (retitle it via a normal PR if the accumulated entries warrant a
# different increment than first declared)
forge-release --bump patch|minor|major --dry-run   # confirm the computed tag
forge-release --bump patch|minor|major             # tag + push — a human runs this
```

An agent prepares CHANGELOG entries and runs `--dry-run` only. Pushing
the tag is the user's action: a `v*` tag *defines* a version the moment
it lands, and there is no un-publishing it from environments that
already resolved it.

### Concurrent releases serialize through git

Two concurrent release branches both edit the same top heading, so git
forces a merge conflict; whoever resolves it moves their entries under
the next number. Deterministic ordering with no external coordinator —
the conflict is the feature, not a nuisance.

### Enforcement

Two opt-in pre-commit steps gate this convention between releases (see
[`configuration.md`](configuration.md), `[tool.forge.changelog]`):
`changelog_version` (heading validity, ordering, tag alignment, and
stranded-entry detection when a tag lands under an open PR) and
`changelog_updated` (the per-PR entry rule). `forge-release`'s CHANGELOG
gate remains the final check at cut time.

### Divergences from Keep a Changelog

- **No `## Unreleased` section** — the declared next version plays that
  role, and the tag gate in `forge-release` checks it mechanically.
- **Headings are `v`-prefixed and dated with an em dash**
  (`## v1.6.0 — 2026-07-01`), matching what `release_headings`
  recognizes, rather than KaC's bracketed `## [1.6.0] - 2026-07-01`.

## Choosing the bump

The same decision procedure forge applies to its own releases, stated
generically so any repo using `forge-release --bump` picks the
increment the same way.

**First, declare your public surface** — the set of things a consumer
of *your* repo can rely on: CLIs and their arguments, importable
APIs, file/output formats, config keys, documented behaviors. Write
the list down in your repo's docs; everything outside it is internal
and never drives the bump.

**The axis is *new capability* vs *new option on an existing one* —
NOT *any visible change*.** The single governing rule: **if a change
does not break or alter the base behavior of an already-adopted
capability, it is never a MINOR — it is a PATCH.**

- **PATCH (Z+1)** — a bug fix; a refactor with identical externals; or
  a new opt-in option layered on an existing capability that leaves
  its base behavior unchanged (a new flag / mode / config key, inert
  until set). A bug fix changes observable behavior almost by
  definition — that alone does not make it MINOR. Also PATCH: doc
  fixes; internal-only changes.
- **MINOR (Y+1, Z→0)** — a genuinely **new capability or artifact**
  that did not exist before, backward-compatible: a new CLI or
  subcommand, a new public API, a new opt-in check or pipeline stage.
  A new *option on an existing* capability is PATCH, not MINOR.
- **MAJOR (X+1, Y→0, Z→0)** — breaking: something in the public
  surface renamed / removed / signature-incompatible; semantics of an
  adopted behavior inverted or altered; any upgrade that requires the
  consumer to act beyond updating the pin.

**When in doubt**, ask the deciding question: *is this a new
capability, or a new option on an existing one?* Tags are free, but
inflation cuts both ways: a MINOR on an opt-in knob sends a consumer
reading `git log vX.Y.Z..vX.(Y+1).0` hunting for a feature that isn't
there, just as a misleading PATCH hides a real one.

## Stable public Python import surface

Repos that need a variation `forge-release` doesn't cover can compose
the same primitives it is built from. The following importable functions
are **public API under forge's semver policy** — signature or behavior
breaks are MAJOR releases:

| Symbol | Contract |
|---|---|
| `forge.git_utils.latest_v_tag(root)` | Highest `v*` tag by semver sort, or `None`. |
| `forge.git_utils.parse_semver(version)` | Leading `X.Y.Z` triple (optional `v`, suffix-tolerant), or `None`. |
| `forge.git_utils.next_version(latest_tag, bump)` | Pure semver bump: `"v1.2.3"` + `"minor"` → `"v1.3.0"`; `None` → `v0.0.0` base. |
| `forge.git_utils.run_git(*args, cwd=..., check=...)` | Run git, return stripped stdout; raises on failure when `check=True`. |
| `forge.git_utils.configure_cli_logging()` | Root logger at `INFO`, bare-message formatter; idempotent. |
| `forge.changelog.release_headings(text)` | Set of `vX.Y.Z` named in `##` release headings. |
| `forge.changelog.changelog_lacks_entry(changelog_text, tag)` | `True` when no `## <tag>` heading is present. |

Anything not in this table (underscore-prefixed or not) is internal and
may change in any release.
