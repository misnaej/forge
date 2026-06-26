# Release process (forge-only)

**This is the single source of truth for forge's versioning and
`dev → main` promotion.** It is the *spec*; the code conforms to it. Every
invariant below names the **test that enforces it** — the executable spec
that goes red if code drifts. If you change versioning or promotion code,
change this doc and its tests **first**, then make the code match.

> Forge-only. The dual-track (`dev`/`main`) model and rolling-next
> convention are specific to forge; consumer plugin authors may use
> trunk-based, gitflow, or another model. CLAUDE.md's release bullets and
> the `/promote` skill **point here** — they do not restate the mechanics
> (FOUNDATION §12, single source of truth).

---

## 1. Rolling-next versioning

`.claude-plugin/plugin.json["version"]` **always names the version about to
be released** — never the last-released version.

- The pre-commit step `plugin_version` (`verify-forge-plugin-version`)
  enforces `plugin.json["version"] > latest tag` on every commit.
- After tagging `vX.Y.Z`, the next PR must bump `plugin.json` to the next
  rolling-next version, or its commits fail the guard.

## 2. Dual-track tag cadence

- **`dev` is tagged `vX.Y.Z` after *every* merge to `dev`** — patch and
  minor alike. `forge-next-prep --tag` (run by `/next`) does this when
  `plugin.json` is ahead of the latest tag. **Never drop `--tag`.**
- **`main` is tagged only at the minor-boundary promotion** — the minor
  `vX.Y.0` is (re-)tagged on main's squash commit.
- Net: `@dev` consumers receive every version; `@main` receives **minors
  only**. Patches accumulate on `dev` and fold into the next minor.
- **A tag is a single ref — it lives in exactly one place.** The minor
  `vX.Y.0` is *created* on the dev commit by `forge-next-prep --tag` when
  the bump lands, then *relocated* to main's squash commit at promotion.
  Because promotion squashes, that commit is not in `dev`'s history; after
  relocation `dev` resolves the minor by `git describe` distance (a
  pre-release suffix), which is correct — `dev` is the pre-release channel.
  `@main` checkouts describe the clean `vX.Y.0`; `@dev` checkouts track the
  branch tip. A single ref cannot resolve to two commits simultaneously.
- **`forge-check-main-tags` enforces the relocation.** It maps each minor
  tag to its base squash commit by **release fingerprint** — tree content
  with `CHANGELOG.md` excluded (`git_utils.release_tree_fingerprint`) — so
  a squash reproduces the tagged release even when the release branch
  finalized the curated `@main` CHANGELOG entry (§5); any *other* file
  difference still leaves the tag unaligned. Reports drift (default,
  read-only) or force-moves the tag (`--fix`). Run automatically by `/next`
  Phase 1 on every run (idempotent — moves a tag only when a promotion
  actually landed); also in the `/promote` post-merge checklist for
  immediate relocation. Self-skips single-branch repos. See §4.

## 3. Promotion: staged catch-up

- **Never merge `dev` directly into `main`.** A promotion PR's head is
  always a dedicated `release/vX.Y.Z` branch.
- **One minor at a time, ascending.** When `main` is several minors behind,
  promote each minor as its own clean squash commit; never lump multiple
  minors into one PR.
- **Build the release branch from `dev`, then merge `main` into it.** Four
  standard-git steps — no special recipe:
  ```bash
  git switch -c release/vX.Y.0 origin/dev   # 1. branch from the dev version
  git merge origin/main                      # 2. merge main IN — REQUIRED
  # 3. resolve CODE/test conflicts toward dev (dev is ahead). EXCEPTION:
  #    CHANGELOG.md is NEVER resolved blindly — reconcile by hand (§5)
  # 4. reconcile CHANGELOG.md (§5) + append the curated ## vX.Y.0 entry; commit
  ```
  **Step 2 is the whole game.** Because every promotion is a squash,
  `main`'s commits are not ancestors of `dev`; branching from `dev`
  *without* merging `main` leaves the merge-base at the pre-squash common
  ancestor, so the PR re-shows all of dev's history (a v2.4.0 attempt
  showed 98 files vs the true 15). Merging `main` in makes it an ancestor →
  the PR diff is exactly this release's delta. Plain git — no
  tree-reconstruction. The branch diverges from the `vX.Y.0` tag only by
  the curated `CHANGELOG.md`, which the tag relocation tolerates via the
  release fingerprint (§2).
