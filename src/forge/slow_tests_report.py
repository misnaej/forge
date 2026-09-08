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
most useful, so callers wire it with ``if: always()`` in CI. Locally,
``code_health/pytest.log`` is produced by ``forge-smart-test`` (which
writes it alongside its own log) or by a manually tee'd ``pytest`` run
— run one of those first, then the no-argument report just works.

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
- ``forge-slow-tests-report --coverage-json coverage.json`` — rank by worth.

**Ranking by worth** (``--coverage-json``) answers the question seconds
alone cannot: *is this test worth what it costs?* Given a ``coverage json
--show-contexts`` export it reports **unique covered statements per
second** per test function — statements no other test covers, over the
test's summed wall time. A slow test with high unique coverage is kept
and its inputs shrunk; a slow test whose coverage is entirely duplicated
is a deletion candidate. Ratios rather than seconds: coverage shares are
machine-invariant where wall-clock is not.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from forge.config import resolve_tool_roots
from forge.git_utils import configure_cli_logging, repo_root
from forge.smart_test.coverage import load_export


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
# Below this summed duration a per-second ratio is noise, not signal —
# dividing by it would rank rounding error above real cost.
WORTH_FLOOR_SECONDS = 0.01
# pytest-cov records a dynamic context as `<nodeid>|<when>`, where `when`
# is setup / run / teardown — `run`, note, not the `call` that pytest's
# own --durations output uses. Both sides of the join must therefore be
# normalized through `nodeid_base` or the join silently finds nothing.
_CONTEXT_PHASE_SEP = "|"
# pytest caps its durations section unless run with `--durations=0`:
# a numbered header, or the "N durations < X s hidden" trailer.
_TRUNCATION_RE = re.compile(
    r"slowest\s+\d+\s+durations|durations?\s*<\s*[\d.]+s\s+hidden",
    re.IGNORECASE,
)

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


def load_baseline(path: Path) -> dict[str, float] | None:
    """Load the baseline mapping from *path*.

    ``None`` and ``{}`` are different answers, and the distinction is the
    whole point: ``None`` means there is no usable baseline at all, so
    there is nothing to compare against, while ``{}`` is a real, empty
    baseline someone committed — every slow test genuinely is new
    against it. Collapsing the two made a repo that had deliberately not
    adopted a baseline read its entire suite back as "new slow".

    Args:
        path: Baseline file location.

    Returns:
        The ``{"nodeid::phase": seconds}`` mapping, or ``None`` when the
        file is absent or malformed — never an error: this reporter's
        always-exit-0 contract must hold even against a corrupted
        committed baseline (bad merge, hand-edit), especially under CI's
        ``if: always()``.
    """
    if not path.is_file():
        logger.info("No duration baseline at %s — nothing to compare.", path)
        return None
    try:
        return {k: float(v) for k, v in json.loads(path.read_text("utf-8")).items()}
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        logger.warning(
            "Baseline at %s is malformed — ignoring it (regenerate with "
            "--update-baseline).",
            path,
        )
        return None


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


def nodeid_base(nodeid: str) -> str:
    """Collapse a node id to the test function that owns it.

    Two suffixes stand between a raw id and the function: pytest-cov's
    context phase (``|run``) and the parametrize bracket
    (``[case-1]``). Both are stripped, in that order — a parametrize id
    may itself contain a ``[``, so the bracket split takes the FIRST
    one. Variants must collapse before uniqueness is computed: sibling
    variants cover nearly the same lines, so per-variant uniqueness is
    ~0 for all of them and the ranking would advise deleting every one.

    Args:
        nodeid: A pytest node id or a coverage context string.

    Returns:
        ``path::test_function`` with phase and parameters removed.
    """
    head = nodeid.split(_CONTEXT_PHASE_SEP, 1)[0]
    return head.split("[", 1)[0]


def seconds_by_base(durations: list[Duration]) -> dict[str, float]:
    """Sum every phase and every parametrized variant per test function.

    Deliberately a different aggregation from :func:`parse_durations`,
    which keeps the max across duplicate entries: the cost of a test
    function is what the whole suite pays for it, so setup, call,
    teardown and all variants add up.

    Args:
        durations: Parsed durations from the pytest log.

    Returns:
        ``{base nodeid: summed seconds}``.
    """
    totals: dict[str, float] = {}
    for d in durations:
        base = nodeid_base(d.nodeid)
        totals[base] = totals.get(base, 0.0) + d.seconds
    return totals


def _in_source_roots(filename: str, source_roots: list[str]) -> bool:
    """Return whether a coverage file entry lies under a source root.

    Args:
        filename: A ``files`` key from the coverage export.
        source_roots: Repo-relative source roots to scope to.

    Returns:
        ``True`` when the path sits under one of *source_roots*. An
        empty root list scopes to everything — a repo whose layout
        forge cannot resolve still gets a ranking rather than silence.
    """
    if not source_roots:
        return True
    posix = Path(filename).as_posix()
    return any(
        posix.startswith(f"{root}/") or f"/{root}/" in posix for root in source_roots
    )


