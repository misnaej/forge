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
- ``forge-smart-test --depth full`` — whole suite + coverage
- ``forge-smart-test --show-files --depth N`` — print the plan, run nothing
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from forge import config as _config
from forge.git_utils import configure_cli_logging
from forge.smart_test import coverage as cov_stage
from forge.smart_test.dependencies import SelectionPlan, render_plan, select_tests
from forge.smart_test.git_helpers import (
    changed_python_files,
    head_commit_message,
    resolve_base_ref,
)
from forge.smart_test.runner import clear_python_cache, run_pytest


configure_cli_logging()
logger = logging.getLogger(__name__)

_FULL = "full"
_DEPTH_CHOICES = ("0", "1", "2", _FULL)
_LOG_RELPATH = Path("code_health") / "smart_test.log"
# Default CI directive: [depth-N] or [full] anywhere in the commit message.
# Override via [tool.forge.smart_test].commit_directive_re.
_DEPTH_DIRECTIVE_RE = r"\[(?:depth-(?P<n>[0-2])|(?P<full>full))\]"


def _smart_test_config(repo_root: Path) -> dict[str, object]:
    """Return the ``[tool.forge.smart_test]`` table, or ``{}`` when absent.

    Args:
        repo_root: Git repo root.

    Returns:
        The subsection dict (``follow_mock_patches`` etc.), or ``{}``.
    """
    data = _config.read_pyproject_raw(repo_root)
    return ((data.get("tool") or {}).get("forge") or {}).get("smart_test") or {}


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


@dataclass
class _RunConfig:
    """Configuration for a tiered test run."""

    coverage: bool
    """Whether to instrument coverage (off by default per tier)."""
    extra_depth0: set[str]
    """Coverage-derived tests to union into the depth-0 batch."""
    header: str
    """One-line run header recorded at the top of the log."""


def _run_tiers(
    repo_root: Path,
    depth: int,
    plan: SelectionPlan,
    config: _RunConfig,
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

    Returns:
        ``(exit_code, combined_output)`` across the batches that ran.
    """
    output = [config.header]
    if not (set(plan.tests_up_to(depth)) | config.extra_depth0):
        output.append("No tests reach the changed files — nothing to run.\n")
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
        output.append(f"\n=== depth {tier}: {len(batch)} test file(s) ===\n")
        code, out = run_pytest(repo_root, batch, coverage=config.coverage)
        output.append(out)
        if code != 0:
            output.append(f"\nFAILED at depth {tier} — skipping higher depths.\n")
            return code, "".join(output)
    output.append("\nAll selected depth tiers passed.\n")
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


def main() -> int:
    """Select and run change-affected tests by depth; write the log.

    Returns:
        The exit code of the run: ``0`` on success / nothing-to-run /
        ``--show-files``, else the first failing batch's pytest exit code.
    """
    args = _build_parser().parse_args()
    repo_root = Path.cwd()
    cfg = _smart_test_config(repo_root)
    follow = bool(cfg.get("follow_mock_patches", False))

    depth_token = args.depth
    if args.from_commit_message and (directive := _depth_from_commit(repo_root, cfg)):
        depth_token = directive
        logger.info("Depth '%s' set from commit-message directive.", depth_token)
    depth_raw = _parse_depth(depth_token)

    if depth_raw == _FULL:
        if args.show_files:
            logger.info("📋 Tests covering changed code (depth full): the entire suite")
            return 0
        code, body = _run_full(repo_root)
        _write_log(repo_root, body)
        logger.info("%s", body.rstrip())
        return code

    depth = cast("int", depth_raw)
    base_ref = resolve_base_ref(repo_root, args.base)
    changed = changed_python_files(repo_root, base_ref)
    plan = select_tests(repo_root, changed, depth, follow_mock_patches=follow)

    coverage_json = args.coverage_json or cfg.get("coverage_json")
    coverage_validate = bool(cfg.get("coverage_validate", False)) or bool(
        args.coverage_json
    )
    extra_depth0: set[str] = set()
    if coverage_validate and isinstance(coverage_json, str):
        extra_depth0 = cov_stage.tests_covering(Path(coverage_json), changed)

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
    )
    code, body = _run_tiers(repo_root, depth, plan, config)
    _write_log(repo_root, body)
    logger.info("%s", body.rstrip())
    return code


if __name__ == "__main__":
    sys.exit(main())