- Run via the `/promote` skill, which uses `forge-next-prep
  --promotion-status` for the ordered pending list.

## 4. Invariants the code MUST satisfy → enforcing tests

This table is the anti-regression contract. **Do not change a behavior in
the left column without its test (right column) staying green.** A change
that violates an invariant must turn its test red — that is how a future
"alignment fix" is stopped from silently amputating a working behavior
(this is exactly how the #43 regression slipped through: it changed an
invariant that had no test).

| Invariant | Where | Enforcing test |
|---|---|---|
| Latest tag resolved **globally** (semver-max, branch-independent) — never ancestry-scoped `git describe`, so the guard and the auto-tagger agree in the dual-track case | `git_utils.latest_v_tag` | `tests/test_git_utils.py::test_latest_v_tag_returns_highest_sorted` |
| Rolling-next guard skips when HEAD's tree reproduces **ANY** `v*` tag (not only the latest) — required so a `release/vX.Y.Z` branch for a minor *below* the global-max tag still passes | `verify_plugin_version._is_release_commit` | `tests/test_verify_plugin_version.py::test_main_skips_when_head_reproduces_older_tag` |
| Guard fails when a real content change leaves `plugin.json ≤ latest tag` | `verify_plugin_version.main` | `tests/test_verify_plugin_version.py::test_fail_when_version_not_strictly_greater` |
| `--promotion-status` lists pending **minors only** (`X.Y.0`); interleaved patch tags fold into the next minor | `next_prep._promotion_status_lines` | `tests/test_next_prep.py::test_promotion_status_excludes_patch_tags` |
| `forge-next-prep --tag` tags + pushes only when `plugin.json` is strictly newer than the latest tag (idempotent) | `next_prep._maybe_tag_release` | `tests/test_next_prep.py::test_maybe_tag_release_creates_and_pushes_new_tag` |
| A minor tag `vX.Y.0` belongs on the `origin/<base>` commit whose **release fingerprint** equals the tag's (the squash commit); verify mode exits non-zero on drift, `--fix` relocates it. A minor with no matching base commit is reported, never invented | `verify_main_tags._tag_states` / `verify_main_tags._verify` | `tests/test_verify_main_tags.py::test_verify_exits_nonzero_when_minor_tag_off_base` |
| Main-tag alignment self-skips single-branch repos (`base_branch == dev_branch`) so consumers on trunk-based flow no-op | `verify_main_tags.main` | `tests/test_verify_main_tags.py::test_main_skips_single_branch_repo` |
| Release-equality ignores `CHANGELOG.md` (the per-promotion curated `@main` entry) but **nothing else** — two trees differing only in `CHANGELOG.md` share a fingerprint; any other diff does not | `git_utils.release_tree_fingerprint` | `tests/test_git_utils.py::test_release_fingerprint_equal_when_only_changelog_differs` / `::test_release_fingerprint_differs_when_other_file_changes` |
| The rolling-next guard skips a release branch that only finalized `CHANGELOG.md`, yet still fails when a non-CHANGELOG change leaves `plugin.json ≤ latest tag` | `verify_plugin_version._is_release_commit` | `tests/test_verify_plugin_version.py::test_skips_when_release_branch_only_adds_changelog` / `::test_fails_when_release_branch_changes_non_changelog_file` |
| `forge-check-main-tags` relocates a minor tag when the base squash diverges from the tag only by `CHANGELOG.md`, but treats a non-CHANGELOG divergence as unreproduced (no move) | `verify_main_tags._base_tree_index` | `tests/test_verify_main_tags.py::test_fix_relocates_when_base_diverges_only_by_changelog` / `::test_base_diverging_by_non_changelog_is_not_a_target` |
| `forge-check-main-tags` warns about un-reproduced minors **above** the base's current line (genuinely pending) but downgrades **ancient** ones below it (never promoted, can't backfill) to INFO — so long-dead dev-only tags don't nag every run | `verify_main_tags._report_unreproduced` | `tests/test_verify_main_tags.py::test_report_unreproduced_warns_pending_but_ignores_ancient` |
| `--promotion-status` flags a pending minor that has no `## vX.Y.0` entry in `origin/<dev>`'s `CHANGELOG.md` (non-blocking advisory; silent when the repo keeps no CHANGELOG) | `next_prep._promotion_status_lines` | `tests/test_next_prep.py::test_promotion_status_flags_missing_changelog_entry` |
| A branch that merged `origin/<base>` in (a promotion or any main-merge) must retain **every** `## vX.Y.0` heading present on `origin/<base>` — a CHANGELOG conflict resolved blindly toward dev that drops one fails the guard. Self-skips when `origin/<base>` is not an ancestor of `HEAD` (plain `dev` may lag, §5) and on single-branch repos | `verify_changelog_history.main` | `tests/test_verify_changelog_history.py::test_fails_when_base_heading_dropped` / `::test_skips_when_base_not_ancestor` |

