# Agent knowledge architecture: status and goals

Status: research deliverable, no policy in force. This document feeds the
maintainer's decision; nothing here is binding until adopted into
FOUNDATION, `CLAUDE.md`, an agent contract or a check. Findings carry
their harness scope and their confidence; claims that were read rather
than run say so.

How instructional knowledge is organised across forge's agent surface, and
what a better organisation would hold to. This is an audit, not a cleanup:
it changes no rule file. Counts are words, the unit the repo measures in.

**Surface audited:** `FOUNDATION.md` (8917 words), `CLAUDE.md` (2481),
`agents/*.md` (14 agents plus `_TEMPLATE.md`, 2565), `skills/*/SKILL.md`
(19), `forge-docs/*.md` (11 pages), the forge-local wrapper
`.claude/agents/issue-triage-forge.md`, `CONTRIBUTING.md`,
`src/forge/data/hooks.toml` and `claude-hooks/`. `agents/` and `skills/`
are symlinks into `src/forge/data/`; the generated copies are counted
once, at their source.

**Two harnesses, one source.** Forge reaches a harness through generated
files, and two converters read the same sources and the same
`src/forge/data/hooks.toml`:

| | `install-forge-claude` (Claude Code) | `install-forge-vibe` (Vibe) |
|---|---|---|
| Always-loaded text | `CLAUDE.md`, which includes `FOUNDATION.md` | `AGENTS.md`: both files flattened into one (11398 words in forge's repo, the sum of the two) |
| Agents | `.claude/agents/forge-<name>.md` | `.vibe/agents/forge-<name>.toml` plus `.vibe/prompts/forge-<name>.md` |
| Skills | `.claude/skills/forge-<name>/SKILL.md` | `.agents/skills/forge-<name>/SKILL.md` |
| Hooks | `.claude/hooks/`, registered in `.claude/settings.json` | `.vibe/hooks.toml`, dispatched through `forge-hook-dispatch` and a roster file |

Every finding carries a **Harness** mark: *Both*, *Claude Code only* or
*Vibe only*. Most findings are about the text itself and so bite both.
The ones that differ are collected in "Harness-specific findings" below.
`forge-docs/integration-staleness.md` records that Vibe is untested;
everything said about Vibe here was read from the converter, its
generated output in this checkout and `install-forge-vibe --check`, never
observed in a running Vibe session.

**How to read confidence.** *High*: both texts, or text and code, were read
and quoted. *Medium*: the text was read, the behavioural consequence is
inferred. *Low*: a pattern worth checking, not yet falsified.

---

## Part 1 — Status

### Summary

| Class | Findings | Worst case |
|---|---|---|
| 1. Redundancy | 12 | Three copies of the wrap-up summary rule, two of them wrong against the code |
| 2. Reaches everyone, needed by few | 8 | About 450 words of single-author PR-text rules loaded into every session of every agent |
| 3. Reaches too few | 8 | Run-time reporter contracts live in a file that neither converter installs |
| 4. Should be a CLI | 11 | The issue-eligibility rule, whose prose once mislabelled 32 issues, is still executed from prose by four roles |
| 5. Misplaced by layer | 6 | `_TEMPLATE.md` mixes author-time guidance with run-time imperatives |
| 6. Unowned, orphaned or false | 18 | None of the 58 relative links in shipped agents and skills resolves from either harness's generated location |

62 findings: 56 about the text, 6 added by the harness pass (F8,
O13–O18). Of the 62, 51 bite both harnesses, 4 are Claude Code only
(R6, M8, L3, O9) and 6 are Vibe only (F8, O13, O14, O16, O17, O18); O15
is the mirror of R6.

Four results came back clean and are worth keeping as baselines:

- **No verbatim copying of FOUNDATION into agents.** `forge-audit-agents`
  reports zero 8-word overlaps (`code_health/audit_agents.log` contains no
  `restatement` line). Every duplicate below is a paraphrase, which that
  check cannot see.
- **Section-name pointers hold.** Of 85 quoted pointers of the form
  `FOUNDATION §N "name"` across agents, skills and both doc folders, one
  names a section that does not exist (finding O5).
- **No orphaned reference page.** Each of the 11 `forge-docs/` pages is
  named by at least one always-loaded file, agent or skill.
- **Every hook FOUNDATION names exists and is wired** in
  `src/forge/data/hooks.toml` (13 names checked). Wired is not the same
  as enforced on every harness: see O13–O17.

### Class 1 — Redundancy

A true duplicate states the rule in two places that can drift. A pointer
that restates the rule in one clause is what
`forge-docs/instruction-compaction.md` requires and is not counted here.
Drift already visible is the evidence that a pair is a duplicate.

| # | Rule | Copies | Evidence of drift | Confidence | Harness |
|---|---|---|---|---|---|
| R1 | **RESOLVED** — `agents/pr-manager.md` now says labelled bullets per `SUMMARY_LABELS`, and `forge-docs/agent-contracts.md` states what `validate` actually refuses (an unfilled slot, a body far past its ceiling) and that an unlabelled bullet only warns. Original finding: shape of the wrap-up summary | `FOUNDATION.md` §6: "The summary is labelled bullets, not prose". `agents/pr-manager.md:92`: "`summary` → one plain line on what the PR does". `agents/_TEMPLATE.md:225`: validate "refuses a wrap-up that … carries more than one summary line" | Code agrees with FOUNDATION only: `src/forge/pr_wrapup.py` `_check_head` says "the count is not capped". Two of three copies are wrong | High | Both |
| R2 | Squash-merge message rules | `FOUNDATION.md` §6 (300 words, the owner). `skills/pr/SKILL.md:480-509` "Squash-merge message hard rules" (279 words). `agents/pr-manager.md:86` | Skill: "**Target 45 words**, advisory". FOUNDATION: "45-word ceiling … A ceiling is never a length to write toward" | High | Both |
| R3 | PR description sections | `FOUNDATION.md` §6: "Summary / Changes / Testing / Breaking Changes" and "no check status outside `Testing`". `skills/pr/SKILL.md:421-436` template | Template heading is `## Test plan`, and says check status "is `Test plan`" | High | Both |
| R4 | Review-comment reply format | `skills/pr-comments/SKILL.md:31-57` (three forms). `agents/pr-manager.md:45-55` (one form) | pr-manager has only the "Resolved" form; categories differ too ("already-resolved/needs-action/needs-discussion" against "actionable fix, question, nit, or out-of-scope" in the skill's prompt, line 18) | High | Both |
| R5 | Base-sync gate commands | `skills/pr/SKILL.md:44-57` and `agents/pr-manager.md:111-119` both spell out `git fetch` / `gh pr view --json mergeable` / `git rev-list --left-right --count` | Same check is also enforced in code by `forge-pr-create` and `forge-pr-wrapup post` (`skills/pr/SKILL.md:101-103`, `462-466`): three statements of one gate | High | Both |
| R6 | Terse voice | `FOUNDATION.md` §8 "Write terse". The `caveman_mode` hook, injected at `SessionStart` and `SubagentStart`. The 58-word "Output style (always)" block in all 14 agents, the wrapper and the template (`agents/_TEMPLATE.md:72-78` requires it "verbatim") | Agent block: "Drop articles when the sentence still reads". Hook: "Caveman full". `[tool.forge.hooks].caveman = false` switches off one of the three | High | Claude Code only as a redundancy; see O15 |
| R7 | Release mechanics | `docs/release-process.md:9-10`: "CLAUDE.md's release bullets **point here** — they do not restate the mechanics". `CLAUDE.md` bullets "Changelog fragments", "Version derivation", "Release process" (349 words) | "A release tag is cut on every merge" appears twice in `CLAUDE.md` itself; "Version derivation" still gives a hand flow, "`git tag vX.Y.Z && git push origin vX.Y.Z`" | High | Both (forge repo) |
| R8 | What `forge-internal` covers | `CLAUDE.md` "`forge-internal` issue label". `.claude/agents/issue-triage-forge.md:41-46` (the forwarded prompt). Same file `:90-98` | Three lists, three contents; the prompt copy still lists "promotion" | High | Both (forge repo) |
| R9 | Reuse idle children (Vibe) | `FOUNDATION.md` §3: "send a follow-up message to an existing idle child — its context persists — instead of spawning a new one". `agents/_TEMPLATE.md:68-70`: the same sentence | None yet; verbatim | High | Both pay, only Vibe acts |
| R10 | PR monitor behaviour | `FOUNDATION.md` §6 says skills "state only call-site delta". `skills/pr/SKILL.md:558-583` (265 words) restates it: "**The exit is the delivery**: it re-invokes this session, which reads the event as its next input" against FOUNDATION's "**The exit is the delivery** — it re-invokes the session, which reads the event as input". `skills/next/SKILL.md:77-103` restates the digest and untrusted-text halves | None yet | High | Both |
| R11 | Close-keyword parsing | `agents/issue-triage.md:97` reads "`Closes #N` / `Fixes #N` / `Resolves #N`". `skills/next/SKILL.md:146-148` reads "`Fixes #N` / `Closes #N`" and adds "keep the two in lockstep if the convention ever changes" | The skill already omits `Resolves` | High | Both |
| R12 | Background memory-audit dispatch | `skills/next/SKILL.md:209-214` and `skills/sentinel/SKILL.md:106-111`, near-identical paragraphs | None yet | High | Both |

**What it costs.** R1–R5 are paid on every `/forge-pr` run and every
`forge-pr-manager` spawn, the most frequent heavyweight path. R6 costs
about 810 words of agent docs (58 × 14) for text the hook already delivers.
R7 is paid by every session in forge's own repo.

Smaller instances, not counted: `agents/docs-types-checker.md` states
"Documentation describes CURRENT state" at lines 117 and 228 and repeats
§8's docstring rules in its own words; `agents/security-checker.md` says
"report only" five times (lines 15, 194, 195, 202, 225), which the template
forbids ("Same prohibition restated 2+ times").

### Class 2 — Reaches everyone, needed by few

`FOUNDATION.md` and `CLAUDE.md` load into every session of every agent.
The compaction doc's test is: one role, or an opted-out repo, means it does
not belong there.

| # | Content | Words | Who acts on it | Confidence | Harness |
|---|---|---|---|---|---|
| E1 | `FOUNDATION.md` §6 "Squash-merge messages" and "PR descriptions" | 454 | One role. §3 forbids anyone else: "PR descriptions or squash messages → `forge-pr-manager`" | High. One clause must stay for all: a close keyword closes the whole issue, which planning roles also rely on | Both |
| E2 | `FOUNDATION.md` §9 "Logging Pattern" | 157 | Only a repo that adopted `common.logging`. The compaction doc names this exact case ("a logging convention with the module"). Forge spends 74 more words in `CLAUDE.md` saying it does not apply | High | Both |
| E3 | `FOUNDATION.md` §15 "Runtime Context Awareness" | 342 | Whoever writes a forge tool, hook or step. The decision rule matters to skills that self-skip, but see M9: an agent cannot evaluate the predicate | Medium | Both |
| E4 | `FOUNDATION.md` §11 "Canonical agent shape" and "Claude Code hook path convention"; §16 | 472 | Whoever authors an agent, hook or wrapper | Medium | Both |
| E5 | `FOUNDATION.md` §14 label schema, Backlog Index, plan-readiness mechanics, decision trail | about 800 of 980 | `forge-issue-triage` and the three planning skills. The label list is a complete enumeration, which the compaction doc says moves | Medium. The untrusted-text rule and the `Requires:` rule are universal and stay | Both |
| E6 | `FOUNDATION.md` §18 "Cost Goals" | 286 | Three reviewers and one optimizer agent; the section itself calls it "a review lens" | Medium | Both |
| E7 | `CLAUDE.md` single-role or rationale-only bullets: closed-issue tier labels (50), `forge-internal` (182), `needs-forge-report` (76), hook version sidecar (87), `pip_audit` history (98), `docstring_coverage` (31), CLI wiring (148) | 672, 27% of the file | The triage wrapper, or nobody at decision time: the CLI-wiring rule is enforced by a step whose failure message can carry it, which is what `CLAUDE.md`'s own first bullet asks for | High for the count, medium for each move | Both (flattened into `AGENTS.md`) |
| E8 | `skills/pr/SKILL.md`: emergency short-circuit (196), stacked PRs (128), `code-review` offer (192 plus one paragraph of Step 2), permission promotion (285) | about 850 of 4863 | Paid on every `/forge-pr` run; each applies to a minority of runs | High for the count | Both; two of the four steps name Claude Code features and are dead weight on Vibe |

**What it costs.** E1–E6 are about 2500 of FOUNDATION's 8917 words, paid by
every session and every subagent in every consumer repo. Not all of it can
move: the estimate is an upper bound, and each row keeps a trigger sentence.

### Class 3 — Reaches too few

The dangerous direction. The compaction doc's first prohibition exists
because an imperative behind a pointer was collapsed to its cheap half.

| # | Rule | Where it lives | Who else must follow it | Confidence | Harness |
|---|---|---|---|---|---|
| F1 | Reporter run-time contracts: the `verified-at:` capture, report-by-exception and its machine-read disposition forms, the evidence-pack rule, no hard-wrapping, "Delegating agents wait for their helpers", the guard-hook contract | `agents/_TEMPLATE.md` only. `src/forge/install_claude.py:258` and `src/forge/install_vibe.py:289` both exclude it from generation, so it is present under neither `.claude/agents/` nor `.vibe/prompts/` | Seven agents link to it 16 times and `skills/pr/SKILL.md` twice, for example `agents/precommit-fixer.md:47` and `:193`, `agents/pr-manager.md:18`, `:92`, `:94`, `:181`. In a consumer repo the target is not on disk | High that the file is not installed; medium on effect, since several agents also inline the header shape | Both |
| F2 | Testing standards (fixture naming, mock docstrings, Null Objects, coverage intent) | `agents/test-writer.md:23-40` | `forge-test-advisor` reviews against them by pointer (`agents/test-advisor.md:21`, `:61-62`, which names `agents/test-writer.md`, a path absent in a consumer repo). `forge-precommit-fixer` and the main session also edit test files. `FOUNDATION.md` §8 says "the rules load with the agent applying them"; that holds for one of the roles | High | Both |
| F3 | `forge-internal` labelling and the `needs-forge-report` gate | The wrapper `.claude/agents/issue-triage-forge.md` | Eight call sites in six shipped skills invoke the generic agent directly (`skills/triage`, `next` ×2, `pr` ×2, `sentinel`, `plan-batch`, `plan-issue`). `CLAUDE.md` asks the caller to remember: "Invoke the wrapper directly" | High | Both (forge repo) |
| F4 | "Closed issues keep their tier labels" | `CLAUDE.md` only | The acting agent's own doc says the opposite (`agents/issue-triage.md:100-102`, `--remove-label tier-X-<NAME>`), and so does the caller's prompt (`skills/pr/SKILL.md:546`, "remove their tier labels"). The override wins only if the subagent ranks `CLAUDE.md` above both | High that the three texts conflict; medium on outcome | Both (forge repo) |
| F5 | "Edit files only with the Edit/Write tools, never by a script or shell redirection" | `agents/_TEMPLATE.md:112-116` says every agent holding `Edit` or `Write` carries it | Present in 3 of 6 such agents. Missing from `docs-types-checker`, `pr-manager` and `weekly-summary`. No audit check looks for it (probe: `grep "only with the Edit\|shell redirection" src/forge/audit/agents.py` returns nothing) | High | Both |
| F6 | How to decide issue eligibility (the permission call, `lastEditedAt`, fail closed) | `skills/plan-issue/SKILL.md:36-59` | `forge-issue-triage` applies `needs-endorsement` across the backlog and reaches the probes by pointer; its own one-clause copy (`agents/issue-triage.md:187-190`) reads "a collaborator author, or a collaborator's `[endorsed]` comment" and drops "after the body's last edit". `/forge-sentinel` track mode re-checks eligibility per member, again by pointer. FOUNDATION §14 carries the rule itself, which limits the damage | High | Both |
| F7 | Reply forms for deferred and not-filed review comments | `skills/pr-comments/SKILL.md:40-57` | `forge-pr-manager` posts replies ("Task: Write Reply to Comment") and sees only the first form | Medium | Both |

F8, Vibe only, is in "Harness-specific findings" below.

**What it costs.** Nothing per session; the cost is a wrong action nobody
notices. F3, F4 and F6 all sit on the triage path, which already produced
the incident the compaction doc records.

### Class 4 — Should be a CLI, not an instruction

FOUNDATION §7 "Mechanical first": output that depends only on the working
tree, GitHub state or caller-supplied text is a program. Each row names the
inputs, so the claim can be falsified by finding a judgment among them.

| # | Prose today | Inputs | Deterministic form | Judgment left | Confidence | Harness |
|---|---|---|---|---|---|---|
| M1 | Issue eligibility (`skills/plan-issue/SKILL.md:36-59`; relied on by triage, plan-batch, sentinel) | Author login, the collaborator permission API, `Issue.lastEditedAt`, comment authors and timestamps, a literal marker | `forge-plan-check eligible <N>` with the same exit codes as its siblings (0 / 1 / 2 unknown). The permission call already exists in `src/forge/plan_staleness_gh.py`; `forge-plan-check --help` lists no such subcommand | None | High | Both |
| M2 | The `needs-forge-report` gate, three ordered steps in a forwarded prompt (`.claude/agents/issue-triage-forge.md:53-70`), written after 32 issues were labelled at once | Permission API result, presence of a fixed marker prefix in the body | A forge-local command that prints the gated issue numbers, or applies the label | Choosing the reply shape and writing the reply | High | Both |
| M3 | `post-pr` mode (`agents/issue-triage.md:95-122`) | Merged PR body, `forge-plan-check overlap` output, a fixed comment template | One command that applies it. A config key could carry forge's keep-tier override and close F4 | Placement questions only | High | Both |
| M4 | `stale-scan`, `bootstrap`, the total-count cross-check on every pull, the `deep-review` 7-day cadence guard, the `plan-readiness` baseline diff (`agents/issue-triage.md:29-49`, `124-147`, `193-196`) | Dates, counts, labels, marker comments | Subcommands or flags beside `forge-backlog-index`, which already refuses a short listing | Tiering, and the coherence verdicts of `deep-review` | High | Both |
| M5 | Base-sync gate (R5) | `git rev-list` counts, `mergeable` | A field in the `forge-pr-plan` JSON, so the skill reads one verdict | None | High | Both |
| M6 | Finding the successor of a merged step (`skills/next/SKILL.md:140-161`) | Merged PR bodies, open issue bodies, `forge-plan-check prerequisites` | `forge-plan-check successors`; also retires R11 | Proposing it and asking | High | Both |
| M7 | Backlog Index freshness: read "Last triage" from the body and compare with 7 days (`skills/next/SKILL.md:178-185`), while the agent doc says "never judge the date yourself" and uses `--if-stale-hours`. Also: confirm `MERGED`, then `git branch -D` (`:252`); stop on a dirty tree (`:17-18`) | Dates, PR state, `git status` | Existing `forge-backlog-index --if-stale-hours`; two more checks inside `forge-next-prep` | Whether to re-triage | High | Both |
| M8 | Tidy `.claude/settings.local.json` (`skills/next/SKILL.md:49-60`) | One JSON file | A command that drops covered and garbage entries | Which rules to widen into wildcards | Medium | Claude Code only |
| M9 | "Self-skip when `forge.run_context.is_ci()`" at `skills/plan-batch/SKILL.md:36`, `skills/pr/SKILL.md:201` and `:539`, `agents/issue-triage.md:222` | Environment | No `[project.scripts]` entry exposes the predicate (probe: `grep "run_context\|is-ci" pyproject.toml` returns nothing), and §15 forbids an inline check. Either a tiny CLI, or the CLIs those steps already call self-skip | None | Medium: another CLI may print it as a side effect | Both |
| M10 | Fixed-pattern scans in `agents/security-checker.md:99-106` | Changed files, two fixed regex lists | An audit over the changed files feeding the evidence pack. The agent already notes ruff `S` gates part of this at lint time | Whether a hit is exploitable in context | Medium | Both |
| M11 | The `verified-at:` capture snippet, copied into `agents/test-advisor.md:30-31` from the template | `git rev-parse`, branch, PR number | Printed by `forge-pr-plan --evidence` into the pack reporters already start from | None | Medium | Both |

**What it costs.** Each row is a model round trip per run that can also
return a different answer per run. M1–M4 run on every triage, plan and
sentinel pickup.

One related gap is not a CLI candidate. `docs/agent-architecture.md:29-31`
already records that §3's pre-write delegations (prior-art, design-checker
before editing) are a guideline "which **no skill actually wires**". The
prior-art half is caught late, by `forge-pr-wrapup compose` refusing a
file-adding diff without the report; the design-checker half rests on
memory alone.

### Class 5 — Misplaced by layer

Measured against the destinations table in the compaction doc.

| # | Content | Is in | Belongs in | Confidence | Harness |
|---|---|---|---|---|---|
| L1 | Run-time imperatives for reporters (see F1) | `agents/_TEMPLATE.md`, an author-time document of 2565 words that also holds the frontmatter table, the skeleton and model tiering | A run-time contract that is installed with the agents, or the agent docs themselves; the template keeps the author-time half | High | Both |
| L2 | A CI workflow recipe and a flag table (`skills/smart-test/SKILL.md`, "CI recipe" and step 2) | A skill loaded to run tests locally | `forge-docs/ci-recipe.md`, which §15 calls "the one shipped CI recipe", and `forge-smart-test --help`. The skill's YAML already differs from the shipped recipe (`--from-commit-message` against `--depth 2`) | High | Both |
| L3 | Permission-rule policy: what to promote, what never to (`skills/pr/SKILL.md` Step 3.5) and what to tidy (`skills/next/SKILL.md` step 4) | Two skills | One referenced page, each skill keeping its trigger sentence | Medium | Claude Code only; the text still ships to Vibe |
| L4 | Rationale and version history: "(since v1.1.3)", "Configurable since v2.3.0", the hook sidecar explanation | `CLAUDE.md`, always loaded | A referenced page. §8 also asks that prose describe current state, not history | High | Both |
| L5 | Cookbook text a tool or a page already owns: the OWASP table and pattern lists in `agents/security-checker.md` (`docs/security.md` is its stated source of truth); docstring format examples in `agents/docs-types-checker.md:148-220` | Agent docs at 893 and 1050 words, both over the 800 ceiling | The referenced page, or nowhere: a model already knows Google-style docstrings | Medium | Both |
| L6 | A decision stated weaker in a lower layer: `skills/pr/SKILL.md:587` "Do NOT auto-merge unless the user explicitly asks" | A skill | Nowhere. §2 already says merging is the user's action and `block_pr_merge` blocks it; the skill's wording implies an agent may merge when asked | High | Both |

### Class 6 — Unowned, orphaned, or false

| # | Finding | Evidence | Confidence | Harness |
|---|---|---|---|---|
| O1 | Relative links in shipped agents and skills resolve only from the source tree | All 58 markdown links in the 14 agents and 19 skills were resolved from three places: 58 resolve from `agents/` or `skills/<name>/`; 0 from `.claude/agents/` or `.claude/skills/forge-<name>/`; 0 from `.vibe/prompts/` or `.agents/skills/forge-<name>/`. (`_TEMPLATE.md` holds 6 more, and is installed by neither converter.) Targets: `FOUNDATION.md` (22), `_TEMPLATE.md` (18), `forge-docs/` pages (11), `docs/` pages (6), a skill (1). Both converters rewrite names and leave paths alone; details under "The link root cause" below. `docs/` pages are not shipped at all (`src/forge/data/docs/` links only `forge-docs/`). `FOUNDATION.md` itself names `agents/_TEMPLATE.md` four times and `src/forge/...` four times, paths a consumer repo does not have | High that the paths do not resolve; medium on effect, since an agent may still find a file by name inside forge's own repo | Both |
| O2 | **RESOLVED** — closed by building the gate rather than withdrawing the claim: CI scans each branch's own commits with a digest-pinned scanner and blocks on a finding, the shipped `forge-docs/ci-recipe.md` carries the same step, and §2 now says a repo that has not adopted the workflow has no gate. Original finding: `FOUNDATION.md` §2 "CI secret scanning blocks detected secrets" | `docs/security.md:53`: the scan is "not bundled — add `gitleaks` or `trufflehog` if you want strict enforcement". Probes: no match for `secret`, `gitleaks` in `.github/workflows/ci.yml`; none for `gitleaks`, `secret_scan` under `src/forge/` | High for what forge ships; a repository-level GitHub setting is invisible to a file probe | Both |
| O3 | **RESOLVED** — §6 now states that a description warns over its ceiling and is refused far over, unlike the squash message, which never refuses on length. Original finding: `FOUNDATION.md` §6 "PR descriptions": "300-word ceiling (same bands as the squash message below)"; "Commit messages": "under the squash message's budget" | The squash rule is "length never refuses", and `src/forge/pr_squash_comment.py` posts anyway. `src/forge/pr_create.py` (`_body_length_refusal`) and `src/forge/commit.py` both refuse past 120% of the ceiling. `skills/commit/SKILL.md:27` has it right | High | Both |
| O4 | **RESOLVED** — the skill now names the destructive-git guard as covering every form it had claimed was uncovered. Original finding: `skills/pr/SKILL.md`: "A few destructive forms (`git reset --hard`, `git clean`) are not hook-covered" | `claude-hooks/block_git_destructive.sh:14` and `:18` block every form of `git reset` and forced `git clean`; FOUNDATION §2 says the same | High | Both |
| O5 | `skills/report-to-forge/SKILL.md:42-70` tells the agent to record a `plugin:installed` line, cites `forge-plugin-sync`, and points at "FOUNDATION §11 "Plugin staleness"" | No `plugin:installed` string under `src/forge/`; no `forge-plugin-sync` in `pyproject.toml`; §11's heading is "Generated-integration staleness". `CLAUDE.md` says "forge ships no plugin" | High | Both |
| O6 | Other leftovers of the removed plugin | `CONTRIBUTING.md:57-58` "work in Claude Code with the forge plugin", `:63` "the plugin version", `:66` "Releases are cut by the owner with `forge-changelog release-pr`" (`CLAUDE.md`: a tag is cut on every merge; `release-pr` only assembles changelog prose). `docs/security.md:19` uses `${CLAUDE_PLUGIN_ROOT}`. `skills/pr-comments/SKILL.md:82` "forge's plugin is not loaded". `skills/test/SKILL.md:40` "(Claude Code: `forge:` prefix)", which survives generation unchanged | High | Both |
| O7 | Nobody owns the reply format | `FOUNDATION.md` §6: "reply format: the `/forge-pr-comments` skill". `agents/pr-manager.md:45`: "post in the reply format of FOUNDATION §6". The agent's pointer lands on a pointer, and its own copy is partial (R4) | High | Both |
| O8 | "The same ceiling covers a skill's instructions" (`FOUNDATION.md` §11, `agents/_TEMPLATE.md:90-91`) | `tests/audit/test_agents.py:529` fails the suite only for agents. Four skills sit over the 1500 cap, `pr` at 4863 | High | Both |
| O9 | The cross-agent copied-text check reports noise in forge's repo | It scans `agents/` and `.claude/agents/` together, so each agent is compared with its own generated copy: `design-checker` shows "1428 substring(s) shared with another agent" at 1429 words. Every word-count finding is also listed twice (67 findings). Real cross-agent copying (R6) is hidden in the flood | High | Claude Code layout, forge repo only |
| O10 | Review scope names the wrong path | `agents/security-checker.md:70` and `:112` audit prompt safety "for changes to `.claude/agents/*.md`". In forge the sources are `agents/` and `skills/`, and the generated copies are gitignored (`.claude/.gitignore`), so a diff never touches that path. Skills and hooks are not in scope anywhere | High for forge's repo; the path is right for a consumer's own agents | Both; the path is wrong for a Vibe consumer too |
| O11 | Conventions no document owns | `.plan/STATUS.md` and `.plan/<PHASE>_*.md` (`skills/pr/SKILL.md:247-248`, `skills/next/SKILL.md:71`): FOUNDATION §10 defines only `.plan/CONTINUATION.md`. "The forge testing-documentation policy" (`skills/test/SKILL.md:11`, `:25`) is a name used nowhere else (probe: grep across FOUNDATION, agents, skills and both doc folders) | High | Both |
| O12 | Budgets recorded as sizes | Every entry in `FORGE_DOCS_CEILINGS` sits 0.5% to 6% above its page (`precommit-fixer.md` 1293 of 1300, `ci-recipe.md` 4361 of 4400). Three agents sit within 71 words of the 1500 cap (1429, 1443, 1450). The compaction doc says a ceiling is "never a target"; these numbers describe the present, they do not constrain it | High for the numbers; the reading is a judgment | Both |

### Harness-specific findings

`hooks.toml` is harness-neutral, but the events it names are Claude
Code's, and Vibe has no equivalent for four of the six. What the Vibe
converter does with each group, read from `src/forge/gen_vibe_hooks.py`,
`src/forge/agent_harness.py` and the generated `.vibe/hooks.toml`:

| Event in `hooks.toml` | Hooks | Claude Code | Vibe |
|---|---|---|---|
| `PreToolUse` Bash / Edit / Write | the 19 + 3 + 3 `block_*`, `check_*`, `warn_*` guards | blocking | blocking (`pre_tool`, `strict = true`), all carried over |
| `PostToolUse` Bash / Edit / Write | `check_continuation_budget`, `keep_squash_comment_last`, `warn_stale_wrapup`, `warn_merge_conflicts` | runs | runs (`post_tool`) |
| `PostToolUse`, no matcher | `log_agent_timing` | runs | dropped: the group holds only an unenforced hook, so no entry is generated |
| `SessionStart` | `check_foundation_sync`, `load_continuation`, `caveman_mode` | runs before the first turn | no event. `load_continuation` is replaced by a `post_agent` hook that fires after the first turn; the other two have no replacement |
| `PreCompact` | `keep_continuation_state` | runs before compaction | no event; replaced by a `post_agent` hook on every main-agent turn |
| `SubagentStart` | `log_agent_timing`, `caveman_mode` | runs | no event, no replacement |
| `SubagentStop` | `log_agent_timing`, `require_helpers_stopped`, `require_fixer_verdict` | blocking | no event. `post_agent` fires after the turn and cannot block; none of the three is carried over |

The installer says so: `install-forge-vibe --check` prints six
"Unenforced protections" from `UNENFORCED_VIBE_HOOKS`
(`src/forge/agent_harness.py:490-514`), and
`forge-docs/integration-staleness.md:93` lists the `SubagentStop` gate as
"not enforceable". The findings below are that the **rule text** an
agent reads does not say it. Each is a class 6 finding (an enforcement
claim that is false on one harness); F8 is the class 3 side of the same
gap.

| # | Claim in shipped prose | On Vibe | Confidence | Harness |
|---|---|---|---|---|
| O13 | `agents/_TEMPLATE.md:59-61`: "`require_helpers_stopped` refuses the first hand-back while a launched helper has not stopped" | No gate at all. The hook is `SubagentStop` only, and it also reads `SubagentStart` rows from the timing ledger (`claude-hooks/require_helpers_stopped.sh:55`), which Vibe never writes, so a `post_agent` port could not work either. The prose is the only protection, and the prose lives in the template neither converter installs (F1): on Vibe the rule "delegating agents wait for their helpers" has no enforcement and no reachable text. The template itself notes that Vibe children outlive the turn, which is the condition the gate exists for | High | Vibe only |
| O14 | `agents/precommit-fixer.md:126-127`: "The `require_fixer_verdict` hook re-runs the verdict and refuses a hand-back it contradicts"; `:114-115`: "the hook refuses a fourth" full run; `:215-216`: "the hook's ledger records what actually ran" | `require_fixer_verdict` does not run. `block_fixer_recon` (the fourth-run cap and the Bash restriction) is registered under `pre_tool`, but it identifies the fixer by the payload's `agent_type` (`claude-hooks/block_fixer_recon.sh:49`, `:68`); the Vibe profile declares that Vibe sends `agent_name` (`payload_field_renames`, `src/forge/agent_harness.py:301-303`) and `translate_payload_field` has no caller (probe: grep for it across `src/forge/*.py` finds only its definition). If the payload reaches the guard unrenamed, the guard exits as "not the fixer" and restricts nothing | High for the verdict gate; medium for the recon guard, which was read, not run | Vibe only |
| O15 | `FOUNDATION.md` §8: "`caveman_mode` hook asks caveman full" | Never delivered: no `SessionStart`, no `SubagentStart`. This reverses R6. On Claude Code the per-agent "Output style (always)" block duplicates the hook; on Vibe it is the only thing that reaches a subagent, which `src/forge/install_vibe.py:247-251` states as the reason the block is required. The block cannot simply be deleted, and `[tool.forge.hooks].caveman = false` means nothing on Vibe | High | Mirror of R6: redundant on Claude Code, load-bearing on Vibe |
| O16 | `skills/next/SKILL.md:105-120` reads "the `log_agent_timing` ledger" for loop suspects and fixer cap breaches | All three registrations of `log_agent_timing` are dropped on Vibe (see the table). The installer calls this "partial at best"; the generated `.vibe/hooks.toml` has no entry that runs it. The step degrades to its "silent skip", so the process defects it exists to surface are never surfaced | High that no entry runs it; medium on whether `forge-agent-profile` recovers some of it from Vibe's session store | Vibe only |
| O17 | `FOUNDATION.md` §10: the handoff note "is read in at session start". Also the installer's own list | On Vibe the note is offered after the first turn, by a `post_agent` hook that returns a deny reason asking the agent to read it (`claude-hooks/vibe_session_start.sh`). `check_foundation_sync` has no Vibe path and is missing from `UNENFORCED_VIBE_HOOKS`, so the list the installer prints names six hooks where seven are affected | High | Vibe only |
| O18 | `FOUNDATION.md` §11: "Reporters do not have `Write` or `Edit`"; `agents/_TEMPLATE.md` "Tool sets per role" | The generated `.vibe/agents/forge-<name>.toml` carries `agent_type`, `description`, `system_prompt_id` and a model alias (`src/forge/install_vibe.py:191-240`); the `tools:` list is not translated. It survives only as frontmatter text inside the prompt. `skills/plan-batch/SKILL.md:153-156` already concedes the general point: "no tool set stops a drafter mutating anything" | Medium: Vibe may restrict subagent tools by some default this audit did not find | Vibe only |

| # | Rule | Where the constraint is real | Who is left with prose only | Confidence | Harness |
|---|---|---|---|---|---|
| F8 | The fixer's closing verdict, the fixer's Bash restriction, wait-for-helpers, reporter tool sets | Claude Code: three hooks and the `tools:` frontmatter | On Vibe: `forge-precommit-fixer`, and every agent holding `Task` (`design-checker`, `security-checker`, `knowledge-search`, `perf-optimizer`, `pr-manager`, `precommit-fixer`). Their docs describe the gates as present | As O13, O14, O18 | Vibe only |

A blocking gate that cannot block is not a weaker gate. O13 and O14 are
the two places where an agent's own doc tells it a hook will catch a
false hand-back, and on Vibe nothing will.

**The link root cause (O1).** The two converters agree with each other
and are both incomplete in the same way. Each rewrites the `forge:`
name marker to the `forge-` prefix (`map_plugin_references`); Vibe also
rewrites the instruction file's name (`CLAUDE.md` → `AGENTS.md`, seen in
five skills). Neither touches a path. But no single relative path could
be right: the source sits one level shallower than every generated copy
(`agents/x.md` against `.claude/agents/forge-x.md` and
`.vibe/prompts/forge-x.md`; `skills/x/SKILL.md` against two locations
three levels deep). A link that resolves from the source cannot resolve
from the copy. The form that works from all five places is the one
`FOUNDATION.md` already uses: a path from the repo root, where an
agent's working directory is, written as text (`forge-docs/x.md`)
rather than as a relative link. That fixes 33 of the 58 (the 22 to
`FOUNDATION.md` and the 11 to `forge-docs/`). The other 25 point at
files no consumer has on either harness (`_TEMPLATE.md`, `docs/`, a
source-tree skill path), and no path form helps: the content has to
move into something that is installed.

