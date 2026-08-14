# FOUNDATION.md — Forge: Claude Engineering Principles

Single source of truth for engineering principles shared across all Forge
consumer repos using the foundation plugin. Consumer `CLAUDE.md` files link to
this document at the top, then add repo-specific rules below.

> **Conflict rule**: when consumer `CLAUDE.md` and this document disagree,
> **the consumer wins** — foundation is shared baseline; repos may layer
> stricter rules.

---

**Sections:** 1 Critical Thinking · 2 Core Safety Rules · 3 Mandatory Delegation
· 4 Pre-commit Enforcement · 5 Ruff Configuration · 6 Git & PR Workflow · 7
Design Principles · 8 Documentation Standards · 9 Logging Pattern · 10
Continuation Protocol · 11 Agent Boundary Protocol · 12 Single Source of Truth ·
13 `code_health/` Convention · 14 Issue Tracking & Triage · 15 Runtime Context
Awareness · 16 Extending shipped agents/skills/CLIs · 17 Smart-test depth model.

---

## 1. Critical Thinking Directive

**Do not be a yes-man.** Help reach the best decision, not agree with the user.

- When the user is wrong, say so clearly with reasons.
- When a plan has flaws, point them out before executing.
- When asked for something suboptimal, propose the better alternative.
- A brief disagreement that leads to a better outcome beats pleasing the user
  with a bad idea. Be honest, critical, and direct.

### Every failure requires investigation

Never dismiss errors as "not related to our changes" or "pre-existing." Every CI
failure, test error, or unexpected behaviour must be investigated — read the
error, trace the cause, verify whether your changes contributed. Then present
options: (1) fix now (small + in scope), (2) fix in this PR (related/blocking),
(3) file an issue with root-cause analysis (truly separate). "Not our problem" is
never acceptable.

### Pattern Investigation Protocol

Before changing established patterns, investigate why they exist (`git log
--oneline <file>`, `git blame`); document "pattern X exists because Y." When
changes expand beyond the task, ask whether to investigate or stay focused. **Red
flags:** test failures in unrelated areas, changing many file types, modifying
public APIs, "fixing" seemingly intentional patterns.

### Read before proposing

Before proposing a non-trivial change, read the subsystem's code AND docs: the
module(s) touched, their direct callers/callees, the README / docstrings /
`docs/` describing the area, and any related GitHub issue (`gh issue list
--search` before opening one). If it's more than a handful of files, delegate the
read to `Explore` — ground truth, not vibe-truth. **Red flags that mean you have
not read enough:** proposing a new helper without checking it already exists;
creating a new module/file when an existing module is the natural home (prefer
extending or relocating within an existing module over minting a new one, §7);
adopting an issue's *suggested* name/path as a directive instead of a
hypothesis to validate against the current layout; proposing a wrapper without
first checking whether the wrapped interface could simply change (§7); a path
that doesn't exist on disk; schema changes without inspecting the current
schema; a fix based on what a function "should" do rather than what it does.

### Plan before executing

For any task touching more than one or two files, or that mutates remote state
(commits, pushes, PRs, issues, tags), write a plan FIRST (files, order, side
effects) and wait for explicit go-ahead. Skip only for genuine one-shots — a
typo, a single-line config, or follow-on edits in a review loop the user drives.

### Ask before acting on ambiguity

Pause and ask when (a) the instruction has two reasonable readings and the user
hasn't picked, (b) the plan produces an unauthorized side effect (extra commits,
version bumps, branch switches), or (c) you're about to act on a remembered
convention without checking current code still matches. Asking beats reverting.

---

## 2. Core Safety Rules

- **NEVER install dependencies** (`pip install`, `conda install`). Tell the user
  the exact command instead — installing deps can break configured environments
  and introduce untested combinations.
- **NEVER use `--no-verify`** to bypass pre-commit hooks. Fix violations instead.
- **NEVER force push** without explicit user approval. Check upstream divergence
  first: `git fetch origin && git log origin/<branch>`. The `block_force_push`
  hook enforces this across every force vector — `--force` / `--force-with-lease`,
  the `-f` / `-uf` cluster, and `+`-prefixed refspecs. A human pushes with `! git
  push ...`.
- **NEVER rebase** from an agent. Rebasing rewrites published history — forcing a
  (blocked) force-push and discarding commits a reviewer saw. forge squash-merges
  every PR, so sync a branch with a plain **merge** of the base (`git merge
  origin/<base>`), never a rebase. The `block_git_rebase` hook enforces this with
  **no bypass**: it blocks `git rebase` and `git pull --rebase` / `-r`. A human
  rebases via `! git rebase ...`.
- **NEVER add Claude/AI attribution** in commits, PRs, or merge messages (no
  `Co-Authored-By`, no `Generated with Claude`, no AI references).
- **NEVER push directly to a protected branch** (`base_branch` / `dev_branch`,
  default `main` / `dev`). Branch first. The `block_protected_branches` hook
  enforces this, defaulting to the same `main` + `dev` set as
  `block_branch_deletion`.
- **NEVER merge PRs autonomously.** Merging is the user's decision — produce the
  squash-merge message and wrap-up comment, then stop. `block_pr_merge` enforces
  this (blocks `gh pr merge` and the equivalent `gh api .../pulls/N/merge`); users
  merge via `! gh pr merge ...`.
- **NEVER delete a protected remote branch** (`base_branch` / `dev_branch`).
  Irreversible, and since an agent runs with the user's credentials it *bypasses*
  server-side deletion rulesets. `block_branch_deletion` enforces this with **no
  bypass** — blocks `git push --delete` / `:ref` and `gh api -X DELETE
  …/branches/…`. Local `git branch -d/-D` is untouched; a human deletes via `! …`.
