"""Unit tests for forge.pr_wrapup_compose — pure wrap-up rendering.

# MOCKING STRATEGY: every function under test here is pure (no subprocess,
# no ``gh``, no filesystem) — every case builds its input in memory and
# asserts on the returned text. No mocking anywhere in this file.
"""

from __future__ import annotations

import pytest

from forge.pr_delta import extract_verified_shas
from forge.pr_wrapup import validate_wrapup
from forge.pr_wrapup_compose import (
    FILL_PREFIX,
    REPORTER_SECTIONS,
    ComposeError,
    ComposeInputs,
    evidence_fence,
    pytest_summary_line,
    render_code_quality,
    render_issue_management,
    render_wrapup,
    slot,
    summarize_rollup,
    unfilled_slots,
)


_ALL_REPORTERS = tuple(reporter for _title, reporter in REPORTER_SECTIONS)


def _pass_report(sha: str | None = None) -> str:
    """Build a clean reporter report, optionally headed by its own ``verified-at:``.

    Args:
        sha: Header SHA to prepend, or ``None`` for a headerless report.

    Returns:
        A report whose first substantive line starts with ``PASS``.
    """
    header = f"verified-at: {sha}   (PR #1, branch x)\n" if sha else ""
    return f"{header}PASS — no issues found.\n"


def _findings_report() -> str:
    """Build a reporter report carrying one real (non-clean) finding.

    Returns:
        A report whose first line does not start with ``PASS``.
    """
    return "Found: an unused import. Disposition: removed before commit.\n"


def _inputs(**overrides: object) -> ComposeInputs:
    """Build full-mode, all-clean ``ComposeInputs``; override per test.

    Args:
        **overrides: Fields to replace on top of the full-mode defaults.

    Returns:
        A ``ComposeInputs`` ready for :func:`render_wrapup`.
    """
    base: dict[str, object] = {
        "head_sha": "abc1234",
        "mode": "full",
        "reasons": (),
        "reporters": _ALL_REPORTERS,
        "reports": {reporter: _pass_report() for reporter in _ALL_REPORTERS},
        "prior_art_report": None,
        "added_non_fragment_paths": (),
        "issue_management": "Closes #1",
        "code_quality": "✅ pre-commit: 5 pass ✅ pytest 100 passed in 1.0s",
    }
    base.update(overrides)
    return ComposeInputs(**base)  # type: ignore[arg-type]


def _fill(text: str) -> str:
    """Replace every ``<!-- forge:fill ... -->`` line with one short line.

    Mirrors what a human author does before ``validate_wrapup`` accepts a
    composed wrap-up — these tests pin the renderer's slot *placement*,
    not prose authoring, so the filler text is uniform and short.

    Args:
        text: Rendered wrap-up text, slots included.

    Returns:
        The same text with every slot line replaced.
    """
    return "\n".join(
        "Filled in for testing." if line.startswith(FILL_PREFIX) else line
        for line in text.split("\n")
    )


def _gate_evidence_block(output: str) -> str:
    """Build a ``git_utils.run_gate_evidence``-shaped block for :func:`evidence_fence`.

    Args:
        output: The gate's raw output to embed inside the fence.

    Returns:
        A ``## `` heading, a headline, and a four-backtick fenced block.
    """
    return f"## Provenance gates\n\nProvenance gates pass.\n\n````\n{output}\n````\n"


# ---------------------------------------------------------------------------
# render_wrapup — per mode
# ---------------------------------------------------------------------------


def test_render_wrapup_full_mode_all_pass_fills_to_a_valid_wrapup() -> None:
    """A full-mode wrap-up with three clean reporter reports has no mode line."""
    inputs = _inputs()
    text = render_wrapup(inputs)
    assert "wrapup-mode:" not in text
    assert text.count("PASS — no issues found.") == 3
    assert extract_verified_shas(text) == [inputs.head_sha]
    assert validate_wrapup(_fill(text)) == []