**Other things both converters leave out while shipped prose points at
them.** `_TEMPLATE.md` (18 links). `docs/security.md`,
`docs/audit-pack.md`, `docs/consumer-release.md`,
`docs/release-process.md` (6 links, plus backticked mentions).
`agents/test-writer.md`, named by path in `agents/test-advisor.md:62`.
`skills/pr/SKILL.md`, linked from `agents/pr-manager.md:132`.
`src/forge/...` modules and `agents/_TEMPLATE.md`, named eight times in
`FOUNDATION.md`. `claude-hooks/<name>.sh`, which
`skills/report-to-forge/SKILL.md:33` gives as the path of a shipped
hook: a consumer has `.claude/hooks/` on Claude Code and the installed
package on Vibe, never that directory.

**Claude Code vocabulary shipped to Vibe.** The skills are written in
Claude Code's terms and reach Vibe with only names rewritten:
`Agent(subagent_type=…)` call syntax in 11 skills, `AskUserQuestion` in
three agents and one skill, `.claude/settings.local.json` in `/next`
step 4 and `/pr` Step 3.5 (M8, L3), the `code-review` built-in in `/pr`,
"Claude Code's built-in `Plan` type" in `skills/plan-batch/SKILL.md:137`.
Whether a Vibe model maps these onto its own tools was not observed.