- **No backwards-compatibility shims** unless explicitly requested — no `OldName =
  NewName`, no deprecation warnings, no re-exports of moved modules. Clean breaks
  by default.
- **No secrets in code or commits.** Use `.env` (gitignored) or env vars; provide
  `.env.example` with placeholders. CI secret scanning blocks detected secrets.
- **No private organizational names in code, docs, or examples.** Use generic
  placeholders (`<repo>`, `<scope>`, `<service>`, `foo`, `bar`) or in-repo
  concrete names only — never private employer / client / project / process
  names: forge is shared open content and consumer `CLAUDE.md` / agent prompts
  are read by everyone. Inspiration from private context is fine; fingerprints in
  the artifact are not. (Forge's own single carve-out for its upstream constant
  lives in its `CLAUDE.md`, not here.)
- **Foundation CLIs are required, not optional.** Any agent / hook / script / CI
  depending on a forge-shipped CLI (`verify-forge-*`, `install-forge-*`,
  `forge-doctor`, `forge-precommit`) must **fail loudly** when it's missing —
  never silently fall back to a raw tool (`ruff`, `gh`, `git`) or `python -m`. The
  error points at the install command (e.g. `forge-scripts not installed. Run
  \`pip install forge-scripts\``). The wrappers give consumers a uniform interface
  forge can extend (logging, defaults, version checks); silent fallbacks defeat that.

---

## 3. Mandatory Delegation

Specific tasks **must** be delegated to specialized subagents. Handling directly
is forbidden.

### Agent naming convention

Foundation agents ship via the `forge` plugin and resolve as `forge:<name>` (e.g.
`forge:pr-manager`); a bare name fails with `Agent type '<name>' not found`. The
thirteen foundation agents are:
`forge:design-checker`, `forge:docs-types-checker`, `forge:git-commit-push`,
`forge:issue-triage`, `forge:knowledge-search`, `forge:perf-optimizer`,
`forge:pr-manager`, `forge:precommit-fixer`, `forge:prior-art`,
`forge:security-checker`, `forge:test-advisor`, `forge:test-writer`,
`forge:weekly-summary`.

**Consumer wrappers MUST use distinct names.** When a consumer repo layers
repo-specific extras on a foundation agent, it ships a local wrapper under
`.claude/agents/<name>.md` that delegates via the `Task` tool. The wrapper name
**must differ** from the canonical name — otherwise the local file shadows the
foundation agent and direct `forge:<name>` calls become unreachable. Convention:
suffix with the repo/scope (`design-checker-<repo>`, `security-checker-<scope>`),
delegating with `Agent(subagent_type="forge:<base-name>", prompt="<repo-specific
extras>... <original task>")`.

| Task | Agent | Trigger |
|---|---|---|
| Create a new file / module / top-level symbol | `forge:prior-art` | **BEFORE** writing — a REUSE verdict means no new file at all |
| Edit existing file | `forge:design-checker` (or a `design-checker-<repo>` wrapper that delegates here) | **BEFORE** writing code |
| Clear pre-commit failures | `forge:precommit-fixer` | Before commit |
| Commit + push | `forge:git-commit-push` | After `forge:precommit-fixer` |
| Plan test coverage / review tests | `forge:test-advisor` | Before writing tests; and after, to review |
| Write tests | `forge:test-writer` | After `forge:test-advisor` (advise) |
| Design / security review | `forge:design-checker` / `forge:security-checker` | Reports only — main agent acts |
| PR lifecycle | `forge:pr-manager` | After all checks |
| Issue triage | `forge:issue-triage` | Backlog management |
| Grounded knowledge retrieval | `forge:knowledge-search` | When summarizing from sources |

**Forbidden — do NOT handle directly:**
- Run `git commit` / `git push` directly → use `forge:git-commit-push`
- Invoke `ruff` (or `fix-forge-ruff`) directly from an agent → use `forge:precommit-fixer` (reads `code_health/` reports, dispatches fixes). Only the pre-commit hook runs ruff here.
- Hand-curate a file list or rule selection for `forge:precommit-fixer` → don't; it scopes itself off the pre-commit report.
- Write PR descriptions or squash-merge messages → use `forge:pr-manager`
- Review code for security / design → use `forge:security-checker` / `forge:design-checker`
- Install dependencies → never do this; tell the user

### Standard workflow orders

**Commit:** `forge:prior-art` (when creating files/symbols — first, a REUSE verdict ends the plan) → `forge:design-checker` (pre-write) → code changes → `forge:precommit-fixer` → `forge:git-commit-push`

**PR finalization:** `forge:design-checker` + `forge:security-checker` + `forge:docs-types-checker` (parallel) → `forge:precommit-fixer` (mode `strict`) → `forge:pr-manager`

**Test writing:** `forge:test-advisor` (advise) → `forge:test-writer` → `forge:test-advisor` (review) → `forge:precommit-fixer`

---

## 4. Pre-commit Hook Enforcement

`.githooks/pre-commit` is the single quality gate. Run it manually between
commits to catch issues early.

**Agent requirement**: if pre-commit blocks, you **must** fix ALL violations
(including pre-existing) before committing — "blocked by pre-existing violations"
without fixing = non-compliance. Scope expands to fix violations in any file you touch.

