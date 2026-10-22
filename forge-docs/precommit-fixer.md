# precommit-fixer — why its rules are shaped the way they are

`forge:precommit-fixer` clears pre-commit failures before every commit. Its
agent doc (`agents/precommit-fixer.md`) carries every rule it must follow;
this page carries the reasoning behind them, so the doc stays inside the
agent length budget and a reader who wants to change a rule can see what
it protects. Nothing here adds a rule the agent doc lacks.

## The loop and its evidence

`forge-precommit` runs each step CLI plus an inline `pip_audit` check, and
each step writes its own `code_health/*.log`. When nothing needs fixing the
ruff step is near-instant and silent; residue (rules without an autofix)
lands in `code_health/ruff.log` and fails the step.

Every log opens with a `# produced-at: tree=…` stamp naming the working
tree it describes. The fixer's own edits post-date the whole opening run:
any `Edit` after it stales every log, and only a full run restores the
input the verdict reads. That is the single reason for the closing order —
finish all edits, then one full run, then `forge-precommit --verdict`.
`forge-precommit --freshness` (read-only, not counted) reports each log
`fresh` or not.

## One command per situation

The Allowed-CLIs table exists because both wrong moves have been made in
practice. A bare `forge-precommit` after a fix spends one of the three
full runs on what a targeted re-check would have answered; a step CLI after
a fix uses a tool the contract allows only *before* dispatch, to refresh a
single stale or missing log. Re-verification is `--only`'s job and nothing
else's ([`docs/step-invocation.md`](../docs/step-invocation.md) in the
forge repository).

Only half of that is enforced by machinery. **The cap is**: the
`block_fixer_recon` hook refuses a fourth full invocation and appends every
one to `code_health/agent_timing.jsonl`, so the agent's memory of the count
does not decide it. **The step-CLI limit is not**: the hook allowlists the
step-CLI tokens unconditionally, checking neither order nor count. Nothing
stops a misuse and nothing records it, which makes keeping it the agent's
responsibility rather than an optional nicety.

## Re-verify narrow, close wide

A step that already passed cannot be broken by a fix to a different step's
finding — except `ruff`, which re-runs format and fix and so picks up any
drift an Edit introduced. So the re-verify set is the steps that failed,
plus `ruff` whenever a Python file was edited, named together in one
`--only`. This is not merely faster: a full battery re-runs regeneration,
the C4 render, the type check and a network-bound CVE scan to confirm a
one-line docstring, and time spent that way is what makes a gate feel worth
skipping.

The hand-back still needs every enabled step's evidence to describe the
tree the fixer leaves, because the commit agent refuses stale logs and the
verdict is read mechanically. Hence one full closing run after the last
Edit. With no Edit since the opening run, the opening run already is the
closing one.

**The cap is a ceiling, not a budget.** The opening and closing runs use
two of the three, leaving one spare for a fix that genuinely needs a full
re-check mid-loop. A run that uses only two is doing it right. Hitting the
cap with a step still failing, or seeing the same finding set twice in a
row, means the loop is stuck, and more runs will not unstick it.

**A formatter-reverted Edit is stuck after one occurrence.** A re-run that
shows an Edit undone by `ruff format` means the finding is
formatter-stable: there is one canonical layout and no re-arrangement
survives the next pass. The classic case is a line only a rename can
shorten — a semantic change outside the fixer's mandate, so it is named for
the main agent instead of retried.

## Why tests re-run after a code or test edit

`forge-precommit --verdict` reports on the pre-commit steps; it does not
run the test suite. The fixer has twice reported PASS after an edit broke
tests: once after bundling a test helper's arguments into a dataclass and
converting only some of the call sites, once after replacing a dash inside
a test string, which changed the string's value. Both edits passed every
pre-commit step. The only evidence that would have caught them is a test
run, so a code or test edit without one is unverified, and a PASS reported
over it is a false report.

`forge-smart-test --depth 0` is the run: it selects the tests that import a
changed module directly, and a changed test file always runs whole at depth
0, so an edit to a test file re-runs that file without the fixer having to
name it. It is the only file-scoped test run the fixer may make; any other
test run is by node-id.

## Why test code is handled more cautiously

- **Parameters.** In test code any parameter may be a side-effect fixture —
  a test requests it only for what it sets up. Ruff flags it as unused;
  removing it, or renaming it to `_x`, switches the fixture off while the
  test stays green. So the fixer never removes or renames a test-code
  parameter and lists the finding for a human instead.
- **Complexity.** A complexity finding in test code (`PLR0913` and the
  rest) is usually a helper or fixture whose shape many tests depend on;
  refactoring it inline is how the dataclass incident above happened. The
  fixer reports it rather than refactoring it.
- **Ambiguous characters.** `RUF001`–`RUF003` inside a string literal are
  fixed only by writing the same code point as a `\uXXXX` escape. A
  different character — an ASCII `'` for `’`, or `-` for an en dash —
  changes the value the test asserts on. Every such fix in a test file is
  listed in the report so a reviewer sees it.

## Report-only steps

- **`pip_audit`.** A dependency pin bump has its own blast radius
  (environment rebuilds, transitive changes, independent rollback) and
  ships in a dedicated `chore(deps)` PR or with explicit user approval
  (FOUNDATION §6). The fixer reports each advisory with the pin, the
  suggested version and where the pin lives. In `strict` mode the CVE scan
  is forced, because the scan runs once per branch and escalating a stale
  answer would report nothing either way; the variable must ride the same
  command, since exporting it in a separate one does not reach it.
- **`changelog_version` "fragments added since this branch's base".** A
  `changelog.d/` file this change did not create belongs to another PR —
  typically a stacked branch carrying its parent's entry. Editing it would
  rewrite someone else's release note.
- **`plugin_version`.** The required version is a repo policy (the
  consumer's `CLAUDE.md` documents it); the log states the version the
  policy demands, and the fixer applies only that.
- **Release actions.** A step whose cure is a tag, a merge or a release
  (`release_tag_guard`, versioning cures that need tags) is reported, never
  performed — delegating it to another agent is the same act.

## Finding symbols

For an `F401` / `F841` / naming finding that needs a symbol lookup, the
generated `docs/api-digest.md` (`forge-gen-api-digest`) answers "is this
name used elsewhere, does a sibling helper exist" in one grep instead of a
walk of the import graph.
