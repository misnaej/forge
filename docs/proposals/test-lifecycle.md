# Test-suite lifecycle — research report (issue #396)

Status: research deliverable, no policy in force. This document feeds the
maintainer's decision; nothing here is binding until adopted into
FOUNDATION §8 / the test-advisor and test-writer contracts. Prior art is
cited inline; claims marked *(unverified)* were not confirmed at source
and must not be treated as established figures.

## 1. Where this repo actually stands (measured 2026-08-31)

One full run on dev (`pytest tests/ -q --durations=30`, single process):

| Metric | Value |
|---|---|
| Test files | 65 |
| Collected tests | 2,319 |
| Total wall time | 60.9 s |
| Tests ≥ 1 s | 4 (all in `test_gen_cli_reference.py`, 2.6–5.6 s — real subprocess round-trips) |
| Everything else | < 1 s each; the suite is dominated by count, not by slow outliers |

Read: this suite has **no wall-time emergency**. A minute of runtime does
not justify machinery by itself. The costs that do grow with count are
maintenance surface (2,319 assertions to keep true through refactors),
review burden per PR, and signal dilution — which of these tests would
actually catch a regression, and which merely re-execute code.

The run also caught a live defect (stale `plugin-roster.toml` missing the
`report-to-forge` skill — fixed in this PR): evidence that full-suite runs
retain diagnostic value that per-change selection had skipped.

## 2. Prior art — what production systems do