### What the audits can and cannot see today

| Property | Measured by | Gap |
|---|---|---|
| Size of each document | `forge-audit-agents`, `tests/test_foundation_budget.py` | Double-counts in forge's repo (O9); skills are reported but not gated (O8) |
| Verbatim copying | `forge-audit-agents` 8-word overlap | Blind to paraphrase, which is every duplicate found here; blind to FOUNDATION ↔ skill and skill ↔ skill pairs |
| Dangling hook and CLI names in the architecture doc | `verify-forge-agent-doc` | Does not resolve file links or section names in agents and skills (O1, O5) |
| Truth of an enforcement claim | Nothing | O2, O3, O4, R1 were found only by reading code |
| Which role can see a rule | Nothing | Class 3 entirely |
| Which harness enforces a rule | `install-forge-vibe` prints `UNENFORCED_VIBE_HOOKS` at install | A hand-kept list, one hook short (O17); nothing ties it to the prose that claims the enforcement (O13, O14) |
| Whether a step is mechanical | The design-checker's "mechanical-first lens", a judgment check on diffs | Never run over the existing surface |

---

## Part 2 — Goals

`forge-docs/instruction-compaction.md` answers one question well: what
belongs in one always-loaded file. It is silent on the architecture across
files, and the findings above cluster exactly there:

