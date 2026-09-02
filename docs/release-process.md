# Release process (forge-only)

**This is the single source of truth for forge's versioning and release
cadence.** It is the *spec*; the code conforms to it. Every invariant
below names the **test that enforces it** — the executable spec that goes
red if code drifts. If you change versioning or release code, change this
doc and its tests **first**, then make the code match.

> Forge-only. The single-track rolling-next convention is specific to
> forge; consumer plugin authors may use trunk-based, gitflow, or another
> model. CLAUDE.md's release bullets **point here** — they do not restate
> the mechanics (FOUNDATION §12, single source of truth).

---

## 1. Rolling-next versioning on `main`

`.claude-plugin/plugin.json["version"]` **always names the version about
to be released** — never the last-released version.

- The pre-commit step `plugin_version` (`verify-forge-plugin-version`)
  enforces `plugin.json["version"] > latest tag` on every commit. The
  guard skips when HEAD's tree reproduces a tagged release.
- After a release tags `vX.Y.Z`, the next PR must bump `plugin.json` to
  the next rolling-next version, or its commits fail the guard.
- Surviving feature branches hit the same version-slot collision on every
  merge — the mechanical resolution is **`forge-rebump`**; the two-state
  mechanics live in the `src/forge/rebump.py` module docstring.

## 2. Tag-on-merge

Every merge to `main` is a release: the squash commit is tagged with the
version `plugin.json` declares.

- **Primary path**: the `tag-main` job in
  [`.github/workflows/tag-release.yml`](../.github/workflows/tag-release.yml)
  — after CI succeeds on a push to `main`, it checks out the exact
  CI-validated commit and runs `forge-next-prep --tag`.
- **Manual fallback**: `forge-next-prep --tag` (run by `/next`) tags and
  pushes locally when the workflow has not done it.
- Both paths are **idempotent**: a tag is cut only when `plugin.json` is
  strictly ahead of the latest `v*` tag; otherwise the step is a no-op.

## 3. Changelog fragments

Forge runs `[tool.forge.changelog].mode = "fragments"`:

- Each PR ships one `changelog.d/<slug>.<type>.md` fragment. Its first
  line is `bump: patch|minor|major` — a bump *level* only; a concrete
  version number anywhere in a fragment (filename or body) is
  gate-rejected. Versions are written exactly once, by the assembler.
- **`CHANGELOG.md` is output, never input.** `forge-changelog assemble
  --version vX.Y.Z --delete` collates the pending fragments into one
  curated entry and stages their deletion. The assembler currently runs
  at explicit release moments, not on every per-merge tag; nothing reads
  `CHANGELOG.md` as a version or bump signal.

## 4. Invariants the code MUST satisfy → enforcing tests

This table is the anti-regression contract. **Do not change a behavior in
the left column without its test (right column) staying green** — a
change that violates an invariant must turn its test red.

| Invariant | Where | Enforcing test |
|---|---|---|
| Latest tag resolved **globally** (semver-max, never ancestry-scoped `git describe`) so the guard and the auto-tagger agree | `git_utils.latest_v_tag` | `tests/test_git_utils.py::test_latest_v_tag_returns_highest_sorted` |
| Rolling-next guard skips when HEAD's tree reproduces **ANY** `v*` tag (not only the latest) | `verify_plugin_version._is_release_commit` | `tests/test_verify_plugin_version.py::test_main_skips_when_head_reproduces_older_tag` |
| Guard fails when a real content change leaves `plugin.json ≤ latest tag` | `verify_plugin_version.main` | `tests/test_verify_plugin_version.py::test_fail_when_version_not_strictly_greater` |
| `forge-next-prep --tag` tags + pushes only when `plugin.json` is strictly newer than the latest tag (idempotent) | `next_prep._maybe_tag_release` | `tests/test_next_prep.py::test_maybe_tag_release_creates_and_pushes_new_tag` |
| The fragment gate rejects a concrete version number in a fragment's filename or body | `changelog_fragments.validate_fragment` | `tests/test_changelog_fragments.py::test_validate_fragment_version_shaped_filename` / `::test_validate_fragment_version_shaped_body` |
| An invalid fragment fails the gate (exit 2) | `changelog_fragments.main` | `tests/test_changelog_fragments.py::test_main_check_exit_two_on_invalid_fragment` |
| `assemble --delete` writes the curated entry into `CHANGELOG.md` and stages the fragment deletions | `changelog_fragments.main` | `tests/test_changelog_fragments.py::test_main_assemble_with_delete_stages_changelog_and_fragment_deletion` |

When you add a versioning behavior, add a row here **and** its test. When
you find an invariant with no test, that gap is a bug to close.

### Retired invariants (dual-track)

The dual-track invariants — promotion status and staged catch-up, minor
tag relocation (`forge-check-main-tags`), release-fingerprint tolerances,
changelog-history preservation across promotion merges, and the
newest-minor hold — are retired. Their machinery self-skips on a
single-track repo (`dev_branch == base_branch`) and is scheduled for
deletion along with its tests (see below).

## Deprecated: dual-track

Forge previously shipped on two branches — `dev` (every patch) and `main`
(minors only, via a staged `dev → main` promotion). That model is
retired: the `dev` branch is frozen, and every release now ships on
`main`. The promotion machinery (`/promote`, `forge-check-main-tags`,
`forge-next-prep --promotion-status`, `verify-forge-changelog-history`)
self-skips because `dev_branch == base_branch`; its deletion is
scheduled (#441 Phase C).
