# Release process (forge-only)

**This is the single source of truth for forge's versioning and release
cadence.** It is the *spec*; the code conforms to it. Every invariant
below names the **test that enforces it** — the executable spec that goes
red if code drifts. If you change versioning or release code, change this
doc and its tests **first**, then make the code match.

> Forge-only. The single-track tag-per-merge convention is specific to
> forge; consumer plugin authors may use trunk-based, gitflow, or another
> model. CLAUDE.md's release bullets **point here** — they do not restate
> the mechanics (FOUNDATION §12, single source of truth).

---

## 1. Plugin identity is the commit

Forge's `.claude-plugin/plugin.json` **declares no `version`**. Claude
Code resolves a plugin's version from the first of: the manifest's
`version`, the marketplace entry's `version`, then "the git commit SHA of
the plugin's source, for … relative-path sources in a git-hosted
marketplace" (plugins-reference, *Version management*). Forge's plugin
source is `"./"` in its GitHub marketplace, so its identity is the commit
the consumer's marketplace resolves to. Every commit is a distinct plugin
version: moving the pin — or refreshing a marketplace that tracks a
branch — and then running `/plugin update` always installs the new
content. The semver release line lives in the `v*` tags (§2) and in the
pip version setuptools-scm derives from them.

A declared version cannot work with tag-per-merge. The tag is cut after
the merge, so no committed manifest can name the tag that ships it, and
Claude Code treats two commits with one declared version as the same
plugin — `/plugin update` answers "already at the latest version" and
installs nothing.

**Plugin freshness follows the commit too.** For forge itself, the
installed commit (the `installed_plugins.json` record for this repo) is
`behind` when `origin/<base>` changed the plugin surface —
`.claude-plugin/`, `agents/`, `skills/`, `claude-hooks/` — since it; the
`plugin_sync` pre-commit step blocks on that (`[tool.forge.plugin_sync]
.blocking = true`) until `/plugin marketplace update forge`, `/plugin
update forge@forge` and `/reload-plugins`. It fires after each merge that
touches the plugin surface, not once per release. An installed commit
this clone does not know, or one that is not an ancestor of the base, is
no finding. A consumer repo (no manifest of its own) is judged on the
installed commit against the marketplace clone's `HEAD`, after checking
the machine's registration serves the ref the consumer pins: Claude Code
keeps one registration per marketplace name per user, so another repo can
hold it at a different ref.

### Declared-version manifests (shipped tooling, not forge)

The tooling still supports plugin repos whose manifest declares a
version; everything in this subsection applies **only** to them. There
`.claude-plugin/plugin.json["version"]` names the version about to be
released:

- The pre-commit step `plugin_version` (`verify-forge-plugin-version`)
  enforces `plugin.json["version"] > latest tag` on every commit. The
  guard skips when HEAD's tree reproduces a tagged release, and skips a
  manifest that declares no version entirely — fragment validity is then
  the `changelog_version` gate's alone.
- After a release tags `vX.Y.Z`, the next PR must bump `plugin.json` to
  the next rolling-next version, or its commits fail the guard.
- Surviving feature branches hit the same version-slot collision on every
  merge — the mechanical resolution is **`forge-rebump`**; the two-state
  mechanics live in the `src/forge/rebump.py` module docstring.

**Fragment mode overrides the per-PR bump.** In
`[tool.forge.changelog].mode = "fragments"` the manifest **parks at or
behind the latest tag between releases**: bump intent lives only in each
PR's conflict-free `changelog.d/` fragment, and the release PR — prepared
by `forge-changelog release` (§3) — is the single writer that advances
`plugin.json`. Such a repo inherits the lag described above: every tag
cut between assemblies ships the previous assembly's version. The
guard's fragment-mode truth table:

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
PR (`forge-changelog release`) collates the changelog; the assembly
carries no fragments, so its own merge is not tagged, and it gates
nothing about delivery (§1).

- **Primary path**: the `tag-main` job in
  [`.github/workflows/tag-release.yml`](../.github/workflows/tag-release.yml)
  — after CI succeeds on a push to `main`, it checks out the exact
  CI-validated commit and runs `forge-changelog auto-tag`, then
  `forge-next-prep --tag`. The second step tags an assembly merge only in
  a repo whose manifest declares a version ahead of the latest tag; for
  forge, whose manifest declares none, it reports that no tag is needed.
- **Opt-in / warn floor**: `[tool.forge.release].auto = "merge"` enables
  auto-tagging; without it the job still emits a loud pending-fragments
  warning — a fragments-mode repo can never accumulate unreleased
  merges silently.
