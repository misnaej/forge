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

**Fragment mode overrides the per-PR bump.** In
`[tool.forge.changelog].mode = "fragments"` the manifest **parks at the
latest tag between releases**: bump intent lives only in each PR's
conflict-free `changelog.d/` fragment, and the release PR — prepared by
`forge-changelog release` (§3) — is the single writer that advances
`plugin.json`. The guard's fragment-mode truth table:

| `plugin.json` vs latest tag | Verdict |
|---|---|
| `==` tag, every pending fragment valid (zero pending included) | healthy — pass |
| `>` tag | release window (the release PR) — pass |
| `<` tag | fragments mode: healthy while every pending fragment is valid (the manifest lags auto-cut tags until the next assembly PR); shared-heading mode: blocks |
| `==` tag, any invalid pending fragment | blocks loudly (bump no longer derivable) |

## 2. Tag-on-merge

Every fragment-carrying merge to `main` is a release: the `tag-main`
job runs `forge-changelog auto-tag`, which reads the last tag, takes
the strongest semver level among the fragments **new since that tag**
(tag-tree membership marks a fragment as consumed), bumps, and pushes
the annotated tag. No commit to `main` is involved — tag refs sit
outside the branch rulesets. Fragment files persist until an assembly
PR (`forge-changelog release`) collates the changelog and syncs the
manifest; between assemblies the manifest lags the tag by design.

- **Primary path**: the `tag-main` job in
  [`.github/workflows/tag-release.yml`](../.github/workflows/tag-release.yml)
  — after CI succeeds on a push to `main`, it checks out the exact
  CI-validated commit and runs `forge-changelog auto-tag`, then
  `forge-next-prep --tag` (which covers assembly-PR merges, where the
  manifest is ahead and no new fragments exist).
- **Opt-in / warn floor**: `[tool.forge.release].auto = "merge"` enables
  auto-tagging; without it the job still emits a loud pending-fragments
  warning — a fragments-mode repo can never accumulate unreleased
  merges silently.
- **Manual fallback**: `forge-changelog auto-tag` locally, or
  `forge-next-prep --tag` after an assembly.
- All paths are **idempotent and race-tolerant**: an existing or
  concurrently created tag defers with an "another runner won" no-op;
  nothing double-tags.

## 3. Changelog fragments

Forge runs `[tool.forge.changelog].mode = "fragments"`:

- Each PR ships one `changelog.d/<slug>.<type>.md` fragment. Its first
  line is `bump: patch|minor|major` — a bump *level* only; a concrete
  version number anywhere in a fragment (filename or body) is
  gate-rejected. Versions are written exactly once, by the assembler.
- **`CHANGELOG.md` is output, never input.** The assembler collates the
  pending fragments into one curated entry and stages their deletion;
  nothing reads `CHANGELOG.md` as a version or bump signal.
- **The version is assembler-owned too.** The next release is always
  `latest v* tag + max(bump level over pending fragments)`:
  - `forge-changelog next-version` — read-only print of the computed
    next version and its level.
  - `forge-changelog release` — computes the version, assembles
    `CHANGELOG.md` under it, writes `plugin.json` to it (the manifest's
    single writer; skipped in manifest-less tag-versioned repos), and
    stages everything. It never commits: branch → run it → ordinary PR
    → merge → tag-on-merge cuts the tag. Racing release PRs collapse
    into an ordinary PR conflict; the loser recovers by taking the
    BASE side of `CHANGELOG.md` and `plugin.json`, restoring its
    consumed fragments from the merge base
    (`git checkout $(git merge-base HEAD MERGE_HEAD) -- changelog.d/`),
    and re-running `forge-changelog release` — its own release commit
    already deleted its fragments, so a bare re-run has nothing to
    compute from.
  - `forge-changelog assemble --version vX.Y.Z --delete` remains the
    explicit-version core for flows that supply their own version.
- `forge-next-prep` logs a pending-fragment advisory (count + the
  release command) so accumulating fragments prompt a release.

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
| Fragment mode: `plugin.json <= latest tag` passes with valid pending fragments (zero included); an invalid fragment blocks even below the tag; shared-heading equality still fails | `verify_plugin_version._not_ahead_verdict` | `tests/test_verify_plugin_version.py::test_fragments_mode_manifest_at_tag_with_valid_pending_passes` / `::test_fragments_mode_manifest_at_tag_with_zero_pending_passes` / `::test_fragments_mode_invalid_fragment_fails_listing_error` / `::test_fragments_mode_manifest_below_tag_with_valid_fragments_passes` / `::test_fragments_mode_manifest_below_tag_invalid_fragment_fails` / `::test_headings_mode_manifest_at_tag_still_fails` |
| The release version is `latest tag + max(pending fragment level)` — computed, never carried per-PR | `changelog_fragments.next_version_from_fragments` | `tests/test_changelog_fragments.py::test_next_version_from_fragments_uses_max_level` |
| A branch adds at most ONE fragment (one unique `changelog.d/` file per PR; extra bullets share it) | `changelog_fragments.branch_added_fragments` via the `changelog_version` fragment gate | `tests/test_precommit.py::test_fragment_gate_blocks_second_branch_added_fragment` |
| `forge-changelog release` assembles under the computed version, rewrites + stages the manifest (single writer), and never commits | `changelog_fragments._cmd_release` | `tests/test_changelog_fragments.py::test_main_release_with_manifest_stages_everything_commits_nothing` |
| The release-commit skip tolerates a `CHANGELOG.md`/`changelog.d/`-only divergence from the tag — a release commit may assemble the changelog — yet still fails when any other file diverges | `git_utils.release_tree_fingerprint` via `verify_plugin_version._is_release_commit` | `tests/test_verify_plugin_version.py::test_skips_when_release_branch_only_adds_changelog` / `::test_fails_when_release_branch_changes_non_changelog_file`; `tests/test_git_utils.py::test_release_fingerprint_equal_when_only_changelog_differs` / `::test_release_fingerprint_differs_when_other_file_changes` |

When you add a versioning behavior, add a row here **and** its test. When
you find an invariant with no test, that gap is a bug to close.

### Retired invariants (dual-track)

The dual-track model — a `dev` integration branch promoted into `main`
per minor — is retired and its machinery deleted: promotion status and
staged catch-up, minor tag relocation (`forge-check-main-tags`),
changelog-history preservation across promotion merges
(`verify-forge-changelog-history`), the newest-minor hold, the
`/promote` skill, and the era-gap pre-commit suppression are gone, with
their tests. The release fingerprint
(`git_utils.release_tree_fingerprint`) is **not** retired: the
rolling-next guard's release-commit skip still depends on its
changelog-tolerant matching (table above) — only the tag aligner's use
of it retired.
