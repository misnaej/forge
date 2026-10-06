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
conflict-free `changelog.d/` fragment, and the release PR — opened by
`forge-changelog release-pr` (§3) — is the single writer that advances
`plugin.json`. The guard's fragment-mode truth table:

| `plugin.json` vs latest tag | Verdict |
|---|---|
| `==` tag, every pending fragment valid (zero pending included) | healthy — pass |
| `>` tag | release window (the release PR) — pass |
| `<` tag | fragments mode: healthy while every pending fragment is valid (only reachable through tags cut before releases were tagged at the assembly merge, or cut by hand); shared-heading mode: blocks |
| `==` tag, any invalid pending fragment | blocks loudly (bump no longer derivable) |

## 2. Tag at the assembly merge

**A release tag is cut only when a release (assembly) PR merges**, and
it points at that merge: the one commit whose tree names the new
version in `plugin.json` and documents it in `CHANGELOG.md`. So every
tag `vX` carries `plugin.json == X` and a changelog whose top heading
is `vX` — the identity Claude Code keys its plugin cache on, and the
changelog `forge-upgrade` reads, both describe the tag they ship in.

Ordinary merges are not releases. Their fragments wait in
`changelog.d/`, untagged, until someone runs `forge-changelog
release-pr` (§3) — assembly is manual.

- **Primary path**: the `tag-main` job in
  [`.github/workflows/tag-release.yml`](../.github/workflows/tag-release.yml)
  — after CI succeeds on a push to `main`, it checks out the exact
  CI-validated commit and runs `forge-next-prep --tag`, which tags
  `v<plugin.json>` when the manifest is strictly ahead of the latest
  tag (true only on the assembly merge) and otherwise logs why it did
  not tag. No commit to `main` is involved — tag refs sit outside the
  branch rulesets.
- **Why not tag every merge**: a per-merge tag lands on a tree whose
  `plugin.json` still names the previous release, so consumers pinned
  to it get a plugin declaring an older version (a reused cache slot)
  and a changelog that never documents it. `forge-changelog auto-tag`
  therefore never tags a repo with a plugin manifest: it logs the
  pending-fragment count and the release command instead, and says
  that `[tool.forge.release].auto = "merge"` is ignored there.
  Tag-per-merge stays available to manifest-less repos, whose version
  comes from the tag alone.
- **Tags from before this rule** (cut per merge) are left in place and
  never moved. Their own trees name an older version, so
  `forge-upgrade` refuses them; adopt a tag cut at an assembly merge.
- **Manual fallback**: `forge-next-prep --tag` after an assembly merge.
  Idempotent: an existing tag is never re-cut.

## 3. Changelog fragments

Forge runs `[tool.forge.changelog].mode = "fragments"`:

- Each PR ships one `changelog.d/<slug>.<type>.md` fragment. Its first
  line is `bump: patch|minor|major` — a bump *level* only; a concrete
  version number anywhere in a fragment (filename or body) is
  gate-rejected. Versions are written exactly once, by the assembler.
- **`CHANGELOG.md` is output, never input.** The assembler collates the
  pending fragments into one curated entry and stages their deletion;
  nothing reads `CHANGELOG.md` as a version or bump signal.
