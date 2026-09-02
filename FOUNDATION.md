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

### Absence of evidence is not evidence of absence

A negative result only disproves a claim when the query could have proved
it. Before reporting "X doesn't exist / isn't configured", verify the probe
covers X: an API that 404s for *other* reasons, a grep missing a naming
variant, or a truncated listing all return "nothing" without meaning
"absent". Absence claims need a coverage-checked query or two independent
probes agreeing.

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
not read enough:** proposing a helper without checking it exists; minting a new
module when an existing one is the natural home (§7); adopting an issue's
*suggested* name/path as a directive instead of a hypothesis; proposing a
wrapper without checking whether the wrapped interface could simply change
(§7); a path that doesn't exist on disk; schema changes without reading the
current schema; a fix based on what a function "should" do rather than what
it does; restating a claim about a file after running anything that could
have rewritten it.

### Your own actions invalidate your reads

A read is a fact only until the tree changes. Any tree-changing action — a
pull, merge, checkout, sync, install, or generated-artifact refresh —
demotes every prior read of an affected file back to a hypothesis. The
specific trap: the agent is usually the one that performed the change, so
nothing external signals that its knowledge went stale, and "I already
checked this" feels like grounds for confidence exactly when it has become
a reason to re-check. Before repeating a claim about a file, ask whether
anything since the read could have rewritten it — and if you ran a sync
yourself, assume yes.

**A read can also be superseded with no action at all.** An earlier
belief formed in the same session — a brief you wrote, an issue's labels
at the time you saw them, your own first summary — is a snapshot, not a
fact. The existing rule triggers on *an action you took*; this sibling
triggers on *elapsed session time and your own prior output*, which
nothing signals. When a claim rests on something established earlier in
the session rather than on something just read, re-verify against the
live source before repeating it — the fresh read wins over the remembered
one, every time.

**A contradiction from the user is a prompt to re-read, not to
re-explain.** When a user disputes a factual claim about the codebase,
re-check the artifact first; restating the reasoning is correct only once
the re-read still supports the claim. Explaining harder is the failure
mode this rule exists to stop. "Do not be a yes-man" (above) is about not
capitulating on *judgment* — it never licenses defending a *fact* that has
not been re-verified.

### Plan before executing

For any task touching more than one or two files, or that mutates remote
state, write a plan FIRST (files, order, side effects) and wait for explicit
go-ahead. Skip only for genuine one-shots — a typo, a single-line config, or
follow-on edits in a review loop the user drives.

### Ask before acting on ambiguity

Pause and ask when (a) the instruction has two reasonable readings, the user
hasn't picked, **and neither the repo nor the session so far determines
which** (how to check: next paragraph); (b) the plan produces an
unauthorized side effect (extra commits, version bumps, branch switches),
or (c) you're about to act on a remembered convention without checking
current code still matches. Asking beats reverting.