def test_render_wrapup_full_mode_with_one_finding_uses_a_findings_slot() -> None:
    """A non-clean report earns a ``findings:`` slot instead of a PASS line."""
    inputs = _inputs(
        reports={
            "design-checker": _findings_report(),
            "security-checker": _pass_report(),
            "docs-types-checker": _pass_report(),
        }
    )
    text = render_wrapup(inputs)
    assert slot("findings: design-checker") in text
    assert validate_wrapup(_fill(text)) == []


def test_render_wrapup_light_code_mode_token_and_mechanical_recommendation() -> None:
    """light-code writes ``wrapup-mode: light`` and a reasons-only Recommendation.

    Pins the refinement in the test plan: only ``light-code`` (and
    emergency) get the ``wrapup-mode:`` token the publish hook matches —
    every other mode renders no mode line at all.
    """
    inputs = _inputs(
        mode="light-code",
        reporters=(),
        reports={},
        reasons=("diff is 10 lines under 50, adds no files",),
    )
    text = render_wrapup(inputs)
    assert "wrapup-mode: light" in text.splitlines()
    assert text.count("SKIPPED (light-code)") == 3
    assert inputs.reasons[0] in text
    assert validate_wrapup(_fill(text)) == []


@pytest.mark.parametrize(
    ("mode", "reporters", "reports", "evidence_block"),
    [
        pytest.param(
            "light-docs",
            ("docs-types-checker",),
            {"docs-types-checker": _pass_report()},
            None,
            id="light-docs",
        ),
        pytest.param(
            "light-regen",
            (),
            {},
            _gate_evidence_block("gate output line"),
            id="light-regen",
        ),
    ],
)
def test_render_wrapup_light_docs_and_light_regen_skip_design_and_security(
    mode: str,
    reporters: tuple[str, ...],
    reports: dict[str, str],
    evidence_block: str | None,
) -> None:
    """Design/Security are always SKIPPED; Documentation Check differs by mode.

    light-docs still runs the docs reporter (a PASS line); light-regen
    skips it too but carries the fenced provenance-gate evidence instead.

    Args:
        mode: forge-pr-plan mode under test.
        reporters: Reporters the mode requires (empty for light-regen).
        reports: Reporter reports for *reporters*.
        evidence_block: A ``run_gate_evidence``-shaped block, or ``None``.
    """
    evidence = evidence_fence(evidence_block) if evidence_block else None
    inputs = _inputs(
        mode=mode, reporters=reporters, reports=reports, light_regen_evidence=evidence
    )
    text = render_wrapup(inputs)
    assert "wrapup-mode:" not in text
    assert text.count(f"SKIPPED ({mode})") == (3 if mode == "light-regen" else 2)
    if mode == "light-docs":
        assert "PASS — no issues found." in text
    else:
        assert "gate output line" in text
        assert "````" in text
    assert validate_wrapup(_fill(text)) == []


def test_render_wrapup_delta_mode_reports_unchanged_since_prior_sha() -> None:
    """Delta reporter sections never render a ``verified-at:`` line.

    The prior SHA is not rendered as a line.
    """
    inputs = _inputs(mode="delta", reporters=(), reports={}, delta_prior_sha="9f8e7d6")
    text = render_wrapup(inputs)
    assert text.count("PASS — unchanged since 9f8e7d6 (delta)") == 3
    assert "wrapup-mode:" not in text
    # Only the real header counts — the quoted prior SHA never starts a line.
    assert extract_verified_shas(text) == [inputs.head_sha]
    assert validate_wrapup(_fill(text)) == []


def test_render_wrapup_delta_mode_falls_back_to_question_mark_without_a_prior_sha() -> (
    None
):
    """A missing ``delta_prior_sha`` renders ``?`` rather than omitting the clause."""
    inputs = _inputs(mode="delta", reporters=(), reports={}, delta_prior_sha=None)
    text = render_wrapup(inputs)
    assert "PASS — unchanged since ? (delta)" in text