**Test-impact selection.** Microsoft TIA selects `impacted ∪
previously-failing ∪ newly-added` — the union, never impacted alone — and
treats "file type not understood → run everything" plus a configurable
periodic full run as the two guarantees that make selection safe
([Microsoft TIA docs](https://learn.microsoft.com/en-us/azure/devops/pipelines/test/test-impact-analysis?view=azure-devops)).
Google TAP selects by reverse dependency closure and found only **1.23 % of
test executions ever detect a breakage** — the value of a suite is
concentrated in a sliver of its executions
([Memon et al., ICSE-SEIP 2017](https://research.google.com/pubs/archive/45861.pdf)).
`pytest-testmon` does the same per-block with coverage checksums, sharing
coverage.py's blind spots ([testmon.org](https://testmon.org/blog/determining-affected-tests/)).

**Validation of a selector.** Microsoft documents a differential
procedure: run selected tier and full suite in sequence, assert identical
failure sets. This is the ready-made answer to "how do we know the lean
run still protects us" — and with telemetry history it can be evaluated
retrospectively instead of doubling CI.

**Size taxonomy.** Google classifies tests by *resources* (small: one
process, no I/O or sleeps; medium: localhost only; large: unrestricted)
precisely because the constraints are mechanically checkable
([SWE at Google, ch. 11](https://abseil.io/resources/swe-book/html/ch11.html)).
The published target mixes disagree (80/15/5 in the book; 70/20/10 in the
earlier internal guidance, whose author says the numbers were "pulled out
of a hat" — [Bland](https://mike-bland.com/2011/11/01/small-medium-large.html)):
adopt the taxonomy, not a ratio.

**Mutation-guided evaluation.** Full mutation testing is expensive;
*extreme mutation* (gut a method's body, see if anything fails) finds
"pseudo-tested" methods — covered but asserting nothing — at a fraction of
the cost ([Descartes, arXiv:1811.03045](https://arxiv.org/pdf/1811.03045)).
Python tooling (mutmut, cosmic-ray) lacks a Descartes-equivalent engine,
so this is a periodic-audit approximation here, not a gate.

**Quarantine lanes.** Dropbox's Athena demotes noisy tests out of the
blocking presubmit path while they keep running postsubmit
([Athena](https://dropbox.tech/infrastructure/athena-our-automated-build-health-management-system));
Fowler bounds the lane with a numeric or time cap and warns quarantine's
failure mode is becoming a landfill
([Fowler](https://martinfowler.com/articles/nonDeterminism.html)).
Notably, **no first-party source documents an exit/re-promotion rule** —
any policy here must define its own. Google measured 84 % of pass→fail
transitions as flakes
([Micco, CI @Google](https://research.google.com/pubs/archive/45880.pdf))
and explicitly abandoned "rerun recently-failed" selection because
failure history is flake-dominated
([Memon et al.](https://research.google.com/pubs/archive/45861.pdf)) — a
telemetry-driven selector must classify flakes before weighting failures.

**Retiring tests — the contested question.** Practitioner essays give
deletable categories: duplicate coverage, implementation-mirroring tests,
type-system-obviated checks, obsolete behavior
([Weber](https://benjiweber.co.uk/blog/2014/04/27/delete-your-tests/),
[Benguella](https://riad.blog/2020/07/21/deleting-tests-is-a-best-practice/)).
The highest-authority source pushes the other way: *SWE at Google* says
de-brittle rather than delete and treat tests as production code. The one
crisp published criterion is Fowler's: **"you are testing too much if you
can remove tests while still having enough"**, where "enough" = rare
production escapes and no fear of change
([TestCoverage](https://martinfowler.com/bliki/TestCoverage.html)) —
gated by Weber's safety condition: retire a narrow test only when a
higher-level test still asserts the behavior. "Scaffolding tests" as a
named category with a published retirement policy: not found in prior art;
if forge adopts the term, it is coining it.

## 3. Proposals (for decision — none adopted by this PR)

### P1 — Classification: behavior vs development tests

Two classes, marked at authoring time:

- **Behavior test**: pins observable contract — CLI output shape, gate
  verdicts, invariants, every regression captured from a real bug. The
  permanent class; retirement not applicable.
- **Development test**: drove an implementation to correctness —
  per-step probes, near-duplicate variants, implementation-mirroring
  assertions. Valuable while the function is in motion; a retirement
  *candidate* once behavior is pinned elsewhere.

Mechanics options, cheapest first: (a) a `@pytest.mark.development`
marker written by `test-writer` at authoring time and reviewed by
`test-advisor`; (b) heuristics over the audit-pack dup scanner
(byte-similar bodies = the class that produced #346's deletion) to flag
candidates retroactively; (c) extreme-mutation audit for pseudo-tested
code as a periodic report. Recommendation: (a) forward-looking +
(b) retroactively; (c) as a later experiment.

### P2 — Retirement rule

A development test may be retired only when **all** hold: the function's
behavior is pinned by at least one behavior test (Weber's condition); the
test has not failed for a real defect within a defined window (telemetry
history is the ledger — flake-classified failures excluded per Google's
finding); and the retirement is a reviewed diff, never an automated
deletion. Disposition options: delete outright (Weber/Benguella), or
demote to an opt-in slow lane (a `full`-tier-only marker in
forge-smart-test's depth model) with deletion after a further quiet
period. Recommendation: demote-then-delete — reversible, and the depth
model already has the lane. Re-activation trigger: any edit to the
function under test pulls its demoted tests back into selection (the
smart-test dependency map already computes this).

### P3 — Profiling wiring

Per-test wall time already has a home: `forge-slow-tests-report` consumes
pytest's `--durations` output today, and `docs/telemetry.md` documents the
pairing (slow-tests names the test, telemetry shows what the run
consumes). The gap is *history*: neither surface persists per-test timing
across runs. Proposal: extend `forge-slow-tests-report` (not a new tool —
§7 fix-the-interface) so a full-tier run appends its over-threshold lines
to a run-labelled ledger next to `telemetry_history.log`. That ledger is
P2's evidence base and makes §1's slowest-N table reproducible per run.

### P4 — The compromise metric

Reject a single number. Track three independently, per full run:
wall time (already in telemetry history), test count (collected), and
behavior-coverage ratio (behavior tests ÷ total, once P1 marks exist).
"Coverage per second" has no prior art — if adopted it is forge's own
invention and should be labelled as such. The suite is *thorough* when the
differential check (§2, Microsoft's T1/T2 procedure) shows tier runs and
full runs failing identically; it is *merely large* when count grows while
the behavior ratio falls.

### P5 — Selector guarantees (documentation gap, independent of P1–P4)

forge-smart-test currently has **neither** an unrecognized-change
fallback (`changed_python_files` filters to `.py`; a non-Python change
selects nothing) nor a stated full-run cadence — both are gaps, not
undocumented facts. Proposal: add them as explicit guarantees
(unrecognized change → full run; a named periodic full-run trigger) in
the depth model and `forge-docs/smart-test.md`, per Microsoft's two
safety conditions (§2). This is a behavior change plus a spec change,
not a documentation catch-up.

## 4. What this report deliberately does not do

No FOUNDATION §8 edits, no agent-contract changes, no markers introduced,
no tests deleted. Issue #396 stays open; the next step is the
maintainer's selection among P1–P5, then a planned implementation PR per
piece adopted.
