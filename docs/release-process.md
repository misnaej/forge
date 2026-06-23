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
  branch tip. (An earlier version of this section claimed the tag resolves
  "on the dev commit *and* the main commit" — impossible for one ref, and
  the reason the relocation went unenforced and silently never happened.)
- **`forge-check-main-tags` enforces the relocation.** It maps each minor
  tag to its base squash commit by **tree equality** (a squash reproduces
  the tagged tree even though the commit SHA differs) and reports drift
  (default, read-only) or force-moves the tag (`--fix`). Run by `/promote`
  after the squash merge. Self-skips single-branch repos. See §4.

## 3. Promotion: staged catch-up

- **Never merge `dev` directly into `main`.** A promotion PR's head is
  always a dedicated `release/vX.Y.Z` branch.
- **One minor at a time, ascending.** When `main` is several minors behind,
  promote each minor as its own clean squash commit; never lump multiple
  minors into one PR.
- The release branch's tree reproduces the target minor's release tree
  (cut from `main`, bring the minor's tree, verify `git diff` is empty).
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
| A minor tag `vX.Y.0` belongs on the `origin/<base>` commit whose tree equals the tag's tree (the squash commit); verify mode exits non-zero on drift, `--fix` relocates it. A minor with no tree-matching base commit is reported, never invented | `verify_main_tags._tag_states` / `verify_main_tags._verify` | `tests/test_verify_main_tags.py::test_verify_exits_nonzero_when_minor_tag_off_base` |
| Main-tag alignment self-skips single-branch repos (`base_branch == dev_branch`) so consumers on trunk-based flow no-op | `verify_main_tags.main` | `tests/test_verify_main_tags.py::test_main_skips_single_branch_repo` |
| `--promotion-status` flags a pending minor that has no `## vX.Y.0` entry in `origin/<dev>`'s `CHANGELOG.md` (non-blocking advisory; silent when the repo keeps no CHANGELOG) | `next_prep._promotion_status_lines` | `tests/test_next_prep.py::test_promotion_status_flags_missing_changelog_entry` |

When you add a versioning/promotion behavior, add a row here **and** its
test. When you find an invariant with no test, that gap is a bug to close.

## 5. CHANGELOG at release

`CHANGELOG.md` records **one entry per promoted minor** (`vX.Y.0`) — the
slow channel ships minors only, so patches do **not** get their own
entry; they fold into the next minor's entry when it promotes.

**Each entry is authored on `dev`, before the promotion** — written as
the minor is finalized (a small docs PR on `dev`, or folded into the
last feature PR of that minor). The promotion branch is cut from `dev`'s
tree, so it carries the entry onto `main` automatically when it merges.

- **`dev` is the single source.** Never hotfix a CHANGELOG entry directly
  onto `main` after promoting, and never back-merge `main → dev` to
  reconcile it — that makes `main` a second source and adds two PRs per
  release. The entry flows one way: authored on `dev`, carried to `main`
  by the release branch.
- **Enforcement is a non-blocking advisory.** `forge-next-prep
  --promotion-status` (run by `/promote`) appends a `⚠️` line when a
  pending minor has no `## vX.Y.0` heading in `origin/<dev>`'s
  `CHANGELOG.md`. It is advisory, not a gate: it never changes the exit
  code, and it stays silent for repos that keep no `CHANGELOG.md`. A
  blocking variant would be a new gate (MINOR) — tracked separately.
- **One-time catch-up exception.** When `main` has *already* shipped a
  minor whose entry was never written (a historical gap), repair it with
  a one-off patch hotfix to `main` plus a back-merge to `dev`. This is a
  repair, not the steady-state flow above.
