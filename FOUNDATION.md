# FOUNDATION.md — Forge: Claude Engineering Principles

Single source of truth for engineering principles shared across all Forge consumer
repos using the foundation plugin. Consumer repo `CLAUDE.md` files link to
this document at the top, then add repo-specific rules below.

> **Conflict rule**: when consumer `CLAUDE.md` and this document disagree,
> **the consumer wins** — foundation is shared baseline; repos may layer
> stricter rules.

---

## Table of Contents

1. [Critical Thinking Directive](#1-critical-thinking-directive)
2. [Core Safety Rules](#2-core-safety-rules)
3. [Mandatory Delegation](#3-mandatory-delegation)
4. [Pre-commit Hook Enforcement](#4-pre-commit-hook-enforcement)
5. [Ruff Configuration](#5-ruff-configuration)
6. [Git & PR Workflow](#6-git--pr-workflow)
7. [Design Principles (SOLID, DRY, KISS, YAGNI)](#7-design-principles)
8. [Documentation Standards](#8-documentation-standards)
9. [Logging Pattern](#9-logging-pattern)
10. [Continuation Protocol](#10-continuation-protocol)
11. [Agent Boundary Protocol](#11-agent-boundary-protocol)
12. [Single Source of Truth (cross-cutting)](#12-single-source-of-truth)
13. [`code_health/` Convention (cross-cutting)](#13-code_health-convention)
14. [Issue Tracking & Triage](#14-issue-tracking--triage)
15. [Runtime Context Awareness (CI vs. workstation)](#15-runtime-context-awareness)
16. [Extending shipped agents, skills, and CLIs](#16-extending-shipped-agents-skills-and-clis)
17. [Smart-test depth model](#17-smart-test-depth-model)

---

## 1. Critical Thinking Directive

**Do not be a yes-man.** The job of the agent is to help reach the best
decision, not agree with the user.

- When the user is wrong, say so clearly with reasons.
- When a plan has flaws, point them out before executing.
- When asked for something suboptimal, propose the better alternative.
- Pleasing the user with bad ideas is worse than a brief disagreement that
  leads to a better outcome.

Be honest, critical, and direct.

### Every failure requires investigation

Never dismiss errors as "not related to our changes" or "pre-existing." Every
CI failure, test error, or unexpected behaviour during work must be investigated
— even if it looks unrelated. Investigation = read the error, trace the cause,
verify whether your changes contributed. Then present options to the user:

1. Fix it now (small + within scope)
2. Fix it in this PR (related or blocking)
3. File an issue with root-cause analysis and proposed fix (truly separate)

"Not our problem" is never an acceptable response.

### Pattern Investigation Protocol

Before changing established patterns or behaviours, investigate why they exist.
Use `git log --oneline <file>` and `git blame`. Document:
"Current pattern X exists because Y."

When changes expand beyond the original task scope, ask explicitly: "This
expands scope to Y. Should I investigate this pattern or stay focused on
original X?"

**Red flags requiring clarification:** test failures in unrelated areas,
changing many file types, modifying public APIs, "fixing" seemingly intentional
patterns. Stop and ask rather than assume.

### Read before proposing

Before proposing a non-trivial change, read the code AND documentation of the
subsystem you're proposing about. "Subsystem" means:

- The module(s) the change would touch
- The module(s) that interact with those (direct callers and callees)
- The README, docstrings, or `docs/` pages that describe the area
- Any obviously related GitHub issue (`gh issue list --search` before opening one)

If the surface area is more than a handful of files, delegate the read to the
`Explore` agent rather than skimming yourself — the goal is ground truth, not
vibe-truth. This rule extends the Pattern Investigation Protocol *upstream*:
investigate the territory before you propose the route, not just before you
cross a known fence.

**Red flags that mean you have not read enough:** proposing a new script /
helper without checking whether the functionality already exists; proposing a
path that does not exist on disk without checking how it was populated before;
proposing schema changes without inspecting the current schema; recommending a
fix based on what a function "should" do rather than what its implementation
actually does.

Cost of pausing to read: minutes. Cost of proposing the wrong change: a
planning round-trip, a wrong implementation, or silent drift into bad
architecture.

### Plan before executing

For any task touching more than one or two files, or that mutates
remote state (commits, pushes, PRs, issues, tags), write a plan
FIRST: files, order, side effects. Wait for explicit go-ahead. Skip
the plan only for genuine one-shots — a typo, a single-line config,
or follow-on edits inside a review loop the user is already driving.

### Ask before acting on ambiguity

Pause and ask when (a) the instruction has two reasonable readings
and the user has not picked, (b) the plan produces a side effect the
user did not authorize (extra commits, version bumps, branch
switches), or (c) you are about to act on a remembered convention
without checking the current code state still matches. Asking is
cheaper than reverting.

---

## 2. Core Safety Rules

- **NEVER install dependencies** (`pip install`, `conda install`). Tell the
  user the exact command instead. Why: installing deps can break carefully
  configured environments, cause version conflicts, introduce untested
  combinations.
- **NEVER use `--no-verify`** to bypass pre-commit hooks. Fix violations
  instead.
- **NEVER force push** without explicit user approval. Check for upstream
  divergence first: `git fetch origin && git log origin/<branch>`.
- **NEVER add Claude/AI attribution** in commits, PRs, or merge messages
  (no `Co-Authored-By`, no `Generated with Claude`, no AI references).
- **NEVER push directly to a protected branch** (`base_branch` /
  `dev_branch`, default `main` / `dev`). Always create a feature branch
  first. The `block_protected_branches` hook enforces this for agents —
  defaulting to the same `main` + `dev` set as the sibling
  `block_branch_deletion` hook.
- **NEVER merge PRs autonomously.** Merging is the user's decision — produce
  the squash-merge message and wrap-up comment, then stop. The
  `block_pr_merge` hook enforces this for agents (blocks `gh pr merge` and
  the equivalent `gh api .../pulls/N/merge` call). Users still merge
  themselves via `! gh pr merge ...`.
- **NEVER delete a protected remote branch** (`base_branch` / `dev_branch`,
  default `main` / `dev`). Deleting a shared branch is irreversible and —
  because an agent runs with the user's credentials — *bypasses* server-side
  rulesets that "restrict deletions". The `block_branch_deletion` hook
  enforces this for agents with **no bypass** (not even
  `forge:git-commit-push`): it blocks `git push --delete` / `git push :ref`
  and `gh api -X DELETE …/branches/…` targeting a protected branch. Local
  `git branch -d/-D` is untouched (it never affects the remote). If a human
  truly intends a remote delete, they run it themselves with `! …`.
- **No backwards-compatibility shims** unless the user explicitly requests.
  No `OldName = NewName`, no deprecation warnings, no re-exports of moved
  modules. Clean breaks are the default.
- **No secrets in code or commits.** Use `.env` (gitignored) or env vars at
  runtime. Provide `.env.example` with placeholders. CI secret scanning
  blocks commits with detected secrets.
- **No private organizational names in code, docs, or examples.**
  When working in forge — or in any consumer repo that adopts forge —
  examples must use generic placeholders (`<repo>`, `<scope>`,
  `<service>`, `foo`, `bar`) or in-repo concrete names only. Never
  embed private employer / project / client / process names. The
  rationale is identical for both: forge is shared open content, and
  consumer `CLAUDE.md` / agent prompts are shared with everyone who
  reads the repo. Inspiration from private context is fine — leaving
  fingerprints in the artifact is not. The only carve-out is forge's
  own canonical upstream constant (`_FORGE_GITHUB_REPO`
  in `src/forge/git_utils.py`, and the URLs derived from it in forge's
  own source/docs), which must name the public upstream. This exception
  covers that specific constant, not "any public GitHub URL".

  **Transition periods.** During a repo move or rename, the runtime
  constant `_FORGE_GITHUB_REPO` stays at the *functional* location
  (where the gh API actually returns 200s) while docs may forward-point
  at the *aspirational* location. This is a deliberate, bounded
  divergence — the constant is the single source of truth for any
  runtime call, and a top-level README callout surfaces the discrepancy
  to readers. The divergence is meant to be short-lived; once any
  rename or transfer completes, docs and the constant converge.
- **Foundation CLIs are required, not optional.** Any agent, hook,
  script, or CI that depends on a forge-shipped CLI (`verify-forge-*`,
  `install-forge-*`, `forge-doctor`, `forge-precommit`) must **fail
  loudly** when the CLI is missing — never silently fall back to a raw
  tool (`ruff`, `gh`, `git`) or a `python -m` invocation. The error must
  point at the install command, e.g.:

  > `forge-scripts not installed. Run \`pip install -e ".[dev]"\` (or your repo's equivalent) and retry.`

  The wrappers exist so consumers get a uniform interface and forge can
  add behavior (logging, defaults, version checks) over time. Silent
  fallbacks defeat that contract.

---

## 3. Mandatory Delegation

Specific tasks **must** be delegated to specialized subagents. Handling
directly is forbidden.

### Agent naming convention

Foundation agents ship via the `forge` Claude Code plugin and resolve as
`forge:<name>` after install (e.g. `forge:pr-manager`). Calling them by
bare name (`pr-manager`) fails with `Agent type '<name>' not found`. The
twelve foundation agents are: `forge:design-checker`,
`forge:docs-types-checker`, `forge:git-commit-push`, `forge:issue-triage`,
`forge:knowledge-search`, `forge:perf-optimizer`, `forge:pr-manager`,
`forge:precommit-fixer`, `forge:security-checker`, `forge:test-advisor`,
`forge:test-writer`, `forge:weekly-summary`.

**Consumer wrappers MUST use distinct names.** When a consumer repo
needs to layer repo-specific extras on top of a foundation agent (extra
rules, paths, custom checks), it ships a local wrapper agent under
`.claude/agents/<name>.md` that delegates to the foundation agent via
the `Task` tool. The wrapper name **must differ** from the canonical
foundation name — otherwise the local file shadows the foundation
agent and direct `forge:<name>` calls become unreachable.

Convention: suffix with the repo name or scope (`design-checker-<repo>`,
`pr-manager-<repo>`, `security-checker-<scope>`). The wrapper delegates
with a prompt like:

```
Agent(subagent_type="forge:<base-name>", prompt="<repo-specific extras>... <original task>")
```

In this document, `forge:`-prefixed names refer to foundation agents
directly; consumer wrappers always carry a `-<repo>` or `-<scope>`
suffix that distinguishes them from the canonical name.

| Task | Agent | Trigger |
|---|---|---|
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
- Invoke `ruff` directly (raw `ruff check`, `ruff format`, or any `verify-forge-ruff*` wrapper) from an agent → use `forge:precommit-fixer`, which reads `code_health/` reports and dispatches fixes. Only the pre-commit hook invokes ruff in this repo.
- Hand-curate a file list or rule selection for `forge:precommit-fixer` → don't; the agent scopes itself off the pre-commit report.
- Write PR descriptions or squash-merge messages → use `forge:pr-manager`
- Review code for security / design → use `forge:security-checker` / `forge:design-checker`
- Install dependencies → never do this; tell the user

### Standard workflow orders

**Commit:** `forge:design-checker` (pre-write) → code changes → `forge:precommit-fixer` → `forge:git-commit-push`

**PR finalization:** `forge:design-checker` + `forge:security-checker` + `forge:docs-types-checker` (parallel) → `forge:precommit-fixer` (mode `strict`) → `forge:pr-manager`

**Test writing:** `forge:test-advisor` (advise) → `forge:test-writer` → `forge:test-advisor` (review) → `forge:precommit-fixer`

---

## 4. Pre-commit Hook Enforcement

`.githooks/pre-commit` is the single quality gate. Run it manually between
commits to catch issues early.

**Agent requirement**: if pre-commit blocks, you **must** fix ALL violations
(including pre-existing) before committing. Reporting "blocked by pre-existing
violations" without fixing = non-compliance. Scope automatically expands to fix
violations in any file you touch.

**Forbidden responses:**
- "Blocked by pre-existing violations in X.py (not in our new code)" — wrong, fix them
- "Should I fix the pre-existing violations?" — yes, don't ask, fix
- "Fixing pre-existing violations would be a lot of work / out of scope" — doesn't matter, fix
- Reporting block and stopping — fix instead

**Never use `--no-verify`** to bypass pre-commit. Docstring WARNINGS are
non-blocking; ERRORS must be fixed.

### Pre-commit scope policy

- **Existing-and-passing tools** (ruff, ruff format, ruff S, custom contracts) → full codebase scope.
- **New tools added to a repo** (gain mypy, gain gitleaks, etc.) → modified files only initially. Tighten to full codebase later once baseline cleared.
- **Coverage-threshold tools** (interrogate) → full codebase, threshold = current passing baseline. Raise over time.

---

## 5. Ruff Configuration

Single config: **`ruff.toml`** at repo root. Strict — `select = ["ALL"]`.

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

**Consumer repos MAY enforce stricter limits in their `ruff.toml`.** Agents
read the consumer's `ruff.toml` as the actual enforcement source — not these
foundation defaults.

### Common per-file ignores

- `scripts/**/*.py`: typically ignore `S603` (subprocess possibly tainted),
  `S607` (partial executable path), `INP001` (implicit namespace package).
  Scripts intentionally invoke external CLIs and live outside packages.
- `tests/**/*.py`: ignore `S101` (assert in tests), `PLR2004` (magic values).

### Convention: `lines-after-imports = 2`

Foundation recommends PEP8-strict 2 blank lines between imports and module
code (`[lint.isort] lines-after-imports = 2`). Avoids cross-repo formatter
divergence on shared scripts.

### Convention: `tests/` (plural) for the test directory

Forge follows the Python community standard — `tests/` (plural). This is
the layout used by pytest's own documentation, the PyPA "Packaging
Python Projects" tutorial, and every major Python project (requests,
flask, django, fastapi, numpy, pandas, ruff, pip, setuptools, …).

New repos adopting forge should use `tests/`. Forge tooling
(`verify-forge-test-naming`, `DEFAULT_SOURCE_DIRS`) still accepts `test/`
(singular) for back-compat with older layouts, but the recommended
canonical name is `tests/`.

---

## 6. Git & PR Workflow

### First step (always)

Start from updated main:

```bash
git checkout main && git pull origin main
git checkout -b <type>/<description>
```

Branch prefixes: `feat/`, `fix/`, `refactor/`, `test/`, `docs/`, `chore/`.

After plan mode, verify branch with `git branch --show-current` before editing.
**Never start work from stale main.**

### Commit messages

- Max 50 words.
- Conventional format — types defined in `forge.pr_squash_comment.CONVENTIONAL_COMMIT_TYPES` (canonical source; shell hook stays in sync via `forge-gen-commit-types`).
- Focus on what + why.
- **No Claude/AI attribution.**

### PR descriptions

- Max 300 words.
- Sections: Summary / Changes / Testing / Breaking Changes (omit if none).
- Update if scope shifts.

### Squash-merge messages (mandatory at PR finalization)

`forge:pr-manager` agent enforces:
- Max 50 words.
- 3–5 bullet points (not 6, not 2).
- Conventional commit format for title.
- No prose paragraphs. Title + bullets only.
- No Claude/AI attribution.
- Posted via **`forge-pr-squash-comment`** — the CLI validates every
  rule above, wraps the body in a literal triple-backtick fence (so the
  user can copy it verbatim into GitHub's squash-merge dialog), and
  posts via `gh`. Agents must not hand-construct the comment body.
  Use `--dry-run` to preview and `--patch <comment-id>` to rewrite an
  existing comment.

The squash-merge message becomes the permanent commit on `main`. Long messages
clutter `git log`. If too big to summarize in 50 words, the PR is too big.

### PR review comments

Reply to every comment with the resolution. Format:

```
✅ **Resolved in commit <hash>**

<brief explanation of what was done and where (file:line)>
```

Use `gh api repos/<owner>/<repo>/pulls/<PR#>/comments/<comment_id>/replies` to post.

---

## 7. Design Principles

Reviewed by `forge:design-checker` agent (foundation) + per-repo wrappers.

### SOLID
- **SRP**: each module / class / agent has one clear purpose.
- **OCP**: open for extension, closed for modification. Prefer adding new modules over editing existing ones.
- **LSP**: only when inheritance is used (composition is preferred).
- **ISP**: focused, minimal interfaces. Callers shouldn't have to ignore half the methods.
- **DIP**: high-level modules depend on abstractions. Isolate I/O behind small seams so logic on top is testable without it.

### DRY (Don't Repeat Yourself)
- Shared logic in one place + referenced, not copied.
- Shared agent behaviours in shared docs (this file or consumer `CLAUDE.md`), referenced by individual agents.
- A fact appears in exactly one place; everywhere else points back.

### KISS (Keep It Simple)
- The right amount of complexity is what the task requires — no more.
- Three similar lines of code is better than a premature abstraction.
- Don't add configurability, plugins, or indirection for hypothetical future needs.

### YAGNI (You Aren't Gonna Need It)
- No speculative abstractions.
- No parameters / flags / options "in case someone needs them."
- No error handling for scenarios that can't happen.
- Trust internal code and framework guarantees. Validate only at system boundaries (user input, external APIs).

---

## 8. Documentation Standards

- **Google-style docstrings** for all public classes / functions / methods, including `__init__`.
- **Args** must match function signature exactly. Read the implementation to confirm names.
- **Returns** required for non-`None` returning functions. Omit for `None`-returning, `@property`, `@abstractmethod`.
- **Type hints** on all parameters and return types.
- **Comments explain WHY, not WHAT.** The code already says what.
- **Docstring body must not restate Args/Returns.** The Args and Returns
  sections carry the WHAT (parameter purpose, return value, failure
  modes). The body adds WHY: invariants, edge-case rationale, design
  context, links to related code. A body that says "Returns the X, or
  None on missing file / malformed JSON" when the Returns section
  already says it is duplication — trim the body or merge into Returns.
- **Comments describe current state, not change history.** Forbidden anti-patterns:
  - `# Clean break - no backward compatibility`
  - `# Updated from legacy format`
  - `# Fix for issue #<n>`
  - `"""Refactored from old implementation to use new format."""`
- **Private helpers** (`_foo`) can have a one-liner docstring.
- **Examples use generic placeholders or in-repo concrete names only.**
  See §2 — no private employer / client / project / process names in
  docstring examples, code samples, or comments.

### Docstring coverage — three layered enforcers

Forge ships THREE distinct docstring enforcement mechanisms. They
overlap on purpose; each layer catches what the others miss. Knowing
which layer does what avoids the trap of "we already enforce this,
why is the new step adding value?".

| Layer | What it enforces | Scope | Blocking? |
|---|---|---|---|
| **ruff D100–D107** (via `select = ["ALL"]`) | Presence of a docstring on every module / class / public function / method / `__init__` / magic method | Modified files (ruff runs on diff) | YES — refuses the commit |
| `verify-forge-docstrings` (step `docstring_verification`) | If a docstring exists, the **Args** match the signature exactly, **Returns** present for non-`None`-returning functions, no `self` / `cls` / `Returns: None` anti-patterns | Modified files | YES — refuses the commit |
| `verify-forge-docstring-coverage` (step `docstring_coverage`) | Aggregate % across the codebase; per-file table; missing-symbol list for the fixer agent; optional README badge | Full `src/` tree | **NO — non-blocking reporter** |

**Why interrogate is non-blocking:** ruff D100–D107 are the actual
gate. Any commit that lands a missing docstring on a top-level
public symbol is refused at the ruff layer. The interrogate step
exists to measure **aggregate coverage across the full tree** (ruff
only sees the diff) and to surface a parseable `MISSING:` list that
`forge:precommit-fixer` can act on. Trivial **nested functions /
closures are exempt by default** (`ignore-nested-functions`) — ruff
already skips them, and a docstring on every throwaway test stub
(`fake_run`, `_stub`) is boilerplate, not signal. Blocking at this
layer would be redundant with ruff.

**Configuration:** the consumer's `pyproject.toml`
`[tool.interrogate]` is the single source of truth — the CLI reads
every standard interrogate key (threshold, `exclude`, `ignore-*`
flags). The foundation default threshold is `fail-under = 90`;
tighten per §4 ("threshold = current passing baseline. Raise over
time.").

Forge reads interrogate's **native** section directly and does **not**
wrap it: re-exposing a third-party tool's whole config surface under a
forge namespace (plus a key-name mapping to maintain) is a needless
wrapper — the tool's own section is the right home, exactly as forge
reads `ruff.toml` rather than copying it. Only keys interrogate has no
concept of live under `[tool.forge.docstring_coverage]`: `badge = true`
(writes `.badges/docstring-coverage.svg`) and `paths` (a per-tool scan-root
override that otherwise defaults to the repo-wide layout
`[tool.forge].source_dirs + test_dirs`). **Project layout** is itself a
`[tool.forge]` single-ground-truth: `source_dirs` (default `["src"]`) and
`test_dirs` (default `["tests"]`) — split source-vs-test so a source-only
tool doesn't pull test dirs in — so every layout-aware tool reads the
repo's roots from one place. **Config-home rule:** a forge tool that
wraps a third-party library reads the library's native config section
directly; only forge-specific keys are namespaced under
`[tool.forge.<tool>]`. `forge-config --list` enumerates every
`[tool.forge.*]` key forge reads and names the native sections (like
`[tool.interrogate]`) it reads too — so the config surface is
discoverable without doc-hunting, and `install-forge-bootstrap` surfaces
it as a post-install nudge.

### Testing documentation standards

Test code is documented for **signal, not uniformly**. This is the
canonical "what"; `forge:test-advisor` (review) and `forge:test-writer`
(produce) own the "how".

- **Injected fixtures are NOT documented as `Args`.** pytest injects them;
  they are not call-site parameters. `verify-forge-docstrings` is
  fixture-aware (it filters `tmp_path` / `monkeypatch` / `caplog` plus
  conftest- and locally-defined fixtures) and is the source of truth for
  real test-param docs. ruff `D417` is therefore ignored in `tests/**` (it
  is not fixture-aware and would demand fixtures once an `Args:` exists).
- **Real (non-fixture) parameters are still documented** in `Args`.
- **Trivial nested helpers / closures need no docstring** —
  `ignore-nested-functions` exempts them; a self-describing name suffices.
- **Fixtures are named for WHAT they contain**, not where used
  (`dataset_with_missing_values`, not `data`).
- **Mock-heavy tests carry a structured docstring** — `SCENARIO:` /
  `MOCK SETUP:` / `EXPECTED BEHAVIOR:`; files that mock extensively carry a
  module-level `# MOCKING STRATEGY:` overview. No tool enforces this format;
  `forge:test-writer` produces it and `forge:test-advisor` reviews it.
- **Prefer Null / Fake objects over `unittest.mock.Mock`** — less brittle
  when interfaces change; reserve `Mock` for when a Null Object costs more
  than it saves.
- **Coverage intent:** each public function gets at least a happy-path plus
  an edge / error case.

---

## 9. Logging Pattern

Python stdlib logging with propagation.

### In modules

```python
from common.logging import get_logger
logger = get_logger(__name__)
```

Never attach handlers in modules.

### In entry-point scripts

Configure the root logger once, early, before heavy imports:

```python
from common.logging import setup_logging
setup_logging(log_file=output_dir / "logs" / "pipeline.log")
```

All module loggers propagate to root automatically. Every package's logs end
up in the same file.

### Logs next to data

When a sub-process writes data to a directory, add a local file handler so
the log lives alongside the results:

```python
from common.logging import add_file_handler, get_logger
job_logger = get_logger(f"pipeline.job.{job_id}")
add_file_handler(job_logger, work_dir / "job.log")
```

This logger's messages go to BOTH the local file AND the root logger.

### Forbidden

- `logging.basicConfig(...)` — use `setup_logging()` instead.
- `logging.getLogger(...)` directly — use `get_logger(...)` from `common.logging`.
- `logger: Logger | None = None` function params — propagation handles it.
- `logger = logger or get_logger(...)` fallbacks — same reason.
- Attaching handlers to module loggers — only entry points configure handlers.

### Tests

Use pytest's `caplog` fixture for log assertions. Don't create file loggers
in tests. Module loggers work via propagation; `caplog` captures them.

> Note: the `common.logging` module convention is consumer-repo-specific (for repos that adopt a  module convention);
> consumer repos either adopt this module or document their own logging entry
> point in their `CLAUDE.md`.

---

## 10. Continuation Protocol

To handle Claude Code context compaction and enable seamless session
continuity, agents maintain a continuation prompt file after every
meaningful work step.

### File: `.plan/CONTINUATION.md` (gitignored)

Append-only by foundation agents (`forge:git-commit-push`, `forge:pr-manager`).
Structured rewrites (Status, Next steps, In progress) are the main agent's
responsibility, not these workhorse agents'.

### After every work session or significant step

Update `.plan/CONTINUATION.md` with:

1. **Current state**: what's done, what's in progress
2. **Next steps**: exact continuation instructions for the next agent / session
3. **Recent activity** (auto-appended): one-line records of commits and PR
   wrap-ups by foundation agents

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

- **Always read `.plan/CONTINUATION.md` first** at session start — contains the most recent state.
- `.plan/CONTINUATION.md` is **gitignored** — never commit it.
- **Never delete `.plan/CONTINUATION.md`** — rewrite its structured sections in
  place. Deleting it (e.g. on `/next`) destroys the cross-context handoff
  exactly when the user clears context to start the next task.
- Foundation agents append one line on success; they never delete or overwrite existing content.
- The main agent owns structured-section rewrites (Status, Next steps).

---

## 11. Agent Boundary Protocol

If an agent returns **"OUTSIDE MY SCOPE"** or **"NOT MY RESPONSIBILITY"**:

1. Read which agent it recommends.
2. Call that agent instead.
3. Return to the original agent only after prerequisites are met.

**Never bypass an agent by doing its task directly.** The agents enforce
quality gates.

### Canonical agent shape

Every forge-shipped agent follows the structure documented in
`agents/_TEMPLATE.md`. Key invariants:

- **Ownership split.** FOUNDATION owns policy, numbers, principles.
  Agents own enforcement protocol, review cookbook, investigation
  recipes. Neither side duplicates the other; both link.
- **Length budget.** 400–800 words body (target); 1500 hard cap.
- **Description = routing trigger**, not a role label. "Use proactively
  when X" — not "Agent for X".
- **Reporters do not have `Write` or `Edit`** in their tool list. (Exception: Reporter-with-artifact agents documented in [`agents/_TEMPLATE.md` "Tool sets per role"](../agents/_TEMPLATE.md#tool-sets-per-role) — currently `docs-types-checker` and `weekly-summary` — may hold the single mutating tool required to produce their artifact.)

`forge-audit-agents` (in `forge-audit-all`) measures every agent against
the template and writes per-agent findings to
`code_health/audit_agents.log`. Non-blocking until Layer 3 trim PRs
converge.

### Plugin staleness — symptoms and recovery

When a forge release renames or adds an agent, an already-running Claude
Code session keeps using the **cached** plugin version it loaded at
startup. The symptom is an error like:

```
Agent type 'forge:<name>' not found.
Available agents: ..., forge:<old-name>, ...
```

even though the agent appears in the latest forge docs and in
`agents/<name>.md` on disk. The plugin cache (under
`~/.claude/plugins/cache/forge/forge/<version>/`) has not been refreshed
since the rename shipped.

**Recovery:**

1. In the Claude Code prompt: `/plugin update forge@forge`
2. Then `/reload-plugins` — picks up new agents, hooks, skills,
   MCP / LSP servers in the running session.
3. **For monitor changes**, restart the session (`Cmd+R`, or open a
   new conversation) — `/reload-plugins` does not refresh monitors.

The `check_upstream` warning (printed by `install-forge-claude-md` and
the `post-merge` / `post-checkout` / `SessionStart` hooks) surfaces this
state automatically whenever the cached plugin version is behind the
latest forge tag. Both forge's own repo and consumer repos see the
warning — it fires on any repo where the plugin is installed.

### Consumer Claude Code hook path convention

Consumer-specific Claude Code hooks live under `.claude/hooks/` and must
be registered in `.claude/settings.json` with paths rooted at
`${CLAUDE_PROJECT_DIR}` (e.g.
`${CLAUDE_PROJECT_DIR}/.claude/hooks/<name>.sh`), never relative paths.
Relative paths break whenever the hook fires from a context where the
shell's cwd is not the repo root (subagents, subdirectories) — you get
spurious `not found` errors. `install-forge-claude-md` scaffolds the
directory + a README documenting the convention; forge's own hooks
ship via the plugin at `${CLAUDE_PLUGIN_ROOT}/claude-hooks/...` and
are not registered here.

---

## 12. Single Source of Truth

A cross-cutting principle. Reviewed by `forge:design-checker`.

- Shared agent behaviours and shared principles live in **one canonical
  place** — this file (`FOUNDATION.md`), the consumer's `CLAUDE.md`, or a
  designated shared library module — and every other reference is a
  pointer back, **never a copy**.

- Flag any agent prompt or doc that re-states a rule already documented
  elsewhere instead of linking to it.

Applies to: design principles (here), repo-specific safety rules (consumer
`CLAUDE.md`), shared agent behaviours (claim labelling, source grounding),
tool conventions (ruff config, docstring style, logging pattern).

---

## 13. `code_health/` Convention

Foundation general convention for capturing pre-commit check results.

- Consumer `.githooks/pre-commit` hooks **write each check's stdout / stderr** to `code_health/<check>.log` (e.g., `ruff.log`, `docstring_verification.log`, `test_naming_check.log`, `repo_structure_check.log`).
- Foundation agents (`forge:precommit-fixer`, `forge:pr-manager`, `forge:design-checker`,
  `forge:git-commit-push`) **read these as the source of truth** for the latest
  pre-commit run instead of re-running the checks themselves.
- `forge:precommit-fixer` is the only agent that may run `forge-precommit`
  to (re)generate the logs. Every other agent reads them. No agent
  invokes `ruff`, `git`, `gh`, or other underlying CLIs directly —
  `forge-precommit` is the only sanctioned wrapper.
- If a log is missing or stale, call `forge:precommit-fixer` to refresh
  it. **Never rewrite the logs from agents** — the pre-commit hook owns
  that responsibility.
- `code_health/` is typically gitignored.

### Repo metadata for agents

Two repo-metadata artifacts let agents orient quickly instead of fanning
out blind filesystem or import scans:

- **`REPO_STRUCTURE.md`** (repo root) — when present, this is the
  canonical, drift-verified map of the repository's directory layout.
  The `repo_structure_check` pre-commit step keeps it accurate. Agents
  should read it first to orient on where things live before fanning out.
- **`code_health/audit_deps_tree.log`** — when present, this is a
  readable Python module dependency tree, written on every
  `forge-audit-deps` run (gitignored, regenerated). Agents assessing
  module structure or coupling should consult it.

Both are optional — consumer repos may not have them. Treat every
reference as conditional ("if present").

---

---

## 14. Issue Tracking & Triage

GitHub is the **canonical** backlog. No markdown files. The `forge:issue-triage`
agent reads live `gh` data, applies labels, and curates a single auto-generated
"📋 Backlog Index" issue per repo.

### Issue structure — lead with `Requires:`

**Every issue opens with a `Requires:` line** as its first content (before
the body), naming any blocking dependency — another issue or PR that must
land first — or `Requires: nothing` when standalone. This surfaces ordering
constraints up front so a blocked task is never mistaken for a quick-win and
started out of order (e.g. a cleanup that depends on an unmerged floor-raise).
`forge:issue-triage` adds a `Requires:` line when an issue lacks one (asking
the author if the dependency is unclear) and labels an issue `blocked` while
its stated prerequisite is still open.

### Canonical label schema

Foundation declares these labels. Use `install-forge-labels` (foundation
pip package) to create any missing ones in a consumer repo.

| Family | Label | Purpose | Color |
|---|---|---|---|
| **Tier** | `tier-1-critical` | Blocks other work / breaks CI / security urgent | `#B60205` red |
| | `tier-2-high` | Important + high ROI | `#D93F0B` orange |
| | `tier-3-standard` | Normal features / refactors | `#0075CA` blue |
| | `tier-4-low` | Nice-to-have / research | `#CCCCCC` grey |
| | `needs-triage` | Newly opened, awaiting tier assignment | `#FBCA04` yellow |
| **State** | `blocked` | Waiting on dependency | `#E99695` pink |
| | `needs-discussion` | Team input required | `#FBCA04` yellow |
| | `waiting-upstream` | Blocked on external release | `#D4C5F9` lavender |
| | `stale` | No activity > 180 days | `#999999` dark grey |
| **Type** | `bug` | Something is broken | `#D73A4A` red |
| | `feature` | New capability | `#A2EEEF` light blue |
| | `refactor` | Internal improvement, no behavior change | `#7B68EE` purple |
| | `docs` | Documentation only | `#0075CA` blue |
| | `tech-debt` | Cleanup / consolidation | `#BFD4F2` light blue |
| | `security` | Security-sensitive | `#B60205` red |
| | `research` | Investigation / spike | `#C5DEF5` pale blue |
| **Surface** | `quick-win` | Easy + isolated + low-risk | `#28A745` green |
| | `architecture` | Cross-cutting design | `#7B68EE` purple |
| | `performance` | Perf-sensitive | `#FF6B6B` salmon |
| | `ci-testing` | CI / test infra | `#FFA500` orange |
| | `breaking-change` | API break | `#B60205` red |

Consumer repos may add domain-specific labels (e.g. `frontend`, `data-pipeline`)
without conflict.

### Backlog Index issue contract

One issue per repo, titled `📋 Backlog Index`. Pinned. Body **owned exclusively
by the agent** — humans do not edit it.

**Deterministic regeneration**: each `triage` run rebuilds the body from
scratch via:

1. `gh issue list --state open --json number,title,labels,updatedAt,assignees`
2. Group by tier (`tier-1-critical` → `tier-2-high` → `tier-3-standard` → `tier-4-low`)
3. Within each tier, sort by `updatedAt` descending (most recent activity first)
4. Render fixed template (see below)
5. Force-overwrite issue body via `gh issue edit --body-file -`

No merge logic. Agent never reads the existing body to compute the new one.
Zero merge-conflict risk because there's nothing to merge.

**Template:**

```markdown
> **Auto-generated by `issue-triage` agent. Do not edit by hand.**
> Last triage: YYYY-MM-DD. To re-triage: invoke the agent in `triage` mode.

## 🔥 Tier 1 — Critical (N)
- #NNN — Title — `label1`, `label2` — _activity: YYYY-MM-DD_

## ⚡ Tier 2 — High Priority (N)
...

## 📋 Tier 3 — Standard (N)
...

## 🌱 Tier 4 — Low Priority (N)
...

## 🚫 Blocked / Waiting (N)
- #NNN — Title — _blocker: <issue or external>_

## 🆕 Needs Triage (N)
<issues opened with no tier label>
```

### Agent modes

The `forge:issue-triage` agent supports five modes. Caller specifies via prompt.

| Mode | Behaviour |
|---|---|
| `bootstrap` | First-run setup. Run `install-forge-labels`. Locate or create the `📋 Backlog Index` issue. If a legacy `docs/development/issue_backlog.md` exists, post each issue's rationale as a comment on the matching live issue (one-time migration), then delete the markdown file. |
| `triage` | Walk all open issues. For any with no `tier-N-*` label, propose one. **Comment the rationale**: `tier-N applied: <reason>. (issue-triage)`. Apply via `gh issue edit --add-label`. Then regenerate `📋 Backlog Index` body. |
| `recommend-next` | Live query: `gh issue list --label tier-1-critical,tier-2-high --state open`. Cross-reference open PRs / branch names for in-progress signals. Apply weighting: blocking other issues, has open PR, recent activity. Return top 3 with rationale. |
| `post-pr` | Detect closed-by-PR issues from PR body (`Closes #N` / `Fixes #N`). Remove tier labels. Regenerate Backlog Index. |
| `stale-scan` | Find issues with no activity > 180 days AND no `waiting-upstream` label. Apply `stale` label + comment "no activity in 180 days — close, defer, or document why still relevant?" Skip `waiting-upstream` (those are legitimately stalled). |

### Override policy

Users can override agent decisions by changing labels manually. Agent
**respects the last applied label** — does not silently re-tier. If signals
strongly suggest a different tier than the user-set one, the agent comments
("tier-3 applied by user; signals suggest tier-1 because <reason>; consider
re-tiering") but does NOT auto-change.

### Issue templates

Foundation does not ship GitHub issue templates. Consumer repos may add their
own under `.github/ISSUE_TEMPLATE/`; templates that auto-apply `needs-triage`
plus a type label (`bug`, `feature`, etc.) pair well with the triage workflow
above.

### Decision trail

Every label change leaves a comment prefixed `[issue-triage]` for filtering.
Auditable, reversible, no silent state.

---

## 15. Runtime Context Awareness

Forge tools, hooks, and CLIs are written from a workstation-developer
default: interactive prompts, staleness warnings, hard-fail exit codes
that assume the user can fix what's missing. Those defaults are wrong
in CI / automation: a missing `gh` auth in GitHub Actions is *expected*,
not an actionable user-facing problem, and a credential prompt
against `/dev/null` causes silent indefinite hangs.

### The contract

Every forge tool, hook, CLI, and pre-commit step that has divergent
interactive vs. non-interactive behavior **MUST** consult
[`forge.run_context`](src/forge/run_context.py) instead of inlining
its own `$CI`-style check. The module owns the detection logic for
the whole repo:

- `is_non_interactive()` — true when the process is running without a
  human at the terminal. Detection: any of the env vars in
  `_CI_MARKERS` (see [`run_context.py`](src/forge/run_context.py)
  for the curated list + selection criterion sourced from the
  `watson/ci-info` canonical vendor list) or `sys.stdin.isatty()` is
  false. Conservative: when in doubt, returns true (over-suppressing
  dev-loop aids is a smaller mistake than hard-failing in CI).
- `git_auth_mode()` — best-effort detection of the git/pip auth
  context the environment can actually use: `"ssh"`, `"https-token"`
  (`GITHUB_TOKEN` / `GH_TOKEN`), `"https-anonymous"`, or `"none"`. Lets
  callers pick a URL form that the runner can authenticate against
  instead of hard-coding HTTPS and blocking on a credential prompt.
- `progress_logger(step_name)` — context manager that emits flushed
  start / done banners with elapsed time. Wraps long-running
  substeps (pip install, bootstrap, audit runs) so CI logs show the
  substep boundary and timing rather than silence-then-result. Future
  hangs become visible.

### What "divergent behavior" looks like in practice

A tool exhibits divergent behavior when any of these is true:

- It prompts the user for input or recommends manual action in its
  warning text.
- It hard-fails on a missing prerequisite that is legitimately
  expected-missing in CI (gh auth, Claude Code plugin, ssh agent).
- It runs as part of a hook (`.githooks/post-checkout`,
  `post-merge`) that may fire on a CI checkout BEFORE forge-scripts
  itself is installed.
- It produces only one line of output before doing minutes of work
  (no per-substep visibility — silent hangs become CI's fault to
  diagnose).
- It hard-codes a URL form or auth method that the consumer's CI
  runner may not have credentials for.

### Enforcement

Greppable: every forge source file that has CI-relevant behavior
imports from `forge.run_context`. Code review rejects new tools that
inline a custom `os.environ.get("CI")` check instead of consulting
the helper. The contract is a foundation-level discipline, not an
informal style guide.

A new CI marker added to a runner that forge users adopt is added to
`_CI_MARKERS` in `run_context.py` — one place, reaches every tool.

### Consumer recipe

The README ["Running forge in CI"](README.md#running-forge-in-ci)
section + [`docs/ci-recipe.md`](docs/ci-recipe.md) ship one recipe
across two workflow files: channel pin (`@main` / `@dev`) + a
per-PR CI workflow + a scheduled `forge-upgrade --apply` workflow
that opens a PR whenever the upgrade produces a diff. Consumer
repos should adopt that recipe rather than rolling a custom
integration; it already honors the contract above.

---

## 16. Extending shipped agents, skills, and CLIs

Consumers (and forge itself) frequently need to layer repo-specific
extras on top of a foundation-shipped agent, skill, or pre-commit
step. There is one rule that covers every case and three concrete
patterns depending on what's being extended.

### The rule

**Never shadow a shipped name with a project-local file under the
same name.** Project-local `.claude/agents/<X>.md` /
`.claude/skills/<X>/SKILL.md` files take precedence over the
plugin-shipped versions. A wrapper that reuses the foundation
name makes the canonical `forge:<X>` invocation unreachable from
that repo. Always use a distinct name.

This is the same rule §3 documents for agents; this section
extends it to skills and pre-commit logic and surfaces the three
patterns one canonical place.

### Pattern A — agent wrapper (§3, repeated for completeness)

Consumer creates `.claude/agents/<base>-<scope>.md` (e.g.
`design-checker-<scope>`). The wrapper delegates to the
foundation agent via the `Task` tool:

```
Task subagent_type="forge:<base>"
prompt="""
<repo-specific extras>

<original task forwarded from the caller>
"""
```

Example: `<consumer-repo>/.claude/agents/design-checker-<scope>.md`
adds repo-specific logging-convention checks, `REPO_STRUCTURE.md`
sync rules, and domain-specific footguns on top of the
foundation `design-checker` agent's standard SOLID/DRY/KISS pass.

### Pattern B — skill wrapper

Skills don't have a runtime delegation mechanism analogous to
`Task`, but the **`Skill` tool** lets one skill invoke another.
Consumer creates `.claude/skills/<base>-<scope>/SKILL.md` (e.g.
`pr-with-changelog`). The wrapper's prose instructs the agent to:

1. Invoke the foundation skill via `Skill(skill="forge:<base>")`
   first.
2. After it returns, perform repo-specific follow-up steps in
   prose.

The frontmatter `name` MUST be the wrapper name (`pr-with-changelog`),
not the base (`pr`). The wrapper's description should explicitly
say it's a wrapper.

Pattern B is the right choice when the extension is multi-step
prose that has no natural home in a CLI (e.g. "after `/forge:pr`
finalizes, also update the CHANGELOG and bump the consumer's
docs-version").

### Pattern C — CLI-gated extension

When the extension is a single discrete check (or a small chunk
of mechanical work) that the foundation CLI is already running,
put the new logic IN the CLI gated on `[tool.forge]` config —
no wrapper agent or skill needed. The shipped foundation skill
will naturally surface the new behavior because it already
invokes the CLI.

Example: forge's `forge-next-prep` emits a `Pending promotion: …`
advisory after the prune step when `[tool.forge].dev_branch !=
base_branch` (dual-track repos). The shipped `/forge:next` skill
calls `forge-next-prep --tag` exactly as before; the new
advisory rides along automatically, and the skill's Phase 1.5 then
acts on it by auto-invoking the repo's promotion flow. Single-branch
consumers never see the line.

Pattern C is the right choice when:

- The extension fits cleanly into an existing CLI's
  responsibility.
- The gating signal is already in `[tool.forge]` (or another
  per-repo config the CLI already reads).
- A wrapper skill would be machinery without benefit.

### When to pick which

| Extension shape | Pattern |
|---|---|
| New agent rules / extra context for an existing review agent | A (agent wrapper) |
| Multi-step procedural extension on top of a shipped skill | B (skill wrapper) |
| One-shot check or transform that fits a shipped CLI's scope | C (CLI gate) |

When in doubt, prefer C over B over A: the smaller the surface
that diverges from the foundation, the less maintenance burden
the consumer carries on every foundation upgrade.

---

## 17. Smart-test depth model

`forge-smart-test` (skill: `/forge:smart-test`) selects the tests a change
set affects via the static import graph and runs them in escalating
**depth tiers**, so a slow suite gives fast feedback locally and a CI
ladder before a full pass. Selection is the `forge.import_graph` reverse
reachability from changed source modules, unioned with directly-changed
test files.

| Depth | Runs | Coverage | Typical use |
|---|---|---|---|
| `0` | Tests importing a changed module **directly** | no | Pre-commit / tight loop |
| `1` | Depth 0 + one import hop removed | no | First CI check on a PR push |
| `2` | Depth 0/1 + two import hops removed | no | Pre-merge gate |
| `full` / `infinity` | The **entire** suite | yes | Default-branch CI; release prep |

Guarantees consumers can rely on:

- **Conservative selection.** The graph walk errs toward including an
  extra test over skipping one a change could affect — a brand-new or
  directly-changed test always runs at depth 0.
- **No false negatives only at `full`.** The smart tiers (`0`/`1`/`2`)
  are deliberately approximate; `full` runs everything.
- **Speed/coverage trade-off.** Coverage instrumentation (~3-5× slower)
  is reserved for `full`; the smart tiers skip it. This is the dominant
  speed difference between tiers.
- **Fail-fast.** A failing depth short-circuits the higher depths and
  exits non-zero, and the import cache is cleared between depths so a
  stale `__pycache__` can't mask a failure.
- **Determinism.** Same `git diff` + same tree → same selection; the
  file order handed to pytest is sorted.

It writes `code_health/smart_test.log` (FOUNDATION §13) for
`forge:precommit-fixer`. The optional `smart_test` pre-commit step is
**off by default** and self-skips unless a repo sets
`[tool.forge.smart_test].precommit_depth`; it is **non-blocking** unless
`[tool.forge.smart_test].blocking = true`. Pytest stays out of the
default pre-commit sequence (too slow); smart-test is the opt-in bridge
for repos that want a change-scoped gate.

### Opt-in correctness extensions

The static import graph **under-selects** when a test couples to code
without an `import` statement. Two opt-in extensions close that gap so the
selector becomes a **safe superset** of the static-only one for
mock-driven or dynamically-wired suites (all default **off** — zero change
for existing consumers):

- **Mock-patch edges** (`follow_mock_patches = true`). `unittest.mock.patch
  ("pkg.mod.attr")` is a real test→`pkg.mod` dependency with no import. With
  this on, a test file's `patch` / `patch.dict` / `mock.`/`mocker.` string
  targets are added as graph edges (reduced to their importable module
  prefix); `patch.dict("sys.modules", …)` keys count too. `patch.object` is
  already covered by its import.
- **Coverage validation** (`coverage_validate = true` + `coverage_db`). After
  the static pass, union the tests whose recorded per-test coverage
  **contexts** touch a changed line — catching runtime-only links (fixtures,
  dynamic dispatch, `importlib`). Needs a fresh map (`pytest --cov-context=
  test`); a stale one under-selects, so regenerate it on `full` runs.

A **CI directive** (`--from-commit-message`) lets a job drive the tier from a
`[depth-N]` / `[full]` tag in the commit message (regex configurable via
`commit_directive_re`); `--depth full` is the documented "run everything"
escape for risky changes (release branches, dependency bumps, broad
refactors). With both extensions on, smart-test is portable across repos
**without losing mock- or coverage-driven test↔code edges** — the
precondition for a consumer to retire a bespoke selector.

---

**End of FOUNDATION.md.**

Consumer-specific rules layer on top in each consumer's `CLAUDE.md`.
