"""forge-smart-test — change-driven test runner with depth tiers.

Selects the tests a change set affects (via the import graph) and runs
them in escalating depth batches with fail-fast: depth 0 (tests importing
a changed module directly), depth 1 (one hop removed), depth 2 (two hops),
or ``full`` (the entire suite, with coverage). Lower depths must pass
before higher ones run, keeping the feedback loop tight. Writes
``code_health/smart_test.log`` for ``forge:precommit-fixer`` per
FOUNDATION §13.

Usage:

- ``forge-smart-test`` — depth 1 (default)
- ``forge-smart-test --depth 0`` — only directly-affected tests
- ``forge-smart-test --depth 2`` — two-hop dependents
- ``forge-smart-test --depth full`` (or ``infinity``) — whole suite + coverage
- ``forge-smart-test --show-files --depth N`` — print the plan, run nothing
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import cast

from forge.git_utils import configure_cli_logging
from forge.smart_test.dependencies import render_plan, select_tests
from forge.smart_test.git_helpers import changed_python_files, resolve_base_ref
from forge.smart_test.runner import clear_python_cache, run_pytest


configure_cli_logging()
logger = logging.getLogger(__name__)

_FULL = "full"
_DEPTH_CHOICES = ("0", "1", "2", _FULL, "infinity")
_LOG_RELPATH = Path("code_health") / "smart_test.log"


def _parse_depth(raw: str) -> int | str:
    """Map a ``--depth`` token to an int tier or the ``full`` sentinel.

    Args:
        raw: One of ``0``, ``1``, ``2``, ``full``, ``infinity``.

    Returns:
        The integer tier, or :data:`_FULL` for ``full`` / ``infinity``.
    """
    if raw in (_FULL, "infinity"):
        return _FULL
    return int(raw)


def _write_log(repo_root: Path, body: str) -> None:
    """Write *body* to ``code_health/smart_test.log``.

    Args:
        repo_root: Git repo root.
        body: Full captured run output.
    """
    log_path = repo_root / _LOG_RELPATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(body, encoding="utf-8")


def _run_full(repo_root: Path) -> tuple[int, str]:
    """Run the entire suite (the ``full`` tier), always with coverage.

    Coverage is unconditionally enabled for ``full`` — it is the tier's
    defining cost/coverage trade-off — so there is no opt-out parameter.

    Args:
        repo_root: Git repo root.

    Returns:
        ``(exit_code, output)`` from the single pytest run.
    """
    logger.info("Running the full suite (depth=full) with coverage.")
    return run_pytest(repo_root, [], coverage=True)


def _run_tiers(
    repo_root: Path, depth: int, *, coverage: bool, base: str | None
) -> tuple[int, str]:
    """Run depth batches 0..*depth* with fail-fast between them.

    Each batch runs only the tests *newly* reachable at its depth (lower
    depths already passed), with the import cache cleared between batches.
    The first failing batch short-circuits and returns its exit code.

    Args:
        repo_root: Git repo root.
        depth: Highest depth to run (0, 1, or 2).
        coverage: Whether to instrument coverage (off by default per tier).
        base: Explicit diff base ref, or ``None`` to auto-detect.

    Returns:
        ``(exit_code, combined_output)`` across the batches that ran.
    """
    base_ref = resolve_base_ref(repo_root, base)
    changed = changed_python_files(repo_root, base_ref)
    plan = select_tests(repo_root, changed, depth)

    output = [f"base: {base_ref}  changed .py files: {len(changed)}\n"]
    selected = plan.tests_up_to(depth)
    if not selected:
        output.append("No tests reach the changed files — nothing to run.\n")
        return 0, "".join(output)

    already: set[str] = set()
    for tier in range(depth + 1):
        batch = sorted(set(plan.tests_up_to(tier)) - already)
        if not batch:
            continue
        already.update(batch)
        clear_python_cache(repo_root)
        output.append(f"\n=== depth {tier}: {len(batch)} test file(s) ===\n")
        code, out = run_pytest(repo_root, batch, coverage=coverage)
        output.append(out)
        if code != 0:
            output.append(f"\nFAILED at depth {tier} — skipping higher depths.\n")
            return code, "".join(output)
    output.append("\nAll selected depth tiers passed.\n")
    return 0, "".join(output)


def main() -> int:
    """Select and run change-affected tests by depth; write the log.

    Returns:
        The exit code of the run: ``0`` on success / nothing-to-run /
        ``--show-files``, else the first failing batch's pytest exit code.
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
        help="Selection depth: 0/1/2 import hops, or full/infinity (default: 1).",
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
        "--base",
        default=None,
        help="Ref to diff against for change detection (default: auto-detect).",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    depth_raw = _parse_depth(args.depth)

    if args.show_files:
        if depth_raw == _FULL:
            logger.info("📋 Tests covering changed code (depth full): the entire suite")
            return 0
        depth: int = cast("int", depth_raw)
        base_ref = resolve_base_ref(repo_root, args.base)
        changed = changed_python_files(repo_root, base_ref)
        plan = select_tests(repo_root, changed, depth)
        logger.info("%s", render_plan(plan, depth))
        return 0

    if depth_raw == _FULL:
        code, body = _run_full(repo_root)
    else:
        depth = cast("int", depth_raw)
        code, body = _run_tiers(
            repo_root, depth, coverage=args.coverage, base=args.base
        )

    _write_log(repo_root, body)
    logger.info("%s", body.rstrip())
    return code


if __name__ == "__main__":
    sys.exit(main())