def test_render_wrapup_emergency_skips_evidence_checks_entirely() -> None:
    """Emergency mode bypasses BOTH the missing-report and prior-art checks.

    ``_check_evidence`` returns early once ``emergency_ledger`` is set — an
    emergency wrap-up owes retroactive verification, not evidence now.
    """
    inputs = _inputs(
        reports={},
        added_non_fragment_paths=("src/x.py",),
        prior_art_report=None,
        emergency_ledger=482,
    )
    text = render_wrapup(inputs)  # must not raise
    assert "wrapup-mode: emergency" in text.splitlines()
    assert text.count("SKIPPED (emergency: ledger #482)") == 3
    assert "emergency: ledger #482; verification owed after delivery" in text
    assert validate_wrapup(_fill(text)) == []


# ---------------------------------------------------------------------------
# render_wrapup — refusals
# ---------------------------------------------------------------------------


def test_render_wrapup_raises_for_a_missing_required_reporter_report() -> None:
    """A plan-required reporter with no report file refuses, naming it."""
    inputs = _inputs(
        reports={
            "design-checker": _pass_report(),
            "security-checker": _pass_report(),
        }
    )
    with pytest.raises(ComposeError, match="docs-types-checker"):
        render_wrapup(inputs)


def test_render_wrapup_raises_when_added_files_have_no_prior_art_report() -> None:
    """Added files with no ``--prior-art`` report refuse, naming the path."""
    inputs = _inputs(
        added_non_fragment_paths=("src/new_thing.py",), prior_art_report=None
    )
    with pytest.raises(ComposeError, match=r"src/new_thing\.py"):
        render_wrapup(inputs)


def test_render_wrapup_accepts_added_files_when_a_prior_art_report_is_given() -> None:
    """A prior-art report's header line is folded into the wrap-up head."""
    prior_art_text = (
        "prior-art-searched: digest=abc123def456 queries=3\n\n"
        "## Prior-Art Report: new_thing\n\n### Verdict: NEW\n"
    )
    inputs = _inputs(
        added_non_fragment_paths=("src/new_thing.py",), prior_art_report=prior_art_text
    )
    text = render_wrapup(inputs)
    assert "prior-art-searched: digest=abc123def456 queries=3" in text
    assert validate_wrapup(_fill(text)) == []


def test_render_wrapup_raises_when_prior_art_report_lacks_the_header_line() -> None:
    """A prior-art report missing its ``prior-art-searched:`` line refuses."""
    inputs = _inputs(prior_art_report="No header here.\nJust prose.\n")
    with pytest.raises(ComposeError, match="prior-art-searched"):
        render_wrapup(inputs)


# ---------------------------------------------------------------------------
# render_wrapup — reporter PASS line SHA suffix
# ---------------------------------------------------------------------------


def test_reporter_pass_line_gets_a_verified_at_suffix_when_the_report_sha_differs() -> (
    None
):
    """A report verified at a different SHA than HEAD names its own SHA."""
    inputs = _inputs(
        head_sha="fed9999",
        reports={
            "design-checker": _pass_report(sha="abc1234"),
            "security-checker": _pass_report(),
            "docs-types-checker": _pass_report(),
        },
    )
    text = render_wrapup(inputs)
    assert "PASS — no issues found. (verified-at abc1234)" in text
    # The report's own header SHA is never mistaken for a wrap-up header.
    assert extract_verified_shas(text) == ["fed9999"]


def test_reporter_pass_line_has_no_suffix_when_the_report_sha_prefixes_head() -> None:
    """A report SHA that prefixes (or is prefixed by) HEAD needs no suffix."""
    inputs = _inputs(
        head_sha="abc1234def",
        reports={
            "design-checker": _pass_report(sha="abc1234"),
            "security-checker": _pass_report(),
            "docs-types-checker": _pass_report(),
        },
    )
    text = render_wrapup(inputs)
    assert "(verified-at" not in text
    assert text.count("PASS — no issues found.") == 3


