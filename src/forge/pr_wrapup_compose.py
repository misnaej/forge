"""pr_wrapup_compose — render a PR wrap-up's mechanical parts, with slots for judgment.

A wrap-up (``code_health/pr_wrapup.md``) mixes two kinds of content. Most
of it follows from the tree, GitHub and the reporter reports: the
``verified-at:`` header, the mode line, which reporters were skipped, a
clean reporter's PASS line, the closing-keyword check, Code Quality and CI
Status. The rest is judgment: the summary, findings prose, the
recommendation. This module renders the first kind and marks the second
with ``<!-- forge:fill <name> -->`` slots, which ``forge-pr-wrapup
validate`` refuses until an author replaces each with its text.

Pure by design — no subprocess, no ``gh``, no filesystem. ``forge.pr_wrapup``
gathers the inputs, so every rendering rule is testable from text, and the
output is shaped to pass ``forge.pr_wrapup.validate_wrapup`` in every mode
once its slots are filled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from forge.pr_delta import VERIFIED_AT_RE


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


FILL_PREFIX: Final[str] = "<!-- forge:fill"
PENDING_CI: Final[str] = "pending — PR not yet published"

# Wrap-up section title → the forge-pr-plan reporter name that fills it.
REPORTER_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("Design Check", "design-checker"),
    ("Security Review", "security-checker"),
    ("Documentation Check", "docs-types-checker"),
)

_NOT_VERIFIED: Final[frozenset[str]] = frozenset({"stale", "unstamped", "unknown"})
_FAILED_CONCLUSIONS: Final[frozenset[str]] = frozenset(
    {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)
_FAILED_STATES: Final[frozenset[str]] = frozenset({"FAILURE", "ERROR"})
_PENDING_STATES: Final[frozenset[str]] = frozenset({"PENDING", "EXPECTED"})
_TIMING_ROW_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>\S+)\s+[\d.]+s\s+(?P<marker>SKIP|PASS|WARN|FAIL)\s*$"
)
_PYTEST_SUMMARY_RE: Final[re.Pattern[str]] = re.compile(
    r"\d+ (?:passed|failed|errors?)\b[^\n]*? in [\d.]+s"
)
_SLOT_RE: Final[re.Pattern[str]] = re.compile(r"<!-- forge:fill (?P<name>.*?) -->")


class ComposeError(ValueError):
    """Raised when the inputs cannot yield an honest wrap-up (evidence missing)."""


def slot(name: str) -> str:
    """Return the fill-in marker line for slot *name*.

    Args:
        name: What the author writes there (e.g. ``summary``).

    Returns:
        The ``<!-- forge:fill <name> -->`` line.
    """
    return f"{FILL_PREFIX} {name} -->"


@dataclass(frozen=True)
class ComposeInputs:
    """Everything a wrap-up is rendered from, gathered by the caller.

    Attributes:
        head_sha: Short HEAD SHA the wrap-up verifies.
        mode: ``forge-pr-plan`` mode (full, light-docs, light-regen,
            light-code, delta).
        reasons: ``forge-pr-plan`` reasons, in order.
        reporters: Reporter names the plan requires.
        reports: Reporter name → full report text.
        prior_art_report: The ``forge:prior-art`` report, when one exists.
        added_non_fragment_paths: Added files other than changelog
            fragments (a non-empty tuple requires a prior-art report).
        issue_management: Rendered Issue Management text.
        code_quality: Rendered one-line Code Quality status.
        ci_status: Rendered one-line CI Status.
        emergency_ledger: Ledger issue of an armed ``forge-emergency``.
        delta_prior_sha: The earlier wrap-up's SHA on a delta refresh.
        light_regen_evidence: Fenced provenance-gate output for light-regen.
    """

    head_sha: str
    mode: str
    reasons: tuple[str, ...]
    reporters: tuple[str, ...]
    reports: Mapping[str, str]
    prior_art_report: str | None
    added_non_fragment_paths: tuple[str, ...]
    issue_management: str
    code_quality: str
    ci_status: str = PENDING_CI
    emergency_ledger: int | None = None
    delta_prior_sha: str | None = None
    light_regen_evidence: str | None = None


def _report_line(report_text: str, head_sha: str) -> str | None:
    """Return a clean report's PASS line, or ``None`` when it has findings.

    Args:
        report_text: Full reporter report.
        head_sha: The wrap-up's HEAD SHA.

    Returns:
        The PASS line (naming the report's own SHA when it differs from
        HEAD), or ``None``.
    """
    lines = [line.strip() for line in report_text.splitlines() if line.strip()]
    report_sha = None
    if lines and (match := VERIFIED_AT_RE.match(lines[0])):
        report_sha = match.group("sha")
        lines = lines[1:]
    if not lines or not lines[0].lower().startswith("pass"):
        return None
    if report_sha and not (
        head_sha.startswith(report_sha) or report_sha.startswith(head_sha)
    ):
        return f"{lines[0]} (verified-at {report_sha})"
    return lines[0]


def _reporter_body(reporter: str, inputs: ComposeInputs) -> list[str]:
    """Render one reporter section's body lines.

    Args:
        reporter: Reporter name (e.g. ``design-checker``).
        inputs: Compose inputs.

    Returns:
        The section body, one line unless light-regen evidence follows.
    """
    if inputs.emergency_ledger is not None:
        return [f"SKIPPED (emergency: ledger #{inputs.emergency_ledger})"]
    if inputs.mode == "delta":
        return [f"PASS — unchanged since {inputs.delta_prior_sha or '?'} (delta)"]
    if reporter not in inputs.reporters:
        body = [f"SKIPPED ({inputs.mode})"]
        if (
            reporter == "docs-types-checker"
            and inputs.mode == "light-regen"
            and inputs.light_regen_evidence
        ):
            body += ["", inputs.light_regen_evidence.rstrip("\n")]
        return body
    line = _report_line(inputs.reports[reporter], inputs.head_sha)
    return [line] if line is not None else [slot(f"findings: {reporter}")]


def _recommendation(inputs: ComposeInputs) -> str:
    """Render the one-line Recommendation, or its slot.

    Args:
        inputs: Compose inputs.

    Returns:
        The mechanical recommendation for light-code and emergency; a slot
        for every mode where the author judges.
    """
    if inputs.emergency_ledger is not None:
        return (
            f"emergency: ledger #{inputs.emergency_ledger}; "
            "verification owed after delivery"
        )
    if inputs.mode == "light-code":
        return "; ".join(inputs.reasons) or "light-code"
    return slot("recommendation")


def _check_evidence(inputs: ComposeInputs) -> None:
    """Refuse inputs that would make the wrap-up claim unverified evidence.

    Args:
        inputs: Compose inputs.

    Raises:
        ComposeError: When a plan-required reporter has no report, or files
            were added without a prior-art report.
    """
    if inputs.emergency_ledger is not None:
        return
    if inputs.mode != "delta":
        missing = [r for r in inputs.reporters if r not in inputs.reports]
        if missing:
            msg = f"missing report for required reporter(s): {', '.join(missing)}"
            raise ComposeError(msg)
    if inputs.added_non_fragment_paths and inputs.prior_art_report is None:
        msg = (
            "the diff adds files ("
            + ", ".join(inputs.added_non_fragment_paths)
            + ") but no prior-art report was given (--prior-art)"
        )
        raise ComposeError(msg)


def render_wrapup(inputs: ComposeInputs) -> str:
    """Render the wrap-up markdown with fill-in slots for judgment.

    Args:
        inputs: Compose inputs.

    Returns:
        The wrap-up text; it passes ``validate_wrapup`` once every slot line
        is replaced by one line of text.

    Raises:
        ComposeError: When required evidence is missing (see
            :func:`_check_evidence`), or the prior-art report has no
            ``prior-art-searched:`` line.
    """
    _check_evidence(inputs)
    out = [f"verified-at: {inputs.head_sha}"]
    if inputs.emergency_ledger is not None:
        out.append("wrapup-mode: emergency")
    elif inputs.mode == "light-code":
        out.append("wrapup-mode: light")
    if inputs.prior_art_report is not None:
        prior = next(
            (
                line.strip()
                for line in inputs.prior_art_report.splitlines()
                if line.strip().startswith("prior-art-searched:")
            ),
            None,
        )
        if prior is None:
            msg = "the prior-art report has no `prior-art-searched:` line"
            raise ComposeError(msg)
        out.append(prior)
    out += ["", slot("summary"), ""]
    for title, reporter in REPORTER_SECTIONS:
        out += [f"## {title}", "", *_reporter_body(reporter, inputs), ""]
    out += ["## Issue Management", "", inputs.issue_management, ""]
    out += ["## Code Quality", "", inputs.code_quality, ""]
    out += ["## CI Status", "", inputs.ci_status, ""]
    out += ["## Recommendation", "", _recommendation(inputs), ""]
    return "\n".join(out)


def _code_quality_rows(
    timing_log: str, verdicts: Mapping[str, str]
) -> tuple[list[str], set[str], int]:
    """Classify the timing log's step rows for the Code Quality line.

    Steps whose logs are not verified at this tree are grouped into one
    part: after an edit every log goes stale at once, and naming each as
    its own part would blow the wrap-up's word budget for one cause.

    Args:
        timing_log: ``code_health/precommit_timing.log`` text.
        verdicts: Log name → ``forge-precommit --freshness`` verdict.

    Returns:
        ``(exception_parts, seen_step_names, verified_pass_count)`` —
        failures first, then warnings, then the not-verified group.
    """
    failed: list[str] = []
    warned: list[str] = []
    unverified: list[str] = []
    seen: set[str] = set()
    passed = 0
    for line in timing_log.splitlines():
        match = _TIMING_ROW_RE.match(line.strip())
        if match is None:
            continue
        name, marker = match.group("name"), match.group("marker")
        seen.add(name)
        if marker == "SKIP":
            continue
        if marker == "FAIL":
            failed.append(f"❌ {name}")
        elif verdicts.get(name) in _NOT_VERIFIED:
            unverified.append(name)
        elif marker == "WARN":
            warned.append(f"⚠️ {name}")
        else:
            passed += 1
    parts = failed + warned
    if unverified:
        parts.append(f"⚠️ not verified at this tree: {', '.join(unverified)}")
    return parts, seen, passed


def _pytest_part(pytest_line: str | None, pytest_verdict: str | None) -> str:
    """Render the pytest part of the Code Quality line.

    Args:
        pytest_line: The pytest summary fragment, if any.
        pytest_verdict: Freshness verdict of the pytest log.

    Returns:
        The pytest status with its symbol.
    """
    if pytest_line is None:
        return "❔ pytest"
    if "failed" in pytest_line or "error" in pytest_line:
        return f"❌ pytest {pytest_line}"
    if pytest_verdict in _NOT_VERIFIED:
        return f"⚠️ pytest {pytest_line} (not verified at this tree)"
    return f"✅ pytest {pytest_line}"


def render_code_quality(
    timing_log: str | None,
    verdicts: Mapping[str, str],
    expected_steps: Sequence[str],
    *,
    pytest_line: str | None,
    pytest_verdict: str | None,
) -> str:
    """Render the one-line Code Quality status, by exception.

    Only failures, warnings, unverified and missing steps are named; the
    rest collapse into a pass count, so a clean run stays within the
    wrap-up's word budget.

    Args:
        timing_log: ``code_health/precommit_timing.log`` text, or ``None``.
        verdicts: Log name → ``forge-precommit --freshness`` verdict.
        expected_steps: Steps a full run executes; one absent from the
            timing log is reported as unverified.
        pytest_line: pytest summary fragment from ``smart_test.log``.
        pytest_verdict: Freshness verdict of ``smart_test.log``.

    Returns:
        One line of space-joined status parts.
    """
    parts: list[str] = []
    if timing_log is None:
        parts.append("❔ pre-commit unverified (no precommit_timing.log)")
    else:
        exceptions, seen, passed = _code_quality_rows(timing_log, verdicts)
        parts += exceptions
        parts += [f"❔ {name}" for name in expected_steps if name not in seen]
        if passed:
            parts.append(f"✅ pre-commit: {passed} pass")
        if not seen and not expected_steps:
            parts.append("❔ pre-commit unverified (no step rows)")
    parts.append(_pytest_part(pytest_line, pytest_verdict))
    return " ".join(parts)


def pytest_summary_line(log_text: str | None) -> str | None:
    """Return the last pytest summary fragment in *log_text*.

    Args:
        log_text: ``code_health/smart_test.log`` text, or ``None``.

    Returns:
        E.g. ``3161 passed in 22.86s``, or ``None`` when absent.
    """
    if not log_text:
        return None
    matches = _PYTEST_SUMMARY_RE.findall(log_text)
    return matches[-1] if matches else None


def _entry_name(entry: Mapping[str, object]) -> str:
    """Return a rollup entry's display name.

    Args:
        entry: One ``statusCheckRollup`` entry.

    Returns:
        The check-run name or status-context name.
    """
    return str(entry.get("name") or entry.get("context") or "?")


def summarize_rollup(rollup: Sequence[Mapping[str, object]]) -> str:
    """Summarize ``gh pr view --json statusCheckRollup`` as one status line.

    Args:
        rollup: ``CheckRun`` / ``StatusContext`` entries.

    Returns:
        ``no checks reported``, ``❌ failed: …``, ``⏳ running: …`` or
        ``✅ passed (<n> checks)`` — a failure outranks a running check.
    """
    if not rollup:
        return "no checks reported"
    failed = [
        _entry_name(e)
        for e in rollup
        if str(e.get("conclusion") or "") in _FAILED_CONCLUSIONS
        or str(e.get("state") or "") in _FAILED_STATES
    ]
    if failed:
        return f"❌ failed: {', '.join(failed)}"
    running = [
        _entry_name(e)
        for e in rollup
        if ("status" in e and str(e.get("status")) != "COMPLETED")
        or str(e.get("state") or "") in _PENDING_STATES
    ]
    if running:
        return f"⏳ running: {', '.join(running)}"
    return f"✅ passed ({len(rollup)} checks)"


def render_issue_management(
    closing_refs: Sequence[int], *, pr_body_checked: bool
) -> str:
    """Render the Issue Management line from the closing keywords found.

    Args:
        closing_refs: Issue numbers the PR would close.
        pr_body_checked: Whether the PR body was part of the search.

    Returns:
        ``Closes #…`` or a warning when none was found; noted when only
        commit messages could be searched.
    """
    if closing_refs:
        text = "Closes " + ", ".join(f"#{ref}" for ref in closing_refs)
    else:
        text = "⚠️ no closing keyword found (Closes/Fixes/Resolves #N on its own line)"
    if not pr_body_checked:
        text += " — commit messages only, PR body not yet available"
    return text


def unfilled_slots(text: str) -> list[str]:
    """Return the names of every fill-in slot still in *text*.

    Scans the raw text — fenced blocks included — so a slot can never
    hide inside a fence.

    Args:
        text: Wrap-up markdown.

    Returns:
        Slot names in document order.
    """
    return [
        match.group("name")
        for line in text.splitlines()
        if FILL_PREFIX in line
        for match in _SLOT_RE.finditer(line)
    ]


def evidence_fence(block: str) -> str:
    """Return only the fenced part of a ``run_gate_evidence`` block.

    The block opens with its own ``##`` heading, which would become an
    extra wrap-up section; the fence alone is the evidence.

    Args:
        block: Markdown returned by ``git_utils.run_gate_evidence``.

    Returns:
        From the first fence line to the end, or ``""`` without a fence.
    """
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("```"):
            return "\n".join(lines[i:])
    return ""
