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

A typical release is therefore:

```bash
git switch main && git pull --ff-only
# author the `## vX.Y.Z` CHANGELOG entry, commit it via a PR
forge-release --bump minor --dry-run   # confirm the computed tag
forge-release --bump minor             # tag + push
```

No config is required: a repo without `[tool.forge]` defaults to
single-track on `main`.

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
