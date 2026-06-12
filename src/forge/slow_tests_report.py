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

Usage:

- ``forge-slow-tests-report`` — parse ``code_health/pytest.log``.
- ``forge-slow-tests-report --log run.log --top 50`` — custom source / depth.
- ``pytest | forge-slow-tests-report --log -`` — parse piped stdin.
- ``forge-slow-tests-report --out code_health/slow_tests.log`` — also persist.
"""

from __future__ import annotations

import argparse
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
    args = parser.parse_args()

    report = format_report(parse_durations(_read_source(args.log)), args.top)
    logger.info("%s", report)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report + "\n", encoding="utf-8")
        logger.info("Report written to %s", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