**Investigation outranks clarification — case (a) only.** Run the
search or read the file that would settle the ambiguity *before* asking;
ambiguity that survives a coverage-checked search (per "Absence of
evidence is not evidence of absence" above) is the user's to resolve —
ambiguity that doesn't is yours. When one reading is strongly favoured —
by the session's own history, by what the repo contains, by what every
artifact so far points at — take it, state the assumption in one line,
and continue: a stated assumption is corrected faster than a menu is
answered, and each question costs a round trip on work the user asked to
be finished. Two bounds: no search settles *authorization* — cases (b)
and (c) still require the pause, however confident the favoured
reading — and where "Plan before executing" also applies, the stated
assumption folds into the plan awaiting go-ahead; it never bypasses that
gate.

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
- **NEVER amend a pushed commit** — the single-commit form of a rebase: it
  rewrites published history and forces the same (blocked) force-push,
  leaving the branch diverged from origin. Make a **new commit** instead
  (squash-merge erases fixup noise anyway). Amending an *unpushed* commit
  stays allowed. The `block_amend_pushed_commit` hook enforces this with
  **no bypass** — it blocks `git commit --amend` whenever `HEAD` already
  exists on a remote-tracking ref. A human amends via `! git commit --amend
  ...`.
- **NEVER destructive git recovery: no `git reset` (ANY form), no forced
  `git clean`, no `git checkout .` / `git restore .`, no `git stash drop` /
  `clear`, no untracked-including stash (`git stash -u` / `-a` — it runs
  `git clean` internally).** Rewinds un-commit published history on a
  synced branch; the rest destroy uncommitted or untracked work — `clean`
  with no recovery at all. Unstage with `git restore --staged <path>`.
  The sanctioned dirty-tree base sync is the **sync ladder**: (1) probe
  with `git merge-tree --write-tree origin/<base> HEAD` — it performs the
  real merge touching neither tree nor index; judge by **exit status**
  (0 clean / 1 conflicts), never by output emptiness; (2) probe clean and
  index clean → plain `git merge origin/<base>` (git's own dirty-overlap
  guard is the backstop); (3) otherwise secure the work as a
  **checkpoint commit** first — `git add -A`, then
  `FORGE_WIP_SYNC=1 git commit -m "wip-sync: <what>"` (the gate defers to
  the next real commit; the PR squash erases the checkpoint) — and merge
  on the clean tree. `git merge --abort` is the permitted recovery verb
  **only after** a checkpoint secured the work (git documents it as lossy
  for uncommitted changes). Stash is no longer part of any sanctioned
  procedure. Humans: never `export FORGE_WIP_SYNC` in a persistent shell
  — it would silently defer the gate on every later commit; use it
  inline, once. The `block_git_destructive` hook enforces all of this with
  **no bypass**; a human runs the blocked form via `! git ...`.
- **On deviation: STOP and report.** When you detect you have deviated from
  instructions or repository state is not what you expected, halt and
  surface it — never undo, rewind, or clean. An unwanted commit is
  trivially fixable; a destroyed tree is not. A blocked command is a signal
  to ask, never a prompt to reach the same effect another way.
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
  To check a branch's real protection, query BOTH
  `gh api repos/{owner}/{repo}/rules/branches/{branch}` and
  `.../rulesets` — never conclude "unprotected" from a 404 on the legacy
  `/branches/{branch}/protection` endpoint alone (it 404s for
  ruleset-protected branches too; §1 "Absence of evidence").
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

Specific tasks **must** be delegated to specialized subagents; handling
them directly is forbidden.

### Agent naming convention

Foundation agents resolve as `forge:<name>`; a bare name fails with
`Agent type '<name>' not found`. The thirteen foundation agents:
`forge:design-checker`, `forge:docs-types-checker`, `forge:git-commit-push`,
`forge:issue-triage`, `forge:knowledge-search`, `forge:perf-optimizer`,
`forge:pr-manager`, `forge:precommit-fixer`, `forge:prior-art`,
`forge:security-checker`, `forge:test-advisor`, `forge:test-writer`,
`forge:weekly-summary`.

**Consumer wrappers MUST use distinct names** — a local
`.claude/agents/<name>.md` matching a shipped name shadows it and makes
`forge:<name>` unreachable. Suffix with the repo/scope
(`design-checker-<repo>`) and delegate per §16 Pattern A.

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

**Forbidden — do NOT handle directly:** running `git commit` / `git push`
(→ `forge:git-commit-push`); invoking `ruff` or `fix-forge-ruff` from
an agent (→ `forge:precommit-fixer`, which reads `code_health/` — only the
pre-commit hook runs ruff); hand-curating a file list for `forge:precommit-fixer` (it
scopes itself off the report); writing PR descriptions or squash messages
(→ `forge:pr-manager`); reviewing security / design yourself (→ the checker
agents); installing dependencies (never — tell the user).

### Standard workflow orders

**Commit:** `forge:prior-art` (when creating files/symbols — first, a REUSE verdict ends the plan) → `forge:design-checker` (pre-write) → code changes → `forge:precommit-fixer` → `forge:git-commit-push`

**PR finalization:** `forge:design-checker` + `forge:security-checker` + `forge:docs-types-checker` (parallel) → `forge:precommit-fixer` (mode `strict`) → `forge:pr-manager` → background PR monitor (§6 — "The flow does not end at posting")

**Test writing:** `forge:test-advisor` (advise) → `forge:test-writer` → `forge:test-advisor` (review) → `forge:precommit-fixer`

---

## 4. Pre-commit Hook Enforcement

`.githooks/pre-commit` is the single quality gate; run it manually between
commits to catch issues early.

**Agent requirement**: if pre-commit blocks, you **must** fix ALL violations
(including pre-existing) before committing — "blocked by pre-existing violations"
without fixing = non-compliance. Scope expands to fix violations in any file you touch.

**Forbidden responses** (all the same evasion — fix instead): "blocked by
pre-existing violations (not our code)"; "should I fix them?" (yes, don't
ask); "out of scope / a lot of work"; or reporting the block and stopping.

**Never use `--no-verify`** to bypass pre-commit. Docstring WARNINGS are
non-blocking; ERRORS must be fixed.

