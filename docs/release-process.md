# Release process (forge-only)

**This is the single source of truth for forge's versioning and release
cadence.** It is the *spec*; the code conforms to it. Every invariant
below names the **test that enforces it** — the executable spec that goes
red if code drifts. If you change versioning or release code, change this
doc and its tests **first**, then make the code match.

> Forge-only. CLAUDE.md's release bullets **point here** — they do not
> restate the mechanics (FOUNDATION §12, single source of truth).

---

## 1. The tag is the version, and the only one

A release version lives in exactly one place: the `v*` git tag. Nothing
in the tree names it.

- The pip package version is derived from the latest tag by
  setuptools-scm. There is no `version =` in `pyproject.toml`.
- Nothing else carries a copy, so nothing can disagree with it.

**Why this is the whole design.** Forge used to keep a second copy, in a
plugin manifest that was supposed to mirror the tag. A mirror drifts, and
both directions bit:

- *Ahead.* A release was assembled, another PR merged behind it, and the
  manifest then named a version no tag had reached. The guard called that
  a stale computation and failed the default branch, which skipped the
  tagging job — so the release was written and never released.
- *Behind.* Re-running the assembly recomputed the version from the
  fragments still pending, and the one carrying the major bump had
  already been consumed. It produced a **lower** version and staged the
  manifest below the tag. Its CI passed, because a manifest behind the
  tag is the normal state of every ordinary PR.

No guard can settle which copy is right. Removing the copy settles it.

**Invariant.** No tracked file declares a release version.
**Enforced by** `tests/test_release_tag_invariant.py`.

## 2. A tag is cut on every merge

Every merge into the default branch cuts a release tag. The increment
comes from that merge's own changelog fragments — the latest tag plus the
strongest `bump:` level among the fragments the merge brought.

- **Mechanism**: the `tag-main` job in
  [`.github/workflows/tag-release.yml`](../.github/workflows/tag-release.yml)
  runs `forge-changelog auto-tag` after CI succeeds on a push to the
  default branch, against the exact CI-validated commit. It pushes a tag
  and nothing else: tag refs sit outside branch rulesets, so no commit to
  a protected branch is needed.
- **Opt-in**: `[tool.forge.release].auto = "merge"`. Without it the CLI
  warns and cuts nothing, so a consumer repo copying the workflow does
  not start tagging by surprise.
- **Which fragments count**: those absent from the latest tag's tree. A
  fragment already inside a tag was counted by that tag and never bumps
  again, which is what makes the per-merge computation stable while
  fragments wait for their prose.
- **Idempotent, and never silent**: an existing tag means another runner
  won; no new fragments means nothing to tag. Each case says so.
- **A merge can still miss its tag, and that is reported, not hidden**:
  the tag push can be refused, or `auto-tag` can fail for its own
  reasons. The next merge's `auto-tag` then counts the orphaned fragments
  into its own tag, so the version stays correct while that merge has no
  release naming it and the next tag's notes cover two merges.
  `forge-changelog tag-gaps` reads git alone and reports it — `HEAD` by
  default, any window with `--since`. The `tag-main` job runs it after
  `auto-tag`, including when `auto-tag` itself failed.
  - **What that wiring does not reach**: the job is gated on CI
    succeeding on a push to the default branch, so a merge whose CI went
    red, whose run never fired, whose runner died, or whose tag job was
    evicted while queued behind two others in the single
    `tag-release-main` concurrency group skips the job entirely — the
    step never runs, and the next merge's `HEAD` window cannot see
    backwards to the orphan. Those cases need `tag-gaps --since <rev>`
    run deliberately, over a window that reaches back past the suspect
    merge.
- **One refusal is worth naming**: a remote applies its workflow-permission
  rule to tag pushes, so tagging a commit whose `.github/workflows/` state
  is not the one the branch tips hold is refused. `auto-tag` reports that
  cause and its remedy; the wording lives there, not here.

**Invariant.** A push to the default branch is never cancelled and never
left queueing: `ci.yml` groups those runs per commit. The job below fires
on CI *success*, so a run that is cancelled, or evicted while pending,
cuts no tag at all. **Enforced by**
`test_main_ci_runs_are_never_cancelled_and_never_queue` in
`tests/test_ci_tree_gate.py`.

**Invariant.** The increment is the strongest level among fragments new
since the latest tag; an already-tagged fragment never bumps again.
**Enforced by** `test_main_auto_tag_takes_strongest_new_level`,
`test_main_auto_tag_cuts_and_pushes_tag_from_merged_fragment` and
`test_main_auto_tag_no_new_fragments_is_a_visible_noop` in
`tests/test_changelog_fragments.py`.

**Invariant.** An invalid pending fragment blocks the tag rather than
guessing the level. **Enforced by**
`test_main_auto_tag_invalid_new_fragment_blocks`.

