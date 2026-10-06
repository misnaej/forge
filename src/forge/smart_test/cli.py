"""forge-smart-test — change-driven test runner with depth tiers.

Selects the tests a change set affects (via the import graph) and runs
them in escalating depth batches with fail-fast: depth 0 (tests importing
a changed module directly), depth 1 (one hop removed), depth 2 (two hops),
or ``full`` (the entire suite, with coverage). Lower depths must pass
before higher ones run, keeping the feedback loop tight. Writes the
run output to two sinks: ``code_health/smart_test.log`` for
``forge:precommit-fixer`` (FOUNDATION §13) and ``code_health/pytest.log``
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
from forge.git_utils import configure_cli_logging
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
            console_output)``.

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
        code, output = run(log)
        log.complete("passed" if code == 0 else f"failed (exit {code})")
        logger.info("%s", output.rstrip())
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
    lifecycle rule, always reported); ``all_tests`` disables that — the
    48h-cadence run and explicit ``--all-tests`` execute truly
    everything. After the run a record-only metrics line (including the
    depth-2 differential check) is appended to the history ledger.

    Args:
        repo_root: Git repo root.
        cfg: The ``[tool.forge.smart_test]`` table.
        changed: Current change set (lifecycle re-inclusion trigger and
            differential input).
        all_tests: Run truly everything — lifecycle deselection off.
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
    skippable: set[str] = set()
    if not all_tests:
        skippable = lifecycle.lifecycle_skippable(
            repo_root, tests, changed, skip_days=skip_days
        )
    label = "all" if all_tests else "full"
    logger.info("Running the full suite (depth=%s) with coverage.", label)
    batch = sorted(tests - skippable) if skippable else []
    started = time.monotonic()
    code, out = run_pytest(
        repo_root, batch, coverage=True, telemetry=telemetry, label=label
    )
    wall_s = time.monotonic() - started
    if skippable:
        out += (
            f"\nlifecycle-skipped: {len(skippable)} development file(s) "
            f"untouched >={skip_days:g}d (run --all-tests to include)\n"
        )
    mismatches: set[str] = set()
    if changed:
        plan = select_tests(repo_root, changed, 2)
        selected = set(plan.tests_up_to(2))
        mismatches = lifecycle.failed_files(out) - selected - skippable
        if mismatches:
            out += (
                f"differential: {len(mismatches)} failing file(s) outside "
                f"depth-2 selection (record-only): "
                + ", ".join(sorted(mismatches))
                + "\n"
            )
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
    telemetry: bool = False
    """Whether to sample resource usage during each pytest batch."""


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

    def emit(text: str) -> None:
        """Append text to the console output and the run log.

        Args:
            text: Text to record.
        """
        output.append(text)
        log.append(text)

    if not (set(plan.tests_up_to(depth)) | config.extra_depth0):
        emit("No tests reach the changed files — nothing to run.\n")
        return 0, "".join(output)

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
        emit(f"\n=== depth {tier}: {len(batch)} test file(s) ===\n")
        code, out = run_pytest(
            repo_root,
            batch,
            coverage=config.coverage,
            telemetry=config.telemetry,
            label=f"depth{tier}",
        )
        emit(out)
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
    return parser


def _escalate_to_full(
    repo_root: Path, base_ref: str, changed: set[str], cfg: dict[str, object]
) -> bool:
    """Return whether the change set must run the full suite.

    Two triggers: a changed conftest outside the test roots (it applies to
    tests the graph cannot see) and a non-Python change the selector
    cannot map.

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

    ignore_cfg = cfg.get("nonpython_ignore")
    ignore = (
        tuple(str(g) for g in ignore_cfg)
        if isinstance(ignore_cfg, list)
        else lifecycle.DEFAULT_NONPYTHON_IGNORE
    )
    unmappable = changed_non_python_files(repo_root, base_ref, ignore_globs=ignore)
    if unmappable:
        logger.info(
            "Safe fallback: %d non-Python change(s) the selector cannot "
            "map (e.g. %s) — escalating to depth=full.",
            len(unmappable),
            min(unmappable),
        )
        return True
    return False


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
        telemetry=args.telemetry,
    )
    return _with_run_log(
        repo_root,
        header,
        lambda log: _run_tiers(repo_root, depth, plan, config, log),
    )


if __name__ == "__main__":
    sys.exit(main())
