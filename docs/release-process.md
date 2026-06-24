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
  read-only) or force-moves the tag (`--fix`). Run by `/promote` after the
  squash merge. Self-skips single-branch repos. See §4.

## 3. Promotion: staged catch-up

- **Never merge `dev` directly into `main`.** A promotion PR's head is
  always a dedicated `release/vX.Y.Z` branch.
- **One minor at a time, ascending.** When `main` is several minors behind,
  promote each minor as its own clean squash commit; never lump multiple
  minors into one PR.
- **Always build the release branch by tree-reconstruction from `main`** —
  *never* branch at `dev`'s tip. Because every promotion is a squash,
  `main`'s commits are not ancestors of `dev`, so a `dev`-tip branch
  produces a PR whose three-dot diff re-shows every already-promoted minor
  (merge-base falls back to an ancient commit). The correct, always-clean
  recipe:
  ```bash
  git switch -c release/vX.Y.0 origin/main
  git rm -r --cached . -q && git checkout vX.Y.0 -- . && git add -A  # tree = the tagged release
  git checkout origin/main -- CHANGELOG.md                            # preserve main's curated CHANGELOG
  # then author the new ## vX.Y.0 entry on top of main's CHANGELOG
  ```
  Parent = `main`, tree = the release + main's CHANGELOG + the new entry.
  The PR diff is exactly the release's code delta plus the one new
  CHANGELOG entry — two-dot == three-dot, no re-shown minors. Restoring
  `CHANGELOG.md` from `main` (not the tag tree) is what keeps `main`'s
  curated history from regressing (§5).
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
| `--promotion-status` flags a pending minor that has no `## vX.Y.0` entry in `origin/<dev>`'s `CHANGELOG.md` (non-blocking advisory; silent when the repo keeps no CHANGELOG) | `next_prep._promotion_status_lines` | `tests/test_next_prep.py::test_promotion_status_flags_missing_changelog_entry` |

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
`main` and is carried forward at each promotion by restoring
`CHANGELOG.md` from `origin/main` in the release branch (§3), *not* from
the tagged `dev` tree. This is the key to never regressing the log: the
release branch starts from `main`'s CHANGELOG and only appends the new
entry, so no prior entry is lost — **no per-release back-merge is
required**, and `dev`'s `CHANGELOG.md` is allowed to lag (it is the
pre-release branch; consumers reading release notes pin `@main`).

- **Back-merging `main → dev` is optional**, not mandatory. Do it if you
  want `dev`'s `CHANGELOG.md` to mirror `main` for local readability (a
  small PR: the entry + the rolling-next `plugin.json` bump). Skip it
  freely when `dev` is moving fast — the §3 recipe already prevents
  regression by restoring the log from `main`, so a stale `dev` CHANGELOG
  has no downstream effect. (Earlier guidance treated this back-merge as
  required; it is not, once the release branch restores `CHANGELOG.md`
  from `main` rather than overwriting it from the tag tree.)

- **Enforcement is a non-blocking advisory.** `forge-next-prep
  --promotion-status` (run by `/promote`) appends a `⚠️` line when a
  pending minor has no `## vX.Y.0` heading in `origin/<dev>`'s
  `CHANGELOG.md` — a reminder to author that entry (in the release
  branch). It never changes the exit code and stays silent for repos that
  keep no `CHANGELOG.md`. A blocking variant would be a new gate (MINOR) —
  tracked separately.