**Invariant.** A `major` fragment carries type `upgrade`, so the release a
consumer cannot adopt without acting always ships the notes saying what to
do — a PR carries one fragment, so no sibling could hold them. **Enforced
by** `test_validate_fragment_rejects_major_outside_upgrade_lane`,
`test_validate_fragment_accepts_minor_upgrade` and
`test_validate_fragment_bad_filename_skips_major_lane_check` in
`tests/test_changelog_fragments.py`. A version whose notes are missing
anyway is reported as a gap rather than as nothing owed —
`test_print_upgrade_notes_warns_when_a_major_documents_no_lane` in
`tests/test_upgrade.py`.

**Invariant.** A tag that already exists, or appears mid-push, defers to
the winner instead of failing. **Enforced by**
`test_main_auto_tag_existing_tag_defers_to_winner` and
`test_main_auto_tag_push_race_defers_to_remote_winner`.

**Invariant.** A tag push the remote refuses under its workflow-permission
rule is reported with that cause, and still fails the run. **Enforced by**
`test_main_auto_tag_workflow_permission_refusal_reports_cause` and
`test_push_tag_workflow_permission_refusal_is_classified`, with
`test_push_tag_other_rejection_is_not_workflow_scope_refusal` as the
non-misclassification half — a rejection for any other reason must not
send the reader after a credential that was never the problem.

**Invariant.** `forge-changelog tag-gaps` reports a commit that brought
fragments but carries no tag of its own, naming the tag its fragments
shipped under. **Enforced by**
`test_tag_gaps_reports_fragment_commit_with_no_tag`,
`test_tag_gaps_names_the_earliest_tag_holding_the_fragments`,
`test_tag_gaps_default_window_is_head_only`,
`test_tag_gaps_annotated_tag_counts_as_tagged` and
`test_cmd_tag_gaps_exit_codes_and_output`. The claim is about the CLI: the
`tag-main` job runs it on every merge, but no test covers that wiring.

## 3. The changelog is prose, written after the fact

A tag ships the code. `CHANGELOG.md` explains it, and is assembled
separately — so a tag never waits on prose, and prose never mints a
version.

- Each PR ships a `changelog.d/<slug>.<type>.md` fragment whose first
  line is `bump: patch|minor|major` — **a level, never a version
  number**. A PR never edits `CHANGELOG.md`.
- The `assemble` job in
  [`.github/workflows/assemble-changelog.yml`](../.github/workflows/assemble-changelog.yml)
  runs `forge-changelog release-pr` weekly, and on demand. It opens a PR
  that collates the pending fragments and deletes them. Merging it is a
  human decision like any other.
- The assembly files each fragment **under the tag that already shipped
  it**, and mints a heading only for fragments no tag has reached yet. So
  the headings match the tags rather than renaming history.
- `release-pr` **is dedup-guarded by the assembly branch prefix**: with
  an assembly PR already open it reports that one instead of opening a
  second. An assembly committed onto any other branch is invisible to
  that check, so the scheduled job opens a second PR beside it.
- `forge-changelog release` is the staging half of the same work and
  stops before committing. It is **not** a dry run: it assembles and
  stages, consuming the pending fragments, so a later `release-pr` finds
  nothing pending until that assembly lands. Reach for it only to commit
  the assembly yourself, on a `chore/assemble-` branch; `release-pr`
  otherwise.

**Invariant.** Assembly writes prose only — it declares no version
anywhere in the tree. **Enforced by**
`tests/test_release_tag_invariant.py`.

### Compaction is manual, deliberately

Summarising old entries down to what still matters is a judgment about
what a reader needs years later, so no job does it. Run it by hand when
the changelog has grown unwieldy; it lands as its own reviewed PR.

## 4. What this replaced

Deleted with the plugin, and not to be reintroduced without changing
this document first:

- Rolling-next versioning, where the manifest always named the version
  about to be released.
- The `plugin_version` pre-commit guard, which compared the manifest to
  the latest tag — there is nothing left to compare.
- `forge-rebump`, which mechanically resolved the version-slot conflict
  every surviving branch hit, and the version-slot half of
  `forge-update-branch`. With no version in the tree, that conflict
  cannot occur.
- `forge-next-prep --tag`, which tagged the assembly merge.
- The manifest validator, and the per-checkout plugin install and its
  marketplace machinery.

## 5. Cutting a release by hand

`forge-release` cuts an annotated tag directly, for when the automatic
path is unavailable. It requires a clean tree on the default branch and,
when `CHANGELOG.md` exists, a heading for the tag being cut.

## 6. Consumers

A consumer repo takes the same shape: tags carry the version, fragments
carry the levels, and the shipped CI recipe
([`forge-docs/ci-recipe.md`](../forge-docs/ci-recipe.md)) wires the same
two jobs. The consumer-facing decision procedure for choosing a bump
level lives in [`docs/consumer-release.md`](consumer-release.md).