- **The version is assembler-owned too, and tag-aware.** A pending
  fragment already inside a `v*` tag's tree was released by that tag
  (a tag cut per merge counted it — a manifest-less repo, or a
  tag from before §2's rule) and assembles under that tag's heading,
  dated when the tag was cut. Only fragments no tag holds mint a new
  version: `latest v* tag + max(bump level over the unreleased
  fragments)`. When every pending fragment is tagged, nothing is minted
  — the assembly backfills the per-tag headings and syncs `plugin.json`
  to the latest tag (the guard's healthy at-tag, zero-pending state):
  - `forge-changelog next-version` — read-only print of the computed
    next version and its level, or `vX.Y.Z (already tagged — N
    fragment(s) across M tag(s); nothing to mint)`.
  - `forge-changelog release` — the staging half of the same work, for
    when you want to inspect the assembly before it becomes a commit. It
    computes the plan, assembles `CHANGELOG.md` (one heading per
    already-cut tag, then the minted heading on top), writes
    `plugin.json` to the plan's version (the manifest's single writer;
    skipped in manifest-less tag-versioned repos), and stages everything.
    It never commits: branch → run it → ordinary PR → merge →
    the tag job cuts the tag at that merge (§2). Racing release PRs collapse
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
- **This is how a release is cut**, and it is opened by hand: run
  `forge-changelog release-pr` when you want one — guard, branch
  `chore/assemble-vX.Y.Z`, stage, commit, push, PR with in-body gate
  evidence. Idempotent (open assembly PR or nothing pending → quiet
  no-op); merging stays human. Forge runs no schedule for this: a
  release is reviewed and tested before it ships, and a cron that
  assembles unattended takes that decision away. Tagging stays
  automatic (§2); assembling does not.

## 4. Invariants the code MUST satisfy → enforcing tests

This table is the anti-regression contract. **Do not change a behavior in
the left column without its test (right column) staying green** — a
change that violates an invariant must turn its test red.

| Invariant | Where | Enforcing test |
|---|---|---|
| Latest tag resolved **globally** (semver-max, never ancestry-scoped `git describe`) so the guard and the auto-tagger agree | `git_utils.latest_v_tag` | `tests/test_git_utils.py::test_latest_v_tag_returns_highest_sorted` |
| Rolling-next guard skips when HEAD's tree reproduces **ANY** `v*` tag (not only the latest) | `verify_plugin_version._is_release_commit` | `tests/test_verify_plugin_version.py::test_main_skips_when_head_reproduces_older_tag` |
| Guard fails when a real content change leaves `plugin.json ≤ latest tag` | `verify_plugin_version.main` | `tests/test_verify_plugin_version.py::test_fail_when_version_not_strictly_greater` |
| A declared `plugin.json` version must have a matching `## vX.Y.Z` heading in `CHANGELOG.md` — existence only, never currency (a parked manifest keeps its heading and passes; staleness is the scheduled assembly's job). Applied to every healthy exit via `main()`'s `return rc or _declared_version_documented(...)` | `verify_plugin_version._declared_version_documented` | `tests/test_verify_plugin_version.py::test_declared_version_documented_fails_with_missing_heading` / `::test_main_fails_when_declared_version_undocumented_in_fragment_mode` |
| Every release tag `vX` is cut at an assembly merge whose own tree has `plugin.json == X` and a `CHANGELOG.md` whose top heading is `vX`, so `forge-upgrade` accepts it — ordinary merges in a plugin repo are never tagged | `changelog_fragments._cmd_auto_tag` + `next_prep._maybe_tag_release` | `tests/test_release_tag_invariant.py::test_every_tag_matches_manifest_and_changelog` / `tests/test_changelog_fragments.py::test_main_auto_tag_never_tags_a_plugin_manifest_repo` |
| `forge-next-prep --tag` tags + pushes only when `plugin.json` is strictly newer than the latest tag (idempotent), and states why whenever it does not tag | `next_prep._maybe_tag_release` | `tests/test_next_prep.py::test_maybe_tag_release_creates_and_pushes_new_tag` / `::test_maybe_tag_release_skips_when_version_equals_latest_tag` / `::test_maybe_tag_release_skips_when_version_behind_latest_tag` / `::test_tag_and_report_logs_reason_when_no_tag` |
| The fragment gate rejects a concrete version number in a fragment's filename or body | `changelog_fragments.validate_fragment` | `tests/test_changelog_fragments.py::test_validate_fragment_version_shaped_filename` / `::test_validate_fragment_version_shaped_body` |
| An invalid fragment fails the gate (exit 2) | `changelog_fragments.main` | `tests/test_changelog_fragments.py::test_main_check_exit_two_on_invalid_fragment` |
| `assemble --delete` writes the curated entry into `CHANGELOG.md` and stages the fragment deletions | `changelog_fragments.main` | `tests/test_changelog_fragments.py::test_main_assemble_with_delete_stages_changelog_and_fragment_deletion` |
| Fragment mode: `plugin.json <= latest tag` passes with valid pending fragments (zero included); an invalid fragment blocks even below the tag; shared-heading equality still fails | `verify_plugin_version._not_ahead_verdict` | `tests/test_verify_plugin_version.py::test_fragments_mode_manifest_at_tag_with_valid_pending_passes` / `::test_fragments_mode_manifest_at_tag_with_zero_pending_passes` / `::test_fragments_mode_invalid_fragment_fails_listing_error` / `::test_fragments_mode_manifest_below_tag_with_valid_fragments_passes` / `::test_fragments_mode_manifest_below_tag_invalid_fragment_fails` / `::test_headings_mode_manifest_at_tag_still_fails` |
| The release version is `latest tag + max(level over UNRELEASED fragments)` — computed, never carried per-PR; fragments already in a tag's tree never bump again | `changelog_fragments.plan_assembly` | `tests/test_changelog_fragments.py::test_plan_assembly_uses_max_level_of_untagged` / `::test_plan_assembly_all_tagged_mints_nothing` |
| Pending fragments partition by the earliest tag whose tree holds them; each group assembles under that tag's heading (dated from the tag), the minted heading lands on top | `changelog_fragments._partition_by_release_tag` via `_render_assembly` | `tests/test_changelog_fragments.py::test_plan_assembly_partitions_by_earliest_tag` / `::test_main_release_backfills_tag_headings_and_syncs_manifest` |
| A branch adds at most ONE fragment (one unique `changelog.d/` file per PR; extra bullets share it) — counted as added since the fork with the PR's *real* base (the open PR's target when one exists, so a stacked PR never counts its parent's fragment; else the configured base) AND absent from that base tip's tree, so a conflicted base merge never counts base-side fragments. The gate is reached on a manifest-versioned repo too: fragments mode is judged before the manifest short-circuit, which otherwise skipped the whole check | `changelog_fragments.branch_added_fragments` via the `changelog_version` fragment gate | `tests/test_precommit.py::test_fragment_gate_blocks_second_branch_added_fragment` / `tests/test_changelog_fragments.py::test_branch_added_fragments_excludes_base_fragments_mid_merge` |
| `release-pr` opens exactly one assembly PR: an already-open one (found up front or via a lost push/create race) defers with exit 0; nothing pending is a quiet 0; guard failures exit 2 | `changelog_fragments._cmd_release_pr` | `tests/test_changelog_fragments.py::test_main_release_pr_defers_to_open_assembly_pr` / `::test_main_release_pr_nothing_pending_is_quiet_noop` |
| `forge-changelog release` assembles under the computed version, rewrites + stages the manifest (single writer), and never commits | `changelog_fragments._cmd_release` | `tests/test_changelog_fragments.py::test_main_release_with_manifest_stages_everything_commits_nothing` |
| The release-commit skip tolerates a `CHANGELOG.md`/`changelog.d/`-only divergence from the tag — a release commit may assemble the changelog — yet still fails when any other file diverges | `git_utils.release_tree_fingerprint` via `verify_plugin_version._is_release_commit` | `tests/test_verify_plugin_version.py::test_skips_when_release_branch_only_adds_changelog` / `::test_fails_when_release_branch_changes_non_changelog_file`; `tests/test_git_utils.py::test_release_fingerprint_equal_when_only_changelog_differs` / `::test_release_fingerprint_differs_when_other_file_changes` |
| A consumer (no manifest of its own) is judged on **content**: a per-area hash (agents, skills, claude-hooks, and the manifest minus its `version`) of the cache slot this repo's install record names against the marketplace clone the pin resolves to — the declared version identifies nothing under fragments mode, and the advisory names the differing areas plus a slot-deletion remedy, never `/plugin update` | `version_surfaces._consumer_cache_status` via `doctor._check_plugin_cache_skew` | `tests/test_version_surfaces.py::test_plugin_cache_status_consumer_names_stale_content_areas` / `tests/test_doctor.py::test_plugin_cache_skew_names_stale_areas_for_a_consumer` |

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
