# Changelog

Notable changes to forge, by release on **`main`**.

forge's slow channel (`@main`) ships **minor releases only** — patches
accumulate on `dev` between minors and fold into the next minor's
promotion. Pin `@main` to track the entries below; pin `@dev` for every
patch. Each entry corresponds to one `dev → main` promotion.

**Reading this as a forge consumer.** You're usually jumping several
minors at once: read every entry newer than your current version, top to
bottom, and read each **⚠️ Upgrade notes** lane first — that's the
actions your repo may need (breaking changes, config, new mandatory
behavior). Releases without that lane are additive or internal and need
nothing from you.

**Format.** Per release: an optional **⚠️ Upgrade notes** lane, then
change groups by conventional-commit type (**Features / Fixes / Refactor
/ Tooling / Docs / Chore**) mirroring the promotion squash message.
Follows [Keep a Changelog](https://keepachangelog.com/) in spirit;
versions follow forge's rolling-next convention.

## v3.4.4 — Unreleased

### Docs
- **Agent loop/content discipline hardened.** `precommit-fixer`: a
  formatter-reverted Edit is STUCK after ONE occurrence — formatter-stable
  findings (e.g. an overlong `def` name whose canonical layout exceeds
  the line limit) never converge by re-layout; the fixer reports the
  semantic fix instead of burning its run budget. `git-commit-push`:
  explicit prohibition on authoring or modifying file content via Bash
  (heredoc, `sed -i`, `tee`, redirects, `cp`/`mv` over tracked paths) —
  it commits the tree exactly as handed over.

## v3.4.3 — Unreleased

### Docs
- **`pr-manager` agent doc back under budget — zero information loss.**
  1499 → ~850 words by the audit metric: every removed passage is a
  pointer to its verified canonical home (FOUNDATION §6/§10, the agent
  template's contracts, the `/pr` skill's steps), a hoist (the
  CONTINUATION append rule is now stated once in FOUNDATION §10 and
  pointed at by both `pr-manager` and `git-commit-push`), or a
  fence-conversion of exact emitted shapes. Also fixes step references
  left stale by the internal renumbering.

## v3.4.2 — Unreleased

### Fixes
- **`TYPE_CHECKING`-only imports are no longer runtime edges.** The
  shared import extractor skips `if TYPE_CHECKING:` bodies by default,
  so the deps audit stops inventing cycles for correctly-guarded code
  and a layering `composes_all_of` clause can no longer be silently
  satisfied by an import that never executes. Design-time consumers opt
  back in: C4 diagrams and smart-test selection keep the annotation
  edges (`include_type_checking=True`).
- **Audits honor the declared layout.** `resolve_roots` with no
  `--roots` now prefers `[tool.forge].source_dirs` + `test_dirs` over
  the broad built-in directory guess — a repo that declared its layout
  stops seeing docs/config/data/vendored trees scanned by every audit
  (measured two-thirds of default-run dup findings on one repo). The
  guess remains for repos with no declared layout; explicit `--roots`
  unchanged.
- **Shipped skills invoke agents by their canonical `forge:` names.**
  Seven skill files used bare names (`pr-manager`, `precommit-fixer`,
  …), which fail with "Agent type not found".
- **The wrap-up gate no longer blocks promotion PRs.**
  `block_unverified_pr_create` self-exempts on `release/vX.Y.Z` branches
  **with provenance**: the matching tag must exist and `HEAD`'s tree must
  reproduce it modulo `CHANGELOG.md` (the curated entry). An era-locked
  release tree has no `/pr` reporter wrap-up — its verification is the
  release fingerprint. A branch merely *named* `release/*`, or a
  suffixed name (`release/vX.Y.Z-rc1`), stays gated.

## v3.4.1 — Unreleased

### Fixes
- **`forge-audit-layering` no longer scans test directories by default.**
  The generic audit default roots include `test/`/`tests/`, so a test
  package mirroring a source namespace was evaluated as a layer child —
  spurious findings on repos with mirrored test layouts. Root resolution
  now routes through the shared source-only seam
  (`[tool.forge.layering].paths` → `[tool.forge].source_dirs` → smart
  auto-detect); explicit `--roots` remains the highest override and can
  still name test dirs deliberately.

## v3.4.0 — Unreleased

### Features
- **`forge:prior-art` agent — the creation gate.** Runs BEFORE any new
  file or top-level symbol is written (new first row in FOUNDATION §3's
  delegation table) and answers the two questions that decide
  duplication: does this already exist, and where does it belong.
  Verdicts REUSE / EXTEND / NEW carry layer context (an un-importable
  "reuse" is called out; EXTEND — move the shared helper down a layer —
  is first-class). The refusal contract makes it auditable: no verdict
  without named queries, their results, and a hash of the digest read
  (`prior-art-searched:` header, checked by `forge-audit-agents` like
  `verified-at:`). `/pr` refuses to author a wrap-up for a file-adding
  diff without the block. `forge-gen-api-digest --symbol <regex>` is
  the agent's cheap live query surface. `design-checker`'s pre-write
  briefing sheds its existence/placement steps to a pointer.

## v3.3.0 — Unreleased

### Fixes
- **`changelog_version` no longer leaves stale branches stuck.** The
  check still blocks, but when it fails only because the latest tag
  lives on the base branch but not on
  the feature branch (the branch is merely behind — nothing staged is
  wrong), the failure now says so and names the one real cure: merge the
  base in (`git merge origin/<base>`, stash dance for a dirty tree, never
  rebase) — and states explicitly that hand-added headings and
  `[no-version]` will not work. `git_utils.is_ancestor` is the new shared
  reachability probe.
- **`precommit-fixer` cannot loop forever.** The agent's process is now
  hard-capped: three `forge-precommit` runs per invocation, individual
  step CLIs allowed once each only to refresh a single stale log,
  diagnosis exclusively from `code_health/*.log` (never ad-hoc command
  output), and a repeated finding set means stop-and-report via a
  dedicated `STUCK` block instead of another loop. It also no longer
  drives `git-commit-push` (the main agent does, off its report) and no
  longer auto-bumps dependency pins — advisories are reported with the
  suggested pin, and bumps ship in a dedicated `chore(deps)` PR per the
  new FOUNDATION §6 "Dependency bumps ship alone" rule.

## v3.2.0 — Unreleased

### Features
- **`forge-audit-layering` + opt-in `layering` pre-commit step.** Layer
  admission rules stop being prose: `[[tool.forge.layering.layer]]`
  declares a *positive* `composes_all_of` clause — every direct child of
  the layer's package must reach each named layer through its transitive
  internal-import closure ("must be built on X", which permission-only
  tools cannot express). Evaluated per direct child over the whole
  closure, not per module. The diff is the baseline: pre-existing
  violations report LOW and never block; a violation in an added/renamed
  module is HIGH and fails the step — the gate fires on exactly the
  commit that decides placement. Exemptions are per-child and rendered
  visibly. Joins `forge-audit-all` and design-checker's recipes
  (Recipe 7); `under_module_prefix` is now the one shared prefix matcher
  (`gen_c4` imports it), and `git_utils.added_or_moved_files` is the
  narrow added/renamed companion to `get_modified_files`. Forge adopts
  the gate itself: audit scripts must compose `audit.common`,
  smart-test's graph-facing children must compose `forge.import_graph`.

### Fixes
- **Audit logs cannot be forged by untrusted content.** `Finding.render()`
  now control-character-escapes path, message, and evidence
  (`sanitize_log_text`) before writing `code_health/audit_*.log` — a git
  filename or config-supplied string (e.g. a layer name) containing a
  newline or ANSI escape can no longer inject a spoofed finding line into
  logs agents treat as trusted ground truth. All audit scripts inherit
  the fix; malformed `composes_all_of`/`exempt` values also fail with a
  clear config error instead of char-splitting.

## v3.1.0 — Unreleased

### Features
- **`design-checker` pre-write briefing asks where new code belongs.** The
  API-digest step now asks two questions — *does this already exist* and
  *where does it belong* (nearest-relative modules by digest path,
  domain-token grep, catch-all-package smell, C4 model consult when
  present) — and the briefing gains a "Where this belongs" section.
  FOUNDATION §1 adds the matching red flags: creating a new module when an
  existing one is the natural home, and adopting an issue's suggested
  name/path as a directive instead of a hypothesis.
- **`/memory-audit` skill.** Reconciles the agent's persistent memory
  against the repo's rule surface — contradictions, duplicates of shipped
  rules, stale references — report-first, confirm-first. Pairs with the new
  FOUNDATION §12 rule that process feedback ships into the rule surface
  (consumer `CLAUDE.md`, skills, or an upstream forge issue), never into
  private agent memory.
- **Verification starts itself.** FOUNDATION §6 and the `/commit` + `/pr`
  skills now state that finalization reviews launch automatically the
  moment a branch's implementation commits are done — an agent must not
  idle at "ready to finalize?" with verification unrun.
- **Wrap-up before PR — hook-enforced.** The wrap-up + squash message are
  authored *before* the PR exists (`/pr` Step 3.92 writes
  `code_health/pr_wrapup.md`, first line `verified-at: <HEAD>`); the new
  `block_unverified_pr_create` hook blocks `gh pr create` unless that file
  names the current `HEAD`, so a tree cannot be published with its
  verification record stale or skipped. Posting still happens after
  creation — only the authoring moves earlier. Skippable on explicit user
  request (`FORGE_SKIP_WRAPUP_GATE=1`).

### Fixes
- **`forge-audit-dup --scope changed` finds prior art.** Changed scope
  previously compared changed units only against each other, so an existing
  twin in an unchanged file was invisible. It now indexes the full tree and
  filters findings to those involving a changed-file unit; changed files
  outside the index roots are indexed too, and the covered-set is computed
  from all exact groups so changed-scope findings stay a strict subset of
  full-scope findings. The function-granularity limitation (inline blocks
  are invisible) is now documented.

## v3.0.0 — Unreleased

### ⚠️ Upgrade notes
- **Skill renamed: `review` → `pr-comments`.** Claude Code ships a
  built-in `/review` that wins the bare invocation, making
  `forge:review` unreachable — and repo-level documentation cannot
  rebind it. Invoke `/forge:pr-comments` for handle-review-feedback;
  update any consumer `CLAUDE.md` binding table or runbook naming
  `forge:review`. FOUNDATION §16 now forbids shipped names colliding
  with Claude Code built-ins.
- **`forge-audit-agents` now audits consumer repos.** Discovery scans
  `agents/` AND `.claude/agents/` (previously forge's own layout only,
  so consumers permanently saw "0 findings"). Consumers will see real
  findings on first run — the step is non-blocking by design (reports
  only, exit 0). `--roots` is now honored; consumer reporter agents
  can be classified via `[tool.forge.audit_agents].reporter_agents` /
  `reporter_with_artifact_agents` (additive to the shipped lists).

### Features
- **`/pr` offers the `code-review` built-in as a second opinion.**
  Prompt-then-consume: the skill prints the command while forge's
  reporters run (user-triggered and billed — confirm-first, never a
  soft blocker, self-skips non-interactive runs and delta mode);
  pasted findings enter the fix pass under an explicit triage contract
  (advisory, per-finding disposition, verify-before-acting,
  deterministic gates win).
- **`block_claude_attribution.sh` catches newer credit phrasings.** The
  hook gained hand-tuned alternatives for `ai-generated`, `assisted by
  ai`, and the vendor-credit phrases the Python validator already
  rejected — previously a raw `git commit -m "ai-generated fix"` passed
  the hook. The hook stays deliberately narrower than the Python phrase
  list (tuned for raw commit-message noise; benign prose like
  "generated with care" still passes).

### Refactor
- **`config.read_tool_forge_section`** — the one `[tool.forge.<section>]`
  reader; eleven hand-rolled lookup sites across `precommit`,
  `pr_delta`, `doctor`, `install_readme_badges`, `verify_agent_doc`,
  `verify_test_naming`, `smart_test`, and `config` itself now route
  through it.

## v2.30.0 — 2026-08-05

### Features
- **`/pr` prints a terminal run summary before delegating to
  `pr-manager`.** Subagent reports never reach the user's terminal, so
  the first human-readable account of a finalization previously
  appeared only on the PR page. The new pre-delegation step summarizes
  the run — changes, branch commits, per-finding disposition,
  deferrals — at the last point where finalization can be redirected
  cheaply. `pr-manager`'s doc now states its report is
  orchestrator-facing and must be relayed to be seen.
- **`/next` resumes a `Requires:`-linked sequence after a merge.** A
  new phase scans the recent merge window for closed issues and
  proposes the single open issue whose `Requires:` line they satisfy
  (confirm-first, never auto-selected; issue numbers validated as bare
  integers — PR bodies are untrusted). Task-selection bypasses are now
  one ordered precedence rule: explicit issue argument > user-named
  carry-over > inferred successor; the explicit-argument path also
  stops detouring through `recommend-next`.

### Fixes
- **`forge-resync` works end-to-end on fresh CI runners and gated
  consumers.** Its commit now routes through the git-write layer's
  identity seam (`git_utils.create_commit`, the commit twin of
  `create_annotated_tag`), so identity-less runners no longer die with
  exit 128; and its branch/commit carry the `no-version` token and
  `[no-version]` marker (public `forge.changelog` constants, regex
  derived from the same spelling), so the `changelog_updated` gate no
  longer blocks the resync commit it ships.
- **`forge-pr-squash-comment` no longer rejects messages naming
  `CLAUDE.md` / `.claude/` paths.** Attribution screening is now
  phrase-based (`co-authored-by:`, `generated with`, `with claude
  code`, …) plus a bare-vendor-token backstop that exempts
  path/filename-shaped tokens — the files forge itself mandates can be
  cited while a bare "thanks Claude" credit still fails.
- **Promotion merge commits pass pre-commit without human bypass.**
  Staged catch-up promotions ran today's toolchain against a
  release-locked historical tree — unfixable by design (the release
  fingerprint forbids content changes), previously concluded by a human
  `--no-verify`. `forge-precommit` now detects the promotion-merge
  context (mid-merge on `release/vX.Y.Z` with the staged tree
  reproducing the tag's release fingerprint) and skips tree-content
  steps; CHANGELOG/versioning guards still run, and any divergence
  beyond `CHANGELOG.md` disengages the suppression so a poisoned tree
  fails loud.

## v2.29.0 — 2026-08-04

### Features
- **"Fix the interface, don't wrap it" — new FOUNDATION §7 principle.**
  When a change can be made either by altering an existing interface or
  by layering a wrapper that compensates for it, alter the interface —
  the break's cost is bounded and visible in the diff; the layer's cost
  hides in the interface that stays wrong. Layer only when the interface
  is genuinely outside your control (§16's shipped-plugin extension case
  is the sanctioned exception, cross-referenced both ways). Enforced by
  a new `design-checker` "wrapper justification" judgment check
  (construct-and-delegate signals; author must justify the layer) and a
  §1 read-before-proposing red flag. Forge's `_FORGE_GITHUB_REPO`
  carve-out relocated from FOUNDATION §2 to forge's own CLAUDE.md
  (forge-specific, not consumer baseline).

### Fixes
- **Deferred changelog mode warns when its guarantee is void.** With
  `[tool.forge.changelog]` `precommit_enforce = false` AND
  `blocking = false`, CI's deferred check degrades to a WARN and the
  red-until-wrap-up guarantee silently stops holding; both the local
  skip notice and the CI failure output now carry an explicit ⚠️ caveat
  pointing at `blocking = true`.
- **`_tag_exists` pins its tag argument behind `--`** — the same
  argument-injection hardening `create_annotated_tag` received; a
  dash-prefixed value can never parse as a `git tag --list` option.

## v2.28.0 — 2026-08-04

### ⚠️ Upgrade notes
- **Diff-scoped checks now compare against `origin/<base_branch>` first.**
  Every diff-base resolution (ruff / docstring / test-naming /
  changelog-updated diff scope, the `[no-version]` commit-tag scan, the
  `changelog_version` stranded-entries check, smart-test change detection)
  now prefers the remote-tracking `origin/<base_branch>` — the ref a PR
  actually merges into — falling back to the local `<base_branch>` only
  when the remote ref is absent (offline / no remote). Previously most
  checks tried the local base first, so a local base branch behind origin
  produced false positives (already-merged commits reported as
  branch-added — e.g. `changelog_version` flagging "stranded" entries on
  an untouched `CHANGELOG.md`). No action needed beyond `forge-upgrade`;
  if a check's diff scope changes for you, your local base was stale.

### Features
- **Docs-only light finalization path for `/pr`.** A PR whose diff is
  entirely docs-shaped (extension-anchored `*.md` / `*.rst` / `*.txt` —
  extendable via `[tool.forge.pr].docs_only_globs`, additive) now skips the
  design/security reporter round and the strict whole-tree pre-commit,
  running only path-relevant gates plus `docs-types-checker` — a
  one-line changelog PR finalizes in seconds, not minutes. Doc-shaped
  files under shipped-behavior paths never qualify, and matching is
  extension-anchored + case-folded (security review: a directory glob
  would have let `docs/evil.py` take the light path, and case-varied
  paths collide with real directories on case-insensitive filesystems).
  `skills/`, `.claude-plugin/`, `.claude/`, and `.github/workflows/`
  joined `HIGH_BLAST_RADIUS_PATHS`, closing pre-existing delta-mode
  gaps.
- **Deferred changelog timing — `[tool.forge.changelog].precommit_enforce`.**
  Default `true` keeps today's behavior (the `changelog_updated` gate
  fires at every local commit). Set `false` for deferred mode: local
  commits — human or agent — self-skip the gate (no mid-PR changelog
  merge conflicts), CI keeps failing with an expected-until-wrap-up
  message, and the `/pr` wrap-up authors the missing bullet
  (mandatory, not skip-when-absent) to flip CI green before merge.
  Orthogonal to `blocking` (timing vs severity). Declared in
  `forge-config --list`; chain documented in `docs/consumer-release.md`.

### Fixes
- **CI skips Dependabot PRs.** Dependabot bumps workflow SHAs but cannot
  author the rolling-next `plugin.json` bump the `plugin_version` gate
  demands, so the CI job could never pass on its PRs; the job now skips
  when Dependabot authors the PR (a skipped required check still
  satisfies branch protection — human review is the gate on deps PRs).
- **git-family hook anchors close subshell + flagged-wrapper gaps.** The
  shared `GIT_ANCHOR` in `block_raw_git` / `block_force_push` /
  `block_git_rebase` missed a bare subshell wrapper (`(git push
  --force)`) and any flag between wrapper and verb (`sudo -n git
  rebase`); the separator class now includes `(` and the wrapper run
  tolerates flag tokens — the same two fixes the continuation-delete
  hook received, keeping the three anchors byte-identical.
- **`block_continuation_delete` hook no longer blocks sibling `.plan/` files.**
  The hook matched `rm`/`unlink` anywhere in the command text and any
  `.plan`-prefixed path — blocking deletion of `.plan/weekly_summary_*.md`,
  commands merely quoting such text (issue bodies), and interpreter
  one-liners on sibling files. It now requires the delete in command
  position (family anchor idiom, plus `xargs` kept deliberately — piped
  deletion was covered before and deletion is irreversible) and a target
  that is `CONTINUATION.md` itself or the `.plan` directory as a whole.
- **Stranded-entries detection no longer false-flags valid restrands.**
  Git renders "insert a new heading above byte-identical entries" as a
  heading rename plus a re-insert of the released heading lower down, so
  the raw diff-line attribution flagged the exact fix-forward the
  stranded error prescribes. `stranded_added_versions` now compares
  heading→content **membership** between the base/tag side and HEAD
  (signature changed to `(old_text, new_text, latest_tag)`; both the
  `changelog_version` step and `forge-release` fetch the old side via
  `git show`). Reorders inside a released section also stop flagging;
  a byte-identical duplicate bullet added post-release is the narrow
  accepted false negative.
- **`forge-release --from-changelog` flags stranded changelog entries.**
  The idempotent no-op ("top heading already tagged → exit 0") also
  covered the failure chain where a failed/raced tag-cut left later PRs
  appending entries under an already-released heading — their commits
  shipped untagged (`X.Y.Z.devN`) while CI stayed green. The no-op now
  classifies `CHANGELOG.md` contents against the released tag (via the
  same detector as the `changelog_version` step) and exits 1 with a
  fix-forward message; no-version merges (which never touch the
  changelog) still rest at exit 0.
- **Annotated tags no longer fail on identity-less CI runners.** All
  three tag-cutting CLIs (`forge-release`, `forge-next-prep --tag`,
  `forge-check-main-tags --fix`) died with git exit 128 ("unable to
  auto-detect email address") on a fresh runner, hidden until the first
  merge that actually bumps a version. One shared
  `forge.git_utils.create_annotated_tag` seam now probes
  `git var GIT_COMMITTER_IDENT` and injects a `forge-release` fallback
  identity only when git has none — a configured identity always wins.
- **`run_git` surfaces git's stderr on failure.** A failing git call
  previously raised a bare `CalledProcessError` ("exit status 128") with
  git's actual message captured but never logged; every git failure in
  every forge CLI now logs `git <args> failed (exit <n>): <stderr>`
  before raising (suppressible per call via `log_errors=False` for
  tolerated failures like a raced tag push, where the caller owns the
  messaging).
- **Every missing-dependency hint now works from consumer repos.** The
  remaining `pip install -e ".[dev]"` hints (`require_cli`, the
  post-merge/post-checkout hook helper) only worked from a forge
  checkout; they now name consumer-valid commands via
  `forge.git_utils.missing_dependency_hint` / `forge_install_command`
  (relocated from `forge.audit.common`). `require_cli` gained per-site
  precision: `pyrefly` → `forge-scripts[typecheck]`, `pytest` →
  `forge-scripts[test]`, `gh` → GitHub CLI install link (a pip hint
  never installed `gh`). Also fixes the v2.27.4 vulture hint, which
  pointed at the `[audit]` extra that no longer contains vulture.
- **`forge-audit-orphans` runs on a default install.** `vulture` moved
  from the `[audit]` extra into core dependencies — the design-checker
  Full Review mandates the orphans recipe, but a bare
  `pip install forge-scripts` couldn't run it (hard failure on the
  missing import). `jsonschema`/`PyYAML` stay in `[audit]`:
  `forge-audit-data` degrades to a visible LOW finding without them.
- **Missing-optional-dependency hints now work from consumer repos.**
  The audit-pack hints said `pip install -e ".[audit]"`, which only
  works from a forge checkout; they now name
  `pip install "forge-scripts[audit]"` via a shared
  `forge.audit.common.missing_dependency_hint` helper (single source of
  truth so the editable form can't come back).
- **`no-version` branch token and `changelog_updated` gate now live in CI.**
  Branch-name resolution read only `git branch --show-current`, which is
  empty on a CI `pull_request` checkout (detached `refs/pull/N/merge`) —
  so the `no-version` branch-token opt-out never fired in CI, and the
  `changelog_updated` step's per-PR guard skipped the whole gate there.
  New `forge.git_utils.resolve_current_branch` (single resolver, used by
  both `forge.changelog.wants_no_version` and the step guard) falls back
  to `GITHUB_HEAD_REF` (the `pull_request` env var carrying the real PR
  source branch) when `--show-current` is empty; a non-empty local
  branch name still wins first, so local behavior is unchanged.
- **Origin-first diff-base resolution, single source of truth.** The four
  independent local-first "resolve the base ref" loops
  (`get_modified_files`, the changelog no-version scan, the
  stranded-entries merge-base, smart-test) collapsed into one canonical
  `forge.git_utils.resolve_base_branch_ref` (+ `merge_base_with_head`),
  origin-first with local fallback and flag-injection guard (a
  `-`-prefixed `base_branch` is rejected everywhere, closing the gap in
  `get_modified_files`).
- **`changelog_version` no longer flags base-sync merges as stranded.**
  During an in-progress `git merge origin/<base>` (MERGE_HEAD present),
  HEAD still predates the merge, so the merge-base is the stale fork
  point and every CHANGELOG bullet the merge brings in from the base
  diffed as branch-added — falsely "stranded", blocking the merge
  commit's pre-commit hook. The stranded-entries diff is now suppressed
  mid-merge (structural heading checks still run); new
  `forge.git_utils.merge_in_progress` resolves `MERGE_HEAD` via
  `git rev-parse --git-path`, so linked worktrees work.

## v2.22.0 — 2026-07-15

Additive — forge's release-tagging primitives become reusable by consumer
repos. Single-track, tag-versioned (setuptools-scm) repos get a first-class
release CLI instead of reimplementing forge's flow; nothing changes for
manifest-versioned or dual-track repos.

### Features
- **`forge-release` — single-track release orchestrator.** For repos whose
  version derives from `v*` tags (setuptools-scm, no
  `.claude-plugin/plugin.json`): guards clean tree + on `base_branch` +
  single-track model + CHANGELOG entry present, then computes the next tag
  (`--bump major|minor|patch` off the latest `v*` tag) and cuts an annotated
  tag + push. `--dry-run` previews. Refuses dual-track and manifest-versioned
  repos, pointing at their flows.
- **Public release primitives.** New `forge.changelog` module
  (`release_headings`, `changelog_lacks_entry` — promoted from private
  helpers in `next_prep` / `verify_changelog_history`) and
  `forge.git_utils.next_version` (pure semver bump). Together with
  `latest_v_tag`, `parse_semver`, `run_git`, `configure_cli_logging` these
  form a documented **stable public Python import surface** — breaking one
  is now a MAJOR release.

### Fixes
- **`forge-config --list` now declares `[tool.forge.agent_doc].path`** — the
  `agent_doc` pre-commit step read it undeclared, so the config surface was
  silently incomplete (also documented in `docs/configuration.md`).

### Docs
- **`docs/consumer-release.md`** — the single-track consumer release recipe
  plus the stable-import-surface table.

## v2.16.0 — 2026-07-01

Additive — `forge-gen-c4` gains **vector PDF export**, and the offline HTML it
builds on now wraps its labels, is consumer-configurable, and is interactive.
The DSL / README / `--format mermaid` output is unchanged.

### Features
- **`forge-gen-c4 --format pdf` — vector PDF export (#137).** Renders every C4
  view to a multi-page **vector** PDF (`docs/architecture.pdf` by default).
  Mermaid is a JS library, so forge drives an already-installed headless browser
  (Chrome / Chromium / Edge / Brave, auto-detected; override with
  `FORGE_C4_BROWSER`) via `--print-to-pdf` — **no new Python dependency, no
  network**. By default (`pdf_fit = "auto"`) **each view prints to its own tight
  page, sized to that diagram** (no letterbox, no blank trailing sheet, the title
  bound to its diagram), so the PDF page count equals the view count. This renders
  each view separately and concatenates with `pdfunite` / `qpdf`; without a merge
  tool it falls back to `contain` (one fixed page per view, scaled to fit). Tunable
  via `[tool.forge.c4.render]` — `pdf_fit` (`auto` / `contain` / `width`),
  `pdf_page_size`, `pdf_orientation`, `pdf_margin`. Fails loudly with a Print →
  Save-as-PDF fallback when no browser is found.
- **C4 layout is restricted to hierarchy-aware engines.** `[tool.forge.c4.render]`
  `layout` accepts only `elk` (= `elk.layered`) and `dagre`; the non-hierarchical
  ELK engines (`elk.stress` / `elk.force` / `elk.radial`) are **rejected at
  config-load** with a clear error — they silently drop cross-cluster edges and
  overlap nodes on C4's multi-cluster views.
- **C4 HTML label-overflow fix (default).** `forge-gen-c4 --format html` now
  emits node labels as Mermaid **markdown strings** and sets
  `flowchart.wrappingWidth` with `markdownAutoWrap`, so the description wraps and
  Mermaid auto-sizes the box — no more single-line overflow in any view or
  orientation. Default behaviour, no config required. Supersedes #138 (#140).
- **`[tool.forge.c4.render]` config.** Each key passes through to
  `mermaid.initialize(...)` — `wrapping_width`, `html_labels`, `font_family`,
  `font_size`, `node_spacing`, `rank_spacing`, `padding`, `custom_css`, `layout`,
  `node_placement_strategy`, `force_node_model_order` (Step 1) plus `theme`,
  `[render.theme_colors]`, `diagram_padding`, `consider_model_order`,
  `merge_edges`, `cycle_breaking_strategy` (Step 2). Defaults reproduce the
  bug-fixed look; unknown keys are tolerated. See `docs/configuration.md` (#140).
- **Interactive C4 HTML — hover-reveal + click-to-open-tab.** Hovering a node
  reveals it, its incident edges and *their* relationship labels, and the
  neighbour nodes while dimming everything else. Clicking a container opens its
  Components tab. Incidence is resolved by **exact node id** (the precise
  endpoints are emitted per pane from the model, not parsed from the ambiguous
  edge DOM id), so prefix-overlapping node names never cross-highlight. Fully
  offline (`file://`), no new dependencies, per-tab, degrades gracefully (#124).

- **Element activation / visibility + tag filtering.** Every `[[person]]` /
  `[[external]]` / `[[container]]` / `[[component]]` accepts `active = false` (or
  `hidden = true`) and `tags = [...]`. A deactivated element — and the components
  an inactive container owns, plus every declared relationship and import-derived
  edge that touches a removed element — is omitted from **all** outputs (DSL,
  README, mermaid, HTML, PDF) while staying in `c4.toml`, so one full model
  renders as slimmer views. `[tool.forge.c4.render].include_tags` /
  `exclude_tags` bulk-filter the **rendered views** by tag (the DSL stays
  canonical). With nothing flagged, output is byte-identical.

- **Visual groups / bands in the Container view.** Elements sharing a `group`
  (e.g. `group = "Capabilities"` / `"Our infrastructure"` / `"Third-party"`)
  cluster into one labelled band, so a dense system reads as a few organized
  zones instead of scattered boxes. Containers band inside the system boundary;
  externals band beside it. Ungrouped elements render flat exactly as before.

### Fixes
- **Duplicate Mermaid SVG id dropped a view (#150).** The HTML rendered panes
  with `mermaid.run()`, which stamps each SVG an id from `Date.now()`; fast
  back-to-back ELK layouts landed in the same millisecond, so two panes got the
  **same** id — an invalid duplicate that left one component view unrendered
  (blank page + title/diagram desync). Each diagram now renders via
  `mermaid.render("c4-view-<i>", …)` with an author-controlled unique id.
- **Empty containers no longer emit a blank Component view.** A container that
  owns no components (e.g. an infrastructure unit) is skipped in the `--format
  html`/`pdf` output, so it produces no empty tab or blank PDF page; containers
  with components are unchanged.

## v2.14.0 — 2026-06-30

Additive — three new pre-commit steps. Two self-skip unless their artifact
is present; the third is bounded by an explicit opt-out and a configured
command, so no consumer action is required.

### Features
- **`vendored_integrity` pre-commit step (blocking).** Verifies every vendored
  `src/forge/data/*.js` bundle (Mermaid, the ELK layout) against the SHA-256
  documented in `VENDORED.md`. A swapped/corrupted blob, or a `*.js` with no
  documented hash, now fails the commit instead of slipping through; an
  orphaned entry (documented but absent) is a non-fatal note. In-process
  (stdlib `hashlib`), self-skips when there is no `VENDORED.md` or no vendored
  `*.js` (#127).
- **`regen_docs` pre-commit step (non-blocking).** Auto-regenerates the two
  generated docs that previously had no drift gate — `docs/api-digest.md` and
  `docs/cli-reference.md` — and re-stages them, the way the ruff step keeps
  formatting fresh. Only refreshes a doc that already exists (never bootstraps
  a surprise file); self-skips when neither is present (#129).
- **`auto_rebuild` pre-commit step.** Heals a stale editable install *before*
  `env_sync` blocks the commit: when a pulled change adds a `[project.scripts]`
  CLI and the install goes stale, it runs the configured
  `[tool.forge.env_sync].rebuild_command` so the gate sees a fresh install.
  Bounded for FOUNDATION §2 — acts only with an explicitly configured command
  (never a defaulted `pip install`), only interactively (skips CI), only when
  a script is actually missing, and never when `FORGE_NO_AUTO_REBUILD` is set
  (#128).

### Refactor
- Relocate the git re-stage helper from `fix_ruff` to
  `git_utils.stage_modified_paths` (public) so both the ruff step and the new
  `regen_docs` step share one git-add-back implementation.

## v2.13.0 — 2026-06-26

Additive — `forge-gen-c4` rendering + modeling improvements. All new
behavior is opt-in via config; the default DSL **and** the default
diagrams are unchanged.

### Features
- **`forge-gen-c4 --format html` renders each C4 view on its own tab.** The
  offline HTML now mirrors the DSL views — System Context, Containers, and one
  Component view per container — as navigable tabs, instead of flattening every
  level into one unreadable diagram. The Container view anchors actors to the
  **system boundary** (not an arbitrary first container) and summarizes
  cross-container relationships at the container level rather than drawing the
  union of component edges (#116).
- **Actors can target a specific container.** `[[person]]` accepts an optional
  `container = "<name>"`, so a role can point at a subsystem instead of always
  the system as a whole; an empty value keeps the system-level relationship
  (back-compatible).
- **`[[relationship]]` endpoints can be any declared element.** Source and
  destination now resolve against persons, containers, components, **and**
  external systems (and the system itself), not only components — so you can
  declare container↔container edges, a component that "publishes results to"
  an external store and another that "reads results from" it (produce/consume
  data flow), or an actor pointing at a specific component. Each endpoint is
  resolved against every element kind and only warns when it matches none;
  component→component behavior is unchanged. When an external is the
  destination of a declared edge, the generic `system → external` edge is
  suppressed **only in the views where the specific edge actually renders**
  (Container / flat) — the System Context view keeps its clean radial
  `system → external` edge.

### Tooling
- **`forge-gen-c4 --format html` is legible on large models.** Each view renders
  on its own scrollable tab at **intrinsic size** (`useMaxWidth: false`, plus a
  CSS override of Mermaid's SVG width cap) instead of being squished to the page
  width, and actors are grouped into an "Actors" band. Crucially, the diagrams
  are laid out by the **ELK engine** — vendored offline as a classic-script
  bundle and registered the Mermaid v11 way (`registerLayoutLoaders` +
  top-level `layout: elk`), with a dagre fallback if it can't load. ELK routes
  the Container view's cross-cluster `container → external` edges cleanly, where
  Mermaid's default dagre engine mis-ranks the external sinks and tangles the
  crossings (a documented dagre clustering limitation). `[tool.forge.c4].direction
  = "LR"` (default) `| "TB"` sets the graph orientation for every diagram.
- **Edge-source control.** `[tool.forge.c4].edges = "imports"` (default) `|
  "declared" | "both"` chooses whether import-derived ("depends-on") edges are
  drawn, or only the hand-authored `[[relationship]]` flow — letting authors
  curate a conceptual flow while keeping the model generated. Per-view
  `container_edges` / `component_edges` overrides let the Container view show a
  clean curated flow while Component views keep real import coupling. Declared
  relationships always render; the `imports` default is byte-identical to
  before.

## v2.12.0 — 2026-06-26

Additive — a new default-on pre-commit step that self-skips outside a
`dev → main` promotion; no consumer action required.

### Features
- **`verify-forge-changelog-history` + the `changelog_history` pre-commit
  step** — guards `main`'s curated CHANGELOG history during promotion. Fails
  when a branch that has merged `origin/<base>` in drops a `## vX.Y.0` heading
  present on the base — catching a CHANGELOG merge conflict resolved blindly
  toward dev. Structural trigger (fires only when the base is an ancestor of
  `HEAD`), so it self-skips on plain `dev` (whose copy may lag) and on
  single-branch repos (#120).

### Docs
- Resolve a contradiction in the release spec: `CHANGELOG.md` is the **one
  exception** to the promotion "resolve toward dev" rule — never resolved
  blindly (`--ours`/`--theirs`); `main`'s curated entries are the source of
  record. Documented in `docs/release-process.md` §3/§5 and the `/promote`
  runbook (#119).

## v2.11.0 — 2026-06-25

### ⚠️ Upgrade notes
- **Docstring-coverage badge SVG renamed.** With
  `[tool.forge.docstring_coverage] badge = true`, forge now writes
  `.badges/docstring-coverage.svg` (was `.badges/DocstringCoverage.svg`) so
  the filename matches the by-responsibility config name. **If you embed the
  badge in a README by path, update the link** — the badge content is
  unchanged. The old `.badges/DocstringCoverage.svg` is no longer written;
  delete the stale file (#81).

### Features
- **`env_sync` forge-scripts version-pin WARN.** When a repo pins
  `forge-scripts==X.Y.Z` in `[project.dependencies]` and the installed
  version is older, the `env_sync` pre-commit step emits a **non-blocking**
  WARN naming the reinstall command. Bounded to the exact `==` form;
  self-skips channel pins, range specifiers, editable/dev builds, and no pin.
  The blocking entry-point freshness check still takes priority (#107).

### Docs / Refactor
- Clarify the docstring-coverage naming — `[tool.forge.docstring_coverage]`,
  the badge SVG, and "the interrogate badge" are one interrogate-powered
  artifact; canonical name `docstring_coverage` (#81).
- Dedup `_GIT_ENV` / `init_git_repo` shared git-test helpers into
  `tests/conftest.py` (#85).

## v2.10.0 — 2026-06-25

Additive — a `/next` release-workflow change for dual-track repos;
single-track repos are unaffected.

### Changed
- **`/next` auto-opens a pending promotion PR.** Phase 1.5 now runs the
  promotion flow itself when a minor is pending (dual-track repos) instead of
  offering it confirm-first. It only **opens** the `release/vX.Y.0` PR — never
  merges, so the human merge stays the one manual step (FOUNDATION §2) — and
  is idempotent (refuses a duplicate open promotion PR). Removes the manual
  `/promote` step from the per-minor loop; declining is just not merging the
  opened PR (#113).

## v2.9.0 — 2026-06-25

Additive — no consumer action required.

### Features
- **`forge-gen-c4` — per-component container assignment.** A rich
  `[[component]]` may name its owning container via `container =
  "<container name>"`; each declared container then renders with its own
  components **and its own component view**. A component that omits
  `container` attaches to the first declared container, so models with no
  `container` keys render byte-identically. Unknown container names — and
  duplicate container names — fail loudly; import-graph edges still render
  across container boundaries (#106).

## v2.8.0 — 2026-06-25

Additive — a new default-on pre-commit step that self-skips unless a
declared CLI is genuinely missing from your install.

### Features
- **`env_sync` pre-commit step** — a deadly-fast, in-process
  install-freshness gate that runs **first**: every CLI declared in
  `[project.scripts]` must be an installed console script, else the editable
  install is stale (a new entry point was added but not reinstalled) and the
  gate may run old code. Blocks by default with the exact reinstall command
  (`./dev/setup.sh` / `pip install -e`); `[tool.forge.env_sync].blocking =
  false` downgrades it to a non-blocking WARN. Self-skips when there is no
  `[project.scripts]` table, the package isn't installed, or the run is
  non-interactive / CI. Never auto-installs (#82).

## v2.7.0 — 2026-06-25

Additive — release-tooling only; dual-track repos benefit, single-track
repos are unaffected (every new step self-skips when `dev == base`).

### Changed
- **Simplified `dev → main` promotion.** `/promote` is now four
  standard-git steps — branch from `dev`, `git merge origin/main`,
  resolve toward `dev`, rewrite the curated CHANGELOG — replacing the
  earlier tree-reconstruction recipe. The merge-in step is what keeps a
  promotion PR's diff to the real release delta instead of re-showing all
  of dev's history (#94).
- **`/next` self-aligns base-branch tags on every run.** Phase 1 now runs
  `forge-check-main-tags --fix`, so after a promotion PR merges the minor
  tag relocates onto `main` automatically — post-promotion is just a
  normal `/next`. Idempotent; self-skips single-branch repos.
- **`forge-check-main-tags` quiets ancient gaps.** Un-promoted minor tags
  *below* the base branch's current line (never promoted, can't backfill)
  log at INFO instead of warning on every run; genuinely pending minors
  above the line still warn (#94).

## v2.6.0 — 2026-06-24

Additive — no consumer action required. The C4 generator is opt-in and
self-skips when `[tool.forge.c4]` (or a `c4.toml`) is absent.

### Features
- **`forge-gen-c4` + the `/c4` skill** — generate a
  [C4](https://c4model.com/) architecture model from the import graph plus a
  human-authored `c4.toml`: emits Structurizr DSL (default), a self-contained
  **offline** HTML view (vendored Mermaid — no Docker/Java/Graphviz/network),
  or raw Mermaid. Component dependencies are machine-derived; context,
  containers, and runtime/subprocess edges are human-declared. Keeps a managed
  diagram block in the README in sync, and adds an opt-in `c4` pre-commit step
  that fails on architecture-diagram drift. `[tool.forge.c4]` config is
  documented in `docs/configuration.md`; design rationale in
  `docs/c4-architecture.md` (#99).

## v2.5.0 — 2026-06-24

Additive — no consumer action required.

### Features
- **`verify-forge-cve-usage --list-inactive`** — read-only reporter listing
  mapped CVEs no longer in pip-audit's live report, i.e. dormant prune
  candidates in `cve_usage_patterns.toml`. Exits 0, writes nothing, never edits
  the map (transient drop-offs make auto-deletion unsafe) (#80).

### Tooling
- **pip-audit now runs once per commit.** The `pip_audit` step writes a
  `code_health/pip_audit.json` sidecar that the `cve_usage` step reuses via a
  new shared `forge.pip_audit_json` seam, instead of each step invoking
  pip-audit independently — halving the OSV round-trips for CVE-usage
  adopters. `verify-forge-cve-usage --audit-json` exposes the same reuse for
  standalone runs (#78).

## v2.4.0 — 2026-06-24

Additive — no consumer action required.

### Fixes
- **Release matching tolerates a curated `@main` CHANGELOG.**
  `forge-check-main-tags` and the `plugin_version` pre-commit guard now
  compare a **release fingerprint** (tree content minus `CHANGELOG.md`),
  so a `release/vX.Y.Z` branch may finalize its condensed `@main`
  CHANGELOG entry without breaking CI or blocking the minor-tag
  relocation. Any non-`CHANGELOG.md` difference is still rejected. Fixes
  the main-tag-alignment feature (v2.3.0) for repos that curate a `@main`
  CHANGELOG (#88, #90).

### Docs
- Document the modified-release-branch pattern and the two `dev`-CHANGELOG
  sync options in `docs/release-process.md` §2–§5 and the `/promote`
  runbook; add the Rust governance-core split RFC under `proposals/` (#42).

## v2.3.0 — 2026-06-24

Additive — no consumer action required (the new check self-skips
single-branch repos).

### Features
- **`forge-check-main-tags`** (`verify-forge-main-tags`) — verifies, and
  with `--fix` repairs, that every minor release tag `vX.Y.0` sits on the
  base branch's squash commit, matched by **tree equality**. Enforces the
  dev/main minor-boundary invariant so `git describe` on `@main` resolves
  to the right minor. Wired as a pre-commit step that self-skips
  single-branch repos (#84).

### Refactor / Tooling
- Consolidated git/tag plumbing into `forge.git_utils` (`run_git`,
  `get_tree_sha`, `read_plugin_version_at_ref`, `read_local_plugin_version`),
  reused by `verify-forge-plugin-version` and `forge-next-prep` (#84).
- `pip_audit` blocking is now configurable via
  `[tool.forge.pip_audit] blocking` (#84).

## v2.2.0 — 2026-06-22

All additive and opt-in — no consumer action required to upgrade.

### Features
- **CVE-usage check** — a usage-scoped second stage on top of `pip_audit`.
  Where `pip_audit` flags vulnerable *packages*, `verify-forge-cve-usage`
  flags vulnerable *usage*: it intersects the CVE IDs pip-audit is currently
  reporting with a consumer `cve_usage_patterns.toml` map and greps the
  source for the patterns, surfacing only real matches with `file:line` +
  risk + mitigation. Self-maintaining (a pattern is checked only while its
  CVE is live), non-blocking, opt-in by presence of the map. New
  `step_cve_usage` pre-commit step (#3).
- **README status badges** — `install-forge-readme-badges` writes a
  drift-aware managed block (`<!-- forge:badges:start/end -->`) of status
  badges (CI, Python version, Ruff, license, forge channel, Claude Code, and
  the local docstring-coverage SVG when present). shields.io URLs where a
  hosted source exists; the existing `.badges/DocstringCoverage.svg` is
  referenced rather than regenerated (DRY). Opt-in via
  `[tool.forge.badges] enabled = true`; wired into `install-forge-bootstrap`
  (#64).

## v2.1.0 — 2026-06-22

### ⚠️ Upgrade notes
- **ruff now honors `[tool.forge].source_dirs` (scope change).** Source-dir
  resolution is unified across every layout-aware tool (ruff, api-digest,
  docstring-coverage, doctest, typecheck) behind one resolver:
  `[tool.forge.<tool>].paths` → `[tool.forge].source_dirs` (+ `test_dirs`)
  → smart auto-detect. ruff previously scanned a fixed broad name-list
  (`src, test, tests, scripts, tools, projects, agents, lib`) and **ignored
  `source_dirs`** (#70). If you set `source_dirs` *and* keep lintable Python
  in a dir outside it (e.g. `scripts/`), add that dir to `source_dirs` (or a
  `[tool.forge.ruff].paths`) so ruff keeps linting it. **This also applies
  when `source_dirs` is unset:** the old broad list scanned `scripts/`,
  `tools/`, `agents/`, `lib/`, and `projects/` too, and smart auto-detect
  does not — if you keep lintable Python in any of those, add it to
  `source_dirs` / `[tool.forge.ruff].paths`. Repos whose Python lives only
  under `src/` (or top-level packages) + `tests/` need no action.
- **New `release_tag_guard` pre-commit step (dual-track repos only).** Blocks
  a commit when `plugin.json` is more than one rolling-next step ahead of the
  latest `v*` tag — i.e. an intermediate release was bumped past without
  being tagged (the failure that shipped v1.25.0 untagged, #66). **Self-skips
  for single-track repos, repos without `.claude-plugin/plugin.json`, and
  when `plugin.json` isn't strictly ahead** — so consumers see nothing. Fix a
  trip by running `forge-next-prep --tag`.

### Features
- **Unified, granular source-dir resolution** — `[tool.forge].source_dirs` /
  `test_dirs` are now the single definition every layout-aware tool scans,
  with optional per-tool `[tool.forge.<tool>].paths` overrides (new for
  `ruff` and `api_digest`; existing for coverage / doctest / typecheck). The
  unset default is **smart auto-detect** (`src/` or top-level packages; then
  `tests/` / `test/`), replacing a fixed name-list that scanned phantom dirs
  (#68, #70).
- **`release_tag_guard`** — pre-commit backstop enforcing the dev release-tag
  cadence (#66).

### Fixes
- **`block_protected_branches` now blocks pushes by their refspec
  destination** — a push from an unprotected branch to a protected one
  (`git push origin HEAD:dev`, `feature:dev`, `feature:refs/heads/dev`,
  `+dev`) was previously missed (only the *current* branch was checked).
  This destination guard has **no agent bypass**: not even
  `forge:git-commit-push` may push directly to a protected branch (matching
  `block_branch_deletion`). The agent bypass now covers only normal
  feature-branch pushes (#74).
- **`block_install_deps` now catches `conda run conda install`** (and the
  general `<mgr> run <mgr> install` wrapper) plus `conda env update` (direct
  and wrapped) — the manager-wrapping-a-manager gaps surfaced in review
  (#62).

## v2.0.0 — 2026-06-19

### ⚠️ Upgrade notes
- **Pre-commit steps now default to whole-tree scope (BREAKING).** The
  three file-selecting steps — `ruff`, `docstring_verification`,
  `test_naming_check` — now run over the **entire tracked source tree**
  by default, not the diff vs main. `ruff` already did; the change affects
  `docstring_verification` (blocking) and `test_naming_check` (warning).
  **A consumer whose tree has pre-existing docstring/signature mismatches
  outside the current diff will be newly blocked on the next commit.** To
  restore the old diff-only behavior, set in `pyproject.toml`:
  ```toml
  [tool.forge.precommit]
  scope = "diff"                       # global, or:
  [tool.forge.precommit.scope_overrides]
  docstring_verification = "diff"
  ```
  See [`docs/configuration.md`](docs/configuration.md) "Changing a step's
  scope". Resolution: `scope_overrides.<step>` → `scope` → `all`.
- **`install-forge-bootstrap` now writes `.claude/settings.json`.** A new
  `claude-settings` step enables the forge Claude Code plugin **per repo**
  (marketplace + `enabledPlugins`), so the plugin loads only where you
  adopt forge — not globally, where its agents would error in repos without
  `forge-scripts`. Idempotent + merge-preserving. Opt out with
  `install-forge-bootstrap --skip claude-settings`.
- **The CVE scan now actually runs by default.** `pip-audit` ships as a
  core dependency (it backs the default `pip_audit` step), so after
  upgrading + reinstalling, the dependency-vulnerability scan runs where it
  previously **silently no-op'd** if `pip-audit` wasn't separately installed
  (#71). It is **non-blocking** (advisory `WARN`), but you may now see CVE
  advisories on commit that were invisible before — review them, or
  `disable = ["pip_audit"]` under `[tool.forge.precommit]` to turn the step
  off deliberately.

### Features
- **Configurable per-step scope** — `[tool.forge.precommit].scope`
  (`all` | `diff`, default `all`) plus `scope_overrides` for per-step
  control, wired through a new `--scope` flag on `fix-forge-ruff`,
  `verify-forge-docstrings`, and `verify-forge-test-naming`. New
  `git_utils.get_tracked_files` is the whole-tree counterpart of
  `get_modified_files` (#65).
- **`install-forge-claude-settings`** — write / verify the per-repo plugin
  enablement; the marketplace `ref` tracks your `forge-scripts` pip pin
  (override `--ref`; `--check` verifies without writing). Wired into
  `install-forge-bootstrap` (#63).

### Fixes
- **`block_claude_attribution` hook** now catches the canonical Claude Code
  footer `Generated with [Claude Code](…)` (the markdown `[` defeated the
  old adjacent-words regex) and the `🤖` emoji signature — the exact
  attribution the harness emits by default no longer slips into history.
- **`forge-gen-api-digest` honors `[tool.forge].source_dirs`** when `--roots`
  is omitted (falling back to `src/` auto-detect when unset), so a multi-root
  repo gets a complete digest and agrees with `verify-forge-docstring-coverage`
  on where the source roots are (#67).
- **`pip_audit` no longer silently no-ops.** `pip-audit` is now a core
  dependency, and a missing binary renders as a loud non-blocking `WARN`
  instead of a silent skip — a security gate that quietly does nothing gave
  false assurance (#71).

### Refactor
- **Shared `claude_settings_schema` module** — the `.claude/settings.json`
  marketplace key path, `forge@forge` id, and empty-hook scaffold now live
  in one place, consumed by both the write side
  (`install-forge-claude-settings`) and the read side
  (`install-forge-claude-md` channel detection). Fixes a standalone
  fresh-repo path that dropped the hook scaffold.

## v1.25.0 — 2026-06-19

### ⚠️ Upgrade notes
- **`block_install_deps` now also blocks pipenv, poetry, and uv** (and the
  `<mgr> run pip install` wrappers), closing a gap where an agent in a
  pipenv/poetry/uv repo could re-resolve unpinned dependencies. If a
  trusted flow legitimately needs an agent to run one, opt out — per
  manager (`[tool.forge.hooks] block_install_deps = ["pip", "conda"]`) or
  entirely (`= false`). The default stays block-all (FOUNDATION §2).

### Features
- **`docs/adopting.md`** — modular adoption guide: three independent
  install tracks (CLIs only / + git hooks / + plugin), a "what lands on
  disk" table, and a drift/refresh/upgrade explainer (#33).
- **`forge-upgrade` surfaces upgrade notes** — after a successful upgrade
  it prints the recent `⚠️ Upgrade notes` so you see the consumer-action
  items; the CHANGELOG now ships as package data to make this work (#34).
- **post-merge tag advisory** — `forge-post-merge` warns on the dev branch
  when `plugin.json` is ahead of the latest tag (a rolling-next release
  that was never tagged), advisory only (#21).
- **`forge-doctor` checks enabled-step tools** — flags when a step in
  `[tool.forge.precommit] enable` lacks its tool (typecheck→pyrefly,
  doctest→pytest) before the commit-time failure (#57).

## v1.24.0 — 2026-06-17

All additive and opt-in — no consumer action required to upgrade.

### Features
- **Pluggable pre-commit step framework** — `[tool.forge.precommit]
  enable` / `disable` (plus `forge-precommit --only` / `--skip`) turn any
  step on or off uniformly, on top of each step's own self-skip (#6).
- **Opt-in `doctest` step** — `pytest --doctest-modules` over
  `[tool.forge.doctest].paths` (default `["src"]`); non-blocking by
  default (#5).
- **Opt-in `typecheck` step** — runs `pyrefly` over
  `[tool.forge.typecheck].paths`; non-blocking by default (#48).
- **Opt-in `doc_consistency` step** + `verify-forge-doc-consistency` CLI —
  checks that every `[project.scripts]` CLI is documented in
  `docs/cli-reference.md`; non-blocking (#4).

### Tooling
- `forge-config --list` now enumerates the new
  `[tool.forge.precommit/doctest/typecheck]` keys, and a drift test
  couples `CONFIG_KEYS` to its readers so the registry can't silently go
  stale (#46).

## v1.23.0 — 2026-06-17

### Features
- `forge-config --list` advisor + repo-wide `[tool.forge].source_dirs` /
  `test_dirs` layout keys + `docs/configuration.md`; `[tool.interrogate]`
  stays native (no wrapper).
- New `/forge:test` skill chaining the test agents (advisor → writer →
  review → precommit-fixer).

### Fixes
- Rolling-next version guard now skips when `HEAD`'s tree reproduces
  **any** published `v*` tag (not only the latest), unblocking staged
  promotion of a minor that sits below the global-max tag.

### Docs
- `docs/release-process.md` — single source of truth for versioning,
  `dev → main` promotion, and the invariant→test contract.

## v1.22.0 — 2026-06-17

### ⚠️ Upgrade notes
- **`block_protected_branches` now also protects `dev` by default.**
  Direct pushes to `[tool.forge].dev_branch` (default `dev`) are blocked
  for agents — open a PR instead. Single-track repos are unaffected
  (`dev_branch` defaults to the base branch).

### Fixes
- `forge-next-prep --promotion-status` lists pending **minors only**
  (`X.Y.0`); interleaved patch tags fold into the next minor.

### Refactor / Tooling
- The version guard and the auto-tagger now resolve "latest release" the
  same way (global semver-max `v*` tag), fixing dual-track disagreement
  where a tag on `main` is absent from `dev`'s history.

## v1.21.0 — 2026-06-12

### Features
- Require a `Requires:` line atop every issue (FOUNDATION convention).

### Refactor / Tooling
- Promotion model: a dedicated `release/vX.Y.Z` branch is now required
  (never a direct `dev → main` merge), with staged catch-up one minor at
  a time, surfaced by the new read-only `forge-next-prep
  --promotion-status` CLI.
- Remove dead `tomllib` import guards now that the Python floor is 3.11.

## v1.20.0 — 2026-06-12

### ⚠️ Upgrade notes
- **Python floor raised to 3.11.** `forge-scripts` no longer installs on
  Python 3.10 (it uses `datetime.UTC` / `tomllib`, both 3.11+ stdlib).
  Move your repo and CI to Python ≥ 3.11 before upgrading forge.
- **Slow-tests CI recipe changed.** If you adopt the slow-tests report,
  pass `--durations` explicitly on the pytest command —
  `pytest --durations=25 --durations-min=1.0 | tee code_health/pytest.log`.
  A bare `pytest` yields an empty report: the durations flags live in
  forge's *own* `pyproject.toml`, not yours.

### Features
- `forge-slow-tests-report` CLI: parses pytest `--durations`, merges
  across batches, and ranks the slowest tests — a read-only reporter for
  CI and local runs (#29).
- Raise the Python floor to 3.11 — `requires-python >= 3.11`, ruff target
  `py311` (#29).

### Tests / Docs
- Test-doc audit fixes; document the dev tag cadence; the CI recipe now
  passes `--durations` explicitly so the slow-tests report works in any
  consumer repo regardless of its pytest config (#27, #29).

## v1.19.0 — 2026-06-12

### Features
- Consumer hook-extension directories — `post-merge.d` / `post-checkout.d`
  run consumer `*.sh` scripts after the managed hook (sorted,
  failure-tolerant, interactive-only, and surviving hook refresh).
  Additive and opt-in; drop scripts in those dirs to use it.

## v1.18.0 — 2026-06-12

### ⚠️ Upgrade notes
- **New `block_branch_deletion` hook.** Claude Code agents can no longer
  delete a protected remote branch (`base_branch` / `dev_branch`). No
  action unless you relied on an agent doing that — run the delete
  yourself with `! …` instead.

### Features
- `block_branch_deletion` hook — blocks agents from deleting protected
  remote branches.

## v1.17.0 — 2026-06-12

### ⚠️ Upgrade notes
- **Hook-version sidecar.** Managed git hooks now read their version from
  a per-clone `.githooks/.forge-hook-version` file (keeps tracked
  `.githooks/*` byte-stable across bumps). Add `.githooks/.forge-hook-version`
  to your `.gitignore` — the installer does not write the ignore rule for
  you.
- **Two new foundation agents** — `forge:test-advisor` + `forge:test-writer`
  become available after `/plugin update forge@forge` + `/reload-plugins`.

### Features
- Add the `forge:test-advisor` + `forge:test-writer` foundation agents
  and the testing-documentation policy they enforce (fixtures excluded
  from `Args`, structured mock docs, Null-Objects-over-Mock; interrogate
  `ignore-nested-functions` + ruff `D417` in tests) — 12 foundation
  agents total.
- Per-clone conda env name via `.conda_env_name`, so parallel forge
  clones each get their own environment (opt-in: drop a `.conda_env_name`
  file at the repo root).

### Fixes
- `forge-post-merge` now accepts git's squash-flag positional argument
  (it had been exiting 2 on every merge, killing the drift check and the
  hook self-refresh).
- Store the git-hook version in a gitignored sidecar so tracked
  `.githooks/*` stay byte-stable across version bumps.

### Docs / Chore
- Complete the README CLI and pre-commit reference tables.
- Share forge-standard CI permissions; allow `-D` for merged branches.

## v1.16.1 — 2026-06-11

### Chore
- Initial published artifacts: git hooks, `docs/api-digest.md`, and
  `docs/cli-reference.md` generated at forge 1.16.1; README refreshed
  around the guardrails thesis.