# ---------------------------------------------------------------------------
# render_code_quality
# ---------------------------------------------------------------------------


def _timing_log(*rows: str, stamped: bool = True) -> str:
    """Build a ``precommit_timing.log`` body from ``"<name> <marker>"`` specs.

    Args:
        *rows: Each a ``"<name> <marker>"`` pair (marker is one of SKIP,
            PASS, WARN, FAIL).
        stamped: Whether to prepend a ``# produced-at:`` line (#538 shape) —
            it must never be mistaken for a step row.

    Returns:
        A timing-log body matching ``precommit._format_timing_log``'s shape.
    """
    lines = []
    if stamped:
        lines.append(
            "# produced-at: tree=unknown head=abc1234 2026-01-01T00:00:00+00:00"
        )
    lines.append("forge-precommit per-step timing (newest run overwrites)")
    lines.append("")
    total = 0.0
    for spec in rows:
        name, marker = spec.split()
        lines.append(f"{name:<28} {1.0:>7.1f}s  {marker}")
        total += 1.0
    lines.append("")
    lines.append(f"{'total':<28} {total:>7.1f}s")
    return "\n".join(lines)


def test_render_code_quality_all_pass_collapses_to_a_count() -> None:
    """A clean run with no exceptions renders only the pass count."""
    log = _timing_log("ruff PASS", "docstrings PASS")
    line = render_code_quality(
        log, {}, ["ruff", "docstrings"], pytest_line=None, pytest_verdict=None
    )
    assert line == "✅ pre-commit: 2 pass ❔ pytest"


def test_render_code_quality_orders_failed_warned_and_groups_unverified() -> None:
    """Code quality renders parts in order: FAIL, then WARN, then grouped unverified."""
    log = _timing_log("ruff PASS", "pip_audit WARN", "typecheck FAIL", "docs PASS")
    line = render_code_quality(
        log,
        {"ruff": "stale", "docs": "unstamped"},
        ["ruff", "pip_audit", "typecheck", "docs"],
        pytest_line=None,
        pytest_verdict=None,
    )
    assert line == (
        "❌ typecheck ⚠️ pip_audit ⚠️ not verified at this tree: ruff, docs ❔ pytest"
    )


def test_render_code_quality_treats_a_stale_warn_as_grouped_not_plain() -> None:
    """A WARN row with a stale verdict is grouped, not rendered as ``⚠️ <name>``."""
    log = _timing_log("pip_audit WARN")
    line = render_code_quality(
        log,
        {"pip_audit": "stale"},
        ["pip_audit"],
        pytest_line=None,
        pytest_verdict=None,
    )
    assert line == "⚠️ not verified at this tree: pip_audit ❔ pytest"


def test_render_code_quality_n_a_verdict_counts_as_verified() -> None:
    """An environment step's ``n/a`` freshness verdict counts as a verified pass."""
    log = _timing_log("env_sync PASS")
    line = render_code_quality(
        log, {"env_sync": "n/a"}, ["env_sync"], pytest_line=None, pytest_verdict=None
    )
    assert line == "✅ pre-commit: 1 pass ❔ pytest"


def test_render_code_quality_skip_row_is_entirely_omitted() -> None:
    """A SKIP row names nothing — not even a ❔ — and doesn't count as a pass."""
    log = _timing_log("doctest SKIP", "ruff PASS")
    line = render_code_quality(
        log, {}, ["doctest", "ruff"], pytest_line=None, pytest_verdict=None
    )
    assert "doctest" not in line
    assert line == "✅ pre-commit: 1 pass ❔ pytest"