- It routes text **out of** an always-loaded file but has no test for a
  **skill**, which is also loaded whole and has its own "who pays" (E8).
- Its destinations table has four rows. The surface has at least four more
  that carry instructions: a skill, a hook-injected context line (R6), a
  wrapper's forwarded prompt (R8, M2), and the text a CLI prints.
- It requires a pointer to state the rule, but not that the pointer
  **resolves where the reader stands** (O1), nor that an agent may not
  point at another agent's doc or a skill for an imperative (F2, F6).
- It says to delete what another file owns, but nothing records **who the
  owner is**, so two copies can each defer to the other (O7).
- It has no rule for a **repo-local override** of shipped behaviour (F3, F4).
- It assumes one harness. "A check nobody has to remember beats a rule
  everybody must" holds only where the check runs; on a harness without
  the event, the rule is back to being remembered, and the text does not
  say so (O13–O18).

The goals below are the agent-surface analogues of SRP, DRY and single
source of truth. Each is phrased so a later change could turn it into a
check.

| # | Goal | Checkable form | Measured by |
|---|---|---|---|
| G1 | **One owner per rule.** A rule is stated in full once. Every other mention is one clause plus the owner's name | Each numeric limit, section list, label list and output format has one declared owner; a second full statement is a finding. Start with what is cheap to detect: numbers and literal formats that also exist as code constants (45, 300, the summary labels, the reply forms) | New check. The existing 8-word overlap cannot do it. Baseline: R1–R12 |
| G2 | **Reach equals the acting set.** Every role that acts on an imperative loads it without following a pointer | For each agent doc: no link whose target is another agent's doc or a skill is the only statement of a rule the agent must apply. For each shipped agent that a repo wraps: count call sites that bypass the wrapper | New check for the first half. The second is a grep today: 8 call sites (F3). Otherwise unmeasurable; a role-by-rule table would have to be kept by hand |
| G3 | **Cost follows audience.** Always-loaded text is acted on by two or more roles, in a repo that opted into nothing | Each `##`/`###` section of `FOUNDATION.md` carries a declared audience (all, or named roles); a single-role section over a small word limit is a finding. The same test for each step of a skill: runs on most invocations, or sits behind a pointer | Extend `tests/test_foundation_budget.py` to report words per section (the per-section table in this audit took one short script). Audience itself is unmeasurable without the declaration |
| G4 | **Mechanical before prose.** An instruction whose inputs are the tree, GitHub state or caller text is a command; prose keeps only the judgment and names it | Count raw `gh` and `git` command lines inside fenced blocks of agents and skills; the count only goes down. Each remaining one names the judgment it serves | New check; baseline not taken (see "Not settled"). The design-checker lens covers new diffs only |
| G5 | **A pointer resolves where it is read.** Every link, path, section name, hook name and CLI name in a shipped document resolves from the generated location, in a repo that has only what forge installs | Resolve every markdown link from each harness's generated agent and skill locations against the installed file set, or forbid relative links in shipped docs and require root-relative paths into the installed set; resolve every `§N "name"` against FOUNDATION's headings and bold leads | New check, a natural extension of `verify-forge-agent-doc`. Baseline: 0 of 58 links on either harness, 84 of 85 section names |
| G6 | **An enforcement claim is true.** "X refuses", "X blocks", "CI scans" names a thing that exists and does that | Existence is checkable: the named hook is in `hooks.toml`, the named CLI in `[project.scripts]`, the named test on disk. The verb is not; the durable fix is to render such sentences from the constant or the tool's own output rather than write them | Existence: new check. Verb: unmeasurable today. Baseline: O2, O3, O4, R1 |
| G7 | **One layer per document.** Author-time guidance, run-time contract, policy and mechanics do not share a file | `_TEMPLATE.md` holds no sentence an agent must obey at run time; whatever an agent must obey is in a file installed beside it. A skill holds no CI recipe or flag table | Partly `forge-audit-agents` (required sections). The run-time/author-time split needs a new check or a file split that makes it structural |
| G8 | **Budgets count once and bite where they say.** One document, one count; a ceiling is set by decision, not by the current size | The audit skips generated copies of a source it already read. The skill hard cap is tested the way the agent cap is. A ceiling within a few percent of the page it guards is reported | Fix to `forge-audit-agents`; one more test beside `tests/audit/test_agents.py:529`; a report line in `tests/test_foundation_budget.py` |
| G9 | **A local override is structure, not memory.** Where a repo diverges from a shipped agent, the divergence is a config key the CLI reads (§16 Pattern C) or a wrapper every call site reaches | No always-loaded sentence contradicts a shipped agent doc or a shipped prompt. Detectable case by case, not in general | Unmeasurable in general. Baseline: F3, F4 |
| G10 | **Removal reaches the rule surface.** When a mechanism is deleted, no instruction still names it | A short list of retired names (commands, labels, features) grepped across the surface in pre-commit; an entry is added in the PR that removes the mechanism (§7 "A change removes what it supersedes") | New check. Baseline: the plugin, named in at least five rule-surface files (O5, O6) |
| G11 | **Enforcement is harness-independent, or the rule says where it holds.** A rule's gate works on every harness forge generates for. Where it cannot, the sentence that claims the gate names the harness that has it and says what protects the other: a CLI-side check, or nothing | Derive, per hook, the set of harnesses on which it runs and can block, from `hooks.toml` and each profile's event map. Then, for every hook name that appears in `FOUNDATION.md`, an agent or a skill next to an enforcement verb ("refuses", "blocks", "re-runs"): the hook is enforced on all harnesses, or the same passage names the exception. The durable fix is the one FOUNDATION §7 already states, "a CLI performing a guarded effect enforces that guard in its own code": a gate inside `forge-commit` or `forge-precommit --verdict` holds on any harness, a gate in a `SubagentStop` hook holds on one | `hooks.toml` coverage per harness is the right base and nearly exists: `UNENFORCED_VIBE_HOOKS` is that table kept by hand. Better is to compute it (it would have caught `check_foundation_sync`) and to join it to the prose, which nothing does today. Coverage alone misses two cases found here: a hook that is registered but cannot identify its agent (O14), and a restriction that is not a hook at all (O18, tool sets). Those need a per-harness test that feeds the guard a payload in that harness's shape. Baseline: 7 of the 32 scripts wired in `hooks.toml` have no equivalent Vibe path; 3 prose claims are unconditional (O13, O14, O15) |