### Pre-commit scope policy

- **Existing-and-passing tools** (ruff, ruff format, ruff S, custom contracts) → full codebase scope.
- **New tools** (gain mypy, gitleaks, …) → modified files initially; tighten to full codebase once the baseline clears.
- **Coverage-threshold tools** (interrogate) → full codebase, threshold = current passing baseline, raised over time.

---

## 5. Ruff Configuration

Single config: **`ruff.toml`** at repo root. Strict — `select = ["ALL"]`.
Under `ALL` every newly stabilized rule auto-activates, so forge pins ruff to
one minor (`>=X.Y,<X.Y+1`) and raises the cap deliberately — never an
unbounded pin. Baseline ignores: `COM812` / `ISC001` (conflict with
`ruff format`), `CPY001` without a per-file copyright policy; repos not
wanting Markdown code-block formatting add `exclude = ["*.md"]` under
`[format]`.

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

### Common per-file ignores and conventions

Standard per-file ignores: `scripts/**` — `S603` / `S607` / `INP001`
(external-CLI scripts outside packages); `tests/**` — `S101` / `PLR2004`.
Conventions: `lines-after-imports = 2` (`[lint.isort]`, cross-repo formatter
parity) and **`tests/` (plural)** as the canonical test directory (`test/`
accepted for back-compat by forge tooling).

---

## 6. Git & PR Workflow

### First step (new task)

Fresh task from a clean state (mid-branch work? — next section instead):
start from updated main (`git checkout main && git pull origin main`), then
`git checkout -b <type>/<description>` — prefixes `feat/`, `fix/`,
`refactor/`, `test/`, `docs/`, `chore/`. Verify with `git branch
--show-current` before editing. **Never start work from stale main.**

### A new request mid-branch — confirm, don't reflex-split

On a feature branch with uncommitted or unmerged work, **never automatically
open a new branch/PR** for a new ask. Small quick change → same branch, just
do it. Genuinely large or independently-releasable → (1) remind what's in
flight, (2) confirm they want it now, (3) ask whether it needs its own
branch. **Favor quick development over branch/PR ceremony**; unsure → stay
and ask.

### Dependency bumps ship alone

A dependency pin change (floor, cap, new pin — security advisory or not)
never rides a feature/fix PR: dedicated `chore(deps)` PR, or a reported
advisory until the user approves. A pin bump has its own blast radius (env
rebuilds, transitive changes, independent rollback) and hides in an
unrelated diff. Agents — including `forge:precommit-fixer` — report
advisories with the suggested pin; they never edit pins.

### Commit messages

- Max 50 words. Focus on what + why. **No Claude/AI attribution.**
- Conventional format — types in `forge.pr_squash_comment.CONVENTIONAL_COMMIT_TYPES` (canonical; shell hook synced via `forge-gen-commit-types`).

### PR descriptions

- Max 300 words. Sections: Summary / Changes / Testing / Breaking
  Changes (omit if none). Update if scope shifts.
- **The `## Summary` lead is plain English** for the reader who uses the
  product but not the codebase: no class/function names or internals; lead
  with the consequence, not the mechanism; say plainly when results stop
  being comparable. Technical detail belongs under `Changes`.

### PR finalization — verify first, never block on CI

- **Verification precedes publication.** The finalization reviews (design /
  security / docs reporters + strict pre-commit pass) run against the local
  tree about to be pushed, so findings are fixed in the PR's own commits —
  not follow-ups — and the changelog version heading settles before the
  branch is published. Order: verify locally → fix → commit → **author the
  wrap-up + squash message** → push → open PR → post them. The
  `block_unverified_pr_create` hook blocks `gh pr create` until the authored
  wrap-up names the current `HEAD` — and, when the wrap-up declares
  `wrapup-mode: light`, additionally re-runs the `forge-pr-plan`
  classifier fail-closed, blocking unless it agrees the diff is
  light-code (`FORGE_SKIP_WRAPUP_GATE=1` on explicit
  user request only; promotion PRs self-exempt when the `release/vX.Y.Z`
  tree reproduces its tag modulo the curated changelog). A **draft PR** is
  the escape hatch when the PR should be visible earlier. A genuine
  emergency uses **`forge-emergency`** — one human-armed, ledger-backed
  `wrapup-mode: emergency` publication that defers only the verification
  ceremony (never pre-commit, never a §2 hook) and owes retroactive
  verification after delivery; agents arm it only on explicit user
  instruction.