**Forbidden responses** (all mean the same evasion — fix instead): "blocked by
pre-existing violations in X.py (not our code)"; "should I fix the pre-existing
violations?" (yes, don't ask); "fixing them is out of scope / a lot of work"
(doesn't matter); or reporting the block and stopping.

**Never use `--no-verify`** to bypass pre-commit. Docstring WARNINGS are
non-blocking; ERRORS must be fixed.

### Pre-commit scope policy

- **Existing-and-passing tools** (ruff, ruff format, ruff S, custom contracts) → full codebase scope.
- **New tools** (gain mypy, gitleaks, …) → modified files initially; tighten to full codebase once the baseline clears.
- **Coverage-threshold tools** (interrogate) → full codebase, threshold = current passing baseline, raised over time.

---

## 5. Ruff Configuration

Single config: **`ruff.toml`** at repo root. Strict — `select = ["ALL"]`.
Under `ALL`, every rule a ruff minor stabilizes auto-activates, so forge pins
ruff to one minor (`>=X.Y,<X.Y+1`) and raises the cap deliberately, triaging
the newly stable rules — never an unbounded pin. Baseline ignores for
`select = ["ALL"]` repos: `COM812` / `ISC001` (conflict with `ruff format`),
and `CPY001` when the repo has no per-file copyright-header policy (a
repo-level `LICENSE` is the copyright policy). Repos not wanting ruff ≥0.16's
Markdown code-block formatting add `exclude = ["*.md"]` under `[format]`.

### Rules
- **NO `# noqa` comments.** Fix the code properly. Only exception: `# noqa: E402`
  for legitimate import order constraints.
- Naming violations (N802, N803, N806) → rename, don't suppress.
- Complexity violations → refactor (extract a helper). Never raise the limit
  without explicit user approval.
- Docstring rules (D100, D103) → add proper docs.
- Boolean params must be keyword-only (after `*`): `def foo(x, *, verbose: bool = False)`.
- All imports at top of file (PLC0415 violations: refactor; do NOT use deferred imports as a habit).
- When moving a deferred import to top-level, all test mocks `patch("orig.module.name")`
  must update to `patch("consuming.module.name")` — `patch` targets the namespace
  where the name is looked up.

### Foundation default complexity limits (loose baseline)

| Metric | Foundation default | Ruff rule |
|---|---|---|
| McCabe complexity | ≤ 15 | C901 |
| Function arguments | ≤ 8 | PLR0913 |
| Branches per function | ≤ 15 | PLR0912 |
| Return statements | ≤ 6 | PLR0911 |
| Statements per function | ≤ 50 | PLR0915 |

**Consumer repos MAY enforce stricter limits in their `ruff.toml`.** Agents read
the consumer's `ruff.toml` as the actual enforcement source — not these defaults.

### Common per-file ignores

- `scripts/**/*.py`: ignore `S603` / `S607` (subprocess, partial path), `INP001`
  (implicit namespace) — scripts invoke external CLIs and live outside packages.
- `tests/**/*.py`: ignore `S101` (assert), `PLR2004` (magic values).

### Conventions

- **`lines-after-imports = 2`** (`[lint.isort]`) — PEP8-strict 2 blank lines
  between imports and module code; avoids cross-repo formatter divergence.
- **`tests/` (plural)** for the test directory — the Python community standard.
  forge tooling (`verify-forge-test-naming`, `DEFAULT_SOURCE_DIRS`) still accepts
  `test/` (singular) for back-compat, but `tests/` is canonical.

---

## 6. Git & PR Workflow

### First step (new task)

Applies when starting a **fresh task from a clean state** — already on a feature
branch with uncommitted or unmerged work? The next section governs instead. Start from updated
main, then branch: `git checkout main && git pull origin main`, then `git
checkout -b <type>/<description>`. Branch prefixes: `feat/`, `fix/`,
`refactor/`, `test/`, `docs/`, `chore/`. After plan mode, verify with `git branch
--show-current` before editing. **Never start work from stale main.**

### A new request mid-branch — confirm, don't reflex-split

When you're on a feature branch with uncommitted or unmerged work and the user
asks for **anything new** — related side task or not: **never automatically open
a new branch/PR.** For a small, quick change, **default to the same branch** and
just do it there. For something genuinely large or independently-releasable:
(1) remind them what's in flight (branch, and PR if one exists), (2) confirm
they want the new thing now, (3) ask whether it should be its own branch.
**Favor quick development over heavy branch/PR ceremony.** When unsure, stay
and ask.

### Dependency bumps ship alone

A dependency pin change (version floor, cap, new pin — security advisory
or not) never rides a feature/fix PR: it lands in its own dedicated
`chore(deps)` PR, or stays a reported advisory until the user approves.
Why: a pin bump has its own blast radius (env rebuilds, transitive
changes, independent rollback) and hides in an unrelated diff. Agents —
including `forge:precommit-fixer` — report advisories with the suggested
pin; they do not edit pins.

### Commit messages

- Max 50 words. Focus on what + why. **No Claude/AI attribution.**
- Conventional format — types in `forge.pr_squash_comment.CONVENTIONAL_COMMIT_TYPES` (canonical; shell hook synced via `forge-gen-commit-types`).

### PR descriptions

- Max 300 words. Sections: Summary / Changes / Testing / Breaking
  Changes (omit if none). Update if scope shifts.
- **The `## Summary` lead is written in plain English** for the reader who
  uses the product but not the codebase: no class/function names or
  internals; lead with the consequence for that reader, not the mechanism;
  and say plainly when results stop being comparable across the change.
  Technical detail belongs under `Changes`.

### PR finalization — verify first, never block on CI

- **Verification precedes publication.** The finalization reviews (design /
  security / docs reporters + strict pre-commit pass) run against the local
  tree about to be pushed, so findings are fixed in the PR's own commits —
  not follow-ups — and the changelog version heading settles before the
  branch is published. Order: verify locally → fix → commit → **author the
  wrap-up + squash message** → push → open PR → post them. The wrap-up is
  written before the PR exists (only its posting needs a PR); the
  `block_unverified_pr_create` hook blocks `gh pr create` until the authored
  wrap-up names the current `HEAD` (skippable via `FORGE_SKIP_WRAPUP_GATE=1`
  — on explicit user request only; promotion PRs self-exempt when the
  `release/vX.Y.Z` branch's tree reproduces its tag modulo the curated
  changelog — provenance, not naming). A **draft PR** is the escape hatch
  when the PR should be visible earlier.
- **Verification starts itself.** The moment a branch's implementation
  commits are done, run the finalization reviews — automatically, without
  stopping to offer or ask. The reviews are read-only; nothing about them
  needs permission. Stopping at "ready to finalize?" with verification unrun
  is stopping too early — only genuinely outward-facing or destructive steps
  pause for the user.
- **The wrap-up never waits on CI.** Its value is the review verdicts; CI
  green is a separate signal on its own schedule. Post as soon as the checks
  are done, and state plainly when CI has not completed — an unqualified
  wrap-up reads as "all green", a false claim.
- Why: a wrap-up posted at one SHA and read at another describes a tree that
  no longer exists. The `verified-at:` header (reporter contract,
  `agents/_TEMPLATE.md`) makes such drift detectable; verifying the tree
  being pushed closes the gap by construction.
- **The flow does not end at posting — monitor the PR.** By default, after
  the wrap-up is posted, delegate one background monitor per open PR watching
  review comments and merge state. On merge: at least the local cleanup
  (sync the base branch, prune the merged local branch — the `/next` cleanup
  phase). On new comments: surface them. The main session stays free for the
  next task. Skip only on explicit user request or when
  `forge.run_context.is_non_interactive()`.

### Squash-merge messages (mandatory at PR finalization)

`forge:pr-manager` enforces: max 50 words; 3–5 bullets; conventional-commit
title; title + bullets only; no Claude/AI attribution. Posted via
**`forge-pr-squash-comment`** — validates every rule, wraps the body in a
triple-backtick fence (copy verbatim into GitHub's squash dialog), and posts via
`gh`; agents must not hand-construct it (`--dry-run` previews, `--patch
<comment-id>` rewrites). The squash message is the permanent `main` commit; if it
can't be summarized in 50 words, the PR is too big.

### PR review comments

Reply to every comment with `✅ **Resolved in commit <hash>**` plus a brief
explanation of what was done and where (file:line). Post via `gh api
repos/<owner>/<repo>/pulls/<PR#>/comments/<comment_id>/replies`.

---

## 7. Design Principles

Reviewed by `forge:design-checker` agent (foundation) + per-repo wrappers.

**SOLID.** **SRP** — one clear purpose per module / class / agent. **OCP** — prefer
adding new modules over editing existing. **LSP** — only when inheritance is used
(composition preferred). **ISP** — focused, minimal interfaces; callers shouldn't
ignore half the methods. **DIP** — depend on abstractions; isolate I/O behind
small seams so logic on top is testable without it.

**DRY.** Shared logic in one place + referenced, not copied; shared agent
behaviours in shared docs (this file or consumer `CLAUDE.md`), referenced by
agents. A fact appears in exactly one place; everywhere else points back.

**Fix the interface, don't wrap it.** When a change can be made either by
altering an existing interface or by layering a wrapper that compensates
for it, alter the interface. A wrapper built to make an awkward API
usable duplicates that API with a reconciliation step attached; the
awkward shape stays, and every later change pays for both. The costs are
asymmetric at review time: the break shows its whole cost in the diff
(converted call sites, one version bump); the layer hides its cost in
the interface that stays wrong. Prefer the break when call sites are
countable; layer only when the interface is genuinely outside your
control — a third-party or published contract, or §16's shipped-plugin
extension case. (Not a reversal of OCP: OCP covers adding genuinely new
capability without touching stable code; this rule covers compensating
for an interface you already control and that is wrong.)

**KISS.** The right complexity is what the task requires — no more. Three similar
lines beat a premature abstraction. No configurability / plugins / indirection for
hypothetical future needs.

**YAGNI.** No speculative abstractions; no parameters / flags "in case someone
needs them"; no error handling for scenarios that can't happen. Trust internal
code and framework guarantees; validate only at system boundaries (user input,
external APIs).

---

## 8. Documentation Standards

- **Google-style docstrings** for all public classes / functions / methods, including `__init__`.
- **Args** must match function signature exactly. Read the implementation to confirm names.
- **Returns** required for non-`None` returning functions. Omit for `None`-returning, `@property`, `@abstractmethod`.
- **Type hints** on all parameters and return types.
- **Comments explain WHY, not WHAT.** The code already says what.
- **Docstring body must not restate Args/Returns.** Args/Returns carry the WHAT;
  the body adds WHY (invariants, edge-case rationale, design context, links). A
  body repeating "Returns X, or None on missing file" when Returns already says
  it is duplication — trim it or merge into Returns.
- **Comments describe current state, not change history.** Forbidden anti-patterns:
  `# Clean break - no backward compatibility`, `# Updated from legacy format`, `#
  Fix for issue #<n>`, `"""Refactored from old implementation..."""`.
- **Prose docs (markdown) describe current state too — no issue/PR numbers.** A
  doc / guide / README describes what *is*, so no `#<n>` tracking markers
  (`tracked in #163`) — they rot. Only exception: a changelog. Defer status to
  GitHub (§14). Agents writing/reviewing a non-changelog doc apply this.
- **Private helpers** (`_foo`) can have a one-liner docstring.
- **Examples use generic placeholders or in-repo concrete names only** (§2) — no
  private employer / client / project names in examples or comments.

### Docstring coverage — three layered enforcers

Forge ships THREE docstring enforcement mechanisms; they overlap on purpose, each
catching what the others miss.

| Layer | What it enforces | Scope | Blocking? |
|---|---|---|---|
| **ruff D100–D107** (`select = ["ALL"]`) | Docstring present on every module / class / public function / method / `__init__` / magic method | Modified files | YES |
| `verify-forge-docstrings` (`docstring_verification`) | If a docstring exists: **Args** match the signature, **Returns** present for non-`None` returns, no `self` / `cls` / `Returns: None` anti-patterns | Modified files | YES |
| `verify-forge-docstring-coverage` (`docstring_coverage`) | Aggregate % across the tree; per-file table; `MISSING:` list for the fixer; optional badge | Full `src/` tree | **NO — reporter** |

**Why interrogate is non-blocking:** ruff D100–D107 are the actual gate — a
missing docstring on a public symbol is refused there. Interrogate measures
**aggregate coverage across the full tree** (ruff only sees the diff) and surfaces
a `MISSING:` list `forge:precommit-fixer` acts on. Trivial nested functions /
closures are exempt (`ignore-nested-functions`), so blocking here is redundant.

**Configuration (config-home rule):** a forge tool wrapping a third-party library
reads that library's native config section; only forge-specific keys are
namespaced under `[tool.forge.<tool>]`. So `[tool.interrogate]` is the single
source of truth (default threshold `fail-under = 90`, tighten per §4); only
`badge` and `paths` live under `[tool.forge.docstring_coverage]`. Project layout
is a `[tool.forge]` ground truth — `source_dirs` / `test_dirs`, read by every
layout-aware tool. `forge-config --list` enumerates the config surface; see
[`docs/configuration.md`](docs/configuration.md).

### Testing documentation standards

Test code is documented for **signal, not uniformly** — the canonical "what";
`forge:test-advisor` and `forge:test-writer` own the "how".

- **Injected fixtures are NOT documented as `Args`** (pytest injects them); real
  (non-fixture) params still are. `verify-forge-docstrings` is fixture-aware
  (filters `tmp_path` / `monkeypatch` / `caplog` + conftest/local fixtures) and
  is the source of truth; ruff `D417` is therefore ignored in `tests/**`.
- **Trivial nested helpers / closures need no docstring** (`ignore-nested-functions`
  exempts them); a self-describing name suffices.
- **Fixtures are named for WHAT they contain** (`dataset_with_missing_values`, not `data`).
- **Mock-heavy tests carry a structured docstring** — `SCENARIO:` / `MOCK SETUP:`
  / `EXPECTED BEHAVIOR:`; heavily-mocking files carry a module-level `# MOCKING
  STRATEGY:` overview. Unenforced; `forge:test-writer` produces it, `forge:test-advisor` reviews.
- **Prefer Null / Fake objects over `unittest.mock.Mock`** — less brittle; reserve
  `Mock` for when a Null Object costs more than it saves.
- **Coverage intent:** each public function gets a happy-path plus an edge/error case.

---

## 9. Logging Pattern

Python stdlib logging with propagation.

- **In modules:** `logger = get_logger(__name__)` from `common.logging`. Never
  attach handlers in modules.
- **In entry-point scripts:** configure the root logger once, early, before heavy
  imports — `setup_logging(log_file=output_dir / "logs" / "pipeline.log")`. All
  module loggers propagate to root, so every package's logs land in one file.
- **Logs next to data:** when a sub-process writes to a directory, add a local
  file handler (`add_file_handler(job_logger, work_dir / "job.log")`) so the log
  lives alongside results — messages go to BOTH the local file AND the root logger.

### Forbidden

- `logging.basicConfig(...)` — use `setup_logging()` instead.
- `logging.getLogger(...)` directly — use `get_logger(...)` from `common.logging`.
- `logger: Logger | None = None` function params — propagation handles it.
- `logger = logger or get_logger(...)` fallbacks — same reason.
- Attaching handlers to module loggers — only entry points configure handlers.

### Tests

Use pytest's `caplog` fixture for log assertions. Don't create file loggers in
tests; module loggers work via propagation and `caplog` captures them.

> Note: `common.logging` is a consumer-repo-specific convention; a repo either
> adopts it or documents its own logging entry point in its `CLAUDE.md`.

---

## 10. Continuation Protocol

To survive context compaction, agents maintain a continuation file after every
meaningful work step.

### File: `.plan/CONTINUATION.md` (gitignored)

Append-only by foundation agents (`forge:git-commit-push`, `forge:pr-manager`).
Structured rewrites (Status, Next steps, In progress) are the main agent's
responsibility, not these workhorse agents'.

### After every work session or significant step

Update `.plan/CONTINUATION.md` with (1) current state, (2) next steps for the next
session, (3) recent activity (auto-appended one-line commit / PR-wrap-up records).
Template:

```markdown
# Continuation — [YYYY-MM-DD HH:MM]

## Status
<one-paragraph current state>

## Done
- <bullet list of completed work>

## In progress
- <list with branch / PR / commit references>

## Next potential work
1. <ranked list>

## Open follow-ups
- <items deferred, why>

## Key references
- <links to plans, foundation, related issues>

## Recent activity (auto-appended)
- YYYY-MM-DD <hash> <subject>
- YYYY-MM-DD PR #N wrap-up: <title>
```

### Rules

- **Always read `.plan/CONTINUATION.md` first** at session start — it holds the most recent state.
- It is **gitignored** — never commit it.
- **Never delete it** — rewrite structured sections in place. Deleting it (e.g. on
  `/next`) destroys the handoff exactly when the user clears context for the next task.
- Foundation agents append one line on success — even when invoked directly,
  outside the `/commit` / `/pr` skills, so session-to-session state survives a
  skill bypass; they never delete or overwrite existing content.
- The main agent owns structured-section rewrites (Status, Next steps).

---

## 11. Agent Boundary Protocol

If an agent returns **"OUTSIDE MY SCOPE"** / **"NOT MY RESPONSIBILITY"**: read
which agent it recommends, call that one instead, and return only after
prerequisites are met. **Never bypass an agent by doing its task directly** — the
agents enforce quality gates.

### Canonical agent shape

Every forge-shipped agent follows the structure in `agents/_TEMPLATE.md`. Key
invariants:

- **Ownership split.** FOUNDATION owns policy, numbers, principles. Agents own
  enforcement protocol, review cookbook, investigation recipes. Neither
  duplicates the other; both link.
- **Length budget.** 400–800 words body (target); 1500 hard cap.
- **Description = routing trigger**, not a role label ("Use proactively when X", not "Agent for X").
- **Reporters do not have `Write` or `Edit`.** Exception: reporter-with-artifact
  agents (currently `docs-types-checker`, `weekly-summary`) may hold the single
  mutating tool their artifact needs — see
  [`agents/_TEMPLATE.md`](../agents/_TEMPLATE.md#tool-sets-per-role).

`forge-audit-agents` (in `forge-audit-all`) measures every agent against the
template, writing findings to `code_health/audit_agents.log`.

### Plugin staleness — symptoms and recovery

When a forge release renames or adds an agent, an already-running session keeps
the **cached** plugin from startup. Symptom: `Agent type 'forge:<name>' not found`
though the agent is on disk — the cache
(`~/.claude/plugins/cache/forge/forge/<version>/`) is behind. Recovery: `/plugin
update forge@forge`, then `/reload-plugins` (picks up new agents / hooks / skills
/ MCP / LSP servers); for **monitor** changes, restart the session
(`/reload-plugins` does not refresh monitors). The `check_upstream` warning (from
`install-forge-claude-md` and the `post-merge` / `post-checkout` / `SessionStart`
hooks) surfaces this automatically whenever the cached version is behind the
latest forge tag.

### Consumer Claude Code hook path convention

Consumer-specific Claude Code hooks live under `.claude/hooks/` and must be
registered in `.claude/settings.json` with `${CLAUDE_PROJECT_DIR}`-rooted paths
(e.g. `${CLAUDE_PROJECT_DIR}/.claude/hooks/<name>.sh`), never relative — relative
paths break when the hook fires from a non-root cwd (subagents, subdirs).
`install-forge-claude-md` scaffolds the directory + README; forge's own hooks ship
via the plugin at `${CLAUDE_PLUGIN_ROOT}/claude-hooks/...`, not registered here.

---

## 12. Single Source of Truth

A cross-cutting principle. Reviewed by `forge:design-checker`.

- Shared agent behaviours and shared principles live in **one canonical place** —
  this file (`FOUNDATION.md`), the consumer's `CLAUDE.md`, or a designated shared
  library module — and every other reference is a pointer back, **never a copy**.
- Flag any agent prompt or doc that re-states a rule already documented elsewhere
  instead of linking to it.
- **Process feedback ships into the rule surface, not agent memory.** When the
  user corrects a workflow or states a working rule, write it where every agent
  and contributor inherits it: the repo's `CLAUDE.md` for repo-specific rules,
  the relevant skill/agent doc for workflow steps, and — when the gap is in
  foundation-shipped content — an **upstream issue/PR against forge** (consumers
  must not patch shipped files locally; upgrades overwrite them). Personal agent
  memory holds only what cannot ship: individual preferences and private
  context.

Applies to: design principles (here), repo-specific safety rules (consumer
`CLAUDE.md`), shared agent behaviours, and tool conventions (ruff config,
docstring style, logging).

---

## 13. `code_health/` Convention

Convention for capturing pre-commit check results.

- Consumer `.githooks/pre-commit` hooks **write each check's stdout / stderr** to `code_health/<check>.log` (`ruff.log`, `docstring_verification.log`, …).
- Foundation agents (`forge:precommit-fixer`, `forge:pr-manager`, `forge:design-checker`, `forge:git-commit-push`) **read these as the source of truth** instead of re-running the checks.
- `forge:precommit-fixer` is the only agent that may run `forge-precommit` to (re)generate the logs — the only sanctioned wrapper; no agent invokes `ruff` / `git` / `gh` directly. If a log is missing or stale, call it to refresh. **Never rewrite the logs from agents.**
- `code_health/` is typically gitignored.

### Repo metadata for agents

Two repo-metadata artifacts let agents orient quickly instead of blind
filesystem / import scans (both optional — treat every reference as conditional):

- **`REPO_STRUCTURE.md`** (repo root) — when present, the canonical drift-verified
  directory map (kept accurate by `repo_structure_check`). Read it first to orient.
- **`code_health/audit_deps_tree.log`** — when present, a readable module
  dependency tree written on every `forge-audit-deps` run. Consult it for module
  structure / coupling.

---

## 14. Issue Tracking & Triage

GitHub is the **canonical** backlog — no markdown files. The `forge:issue-triage`
agent reads live `gh` data, applies labels, and curates a single auto-generated
"📋 Backlog Index" issue per repo. The agent owns the per-mode cookbook
(`bootstrap` / `triage` / `recommend-next` / `post-pr` / `stale-scan` /
`deep-review` / `plan-readiness`), the Backlog Index template, and regeneration;
this section owns the policy. The weekly `deep-review` mode re-reads the backlog (whole or
topic-scoped) and may — only with explicit user approval — create umbrella issues
grouping related work and emit sequenced local goal files to execute them
(mechanics in the agent doc).

### Issue structure — lead with `Requires:`

**Every issue opens with a `Requires:` line** (before the body) naming any
blocking dependency or `Requires: nothing`. This surfaces ordering up front so a
blocked task isn't mistaken for a quick-win. `forge:issue-triage` adds one when
missing (asking the author if unclear) and labels the issue `blocked` while its
stated prerequisite is open.

### Canonical label schema

Foundation declares these labels; `install-forge-labels` creates any missing ones
and owns their **colors** (`src/forge/install_labels.py` `CANONICAL_LABELS`). The
taxonomy by family:

- **Tier** — `tier-1-critical` (blocks other work / breaks CI / security urgent),
  `tier-2-high` (important + high ROI), `tier-3-standard` (normal features /
  refactors), `tier-4-low` (nice-to-have / research), `needs-triage` (awaiting
  tier assignment).
- **State** — workflow gates, blocking and enabling — `blocked` (waiting on
  dependency), `needs-discussion` (team input), `waiting-upstream` (blocked on
  external release), `stale` (no activity > 180 days), `plan-ready` (validated
  plan attached as a `plan-validated` comment; cleared for autonomous
  execution — see "Plan-readiness pipeline" below).
- **Type** — `bug`, `feature`, `refactor` (no behavior change), `docs`,
  `tech-debt` (cleanup / consolidation), `security`, `research` (spike).
- **Surface** — `quick-win` (easy + isolated + low-risk), `architecture`
  (cross-cutting design), `performance`, `ci-testing` (CI / test infra),
  `breaking-change` (API break).

Consumer repos may add domain-specific labels (e.g. `frontend`, `data-pipeline`)
without conflict.

### Backlog Index issue

One issue per repo, titled `📋 Backlog Index`. Pinned. Body **owned exclusively by
the agent** — humans do not edit it. Each `triage` run rebuilds it from scratch
(no merge logic); template + regeneration steps live in the agent doc.

### Override policy

Users override by changing labels manually. The agent **respects the last applied
label** — it never silently re-tiers. If signals suggest a different tier, it
comments ("…consider re-tiering") but does NOT auto-change.

### Issue templates

Foundation ships no GitHub issue templates. Consumer repos may add their own under
`.github/ISSUE_TEMPLATE/`; ones that auto-apply `needs-triage` + a type label pair
well with the triage workflow.

### Plan-readiness pipeline

Screening → human-validated planning → autonomous execution, each with one
owner: the `plan-readiness` triage mode **screens** every open issue against
the whole backlog (actual / non-colliding / aligned / unblocked) and surfaces
needs-plan candidates; the `/plan-issue` skill is the **human gate** — it
investigates read-only, confirms scope / approach / edge cases / versioning
with the user, and on explicit validation has `issue-triage` record the plan;
the `/sentinel` skill **executes** only recorded plans, to a PR wrap-up and
never past it (merging stays the user's decision; all §2 guards hold).
Screening is mechanical and repeatable; planning judgment is validated once,
up front — that is what makes the execution safe to run unattended.

### Decision trail

Every label change leaves a comment prefixed `[issue-triage]` for filtering.
Auditable, reversible, no silent state.

Two comment conventions, deliberately distinct: the `[issue-triage]` prefix
marks short **audit lines** (who changed what, why); a comment opening with
`[issue-triage] plan-validated:` is an **execution payload** — the full
user-validated plan `/sentinel` executes — posted only by `issue-triage` on
delegation from `/plan-issue`, alongside the `plan-ready` label. The issue
body (the original ask) is never edited.

---

## 15. Runtime Context Awareness

Forge tools default to workstation behavior: interactive prompts, staleness
warnings, hard-fail exit codes assuming the user can fix what's missing. Those are
wrong in CI — a missing `gh` auth is *expected*, and a credential prompt against
`/dev/null` hangs indefinitely.

### The contract

Every forge tool, hook, CLI, and pre-commit step with divergent interactive vs.
non-interactive behavior **MUST** consult
[`forge.run_context`](src/forge/run_context.py) instead of inlining its own
`$CI`-style check. The module owns detection for the whole repo:

- `is_non_interactive()` — true when running without a human at the terminal
  (any of `_CI_MARKERS`, or `sys.stdin.isatty()` false). Conservative: when in
  doubt returns true (over-suppressing dev-loop aids beats hard-failing in CI).
- `git_auth_mode()` — best-effort detection of the usable auth context (`ssh`,
  `https-token` via `GITHUB_TOKEN` / `GH_TOKEN`, `https-anonymous`, `none`), so
  callers pick a URL form the runner can authenticate against instead of blocking
  on a credential prompt.
- `progress_logger(step_name)` — context manager emitting start / done banners
  with elapsed time; wraps long-running substeps so CI logs show boundaries and
  timing, making future hangs visible.

### What "divergent behavior" looks like

Any of: prompts or recommends manual action in warning text; hard-fails on a
prerequisite expected-missing in CI (gh auth, Claude Code plugin, ssh agent);
runs inside a hook (`post-checkout`, `post-merge`) that may fire before
forge-scripts is installed; emits one line of output before minutes of work; or
hard-codes a URL form / auth method the runner may lack credentials for.

### Enforcement

Greppable: every forge source file with CI-relevant behavior imports from
`forge.run_context`; review rejects a tool that inlines a custom
`os.environ.get("CI")` check. A new CI marker goes in `_CI_MARKERS` — one place,
reaches every tool.

### Consumer recipe

The README ["Running forge in CI"](README.md#running-forge-in-ci) +
[`docs/ci-recipe.md`](docs/ci-recipe.md) ship one recipe: channel pin (`@main` /
`@dev`) + a per-PR CI workflow + a scheduled `forge-upgrade --apply` workflow that
PRs any upgrade diff. Adopt it rather than a custom integration.

---

## 16. Extending shipped agents, skills, and CLIs

Consumers (and forge itself) frequently layer repo-specific extras on a
foundation-shipped agent, skill, or pre-commit step. One rule covers every case,
plus three patterns by extension type. (This is the sanctioned exception to
§7's fix-the-interface rule: shipped plugin surface is outside the
consumer's control, so wrapping is the correct move here.)

### The rule

**Never shadow a shipped name with a project-local file of the same name.**
Project-local `.claude/agents/<X>.md` / `.claude/skills/<X>/SKILL.md` take
precedence over the plugin-shipped versions, making the canonical `forge:<X>`
invocation unreachable. Always use a distinct name. (Same rule as §3, extended to
skills and pre-commit logic.)

A shipped skill or agent name must also not collide with a **Claude Code
built-in** command (`review`, `code-review`, `security-review`, `init`, `run`,
`simplify`, …). The built-in wins the bare invocation, and no repo-level
documentation can rebind it — resolve the collision by naming, never by
instruction.

### Pattern A — agent wrapper

Consumer creates `.claude/agents/<base>-<scope>.md` that delegates via the `Task`
tool: `Task(subagent_type="forge:<base>", prompt="<repo-specific extras> +
<forwarded task>")`. Example: a `design-checker-<scope>` wrapper adds
repo-specific checks on top of the foundation `design-checker`'s SOLID/DRY/KISS pass.

### Pattern B — skill wrapper

The **`Skill` tool** lets one skill invoke another. Consumer creates
`.claude/skills/<base>-<scope>/SKILL.md` whose prose (1) invokes the foundation
skill via `Skill(skill="forge:<base>")`, then (2) does repo-specific follow-up.
The frontmatter `name` MUST be the wrapper name, not the base. Right when the
extension is multi-step prose with no natural home in a CLI (e.g. "after
`/forge:pr`, also update the CHANGELOG").

### Pattern C — CLI-gated extension

When the extension is a single discrete check the foundation CLI already runs,
put the logic IN the CLI gated on `[tool.forge]` config — no wrapper needed; the
shipped skill surfaces it because it already invokes the CLI. Example:
`forge-next-prep` emits a `Pending promotion: …` advisory when
`[tool.forge].dev_branch != base_branch`, invisible to single-branch consumers.
Right when the extension fits an existing CLI and the gating signal is already in
`[tool.forge]`.

### When to pick which

| Extension shape | Pattern |
|---|---|
| New agent rules / extra context for an existing review agent | A (agent wrapper) |
| Multi-step procedural extension on top of a shipped skill | B (skill wrapper) |
| One-shot check or transform that fits a shipped CLI's scope | C (CLI gate) |

When in doubt, prefer C over B over A: the smaller the divergent surface, the less
maintenance burden on every foundation upgrade.

---

## 17. Smart-test depth model

`forge-smart-test` (skill `/forge:smart-test`) selects the tests a change set
affects — `forge.import_graph` reverse reachability from changed source modules,
unioned with directly-changed test files — and runs them in escalating **depth
tiers**: fast local feedback, then a CI ladder before a full pass.

| Depth | Runs | Coverage | Typical use |
|---|---|---|---|
| `0` | Tests importing a changed module **directly** | no | Pre-commit / tight loop |
| `1` | Depth 0 + one import hop removed | no | First CI check on a PR push |
| `2` | Depth 0/1 + two import hops removed | no | Pre-merge gate |
| `full` | The **entire** suite | yes | Default-branch CI; release prep |

Guarantees consumers can rely on:

- **Conservative selection.** The walk errs toward including an extra test over
  skipping one a change could affect; a new or directly-changed test always runs
  at depth 0.
- **No false negatives only at `full`.** The smart tiers (`0`/`1`/`2`) are
  deliberately approximate; `full` runs everything.
- **Speed/coverage trade-off.** Coverage instrumentation (~3–5× slower) is
  reserved for `full` — the dominant speed difference between tiers.
- **Fail-fast.** A failing depth short-circuits higher depths and exits non-zero;
  the import cache is cleared between depths so a stale `__pycache__` can't mask a failure.
- **Determinism.** Same `git diff` + same tree → same selection; pytest's file
  order is sorted.
- **Import-root naming.** A changed source module is named by its real `sys.path`
  import root (top of its `__init__.py` chain), not by stripping `source_dirs` —
  so a package-rooted entry (`libs/…` → `libs.thing.core`) or a nested `*/src`
  root resolves to the name importers actually use. If it resolves to a name **no
  importer references**, smart-test warns rather than silently selecting zero tests.

It writes `code_health/smart_test.log` (§13). The optional `smart_test` pre-commit
step is **off by default** (self-skips unless `[tool.forge.smart_test].precommit_depth`
is set) and **non-blocking** unless `blocking = true`. Pytest stays out of the
default sequence (too slow); smart-test is the opt-in change-scoped bridge.

### Opt-in correctness extensions

The static graph **under-selects** when a test couples to code without an
`import`. Two opt-in extensions (default **off**) make the selector a **safe
superset** for mock-driven or dynamically-wired suites:

- **Mock-patch edges** (`follow_mock_patches = true`) — treats a test's `patch`
  string targets as graph edges; matters only for the patch-*only* case (e.g. a
  `sys.modules` fake against a deferred import). Orthogonal to module naming: it
  adds edges but does not fix a source-dir/import-root mismatch.
- **Coverage validation** (`coverage_validate = true` + `coverage_json`) — unions
  tests whose per-test coverage **contexts** touch a changed line, catching
  runtime-only links (fixtures, dynamic dispatch, `importlib`). Needs a fresh
  `coverage json --show-contexts` export (`pytest --cov-context=test`); regenerate
  on `full` runs.

A **CI directive** (`--from-commit-message`) drives the tier from a `[depth-N]` /
`[full]` commit tag (regex via `commit_directive_re`); `--depth full` is the "run
everything" escape for risky changes. With both extensions on, smart-test is
portable without losing mock- or coverage-driven test↔code edges.

---

**End of FOUNDATION.md.**

Consumer-specific rules layer on top in each consumer's `CLAUDE.md`.
