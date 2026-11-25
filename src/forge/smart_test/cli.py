"""forge-smart-test — change-driven test runner with depth tiers.

Selects the tests a change set affects (via the import graph) and runs
them in escalating depth batches with fail-fast: depth 0 (tests importing
a changed module directly), depth 1 (one hop removed), depth 2 (two hops),
or ``full`` (the entire suite, with coverage). Lower depths must pass
before higher ones run, keeping the feedback loop tight. Writes the
run output to two sinks: ``code_health/smart_test.log`` for
``forge-precommit-fixer`` (FOUNDATION §13) and ``code_health/pytest.log``
so ``forge-slow-tests-report``'s no-argument default works after any
smart-test run. Both are stamped when the run starts, grow tier by tier,
end with a ``# complete:`` line, and are written by one run at a time
(:mod:`forge.smart_test.run_log`).

Usage:

- ``forge-smart-test`` — depth 1 (default)
- ``forge-smart-test --depth 0`` — only directly-affected tests
- ``forge-smart-test --depth 2`` — two-hop dependents
- ``forge-smart-test --depth full`` — whole suite + coverage
- ``forge-smart-test --show-files --depth N`` — print the plan, run nothing
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from forge import config as _config
from forge.git_utils import code_health_dir, configure_cli_logging
from forge.ledger import parse_ledger
from forge.run_context import is_ci
from forge.smart_test import coverage as cov_stage
from forge.smart_test import lifecycle
from forge.smart_test.dependencies import (
    SelectionPlan,
    all_test_files,
    render_plan,
    select_tests,
    unscanned_conftests,
)
from forge.smart_test.git_helpers import (
    changed_non_python_files,
    changed_python_files,
    effective_base_ref,
    head_commit_message,
    resolve_base_ref,
)
from forge.smart_test.run_log import (
    LockHeldError,
    RunLog,
    acquire_lock,
    release_lock,
)
from forge.smart_test.runner import clear_python_cache, run_pytest


if TYPE_CHECKING:
    from collections.abc import Callable


configure_cli_logging()
logger = logging.getLogger(__name__)

_FULL = "full"
_DEPTH_CHOICES = ("0", "1", "2", _FULL)
_LOG_NAME = "smart_test.log"
_PYTEST_LOG_NAME = "pytest.log"
# Default CI directive: [depth-N] or [full] anywhere in the commit message.
# Override via [tool.forge.smart_test].commit_directive_re.
_DEPTH_DIRECTIVE_RE = r"\[(?:depth-(?P<n>[0-2])|(?P<full>full))\]"


def _smart_test_config(repo_root: Path) -> dict[str, object]:
    """Return the ``[tool.forge.smart_test]`` table, or ``{}`` when absent.

    Args:
        repo_root: Git repo root.

    Returns:
        The ``[tool.forge.smart_test]`` subsection dict, or ``{}`` when absent.
    """
    return _config.read_tool_forge_section(repo_root, "smart_test")


def _depth_from_commit(repo_root: Path, cfg: dict[str, object]) -> str | None:
    """Read a depth directive from ``HEAD``'s commit message, if present.

    Matches ``[depth-N]`` / ``[full]`` (or a consumer regex from
    ``commit_directive_re``) and maps it to a ``--depth`` token. Lets CI
    drive the tier from the commit without re-implementing the grep.

    Args:
        repo_root: Git repo root.
        cfg: The ``[tool.forge.smart_test]`` table (for ``commit_directive_re``).

    Returns:
        A depth token (``"0"``/``"1"``/``"2"``/``"full"``), or ``None`` when
        the message carries no directive.
    """
    pattern = cfg.get("commit_directive_re")
    regex = pattern if isinstance(pattern, str) else _DEPTH_DIRECTIVE_RE
    match = re.search(regex, head_commit_message(repo_root), re.IGNORECASE)
    if not match:
        return None
    groups = match.groupdict()
    if groups.get("full"):
        return _FULL
    return groups.get("n")


def _parse_depth(raw: str) -> int | str:
    """Map a ``--depth`` token to an int tier or the ``full`` sentinel.

    Args:
        raw: One of ``0``, ``1``, ``2``, ``full``.

    Returns:
        The integer tier, or :data:`_FULL` for ``full``.
    """
    if raw == _FULL:
        return _FULL
    return int(raw)


def _with_run_log(
    repo_root: Path, header: str, run: Callable[[RunLog], tuple[int, str]]
) -> int:
    """Run *run* holding the log lock, with the log stamped up front.

    The stamp names the tree the tests are about to run against; *run*
    appends each tier as it finishes; the ``# complete:`` line goes last,
    so an interrupted run leaves a log readers treat as unknown.

    Args:
        repo_root: Git repo root.
        header: First body text of the log.
        run: The test run; receives the log, returns ``(exit_code,
            output)``. The output is shown by *run* as it happens, so it
            is returned here only for the log, not re-printed.

    Returns:
        The run's exit code, or ``1`` when another live run holds the
        lock (nothing is run or written then).
    """
    try:
        lock = acquire_lock(repo_root)
    except LockHeldError as exc:
        sys.stderr.write(f"forge-smart-test: {exc}\n")
        return 1
    try:
        log = RunLog.for_repo(repo_root, (_LOG_NAME, _PYTEST_LOG_NAME))
        log.start(repo_root, header)
        code, _output = run(log)
        log.complete("passed" if code == 0 else f"failed (exit {code})")
        return code
    finally:
        release_lock(lock)


def _run_full(
    repo_root: Path,
    cfg: dict[str, object],
    changed: set[str],
    *,
    all_tests: bool = False,
    telemetry: bool = False,
) -> tuple[int, str]:
    """Run the ``full`` tier with lifecycle deselection and metrics.

    Coverage is unconditionally enabled for ``full`` — it is the tier's
    defining cost/coverage trade-off — so there is no opt-out parameter.
    Ordinary full runs deselect stale development-marked files (the
    lifecycle rule, always reported). Two things disable that and execute
    truly everything: an explicit ``all_tests``, and running in CI.

    CI is unconditional because CI *is* the guarantee that makes skipping
    safe anywhere else: a job that quietly deselected would leave the
    skipped tests unrun by anyone, while still reporting green. A
    ``--depth full`` job therefore needs no extra flag to be truly all.

    After the run a record-only metrics line (including the depth-2
    differential check) is appended to the history ledger.

    Args:
        repo_root: Git repo root.
        cfg: The ``[tool.forge.smart_test]`` table.
        changed: Current change set (lifecycle re-inclusion trigger and
            differential input).
        all_tests: Run truly everything — lifecycle deselection off. CI
            forces this on regardless of the flag.
        telemetry: Sample resource usage during the run.

    Returns:
        ``(exit_code, output)`` from the single pytest run.
    """
    tests = all_test_files(repo_root)
    dev_files = lifecycle.development_marked_files(repo_root, tests)
    skip_days_raw = cfg.get("lifecycle_skip_days", lifecycle.DEFAULT_SKIP_DAYS)
    skip_days = (
        float(skip_days_raw)
        if isinstance(skip_days_raw, (int, float))
        else lifecycle.DEFAULT_SKIP_DAYS
    )
    truly_all = all_tests or is_ci()
    skippable: set[str] = set()
    if not truly_all:
        skippable = lifecycle.lifecycle_skippable(
            repo_root, tests, changed, skip_days=skip_days
        )
    label = "all" if truly_all else "full"
    if truly_all and not all_tests:
        logger.info("CI detected: lifecycle deselection off, running truly everything.")
    logger.info("Running the full suite (depth=%s) with coverage.", label)
    batch = sorted(tests - skippable) if skippable else []
    started = time.monotonic()
    code, out = run_pytest(
        repo_root,
        batch,
        coverage=True,
        telemetry_label=label if telemetry else None,
        on_output=_show,
    )
    wall_s = time.monotonic() - started
    if skippable:
        # Shown as well as recorded: these lines are produced after the
        # child exits, so streaming the child's output does not carry
        # them, and the full run is the one a reader waits longest for.
        tail = (
            f"\nlifecycle-skipped: {len(skippable)} development file(s) "
            f"untouched >={skip_days:g}d (run --all-tests to include)\n"
        )
        out += tail
        _show(tail)
    mismatches: set[str] = set()
    if changed:
        plan = select_tests(repo_root, changed, 2)
        selected = set(plan.tests_up_to(2))
        mismatches = lifecycle.failed_files(out) - selected - skippable
        if mismatches:
            tail = (
                f"differential: {len(mismatches)} failing file(s) outside "
                f"depth-2 selection (record-only): "
                + ", ".join(sorted(mismatches))
                + "\n"
            )
            out += tail
            _show(tail)
    metrics = lifecycle.RunMetrics(
        label=label,
        wall_s=wall_s,
        total_files=len(tests),
        dev_files=len(dev_files),
        lifecycle_skipped=len(skippable),
        differential_mismatches=len(mismatches),
    )
    lifecycle.append_history(repo_root, metrics)
    return code, out


@dataclass
class _RunConfig:
    """Configuration for a tiered test run."""

    coverage: bool
    """Whether to instrument coverage (off by default per tier)."""
    extra_depth0: set[str]
    """Coverage-derived tests to union into the depth-0 batch."""
    header: str
    """One-line run header recorded at the top of the log."""
    notice: str
    """Widening warning, or empty. Separate from ``header`` because the
    header is printed for every tier while this is printed only when a
    tier turns out to run nearly the whole suite."""
    telemetry: bool = False
    """Whether to sample resource usage during each pytest batch."""


#: Fraction of the suite a tier has to reach before it is called a
#: widening. Fixed rather than configurable: the number exists to make a
#: surprise visible, and a repo that tuned it would be tuning away the
#: warning it asked for.
_WIDENING_FRACTION = 0.9

#: Suites smaller than this are never called a widening, however large
#: the fraction. Running 3 of 3 test files is not a surprise worth a
#: warning; running 300 of 320 is.
_WIDENING_MIN_TESTS = 20


def _show(text: str) -> None:
    """Write *text* to the console exactly as given.

    Output reaches the reader while the run is happening, which is the
    whole point of streaming it: a tier that takes minutes has to look
    different from a hang. ``sys.stdout.write`` rather than
    :func:`forge.git_utils.emit` because these chunks already carry
    their own newlines, and rather than a logger because pytest's output
    is not log records.

    Args:
        text: Text to write, newline included.
    """
    sys.stdout.write(text)
    sys.stdout.flush()


def _widening_notice(
    plan: SelectionPlan,
    depth: int,
    extra_depth0: set[str],
    changed: set[str],
) -> str:
    """Return a warning when this run selects nearly the whole suite.

    The cheapest tier selecting almost everything is the symptom worth
    announcing: a changed ``conftest.py`` or package ``__init__.py`` is
    imported by nearly every test, so "depth 0" quietly becomes a full
    run that takes minutes with nothing said about why.

    The fraction is measured over everything the run will execute up to
    *depth*, not per tier: higher tiers only add newly reachable tests,
    so a per-tier fraction would almost never trip.

    Args:
        plan: The computed selection.
        depth: Highest depth this run will execute.
        extra_depth0: Coverage-derived additions to the depth-0 batch.
        changed: Repo-relative changed ``.py`` paths.

    Returns:
        The warning line, or an empty string when the run is not wide or
        the suite is too small for the question to mean anything.
    """
    selected = set(plan.tests_up_to(depth)) | extra_depth0
    total = plan.total_tests
    if total < _WIDENING_MIN_TESTS or not selected:
        return ""
    if len(selected) < _WIDENING_FRACTION * total:
        return ""
    conftests = sorted(f for f in changed if Path(f).name == "conftest.py")
    inits = sorted(f for f in changed if Path(f).name == "__init__.py")
    if conftests:
        cause = f"changed {', '.join(conftests)}"
    elif inits:
        cause = f"changed {', '.join(inits)}"
    else:
        # No single culprit to name. Say what is observable — the tests
        # reach the change — rather than guessing at a cause.
        cause = "most test files import something that changed"
    return (
        f"selection is {len(selected)} of {total} test file(s) — nearly the "
        f"whole suite ({cause}); expect a full run's wall time\n"
    )


def _run_tiers(
    repo_root: Path,
    depth: int,
    plan: SelectionPlan,
    config: _RunConfig,
    log: RunLog,
) -> tuple[int, str]:
    """Run depth batches 0..*depth* with fail-fast between them.

    Each batch runs only the tests *newly* reachable at its depth (lower
    depths already passed); coverage-validated extras join the
    depth-0 batch. The import cache is cleared between batches and the
    first failing batch short-circuits.

    Args:
        repo_root: Git repo root.
        depth: Highest depth to run (0, 1, or 2).
        plan: The precomputed static selection.
        config: Run configuration (coverage, extra_depth0, header).
        log: The run log; each section is appended as soon as it exists
            (the header is already in it).

    Returns:
        ``(exit_code, combined_output)`` across the batches that ran.
    """
    output = [config.header]
    _show(config.header)

    def record(text: str) -> None:
        """Keep text for the log and the returned output, without showing it.

        Used for pytest's own output, which the reader has already seen
        streamed line by line — recording it again here is what keeps the
        log and the return value complete without printing twice.

        Args:
            text: Text to record.
        """
        output.append(text)
        log.append(text)

    def emit(text: str) -> None:
        """Show text and record it.

        Args:
            text: Text to show and record.
        """
        record(text)
        _show(text)

    if not (set(plan.tests_up_to(depth)) | config.extra_depth0):
        emit("No tests reach the changed files — nothing to run.\n")
        return 0, "".join(output)

    if config.notice:
        # Shown by the logger, recorded for the log — not `emit`, which
        # would do both and print it twice on a terminal.
        logger.warning("%s", config.notice.rstrip())
        record(config.notice)

    already: set[str] = set()
    for tier in range(depth + 1):
        batch_set = set(plan.tests_up_to(tier))
        if tier == 0:
            batch_set |= config.extra_depth0
        batch = sorted(batch_set - already)
        if not batch:
            continue
        already.update(batch)
        clear_python_cache(repo_root)
        emit(
            f"\n=== depth {tier}: {len(batch)} of {plan.total_tests} test file(s) ===\n"
        )
        code, out = run_pytest(
            repo_root,
            batch,
            coverage=config.coverage,
            telemetry_label=f"depth{tier}" if config.telemetry else None,
            on_output=_show,
        )
        record(out)
        if code != 0:
            emit(f"\nFAILED at depth {tier} — skipping higher depths.\n")
            return code, "".join(output)
    emit("\nAll selected depth tiers passed.\n")
    return 0, "".join(output)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the ``forge-smart-test`` argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="forge-smart-test",
        description=(
            "Run only the tests a change set affects, in escalating import-"
            "depth tiers with fail-fast. Depth full runs the whole suite "
            "with coverage."
        ),
    )
    parser.add_argument(
        "--depth",
        default="1",
        choices=_DEPTH_CHOICES,
        help="Selection depth: 0/1/2 import hops, or full (default: 1).",
    )
    parser.add_argument(
        "--show-files",
        action="store_true",
        help="Print the selected-test plan and exit without running pytest.",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Enable coverage (always on for --depth full).",
    )
    parser.add_argument(
        "--all-tests",
        action="store_true",
        help="With --depth full: run truly everything — disable the "
        "lifecycle deselection of stale development-marked files.",
    )
    parser.add_argument(
        "--telemetry",
        action="store_true",
        help="Sample RSS/CPU during the run via forge-telemetry (needs the "
        "[telemetry] extra; degrades to an unprofiled run when absent).",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Ref to diff against for change detection (default: auto-detect).",
    )
    parser.add_argument(
        "--from-commit-message",
        action="store_true",
        help="Override --depth from a [depth-N]/[full] directive in HEAD's message.",
    )
    parser.add_argument(
        "--coverage-json",
        default=None,
        help="Path to a `coverage json --show-contexts` export; unions tests "
        "covering a changed line into the selection (enables coverage validation).",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Print the recorded full-run history (wall time, file counts, "
        "development fraction, lifecycle skips, differential mismatches) and "
        "exit without running tests.",
    )
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="Print which test files are development-marked and how long each "
        "has gone untouched, and exit without running tests.",
    )
    return parser


def _render_history(repo_root: Path) -> int:
    """Print the recorded full-run history and return ``0``.

    FOUNDATION §8 calls these metrics record-only, which is a statement
    about what they may *gate*, not a reason to leave them unread. Every
    full run appends a line and nothing read them back, so a suite
    growing slower or a development fraction drifting was recorded and
    invisible. Rendered newest-last so a trend reads top-to-bottom, like
    ``forge-telemetry --history``.

    Args:
        repo_root: Git repo root.

    Returns:
        ``0`` always — reading history never gates.
    """
    path = code_health_dir(repo_root) / lifecycle.HISTORY_NAME
    if not path.is_file():
        logger.info(
            "No smart-test history at %s — run `forge-smart-test --depth full` first.",
            path,
        )
        return 0
    rows = parse_ledger(path.read_text(encoding="utf-8"))
    if not rows:
        logger.info("Smart-test history at %s holds no parsable runs.", path)
        return 0
    header = (
        f"{'ts':<26} {'label':<6} {'wall_s':>8} {'files':>6} "
        f"{'dev':>4} {'dev_frac':>9} {'skipped':>8} {'missed':>7}"
    )
    lines = [f"Smart-test full-run history ({len(rows)} runs):", header]
    lines += [
        f"{r.get('ts', '?'):<26} {r.get('label', '-'):<6} "
        f"{r.get('wall_s', '?'):>8} {r.get('files', '?'):>6} "
        f"{r.get('dev_files', '?'):>4} {r.get('dev_fraction', '?'):>9} "
        f"{r.get('lifecycle_skipped', '?'):>8} "
        f"{r.get('differential_mismatches', '?'):>7}"
        for r in rows
    ]
    lines.append(
        "`missed` counts files that failed outside a would-be depth-2 "
        "selection: evidence the tiers lose nothing. Nothing here gates."
    )
    logger.info("%s", "\n".join(lines))
    return 0


def _render_lifecycle(repo_root: Path, cfg: dict[str, object]) -> int:
    """Print development-marked files and their quiet time; return ``0``.

    Answers the question the ledger's ``lifecycle_skipped`` count raises
    but cannot settle: *which* files, and how close each is to leaving
    ordinary runs. A count alone gives nobody anything to act on.

    Args:
        repo_root: Git repo root.
        cfg: The ``[tool.forge.smart_test]`` table.

    Returns:
        ``0`` always — reading state never gates.
    """
    tests = all_test_files(repo_root)
    marked = lifecycle.development_marked_files(repo_root, tests)
    skip_days_raw = cfg.get("lifecycle_skip_days", lifecycle.DEFAULT_SKIP_DAYS)
    skip_days = (
        float(skip_days_raw)
        if isinstance(skip_days_raw, (int, float))
        else lifecycle.DEFAULT_SKIP_DAYS
    )
    if not marked:
        logger.info(
            "No development-marked test files among %d: every test is a "
            "behavior test, so nothing is ever deselected (FOUNDATION §8).",
            len(tests),
        )
        return 0
    rows = sorted(
        ((lifecycle.days_since_last_touch(repo_root, p), p) for p in marked),
        reverse=True,
    )
    lines = [
        (
            f"Development-marked test files ({len(marked)} of {len(tests)}; "
            f"quiet window {skip_days:g}d):"
        )
    ]
    lines += [
        f"  {'SKIPPED' if days >= skip_days else 'active ':<8} "
        f"{days:>6.1f}d untouched  {path}"
        for days, path in rows
    ]
    lines.append(
        "A skipped file re-enters the moment it changes, and CI's truly-all "
        "run executes every one of them. Deletion is not part of the model."
    )
    logger.info("%s", "\n".join(lines))
    return 0


def _escalate_to_full(
    repo_root: Path, base_ref: str, changed: set[str], cfg: dict[str, object]
) -> bool:
    """Return whether the change set must run the full suite.

    Two triggers: a changed conftest outside the test roots (it applies to
    tests the graph cannot see) and a non-Python change the selector
    cannot map. ``safe_fallback`` (:func:`_safe_fallback_mode`) governs
    only the second. The conftest trigger always escalates: a conftest is
    Python that rewires fixtures for tests the graph cannot reach, so there
    is no narrower run that is still honest, and a repo turning the
    non-Python fallback down is saying "these files cannot affect tests" —
    never true of a conftest.

    Args:
        repo_root: Git repo root.
        base_ref: The base ref to diff against.
        changed: Changed repo-relative paths.
        cfg: Smart-test configuration dict.

    Returns:
        True if escalation to full suite is required.
    """
    if conftests := unscanned_conftests(repo_root, changed):
        logger.info(
            "Safe fallback: %s applies outside the scanned test roots — "
            "escalating to depth=full.",
            min(conftests),
        )
        return True

    mode = _safe_fallback_mode(cfg)
    if mode == "off":
        # WARNING, not INFO: this is the most permissive mode, and a run
        # that silently under-selects must still say so in CI logs.
        logger.warning(
            "Safe fallback off ([tool.forge.smart_test].safe_fallback) — "
            "non-Python changes never escalate this run."
        )
        return False
    ignore_cfg = cfg.get("nonpython_ignore")
    ignore = (
        tuple(str(g) for g in ignore_cfg)
        if isinstance(ignore_cfg, list)
        else lifecycle.DEFAULT_NONPYTHON_IGNORE
    )
    unmappable = changed_non_python_files(repo_root, base_ref, ignore_globs=ignore)
    if not unmappable:
        return False
    if mode == "warn":
        logger.warning(
            "Safe fallback (warn): %d non-Python change(s) the selector cannot "
            "map were NOT escalated — tests they affect may go unselected: %s",
            len(unmappable),
            ", ".join(sorted(unmappable)),
        )
        return False
    logger.info(
        "Safe fallback: %d non-Python change(s) the selector cannot "
        "map (e.g. %s) — escalating to depth=full.",
        len(unmappable),
        min(unmappable),
    )
    return True


_SAFE_FALLBACK_MODES = ("escalate", "warn", "off")


def _safe_fallback_mode(cfg: dict[str, object]) -> str:
    """Resolve ``[tool.forge.smart_test].safe_fallback``.

    An unrecognised value escalates — the mode that can only over-test —
    and says so, because a typo silently weakening the fallback is the
    failure this setting must never introduce.

    Args:
        cfg: Smart-test configuration dict.

    Returns:
        ``"escalate"`` (default), ``"warn"`` or ``"off"``.
    """
    raw = cfg.get("safe_fallback", "escalate")
    if isinstance(raw, str) and raw in _SAFE_FALLBACK_MODES:
        return raw
    logger.warning(
        "[tool.forge.smart_test].safe_fallback = %r is not one of %s — "
        "using 'escalate'.",
        raw,
        ", ".join(_SAFE_FALLBACK_MODES),
    )
    return "escalate"


def _resolve_run_inputs(
    args: argparse.Namespace, repo_root: Path, cfg: dict[str, object]
) -> tuple[str | int, str, set[str]]:
    """Resolve what a run needs: its depth, the ref it diffs against, the changes.

    The base is the *effective* one: on the base branch with a clean tree
    it is ``HEAD^1`` (an empty diff would test nothing), and a root commit
    forces ``full``. Safe-fallback triggers also force ``full``.

    Args:
        args: Parsed command-line arguments.
        repo_root: Git repo root.
        cfg: Smart-test configuration dict.

    Returns:
        ``(depth_raw, base_ref, changed)`` — the depth token or ``"full"``,
        the effective base ref, and the changed ``.py`` paths.
    """
    depth_token = args.depth
    if args.from_commit_message and (directive := _depth_from_commit(repo_root, cfg)):
        depth_token = directive
        logger.info("Depth '%s' set from commit-message directive.", depth_token)
    depth_raw = _parse_depth(depth_token)

    base_ref = resolve_base_ref(repo_root, args.base)
    # An override counts as explicit only when resolve_base_ref honoured it;
    # a rejected one (unresolvable or flag-shaped) fell back to auto-detection.
    effective, reason = effective_base_ref(
        repo_root, base_ref, explicit=args.base is not None and base_ref == args.base
    )
    if reason:
        logger.info("%s.", reason)
    if effective is None:
        depth_raw = _FULL
    else:
        base_ref = effective
    changed = changed_python_files(repo_root, base_ref)

    if depth_raw != _FULL and _escalate_to_full(repo_root, base_ref, changed, cfg):
        depth_raw = _FULL

    return depth_raw, base_ref, changed


def _coverage_additions(
    args: argparse.Namespace, cfg: dict[str, object], changed: set[str]
) -> tuple[set[str], bool]:
    """Resolve coverage-validation settings and collect coverage additions.

    Args:
        args: Parsed command-line arguments.
        cfg: Smart-test configuration dict.
        changed: Changed repo-relative paths.

    Returns:
        Tuple of (extra_depth0, coverage_validate).
    """
    coverage_json = args.coverage_json or cfg.get("coverage_json")
    coverage_validate = bool(cfg.get("coverage_validate", False)) or bool(
        args.coverage_json
    )
    extra_depth0: set[str] = set()
    if coverage_validate and isinstance(coverage_json, str):
        extra_depth0 = cov_stage.tests_covering(Path(coverage_json), changed)
    return extra_depth0, coverage_validate


def main() -> int:
    """Select and run change-affected tests by depth; write the log.

    Depth tiers escalate to ``full`` automatically when the change set
    contains non-Python paths the selector cannot map (safe fallback,
    minus the ``nonpython_ignore`` globs).

    Returns:
        The exit code of the run: ``0`` on success / nothing-to-run /
        ``--show-files``, else the first failing batch's pytest exit code.
    """
    args = _build_parser().parse_args()
    repo_root = Path.cwd()
    cfg = _smart_test_config(repo_root)
    # Both read recorded state and run nothing, so they answer before any
    # selection work, lock or log write happens.
    if args.history:
        return _render_history(repo_root)
    if args.lifecycle:
        return _render_lifecycle(repo_root, cfg)
    follow = bool(cfg.get("follow_mock_patches", False))

    depth_raw, base_ref, changed = _resolve_run_inputs(args, repo_root, cfg)

    if depth_raw == _FULL:
        if args.show_files:
            logger.info("📋 Tests covering changed code (depth full): the entire suite")
            return 0

        def full(log: RunLog) -> tuple[int, str]:
            """Run the entire suite, recording output in the run log.

            Args:
                log: Run log that receives the suite output.

            Returns:
                ``(exit_code, combined_output)`` of the full-suite run.
            """
            code, body = _run_full(
                repo_root,
                cfg,
                changed,
                all_tests=args.all_tests,
                telemetry=args.telemetry,
            )
            log.append(body)
            return code, body

        return _with_run_log(repo_root, "", full)

    depth = cast("int", depth_raw)
    plan = select_tests(repo_root, changed, depth, follow_mock_patches=follow)

    extra_depth0, coverage_validate = _coverage_additions(args, cfg, changed)

    if args.show_files:
        logger.info("%s", render_plan(plan, depth))
        if extra_depth0:
            extras = "\n".join(f"  - {t}" for t in sorted(extra_depth0))
            logger.info("📋 Coverage-validated additions (depth 0):\n%s", extras)
        return 0

    header = (
        f"base: {base_ref}  changed .py files: {len(changed)}  "
        f"follow_mock_patches={follow}  coverage_validate={coverage_validate}\n"
    )
    config = _RunConfig(
        coverage=args.coverage,
        extra_depth0=extra_depth0,
        header=header,
        notice=_widening_notice(plan, depth, extra_depth0, changed),
        telemetry=args.telemetry,
    )
    return _with_run_log(
        repo_root,
        header,
        lambda log: _run_tiers(repo_root, depth, plan, config, log),
    )


if __name__ == "__main__":
    sys.exit(main())