When you add a versioning/promotion behavior, add a row here **and** its
test. When you find an invariant with no test, that gap is a bug to close.

## 5. CHANGELOG at release

`CHANGELOG.md` is the **`@main` channel log**: one **curated, condensed
entry per promoted minor** (`vX.Y.0`). The slow channel ships minors
only, so patches do **not** get their own entry — they fold into the next
minor's entry. Condensing many `dev` patches into one readable minor
entry is editorial work that belongs **with the release**, so the entry
is **authored in the `release/vX.Y.Z` branch** as part of the promotion,
not pre-written on `dev`.

This is the **modified-release-branch pattern**: the release branch's
tree legitimately diverges from the tagged `dev` release by exactly one
file — `CHANGELOG.md`. Both release guards tolerate that and *only* that
(§2, §4): the rolling-next guard
(`verify_plugin_version._is_release_commit`) and the tag aligner
(`forge-check-main-tags`) compare on the **release fingerprint** (tree
minus `CHANGELOG.md`). So the release branch passes CI and the minor tag
still relocates onto `main`, while any *non*-CHANGELOG edit to a release
branch is rejected exactly as before.

**`main` is the CHANGELOG source of record.** The curated log lives on
`main` and is carried forward at each promotion by the **`git merge
origin/main`** in the release branch (§3 step 2): the merge brings main's
prior curated entries onto the branch, and you then append only the new
`vX.Y.0` entry. **No per-release back-merge is required**, and `dev`'s
`CHANGELOG.md` is allowed to lag (it is the pre-release branch; consumers
reading release notes pin `@main`).

> **CHANGELOG.md is the one file exempt from the §3 step 3 "resolve toward
> dev" rule.** Precisely *because* `dev`'s copy may lag — or
> independently diverge from — `main`, a `CHANGELOG.md` merge conflict must
> **never** be settled with a blind `git checkout --ours` / `--theirs`.
> Resolving toward `dev` **erases main's curated history** when dev lags;
> resolving toward `main` **drops a genuine dev-side addition** when dev is
> ahead. It always needs a human read. Reconcile by hand: **keep every
> `## vX.Y.0` heading present on `main`** (a more-recent `dev` copy never
> erases a curated main entry just because it is ahead in history), **fold
> in any real dev-side additions**, then **append the new `vX.Y.0` entry**.
> When unsure, diff `git show origin/main:CHANGELOG.md` against the branch
> copy before resolving. For already-released versions, the main-side body
> wins; the new minor's entry is authored fresh.

- **Back-merging `main → dev` is optional**, not mandatory. Do it if you
  want `dev`'s `CHANGELOG.md` to mirror `main` for local readability (a
  small PR: the entry + the rolling-next `plugin.json` bump). Skip it
  freely when `dev` is moving fast — because §3 merges `main` into every
  release branch, the curated log never regresses regardless of whether
  `dev`'s copy is current.

- **Enforcement is a non-blocking advisory.** `forge-next-prep
  --promotion-status` (run by `/promote`) appends a `⚠️` line when a
  pending minor has no `## vX.Y.0` heading in `origin/<dev>`'s
  `CHANGELOG.md` — a reminder to author that entry (in the release
  branch). It never changes the exit code and stays silent for repos that
  keep no `CHANGELOG.md`. A blocking variant would be a new gate (MINOR) —
  tracked separately.

- **The dropped-entry guard *is* blocking.** `forge-check-changelog-history`
  (pre-commit step `changelog_history`) fails when a branch that has
  merged `origin/<base>` in — a promotion or any main-merge — drops a
  `## vX.Y.0` heading present on `origin/<base>`. It enforces the
  CHANGELOG-conflict rule above: a conflict mistakenly resolved with
  `--ours` that erases a curated main entry turns CI red instead of
  shipping a regressed log. It self-skips on plain `dev` (base is not an
  ancestor of `HEAD`, so dev's copy may still lag) and on single-branch
  repos. This is the §4 invariant that makes the rule discipline-free.
