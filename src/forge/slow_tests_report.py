"""forge-slow-tests-report — surface the slowest tests from a pytest run.

A pytest run invoked with ``--durations=N`` prints one or more
``slowest N durations`` sections to its output. When a suite runs in
several batches (e.g. tiered selection) each batch emits its own
section, so the slowest tests are scattered across the log and never
ranked together. This CLI parses every durations section out of a
saved pytest log (or stdin), merges them into a single ranking, and
prints the top-N slowest tests.

It is a read-only reporter: it never runs tests, never edits source,
and always exits ``0``. Slow + failing is exactly when the report is
most useful, so callers wire it with ``if: always()`` in CI. The same
report runs locally against ``code_health/pytest.log`` after a normal
``pytest`` invocation.

The durations flags themselves live once in ``[tool.pytest.ini_options]``
(``addopts``), so a bare local ``pytest`` and CI emit the same sections
this parser consumes — the flags are not repeated at each call site.

A committed duration **baseline** turns the one-shot report into a
cross-run regression signal: ``--update-baseline`` writes the parsed
durations as reviewable flat JSON at :data:`DEFAULT_BASELINE` (tracked,
NOT under the gitignored ``code_health/``), and ``--baseline`` compares
the current run against it, appending a regression block to the report.
The baseline is refreshed deliberately — a human runs
``--update-baseline`` in a dedicated ``chore(perf)`` PR once a slowdown
is confirmed intentional — never automatically, so silent drift cannot
hide real signal. Wall-clock comparisons stay WARN-shaped: shared
runners make timings non-reproducible, so this reporter never gates.

Usage:

- ``forge-slow-tests-report`` — parse ``code_health/pytest.log``.
- ``forge-slow-tests-report --log run.log --top 50`` — custom source / depth.
- ``pytest | forge-slow-tests-report --log -`` — parse piped stdin.
- ``forge-slow-tests-report --out code_health/slow_tests.log`` — also persist.
- ``forge-slow-tests-report --baseline`` — compare against the committed baseline.
- ``forge-slow-tests-report --update-baseline`` — rewrite the baseline (chore PR).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from forge.git_utils import configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


DEFAULT_LOG = Path("code_health") / "pytest.log"
DEFAULT_TOP = 25
# Committed at the repo root (pytest-split's `.test_durations` precedent)
# so the baseline is diffable in review — never under code_health/,
# which is gitignored and would make every update a silent no-op.
DEFAULT_BASELINE = Path(".forge-test-durations.json")
# A test regresses when it is at least this factor slower than its
# baseline AND slower than the floor — the floor keeps sub-second jitter
# from flagging, mirroring pytest's own --durations-min idea.
REGRESSION_FACTOR = 1.5
REGRESSION_FLOOR_SECONDS = 1.0

# A durations section header, e.g. "==== slowest 25 durations ====" or,
# under --durations=0, "==== slowest durations ====".
_SECTION_RE = re.compile(r"slowest\s+(?:\d+\s+)?durations", re.IGNORECASE)
# A pytest banner / separator line ("==== ... ===="). Ends a section.
# Anchored run-of-3+ only (no trailing `=+` arm) — avoids polynomial
# backtracking on a long run of '=' followed by a non-'=' character.
_SEPARATOR_RE = re.compile(r"^={3,}")
# A single duration entry: "1.23s call tests/test_x.py::test_y".
_ENTRY_RE = re.compile(r"^\s*(\d+\.\d+)s\s+(call|setup|teardown)\s+(.+?)\s*$")


@dataclass(frozen=True)
class Duration:
    """One test-phase timing parsed from a pytest durations section.

    Attributes:
        seconds: Wall-clock duration pytest reported for the phase.
        phase: The pytest phase — ``call``, ``setup``, or ``teardown``.
        nodeid: The test node id (``path::test`` or parametrized form).
    """

    seconds: float
    phase: str
    nodeid: str


def parse_durations(text: str) -> list[Duration]:
    """Extract and rank every durations entry in a pytest log.

    Scans for ``slowest ... durations`` section headers and collects the
    timing lines that follow each one until the next banner separator,
    so multiple sections (one per test batch) are all captured. When the
    same ``(nodeid, phase)`` appears in more than one section, the
    largest duration is kept — batches re-running a test should rank by
    its worst observed time, not double-count it.

    Args:
        text: The full pytest output to parse.

    Returns:
        Durations sorted slowest first. Empty when the log contains no
        durations section (``--durations`` not used, or no tests ran).
    """
    worst: dict[tuple[str, str], float] = {}
    in_section = False
    for line in text.splitlines():
        if _SECTION_RE.search(line):
            in_section = True
            continue
        if not in_section:
            continue
        entry = _ENTRY_RE.match(line)
        if entry:
            seconds, phase, nodeid = float(entry[1]), entry[2], entry[3]
            key = (nodeid, phase)
            worst[key] = max(worst.get(key, 0.0), seconds)
        elif _SEPARATOR_RE.match(line):
            in_section = False
    durations = [
        Duration(seconds=seconds, phase=phase, nodeid=nodeid)
        for (nodeid, phase), seconds in worst.items()
    ]
    durations.sort(key=lambda d: d.seconds, reverse=True)
    return durations


def format_report(durations: list[Duration], top: int) -> str:
    """Render a ranked durations table as plain text.

    Args:
        durations: Parsed durations, already sorted slowest first.
        top: Maximum number of rows to show.

    Returns:
        A multi-line report: a header line, then one aligned row per
        test, or a single "no timing data" line when nothing parsed.
    """
    if not durations:
        return "Slowest tests: no timing data found (run pytest with --durations)."
    shown = durations[:top]
    header = f"Slowest tests (top {len(shown)} of {len(durations)}):"
    rows = [f"  {d.seconds:8.2f}s  {d.phase:<8}  {d.nodeid}" for d in shown]
    return "\n".join([header, *rows])


def _baseline_key(duration: Duration) -> str:
    """Return *duration*'s flat JSON key (``nodeid::phase``).

    Args:
        duration: The parsed test-run duration record.

    Returns:
        A flat key in the format ``"nodeid::phase"`` for use in the baseline.
    """
    return f"{duration.nodeid}::{duration.phase}"


def save_baseline(durations: list[Duration], path: Path) -> None:
    """Write *durations* as the committed baseline JSON at *path*.

    Flat ``{"nodeid::phase": seconds}`` with sorted keys, so a baseline
    refresh produces a minimal, human-reviewable diff.

    Args:
        durations: Parsed durations to persist.
        path: Baseline file location (tracked, repo-relative).
    """
    data = {_baseline_key(d): d.seconds for d in durations}
    body = json.dumps(dict(sorted(data.items())), indent=2)
    path.write_text(body + "\n", encoding="utf-8")


def load_baseline(path: Path) -> dict[str, float]:
    """Load the baseline mapping from *path*.

    Args:
        path: Baseline file location.

    Returns:
        The ``{"nodeid::phase": seconds}`` mapping, or ``{}`` when the
        file is absent — a missing baseline means "nothing to compare",
        not an error, so first runs degrade gracefully.
    """
    if not path.is_file():
        logger.info("No duration baseline at %s — nothing to compare.", path)
        return {}
    return {k: float(v) for k, v in json.loads(path.read_text("utf-8")).items()}


def format_baseline_delta(durations: list[Duration], baseline: dict[str, float]) -> str:
    """Render the regression block comparing *durations* to *baseline*.

    A test counts as regressed when it is ``REGRESSION_FACTOR`` slower
    than its baseline entry and above ``REGRESSION_FLOOR_SECONDS``;
    a test above the floor with no baseline entry is reported as
    new-slow. WARN-shaped prose only — the caller never gates on it.

    Args:
        durations: Current parsed durations.
        baseline: The committed baseline mapping.

    Returns:
        A multi-line block, or an all-clear one-liner when nothing
        regressed.
    """
    regressed: list[str] = []
    new_slow: list[str] = []
    for d in durations:
        if d.seconds < REGRESSION_FLOOR_SECONDS:
            continue
        base = baseline.get(_baseline_key(d))
        if base is None:
            new_slow.append(f"  {d.seconds:8.2f}s  (new)      {d.phase:<8}  {d.nodeid}")
        elif d.seconds >= base * REGRESSION_FACTOR:
            regressed.append(
                f"  {d.seconds:8.2f}s  (was {base:.2f}s)  {d.phase:<8}  {d.nodeid}"
            )
    if not regressed and not new_slow:
        return (
            f"Baseline: no regressions (factor {REGRESSION_FACTOR:.1f}x, "
            f"floor {REGRESSION_FLOOR_SECONDS:.1f}s)."
        )
    lines = [
        (
            f"Baseline comparison (WARN only — factor {REGRESSION_FACTOR}x, "
            f"floor {REGRESSION_FLOOR_SECONDS}s):"
        )
    ]
    if regressed:
        lines.append(f"Regressed ({len(regressed)}):")
        lines += regressed
    if new_slow:
        lines.append(f"New slow tests ({len(new_slow)}):")
        lines += new_slow
    return "\n".join(lines)


def _read_source(log: str) -> str:
    """Read the pytest log from a file path or stdin.

    Args:
        log: A filesystem path, or ``-`` to read stdin.

    Returns:
        The log contents, or an empty string when the path is absent —
        a missing log is treated as "no timing data" rather than an
        error, since CI may report before any tests produced one.
    """
    # Trust model: the log is locally generated by pytest or a CI artifact
    # the repo owner controls — not attacker-supplied — so stdin is read
    # whole and no path-traversal guard is applied to the source path.
    if log == "-":
        return sys.stdin.read()
    path = Path(log)
    if not path.is_file():
        logger.info("No pytest log at %s — nothing to report.", path)
        return ""
    return path.read_text(encoding="utf-8")


def main() -> int:
    """Entry point for ``forge-slow-tests-report``.

    Returns:
        Always ``0`` — this is a non-gating reporter, never a quality
        gate that should fail a build.
    """
    parser = argparse.ArgumentParser(
        prog="forge-slow-tests-report",
        description=(
            "Parse pytest --durations sections from a log (or stdin) and "
            "print the slowest tests, merged across all batches."
        ),
    )
    parser.add_argument(
        "--log",
        default=str(DEFAULT_LOG),
        help=(
            "Path to the pytest log to parse, or '-' for stdin "
            f"(default: {DEFAULT_LOG})."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Number of slowest tests to show (default: {DEFAULT_TOP}).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Also write the report to this file (e.g. code_health/slow_tests.log).",
    )
    parser.add_argument(
        "--baseline",
        nargs="?",
        const=str(DEFAULT_BASELINE),
        default=None,
        help=(
            "Compare against a committed duration baseline and append a "
            f"WARN-shaped regression block (default path: {DEFAULT_BASELINE})."
        ),
    )
    parser.add_argument(
        "--update-baseline",
        nargs="?",
        const=str(DEFAULT_BASELINE),
        default=None,
        help=(
            "Rewrite the baseline from this run's durations — run "
            "deliberately, in a dedicated chore(perf) PR "
            f"(default path: {DEFAULT_BASELINE})."
        ),
    )
    args = parser.parse_args()

    durations = parse_durations(_read_source(args.log))
    report = format_report(durations, args.top)
    if args.baseline:
        delta = format_baseline_delta(durations, load_baseline(Path(args.baseline)))
        report = f"{report}\n\n{delta}"
    logger.info("%s", report)
    if args.update_baseline:
        save_baseline(durations, Path(args.update_baseline))
        logger.info("Baseline written to %s", args.update_baseline)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report + "\n", encoding="utf-8")
        logger.info("Report written to %s", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