### Delivering the goals: the C4 analogue, and its limit

Forge already models one architecture this way. `c4.toml` holds the
human-authored skeleton — system, containers, component groupings — while
the component-to-component edges are derived from the import graph, and
`forge-gen-c4 --check` fails on drift. The expensive half is derived, so
it cannot rot quietly. The rule surface admits the same split:

| C4 part | Rule-surface analogue |
|---|---|
| Containers | Load tiers: always-loaded, per-agent doc, on-demand page, mechanical enforcer |
| Components | Each document, and the rules it owns |
| Import graph (derived) | The pointer graph: markdown links, `§N "name"` citations, `forge:` markers |
| Declared runtime edges | Enforcement edges — which hook, step or CLI backs a rule, per harness |
| `--check` fails on drift | G5, G6, G8, G11 as failing checks rather than prose |

The limit decides the order. Most of what Part 1 found is derivable
today — link resolution per harness (G5), a claimed enforcer that does
not exist (G6), a count taken twice or a ceiling tracking its page (G8),
hook coverage per harness (G11). Those belong in `forge-audit-agents`
and the budget test, not in a new declared model, because the method
doc's own rule applies: prefer the lowest row that fits, and a check
nobody has to remember beats a rule everybody must.

A declared model earns its place only for what no grep can derive: which
document **owns** each rule (G2), and the enforcement edges (G11's
join). Building it before the derived layer exists would add a second
artifact that itself needs remembering — the failure this audit
documents, in a new file.

