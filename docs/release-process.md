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
- Because promotion squashes, a minor re-tagged on main's squash commit is
  not reachable from `dev`'s history — expected. `@dev` resolves the tag on
  the dev commit; `@main` on the main commit.

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

When you add a versioning/promotion behavior, add a row here **and** its
test. When you find an invariant with no test, that gap is a bug to close.