def test_render_code_quality_reports_missing_step_before_pass_count() -> None:
    """A step with no row at all renders ❔, ordered before the pass count."""
    log = _timing_log("ruff PASS")
    line = render_code_quality(
        log, {}, ["ruff", "typecheck"], pytest_line=None, pytest_verdict=None
    )
    assert line == "❔ typecheck ✅ pre-commit: 1 pass ❔ pytest"


def test_render_code_quality_no_timing_log_is_unverified() -> None:
    """Absent ``precommit_timing.log`` renders a distinct unverified message."""
    line = render_code_quality(
        None, {}, ["ruff"], pytest_line=None, pytest_verdict=None
    )
    assert line == "❔ pre-commit unverified (no precommit_timing.log) ❔ pytest"


def test_render_code_quality_empty_log_and_no_expected_steps_is_unverified() -> None:
    """A present but rowless log with nothing expected still reports unverified."""
    line = render_code_quality("", {}, [], pytest_line=None, pytest_verdict=None)
    assert line == "❔ pre-commit unverified (no step rows) ❔ pytest"


@pytest.mark.parametrize(
    ("pytest_line", "pytest_verdict", "expected"),
    [
        (None, None, "❔ pytest"),
        ("3161 passed in 22.86s", None, "✅ pytest 3161 passed in 22.86s"),
        (
            "2 failed, 10 passed in 3.1s",
            None,
            "❌ pytest 2 failed, 10 passed in 3.1s",
        ),
        ("3 errors in 1.2s", None, "❌ pytest 3 errors in 1.2s"),
        (
            "3161 passed in 22.86s",
            "stale",
            "⚠️ pytest 3161 passed in 22.86s (not verified at this tree)",
        ),
    ],
)
def test_render_code_quality_pytest_part_covers_each_status(
    pytest_line: str | None, pytest_verdict: str | None, expected: str
) -> None:
    """The pytest status token reflects absence, pass, failure, and staleness.

    Args:
        pytest_line: pytest summary fragment fed to the renderer.
        pytest_verdict: Freshness verdict of the smart_test log.
        expected: The exact pytest-part suffix expected.
    """
    line = render_code_quality(
        None, {}, [], pytest_line=pytest_line, pytest_verdict=pytest_verdict
    )
    assert line.endswith(expected)


# ---------------------------------------------------------------------------
# pytest_summary_line
# ---------------------------------------------------------------------------


def test_pytest_summary_line_none_when_log_text_is_none() -> None:
    """``None`` input yields ``None`` — no smart_test.log to read."""
    assert pytest_summary_line(None) is None


def test_pytest_summary_line_none_when_no_summary_present() -> None:
    """Log text with no pytest summary fragment yields ``None``."""
    assert pytest_summary_line("nothing pytest-shaped in here\n") is None


def test_pytest_summary_line_extracts_the_fragment() -> None:
    """The summary fragment is extracted without its ``===`` padding."""
    text = "===== 3161 passed in 22.86s =====\n"
    assert pytest_summary_line(text) == "3161 passed in 22.86s"


def test_pytest_summary_line_last_match_wins_across_multiple_runs() -> None:
    """Two logged runs in one file → the LAST summary line is returned."""
    text = "1000 passed in 5.0s\n...\n2 failed, 10 passed in 3.1s\n"
    assert pytest_summary_line(text) == "2 failed, 10 passed in 3.1s"


# ---------------------------------------------------------------------------
# summarize_rollup
# ---------------------------------------------------------------------------


def test_summarize_rollup_empty_is_no_checks_reported() -> None:
    """No entries at all → the CI-not-yet-run message."""
    assert summarize_rollup([]) == "no checks reported"