- **Verification starts itself.** The moment a branch's implementation
  commits are done, run the finalization reviews automatically — they are
  read-only and need no permission. Stopping at "ready to finalize?" with
  verification unrun is stopping too early; only genuinely outward-facing or
  destructive steps pause for the user.
- **The wrap-up never waits on CI.** Post as soon as the checks are done and
  state plainly when CI has not completed — an unqualified wrap-up reads as
  "all green", a false claim.
- Why: a wrap-up posted at one SHA and read at another describes a tree that
  no longer exists — the `verified-at:` header (reporter contract,
  `agents/_TEMPLATE.md`) makes that drift detectable.
- **The flow does not end at posting — monitor the PR.** This bullet is the
  canonical description of the post-wrap-up monitor; skills point here and
  state only their call-site delta. By default, after the wrap-up is posted,
  delegate one background monitor per open PR watching four signals:
  - **New review comments** → surface them.
  - **Merged / closed** → on merge: the local cleanup (sync the base
    branch, prune the merged local branch — the `/next` cleanup phase)
    **and** `issue-triage`'s `post-pr` mode (closed-issue tier-label
    removal + Backlog Index regeneration).
  - **`mergeable == CONFLICTING`** → alert only; **the monitor never
    syncs the branch it watches** (a read-only watcher must not mutate
    what it observes).
  - **A CI run concluding in failure** → surface it and investigate
    (§1); never auto-push fixes.

  Resolving a conflict — by whoever picks the work up, never the
  monitor — is a plain base merge: `git merge origin/<base>` (§2 —
  never rebase; dirty tree → §2's sync ladder, never `reset --hard`).
  It needs no permission: a base merge adds a merge commit and destroys
  nothing, and keeping a branch current with its base is routine work
  CI correctness depends on. The care belongs in resolving the conflict
  — read what each side intended, keep both, and ask only when one
  side's purpose cannot be determined — a question about the code, not
  about permission.

  The main session stays free for the next task. Skip only on explicit
  user request or when `forge.run_context.is_non_interactive()` — except
  `/sentinel`, whose monitors always run (they are its only alert path).

### Squash-merge messages (mandatory at PR finalization)

`forge:pr-manager` enforces: max 50 words; 3–5 bullets; conventional-commit
title; title + bullets only; no Claude/AI attribution. Posted via
**`forge-pr-squash-comment`** (validates every rule, fences the body for
verbatim copy into GitHub's squash dialog; `--dry-run` previews, `--patch`
rewrites) — never hand-constructed. The squash message is the permanent
`main` commit; if it can't be summarized in 50 words, the PR is too big.

### PR review comments

Reply to every comment with `✅ **Resolved in commit <hash>**` plus what was
done and where (file:line), via `gh api
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
for it, alter the interface: a wrapper keeps the awkward shape and every
later change pays for both. The break shows its whole cost in the diff
(converted call sites, one version bump); the layer hides its cost in the
interface that stays wrong. Prefer the break when call sites are countable;
layer only when the interface is genuinely outside your control — a
third-party or published contract, or §16's shipped-plugin case. (Not a
reversal of OCP: OCP adds genuinely new capability without touching stable
code; this rule covers compensating for an interface you control that is
wrong.)

**KISS.** The right complexity is what the task requires — no more. Three
similar lines beat a premature abstraction. No configurability / plugins /
indirection for hypothetical needs.

**YAGNI.** No speculative abstractions, "just in case" parameters, or error
handling for scenarios that can't happen. Trust internal code and framework
guarantees; validate only at system boundaries (user input, external APIs).

---

## 8. Documentation Standards

- **Google-style docstrings** for all public classes / functions / methods, including `__init__`.
- **Args** must match function signature exactly. Read the implementation to confirm names.
- **Returns** required for non-`None` returning functions. Omit for `None`-returning, `@property`, `@abstractmethod`.
- **Type hints** on all parameters and return types.
- **Comments explain WHY, not WHAT.** The code already says what.
- **Docstring body must not restate Args/Returns.** Args/Returns carry the
  WHAT; the body adds WHY (invariants, edge-case rationale, design context) —
  a body repeating what Returns already says is duplication.
- **Comments describe current state, not change history.** Forbidden anti-patterns:
  `# Clean break - no backward compatibility`, `# Updated from legacy format`, `#
  Fix for issue #<n>`, `"""Refactored from old implementation..."""`.
- **Prose docs (markdown) describe current state too — no issue/PR numbers.**
  A doc describes what *is*; `#<n>` tracking markers rot. Only exception: a
  changelog. Defer status to GitHub (§14).
- **Private helpers** (`_foo`) can have a one-liner docstring.
- **Examples use generic placeholders or in-repo concrete names only** (§2) — no
  private employer / client / project names in examples or comments.

### Docstring coverage — three layered enforcers

Forge enforces docstrings with three deliberate layers — ruff D100–D107
(presence, blocking), `verify-forge-docstrings` (accuracy, blocking), and
`verify-forge-docstring-coverage` (aggregate %, non-blocking reporter with a
`MISSING:` list for `forge:precommit-fixer`). The layer table and the
why-interrogate-is-non-blocking rationale live in
[`forge-docs/configuration.md`](forge-docs/configuration.md) under
`[tool.forge.docstring_coverage]`.

**Config-home rule:** a forge tool wrapping a third-party library reads that
library's native config section; only forge-specific keys are namespaced
under `[tool.forge.<tool>]` (so `[tool.interrogate]` owns the coverage gate;
only `badge` / `paths` are forge keys). Project layout is a `[tool.forge]`
ground truth — `source_dirs` / `test_dirs`, read by every layout-aware tool.
`forge-config --list` enumerates the config surface.

### Testing documentation standards

Test code is documented for **signal, not uniformly** — the canonical "what";
`forge:test-advisor` and `forge:test-writer` own the "how".

- **Injected fixtures are NOT documented as `Args`**; real (non-fixture)
  params still are. `verify-forge-docstrings` is fixture-aware and is the
  source of truth; ruff `D417` is therefore ignored in `tests/**`.
- **Trivial nested helpers / closures need no docstring**; a self-describing
  name suffices.
- **Fixtures are named for WHAT they contain** (`dataset_with_missing_values`, not `data`).
- **Mock-heavy tests carry a structured docstring** — `SCENARIO:` /
  `MOCK SETUP:` / `EXPECTED BEHAVIOR:`; heavily-mocking files carry a
  module-level `# MOCKING STRATEGY:` overview. Unenforced;
  `forge:test-writer` produces it, `forge:test-advisor` reviews.
- **Prefer Null / Fake objects over `unittest.mock.Mock`**; reserve `Mock`
  for when a Null Object costs more than it saves.
- **Coverage intent:** each public function gets a happy-path plus an edge/error case.

### Test lifecycle — behavior vs development tests

Runtime and authoring are both costs: a suite grows monotonically unless
tests carry a lifecycle. Two classes, marked at authoring time:

- **Behavior tests** (the unmarked default) pin observable contracts —
  CLI output shapes, gate verdicts, invariants, every regression from a
  real bug. Permanent; retirement does not apply.
- **Development tests** drove an implementation to correctness —
  per-step probes, near-duplicate variants, implementation-mirroring
  assertions. A file that is wholly scaffolding declares it at module
  level: `pytestmark = pytest.mark.development` (file-level, matching
  the selection model's granularity).

**The necessity gate comes first**: `forge:test-advisor` (advise mode)
rejects planned tests that duplicate existing coverage or mirror the
implementation, and `forge:test-writer` states each test's class and a
one-line justification before writing — not writing an unnecessary test
beats retiring it later.

**Lifecycle rule (skip, never delete)**: a development file untouched
for 30 days (`[tool.forge.smart_test].lifecycle_skip_days`) leaves
ordinary full runs — always reported as `lifecycle-skipped: N`, never
silent — and re-enters automatically the moment its file changes. The
48-hour cadence run (below) executes truly everything, so nothing rots
unobserved. Deletion is deliberately not part of the policy.

**Selection guarantees** (spec: [`forge-docs/smart-test.md`](forge-docs/smart-test.md)):
depth-tier selection is safe by contract — any changed non-Python path
the selector cannot map escalates the run to `full`; the tracked
`.forge-full-run` stamp guarantees a truly-all run at least every 48
hours (`full_run_max_age_hours`), staged into the commit that earned
it — that committed-stamp escalation is `cadence_mode = "commit"`, for
repos whose testing happens on workstations; CI-fleet repos use
`advisory`/`external` with the classic PR-tiers + main-push-full CI
schema (spec's "Who carries the cadence"); after full runs a depth-2
differential check records (never gates) any failing file selection
would have missed.

**Metrics are record-only**: wall time, file counts, development
fraction, lifecycle skips, and differential mismatches append to
`code_health/smart_test_history.log` per full run — trends inform
reviews; nothing blocks on a health metric.

---

## 9. Logging Pattern

Python stdlib logging with propagation.

- **In modules:** `logger = get_logger(__name__)` from `common.logging`. Never
  attach handlers in modules.
- **In entry-point scripts:** configure the root logger once, early, before heavy
  imports — `setup_logging(log_file=output_dir / "logs" / "pipeline.log")`. All
  module loggers propagate to root, so every package's logs land in one file.
- **Logs next to data:** when a sub-process writes to a directory, add a
  local file handler so the log lives alongside results — messages go to
  BOTH the local file AND the root logger.

### Forbidden

- `logging.basicConfig(...)` — use `setup_logging()` instead.
- `logging.getLogger(...)` directly — use `get_logger(...)` from `common.logging`.
- `logger: Logger | None = None` function params — propagation handles it.
- `logger = logger or get_logger(...)` fallbacks — same reason.
- Attaching handlers to module loggers — only entry points configure handlers.

Tests: use pytest's `caplog` for log assertions; never create file loggers in
tests — propagation covers module loggers. (`common.logging` is a
consumer-repo convention; a repo adopts it or documents its own logging
entry point in its `CLAUDE.md`.)

---

## 10. Continuation Protocol

To survive context compaction, agents maintain `.plan/CONTINUATION.md`
(gitignored) after every meaningful work step — append-only for foundation
agents (`forge:git-commit-push`, `forge:pr-manager`); structured rewrites are
the main agent's responsibility.

### After every work session or significant step

Update `.plan/CONTINUATION.md` with (1) current state, (2) next steps for the next
session, (3) recent activity (auto-appended one-line commit / PR-wrap-up records).
Sections, in order: `Status` (one paragraph) · `Done` · `In progress` (with
branch / PR / commit refs) · `Next potential work` (ranked) · `Open follow-ups`
(deferred, why) · `Key references` · `Recent activity (auto-appended)` — one
`YYYY-MM-DD <hash> <subject>` or `YYYY-MM-DD PR #N wrap-up: <title>` line each.

### Rules

- **Always read `.plan/CONTINUATION.md` first** at session start.
- It is **gitignored** — never commit it; **never delete it** (deleting on
  `/next` destroys the handoff exactly when context is cleared) — rewrite
  structured sections in place.
- Foundation agents append one line on success — even invoked outside the
  `/commit` / `/pr` skills — and never delete or overwrite existing content;
  the main agent owns structured-section rewrites.
- **The ledger is bounded; the archive is not.** Every append rotates the
  activity tail: done entries older than one week (or beyond the count
  cap) move verbatim to `.plan/CONTINUATION-archive.md` — never deleted —
  and collapse into per-day digest lines; entries referencing PRs/issues
  still named in the structured sections are pinned (undone work stays).
  `/next`'s continuation-hygiene step adds the judgment layer: a critical
  curation pass that deletes stale structured-section content outright —
  the file owes the next session orientation, not a museum. Session
  starts read only `CONTINUATION.md`, never the archive.

---

## 11. Agent Boundary Protocol

If an agent returns **"OUTSIDE MY SCOPE"** / **"NOT MY RESPONSIBILITY"**: read
which agent it recommends, call that one instead, and return only after
prerequisites are met. **Never bypass an agent by doing its task directly** — the
agents enforce quality gates.

### Canonical agent shape

Every forge-shipped agent follows the structure in `agents/_TEMPLATE.md`. Key
invariants:

- **Ownership split.** FOUNDATION owns policy, numbers, principles; agents
  own enforcement protocol, cookbook, recipes. Neither duplicates; both link.
- **Length budget.** 400–800 words body (target); 1500 hard cap.
- **Description = routing trigger**, not a role label ("Use proactively when X").
- **Reporters do not have `Write` or `Edit`.** Exception: reporter-with-artifact
  agents may hold the single mutating tool their artifact needs — see
  [`agents/_TEMPLATE.md`](../agents/_TEMPLATE.md#tool-sets-per-role).

`forge-audit-agents` measures every agent against the template
(`code_health/audit_agents.log`).

### Plugin staleness — symptoms and recovery

When a forge release renames or adds an agent, an already-running session keeps
the **cached** plugin from startup. Symptom: `Agent type 'forge:<name>' not found`
though the agent is on disk — the cache
(`~/.claude/plugins/cache/forge/forge/<version>/`) is behind. Recovery: `/plugin
update forge@forge`, then `/reload-plugins` — agents, hooks, and MCP / LSP
servers reload reliably; **skills and monitors may need a full session
restart** — trust the command's own output over any fixed rule, and restart
when a surface stays stale. The
`check_upstream` warning (`install-forge-claude-md` + the `post-merge` /
`post-checkout` / `SessionStart` hooks) surfaces the version lag automatically.

### Consumer Claude Code hook path convention

Consumer hooks live under `.claude/hooks/`, registered in
`.claude/settings.json` with `${CLAUDE_PROJECT_DIR}`-rooted paths, never
relative — relative paths break when a hook fires from a non-root cwd.
`install-forge-claude-md` scaffolds the directory; forge's own hooks ship via
the plugin at `${CLAUDE_PLUGIN_ROOT}/claude-hooks/...`, not registered here.

---

## 12. Single Source of Truth

Reviewed by `forge:design-checker`.

- Shared behaviours and principles live in **one canonical place** — this
  file, the consumer's `CLAUDE.md`, or a designated shared module — and every
  other reference is a pointer back, **never a copy**.
- Flag any agent prompt or doc that re-states a rule already documented elsewhere
  instead of linking to it.
- **Process feedback ships into the rule surface, not agent memory.** When the
  user corrects a workflow, write it where every agent and contributor
  inherits it: the repo's `CLAUDE.md`, the relevant skill/agent doc, or —
  for foundation-shipped content — an **upstream issue/PR against forge**
  (consumers must not patch shipped files locally; upgrades overwrite them).
  Personal agent memory holds only what cannot ship: individual preferences
  and private context.

---

## 13. `code_health/` Convention

- Consumer `.githooks/pre-commit` hooks **write each check's stdout / stderr** to `code_health/<check>.log` (`ruff.log`, `docstring_verification.log`, …).
- Foundation agents (`forge:precommit-fixer`, `forge:pr-manager`, `forge:design-checker`, `forge:git-commit-push`) **read these as the source of truth** instead of re-running the checks.
- `forge:precommit-fixer` is the only agent that may run `forge-precommit` to (re)generate the logs — the only sanctioned wrapper; no agent invokes `ruff` / `git` / `gh` directly. If a log is missing or stale, call it to refresh. **Never rewrite the logs from agents.**
- `code_health/` is typically gitignored.

### Repo metadata for agents

Two optional artifacts let agents orient without blind scans (treat every
reference as conditional): **`REPO_STRUCTURE.md`** (canonical drift-verified
directory map, kept accurate by `repo_structure_check` — read first) and
**`code_health/audit_deps_tree.log`** (module dependency tree from
`forge-audit-deps` — consult for structure / coupling).

---

## 14. Issue Tracking & Triage

GitHub is the **canonical** backlog — no markdown files. `forge:issue-triage`
reads live `gh` data, applies labels, and curates one auto-generated
"📋 Backlog Index" issue per repo; it owns the per-mode cookbook
(`bootstrap` / `triage` / `recommend-next` / `post-pr` / `stale-scan` /
`deep-review` / `plan-readiness`) and the Index template — this section owns
the policy. `deep-review` may create umbrella issues only with explicit user
approval.

### Issue structure — lead with `Requires:`

**Every issue opens with a `Requires:` line** naming any blocking dependency
or `Requires: nothing`, so a blocked task isn't mistaken for a quick-win.
`forge:issue-triage` adds one when missing and labels the issue `blocked`
while its stated prerequisite is open.

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

### Backlog Index, overrides, templates

One pinned issue per repo, titled `📋 Backlog Index`, body **owned exclusively
by the agent** (humans do not edit it); each `triage` run rebuilds it from
scratch — template in the agent doc. Users override by changing labels
manually: the agent **respects the last applied label**, never silently
re-tiers (it may comment "…consider re-tiering"). Foundation ships no GitHub
issue templates; consumer repos may add their own (ones that auto-apply
`needs-triage` + a type label pair well with triage).

### Plan-readiness pipeline

Screening → human-validated planning → autonomous execution, one owner each:
the `plan-readiness` triage mode **screens** the backlog (actual /
non-colliding / aligned / unblocked) for needs-plan candidates; `/plan-issue`
is the **human gate** — read-only investigation, scope / approach / edge
cases / versioning confirmed with the user, then `issue-triage` records the
plan; `/sentinel` **executes** only recorded plans, to a PR wrap-up and
never past it (merging stays the user's; all §2 guards hold). Screening is
mechanical and repeatable; planning judgment is validated once, up front —
that is what makes unattended execution safe.

### Decision trail

Every label change leaves a comment prefixed `[issue-triage]` — auditable,
reversible, no silent state. Two comment kinds, deliberately distinct: the
bare prefix marks short **audit lines**; a comment opening
`[issue-triage] plan-validated:` is the **execution payload** `/sentinel`
runs — posted only by `issue-triage` on delegation from `/plan-issue`,
alongside the `plan-ready` label. The issue body is never edited.

---

## 15. Runtime Context Awareness

Forge tools default to workstation behavior. That is wrong in CI — a missing
`gh` auth is *expected* there, and a credential prompt against `/dev/null`
hangs indefinitely.

### The contract

Every forge tool, hook, CLI, and pre-commit step with divergent interactive vs.
non-interactive behavior **MUST** consult
[`forge.run_context`](src/forge/run_context.py) instead of inlining its own
`$CI`-style check. The module owns detection for the whole repo:

- `is_non_interactive()` — true when running without a human at the terminal
  (any of `_CI_MARKERS`, or `sys.stdin.isatty()` false). Conservative: when in
  doubt returns true (over-suppressing dev-loop aids beats hard-failing in CI).
- `git_auth_mode()` — best-effort detection of the usable auth context (`ssh`,
  `https-token`, `https-anonymous`, `none`), so callers pick a URL form the
  runner can authenticate against instead of blocking on a credential prompt.
- `progress_logger(step_name)` — start / done banners with elapsed time around
  long-running substeps, so CI logs show boundaries and hangs stay visible.

"Divergent behavior" means any of: prompting or recommending manual action;
hard-failing on a prerequisite expected-missing in CI; running inside a
`post-checkout` / `post-merge` hook that may fire before forge-scripts is
installed; emitting one line of output before minutes of work; or
hard-coding a URL form / auth method the runner may lack.
Enforcement is greppable: CI-relevant files import `forge.run_context`;
review rejects inline `os.environ.get("CI")` checks; new CI markers go in
`_CI_MARKERS` — one place, every tool.

Consumers adopt the one shipped CI recipe (README "Running forge in CI" +
[`forge-docs/ci-recipe.md`](forge-docs/ci-recipe.md)), never a custom one.

---

## 16. Extending shipped agents, skills, and CLIs

Consumers (and forge itself) layer repo-specific extras on shipped agents,
skills, and pre-commit steps: one rule, three patterns. (The sanctioned
exception to §7's fix-the-interface rule — shipped plugin surface is outside
the consumer's control, so wrapping is correct here.)

### The rule

**Never shadow a shipped name with a project-local file of the same name** —
local `.claude/agents/<X>.md` / `.claude/skills/<X>/SKILL.md` take precedence
and make the canonical `forge:<X>` invocation unreachable. Always use a
distinct name (§3's rule, extended to skills and pre-commit logic).

A shipped skill or agent name must also not collide with a **Claude Code
built-in** command (`review`, `code-review`, `security-review`, `init`, `run`,
`simplify`, …). The built-in wins the bare invocation, and no repo-level
documentation can rebind it — resolve the collision by naming, never by
instruction.

### The three patterns

- **A — agent wrapper**: `.claude/agents/<base>-<scope>.md` delegating via
  `Task(subagent_type="forge:<base>", prompt="<repo-specific extras> +
  <forwarded task>")`. For new agent rules / extra review context.
- **B — skill wrapper**: `.claude/skills/<base>-<scope>/SKILL.md` that (1)
  invokes the foundation skill via `Skill(skill="forge:<base>")`, then (2)
  does the repo-specific follow-up; frontmatter `name` is the wrapper name,
  never the base. For multi-step prose with no natural CLI home.
- **C — CLI-gated extension**: put the logic IN the foundation CLI, gated on
  `[tool.forge]` config — no wrapper; the shipped skill surfaces it because
  it already invokes the CLI (e.g. `forge-next-prep`'s `Pending promotion:`
  advisory, invisible to single-branch consumers). For one-shot checks that
  fit an existing CLI's scope with the gating signal already in config.

When in doubt, prefer C over B over A: the smaller the divergent surface, the less
maintenance burden on every foundation upgrade.

---

## 17. Smart-test depth model

`forge-smart-test` (skill `/forge:smart-test`) selects the tests a change
set affects and runs them in escalating depth tiers (`0`/`1`/`2`/`full`).
The depth model, the guarantees consumers can rely on, and the opt-in
correctness extensions are specified in **[`forge-docs/smart-test.md`](forge-docs/smart-test.md)** — the single source of truth; this section is a pointer only.

---

**End of FOUNDATION.md.**

Consumer-specific rules layer on top in each consumer's `CLAUDE.md`.