- **Manual fallback**: `forge-changelog auto-tag` locally, on the commit
  that carried the fragments.
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
- **The version is assembler-owned too, and tag-aware.** A pending
  fragment already inside a `v*` tag's tree was released by that tag
  (tag-per-merge counted it) and assembles under that tag's heading,
  dated when the tag was cut. Only fragments no tag holds mint a new
  version: `latest v* tag + max(bump level over the unreleased
  fragments)`. When every pending fragment is tagged, nothing is minted
  — the assembly backfills the per-tag headings (and, in a repo whose
  manifest declares a version, syncs `plugin.json` to the latest tag —
  the guard's healthy at-tag, zero-pending state):
  - `forge-changelog next-version` — read-only print of the computed
    next version and its level, or `vX.Y.Z (already tagged — N
    fragment(s) across M tag(s); nothing to mint)`.
  - `forge-changelog release` — computes the plan, assembles
    `CHANGELOG.md` (one heading per already-cut tag, then the minted
    heading on top), writes `plugin.json` to the plan's version when the
    manifest declares one (the manifest's single writer; skipped when it
    declares none, as forge's does, and in manifest-less tag-versioned
    repos), and stages everything. It never commits: branch → run it → ordinary PR
    → merge → tag-on-merge cuts the tag. Racing release PRs collapse
    into an ordinary PR conflict; the loser recovers by taking the
    BASE side of `CHANGELOG.md` (and of `plugin.json` where it carries
    a version), restoring its
    consumed fragments from the merge base
    (`git checkout $(git merge-base HEAD MERGE_HEAD) -- changelog.d/`),
    and re-running `forge-changelog release` — its own release commit
    already deleted its fragments, so a bare re-run has nothing to
    compute from.
  - `forge-changelog assemble --version vX.Y.Z --delete` remains the
    explicit-version core for flows that supply their own version.
- `forge-next-prep` logs a pending-fragment advisory (count + the
  release command) so accumulating fragments prompt a release.
- **The assembly PR opens itself**: the `assemble-release` workflow
  (daily cron + manual dispatch) runs `forge-changelog release-pr` —
  guard, branch `chore/assemble-vX.Y.Z`, stage, commit, push, PR with
  in-body gate evidence. Idempotent (open assembly PR or nothing
  pending → quiet no-op); merging stays human.

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
| `forge-next-prep --tag` tags + pushes only when `plugin.json` is strictly newer than the latest tag (idempotent) | `next_prep._maybe_tag_release` | `tests/test_next_prep.py::test_maybe_tag_release_creates_and_pushes_new_tag` |
| The fragment gate rejects a concrete version number in a fragment's filename or body | `changelog_fragments.validate_fragment` | `tests/test_changelog_fragments.py::test_validate_fragment_version_shaped_filename` / `::test_validate_fragment_version_shaped_body` |
| An invalid fragment fails the gate (exit 2) | `changelog_fragments.main` | `tests/test_changelog_fragments.py::test_main_check_exit_two_on_invalid_fragment` |
| `assemble --delete` writes the curated entry into `CHANGELOG.md` and stages the fragment deletions | `changelog_fragments.main` | `tests/test_changelog_fragments.py::test_main_assemble_with_delete_stages_changelog_and_fragment_deletion` |
| Fragment mode: `plugin.json <= latest tag` passes with valid pending fragments (zero included); an invalid fragment blocks even below the tag; shared-heading equality still fails | `verify_plugin_version._not_ahead_verdict` | `tests/test_verify_plugin_version.py::test_fragments_mode_manifest_at_tag_with_valid_pending_passes` / `::test_fragments_mode_manifest_at_tag_with_zero_pending_passes` / `::test_fragments_mode_invalid_fragment_fails_listing_error` / `::test_fragments_mode_manifest_below_tag_with_valid_fragments_passes` / `::test_fragments_mode_manifest_below_tag_invalid_fragment_fails` / `::test_headings_mode_manifest_at_tag_still_fails` |
| The release version is `latest tag + max(level over UNRELEASED fragments)` — computed, never carried per-PR; fragments already in a tag's tree never bump again | `changelog_fragments.plan_assembly` | `tests/test_changelog_fragments.py::test_plan_assembly_uses_max_level_of_untagged` / `::test_plan_assembly_all_tagged_mints_nothing` |
| Pending fragments partition by the earliest tag whose tree holds them; each group assembles under that tag's heading (dated from the tag), the minted heading lands on top | `changelog_fragments._partition_by_release_tag` via `_render_assembly` | `tests/test_changelog_fragments.py::test_plan_assembly_partitions_by_earliest_tag` / `::test_main_release_backfills_tag_headings_and_syncs_manifest` |
| A branch adds at most ONE fragment (one unique `changelog.d/` file per PR; extra bullets share it) — counted as added since the fork with the PR's *real* base (the open PR's target when one exists, so a stacked PR never counts its parent's fragment; else the configured base) AND absent from that base tip's tree, so a conflicted base merge never counts base-side fragments. The gate is reached on a manifest-versioned repo too: fragments mode is judged before the manifest short-circuit, which otherwise skipped the whole check | `changelog_fragments.branch_added_fragments` via the `changelog_version` fragment gate | `tests/test_precommit.py::test_fragment_gate_blocks_second_branch_added_fragment` / `tests/test_changelog_fragments.py::test_branch_added_fragments_excludes_base_fragments_mid_merge` |
| `release-pr` opens exactly one assembly PR: an already-open one (found up front or via a lost push/create race) defers with exit 0; nothing pending is a quiet 0; guard failures exit 2 | `changelog_fragments._cmd_release_pr` | `tests/test_changelog_fragments.py::test_main_release_pr_defers_to_open_assembly_pr` / `::test_main_release_pr_nothing_pending_is_quiet_noop` |
| `forge-changelog release` assembles under the computed version, rewrites + stages the manifest (single writer) when it declares a version, leaves a manifest that declares none untouched, and never commits | `changelog_fragments._cmd_release` / `_stage_release` | `tests/test_changelog_fragments.py::test_main_release_with_manifest_stages_everything_commits_nothing` / `::test_main_release_version_less_manifest_left_untouched` |
| The assembly PR body claims a manifest sync or a manifest-driven tag only when the manifest declares a version; a tag-per-merge repo is told the fragment-carrying merge carries the tag, never that someone must cut it after merging | `changelog_fragments._assembly_pr_body` | `tests/test_changelog_fragments.py::test_assembly_pr_body_version_less_manifest_claims_no_sync` / `::test_assembly_pr_body_per_merge_repo_names_fragment_merge_tag` |
| Forge's own `plugin.json` declares no `version` — its plugin identity is the commit (§1) | `.claude-plugin/plugin.json` | `tests/test_manifests.py::test_forge_manifest_declares_no_version` |
| A manifest with no `version` key skips the rolling-next and declared-version checks; one whose version is present but malformed (or that does not parse) still counts as declaring, so it still fails | `verify_plugin_version.main` via `git_utils.plugin_manifest_declares_version` | `tests/test_git_utils.py::test_plugin_manifest_declares_version`; `tests/test_verify_plugin_version.py::test_main_skips_version_less_manifest` / `::test_main_fails_on_malformed_declared_version` |
| `forge-next-prep --tag` on a manifest that declares no version reports that no tag is needed — never the "no plugin.json" misuse warning; a declared version that is not bare semver warns as such | `next_prep._tag_misuse_warning` via `_tag_and_report` | `tests/test_next_prep.py::test_tag_and_report_no_tag_needed_for_version_less_manifest` / `::test_tag_misuse_warning_malformed_semver_warns` |
| `changelog_version` defers to `plugin_version` only when the manifest declares a version; a shared-heading repo whose manifest declares none gets the heading checks | `precommit._changelog_version_skip_gate` | `tests/test_precommit.py::test_changelog_version_runs_for_version_less_manifest` |
| The installed plugin is the one `installed_plugins.json` records for this repo (its local/project record, else the user-scope one) — never the highest-named cache slot, since commit-SHA slot names do not sort | `version_surfaces.installed_record` / `find_install_dir` | `tests/test_version_surfaces.py::test_installed_record_prefers_this_repos_record` / `::test_find_install_dir_prefers_recorded_slot_over_semver_name` |
| A plugin-shipping repo whose manifest declares no version is `behind` exactly when `origin/<base>` changed the plugin surface (`.claude-plugin/`, `agents/`, `skills/`, `claude-hooks/`) since the installed commit; a commit the clone does not know, or one not an ancestor of the base, is no finding; `plugin_sync` blocks on `behind` | `version_surfaces.plugin_cache_status` via `precommit.step_plugin_sync` | `tests/test_version_surfaces.py::test_sha_identity_behind_when_base_changed_plugin_surface` / `::test_sha_identity_current_when_base_changed_only_other_paths` / `::test_sha_identity_unknown_commit_is_no_finding`; `tests/test_precommit.py::test_plugin_sync_blocks_version_less_manifest_behind_base` |
| The release-commit skip tolerates a `CHANGELOG.md`/`changelog.d/`-only divergence from the tag — a release commit may assemble the changelog — yet still fails when any other file diverges | `git_utils.release_tree_fingerprint` via `verify_plugin_version._is_release_commit` | `tests/test_verify_plugin_version.py::test_skips_when_release_branch_only_adds_changelog` / `::test_fails_when_release_branch_changes_non_changelog_file`; `tests/test_git_utils.py::test_release_fingerprint_equal_when_only_changelog_differs` / `::test_release_fingerprint_differs_when_other_file_changes` |
| A consumer (no manifest of its own) is judged on the **installed commit** against the marketplace clone's `HEAD`, after checking the machine's registration serves the ref the repo pins (else `wrong-ref`, naming both refs). A record naming no commit (no `gitCommitSha`, a semver version) is no finding, never stale. The remedy follows the pinned manifest: `/plugin update` when it declares no version, slot deletion when it declares one (an unmoved declared version hides new content from `/plugin update`) | `version_surfaces._consumer_cache_status` via `doctor._check_plugin_cache_skew` | `tests/test_version_surfaces.py::test_consumer_status_stale_when_installed_commit_differs_from_clone_head` / `::test_consumer_status_wrong_ref_when_registration_serves_another_ref` / `::test_consumer_status_semver_record_without_commit_is_no_finding`; `tests/test_doctor.py::test_plugin_cache_skew_consumer_remedy_follows_manifest_identity` |

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
rolling-next guard's release-commit skip (declared-version manifests)
still depends on its changelog-tolerant matching (table above) — only
the tag aligner's use of it retired.