def unique_statements(
    data: dict[str, object], source_roots: list[str]
) -> tuple[dict[str, int], set[str]]:
    """Count statements covered by exactly one test function.

    Args:
        data: A parsed ``coverage json --show-contexts`` document.
        source_roots: Repo-relative source roots to scope the statement
            universe to, so boilerplate in test files and other
            instrumented paths cannot pad a test's unique count.

    Returns:
        ``({base nodeid: unique statement count}, every base nodeid the
        export attributes any line to)``. The second element separates
        "covers nothing uniquely" from "this run knew nothing about the
        test" — a zero that means data, from a zero that means silence.
        It is deliberately gathered from the WHOLE export, scoping or
        not: a test whose only lines fall outside *source_roots* has a
        real verdict (nothing unique in scope), not a data gap.
    """
    unique: dict[str, int] = {}
    seen: set[str] = set()
    files = data.get("files")
    for fname, info in (files if isinstance(files, dict) else {}).items():
        if not isinstance(info, dict):
            continue
        in_scope = _in_source_roots(str(fname), source_roots)
        contexts = info.get("contexts")
        for ctx_list in (contexts if isinstance(contexts, dict) else {}).values():
            # A line records one context per phase, so the same test can
            # appear twice; collapse to identities before asking "one?".
            owners = {nodeid_base(ctx) for ctx in ctx_list if ctx}
            seen |= owners
            if in_scope and len(owners) == 1:
                owner = next(iter(owners))
                unique[owner] = unique.get(owner, 0) + 1
    return unique, seen


def _worth_row(base: str, seconds: float, unique: int | None) -> tuple[float, str]:
    """Render one ranking row and the key it sorts on.

    Args:
        base: The test function's node id.
        seconds: Summed seconds across its phases and variants.
        unique: Statements only this test covers, or ``None`` when the
            export knows nothing about it.

    Returns:
        ``(sort key, rendered row)`` — lowest worth first, and rows with
        no coverage data sort last (they are a data gap, not a verdict).
    """
    if unique is None:
        return (
            float("inf"),
            f"  {seconds:8.2f}s  {'—':>9}  (no coverage data)  {base}",
        )
    if seconds < WORTH_FLOOR_SECONDS:
        return (
            float("inf"),
            f"  {seconds:8.2f}s  {unique:6d} uniq  (too fast to rank)  {base}",
        )
    per_second = unique / seconds
    return (
        per_second,
        f"  {seconds:8.2f}s  {unique:6d} uniq  {per_second:8.2f} uniq/s  {base}",
    )


def format_coverage_ranking(
    durations: list[Duration],
    data: dict[str, object],
    source_roots: list[str],
    *,
    top: int,
    truncated: bool,
) -> str:
    """Rank test functions by unique covered statements per second.

    Lowest worth first: the top of this block is where deleting or
    shrinking a test costs the suite least. Zero unique statements is
    the most actionable row there is, not a row to filter out.

    Args:
        durations: Parsed durations from the pytest log.
        data: A parsed ``coverage json --show-contexts`` document.
        source_roots: Repo-relative source roots to scope statements to.
        top: Maximum rows to render.
        truncated: Whether pytest capped the durations section, making
            every summed duration a lower bound.

    Returns:
        A multi-line block, or a one-line skip notice when the export
        carries no per-test contexts to rank by.
    """
    unique, seen = unique_statements(data, source_roots)
    if not seen:
        return (
            "Coverage worth: the export records no per-test contexts — "
            "rerun pytest with --cov-context=test and export with "
            "`coverage json --show-contexts` to rank by worth."
        )
    rows = [
        _worth_row(base, seconds, unique.get(base, 0) if base in seen else None)
        for base, seconds in seconds_by_base(durations).items()
    ]
    if not rows:
        return "Coverage worth: no timing data to rank (run pytest with --durations)."
    ranked = [row for _, row in sorted(rows, key=lambda pair: pair[0])][:top]
    header = [
        (
            f"Coverage worth (lowest first, top {len(ranked)} of {len(rows)}) — "
            "unique covered statements per second:"
        )
    ]
    if truncated:
        header.append(
            "  NOTE: pytest capped the durations section, so these seconds are "
            "lower bounds — rerun with --durations=0 for an honest ranking."
        )
    return "\n".join([*header, *ranked])


def durations_truncated(text: str) -> bool:
    """Return whether pytest capped the durations section in *text*.

    Args:
        text: The raw pytest log.

    Returns:
        ``True`` when a numbered section header or a "durations hidden"
        trailer shows the list is partial — which makes any per-test
        sum a lower bound rather than the real cost.
    """
    return bool(_TRUNCATION_RE.search(text))


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
    parser.add_argument(
        "--coverage-json",
        default=None,
        metavar="PATH",
        help=(
            "Rank tests by unique covered statements per second, from a "
            "`coverage json --show-contexts` export (record it with "
            "pytest --cov-context=test)."
        ),
    )
    args = parser.parse_args()

    source = _read_source(args.log)
    durations = parse_durations(source)
    blocks = [format_report(durations, args.top)]
    if args.baseline:
        baseline = load_baseline(Path(args.baseline))
        blocks.append(
            f"Baseline: no usable baseline at {args.baseline} — comparison skipped."
            if baseline is None
            else format_baseline_delta(durations, baseline)
        )
    if args.coverage_json:
        data = load_export(Path(args.coverage_json))
        blocks.append(
            f"Coverage worth: no usable coverage export at {args.coverage_json} "
            "— ranking skipped."
            if data is None
            else format_coverage_ranking(
                durations,
                data,
                resolve_tool_roots(repo_root(), "slow_tests_report"),
                top=args.top,
                truncated=durations_truncated(source),
            )
        )
    report = "\n\n".join(blocks)
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