def test_summarize_rollup_all_success_check_runs() -> None:
    """Every CheckRun completed and succeeded → the passed-count summary."""
    rollup = [
        {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    assert summarize_rollup(rollup) == "✅ passed (2 checks)"


def test_summarize_rollup_reports_running_check_names() -> None:
    """An incomplete CheckRun is named as running."""
    rollup = [{"name": "build", "status": "IN_PROGRESS", "conclusion": None}]
    assert summarize_rollup(rollup) == "⏳ running: build"


def test_summarize_rollup_failure_beats_running() -> None:
    """A failure outranks a simultaneous running check — failure wins."""
    rollup = [
        {"name": "build", "status": "IN_PROGRESS", "conclusion": None},
        {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert summarize_rollup(rollup) == "❌ failed: test"


def test_summarize_rollup_status_context_legacy_shapes() -> None:
    """StatusContext (pre-Checks-API) entries use ``context``/``state``.

    Not ``name``/``status`` — all three verdicts route through the same
    fields.
    """
    assert summarize_rollup([{"context": "ci/legacy", "state": "SUCCESS"}]) == (
        "✅ passed (1 checks)"
    )
    assert summarize_rollup([{"context": "ci/legacy", "state": "PENDING"}]) == (
        "⏳ running: ci/legacy"
    )
    assert summarize_rollup([{"context": "ci/legacy", "state": "ERROR"}]) == (
        "❌ failed: ci/legacy"
    )


# ---------------------------------------------------------------------------
# render_issue_management
# ---------------------------------------------------------------------------


def test_render_issue_management_lists_closing_refs() -> None:
    """Multiple refs are comma-joined, each with its own ``#``."""
    assert render_issue_management([1, 2], pr_body_checked=True) == "Closes #1, #2"


def test_render_issue_management_warns_when_none_found() -> None:
    """No closing keyword found → the warning line, naming the accepted forms."""
    text = render_issue_management([], pr_body_checked=True)
    assert text.startswith("⚠️ no closing keyword found")


def test_render_issue_management_notes_pr_body_not_yet_checked() -> None:
    """Before a PR exists, the line notes commit messages were the only source."""
    text = render_issue_management([7], pr_body_checked=False)
    assert text == "Closes #7 — commit messages only, PR body not yet available"


def test_render_issue_management_notes_absence_before_pr_body_checked() -> None:
    """The not-yet-available note appends even when nothing was found either."""
    text = render_issue_management([], pr_body_checked=False)
    assert text.startswith("⚠️ no closing keyword found")
    assert text.endswith("— commit messages only, PR body not yet available")


# ---------------------------------------------------------------------------
# unfilled_slots
# ---------------------------------------------------------------------------


def test_unfilled_slots_returns_names_in_document_order() -> None:
    """Slot names are returned in the order they appear in the text."""
    text = f"{slot('summary')}\nsome text\n{slot('findings: design-checker')}\n"
    assert unfilled_slots(text) == ["summary", "findings: design-checker"]


def test_unfilled_slots_empty_when_none_present() -> None:
    """A fully-authored text (no slot markers) yields an empty list."""
    assert unfilled_slots("nothing to fill here\n") == []


def test_unfilled_slots_reads_inside_fenced_blocks_too() -> None:
    """A slot hidden inside a fence is still caught.

    The scan runs on the raw text — unlike the budget/section checks,
    which strip fences first — so a slot can never hide from ``validate``
    inside quoted tool output.
    """
    text = f"```\n{slot('summary')}\n```\n"
    assert unfilled_slots(text) == ["summary"]


# ---------------------------------------------------------------------------
# evidence_fence
# ---------------------------------------------------------------------------


def test_evidence_fence_returns_from_the_first_fence_to_the_end() -> None:
    """Only the fenced part survives — the block's own ``##`` heading is dropped."""
    block = _gate_evidence_block("output line 1")
    fenced = evidence_fence(block)
    assert fenced.startswith("````")
    assert "## Provenance gates" not in fenced
    assert "output line 1" in fenced


def test_evidence_fence_empty_when_no_fence_present() -> None:
    """A block with no fence at all returns an empty string."""
    assert evidence_fence("## Heading\n\nNo fence here.\n") == ""