### Suggested order

1. **G5, then F1.** Link resolution is mechanical, the prototype exists,
   and it exposes the template problem that F1 and L1 describe from two
   sides. It is also the one finding that affects every consumer repo.
2. **M1 and M2 (under G4).** Both encode a rule that has already failed
   once from prose, and both have no judgment in them.
3. **G6 for the four false claims (O2, O3, O4, R1).** A false sentence
   in an always-loaded file is the costliest kind: the reader stops
   checking.
4. **G11 for O13 and O14**, if Vibe is meant to be supported rather than
   experimental: two agent docs promise a blocking hook that Vibe does
   not have. The cheap half is one sentence in each; the real half is
   moving the gate into the CLI the agent already runs.

### Not settled

- **Whether the summary-label rule warns or refuses.** FOUNDATION §6 says
  "`forge-pr-wrapup validate` warns, never refuses" for the labelled
  summary; `_check_head` in `src/forge/pr_wrapup.py` adds a violation for
  an unlabelled multi-line summary. Whether that violation refuses was not
  traced to the exit code.
- **How much an unresolvable link costs in practice (O1, F1).** The paths
  do not resolve; whether agents fail, search by name, or carry on without
  the contract was not observed.
- **Repository-level secret scanning (O2).** GitHub push protection may be
  on for forge's repo; no tracked file can show it.
- **The G4 baseline.** A scripted count of raw `gh`/`git` lines in fenced
  blocks was refused by the `block_raw_git` hook, because the script text
  contained the word it scans for although it ran no git. It was not
  retried in another form. The refusal is itself a small finding: a
  read-only text scan cannot be told apart from interpreter-run git.
- **Vibe at run time.** Every Vibe statement here comes from the
  converter, its generated files and `install-forge-vibe --check`. No
  Vibe session was run. In particular: whether the payload Vibe sends
  carries `agent_name` only (O14), whether Vibe restricts subagent tools
  by default (O18), and whether a Vibe-only consumer has `FOUNDATION.md`
  and `forge-docs/` on disk at the root (which decides whether the
  root-relative form in "The link root cause" resolves there).
- **A second guard false positive.** A read-only search over the
  generated Vibe prompts was refused by `block_vibe_config`, because the
  command piped through a `sed` expression whose text contained that
  path. It was not retried in that form; the same question was answered
  from the sources, which differ from the generated copies only in names.
- **Class 2 estimates.** The word counts are exact; how much of each
  section can leave without moving an imperative behind a pointer needs
  the compaction doc's three questions applied passage by passage.
