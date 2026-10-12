"""Pytest execution for smart-test.

Owns the mechanics of actually running a selected batch of tests: clear
the import cache so a stale ``__pycache__`` from an earlier tree can't
mask a real failure, then invoke ``pytest`` **once** over the batch with
a deterministic (sorted) file order. Coverage instrumentation — which
slows pytest ~3-5x — is reserved for the ``full`` tier and self-disables
when ``pytest-cov`` is not installed rather than erroring.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from forge import telemetry as telemetry_mod
from forge.git_utils import missing_dependency_hint


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


# pytest's "no tests collected" exit code — `_finalize` treats it as success
# only for a whole-suite run; named test files that collect nothing fail.
_PYTEST_NO_TESTS = 5


def clear_python_cache(repo_root: Path) -> None:
    """Delete every ``__pycache__`` directory under *repo_root*.

    Run between depth batches so a ``.pyc`` compiled against a stale source
    tree cannot satisfy an import and hide a real failure.

    Args:
        repo_root: Git repo root.
    """
    for cache in repo_root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _coverage_available() -> bool:
    """Return whether the ``pytest-cov`` plugin is importable."""
    return importlib.util.find_spec("pytest_cov") is not None


def run_pytest(
    repo_root: Path,
    test_paths: Sequence[str],
    *,
    coverage: bool = False,
    telemetry: bool = False,
    label: str = "",
) -> tuple[int, str]:
    """Run ``pytest`` once over *test_paths* and return ``(exit_code, output)``.

    A deterministic sorted path order is passed so collection is
    reproducible across runs (deterministic by design). An empty
    *test_paths* with ``coverage`` runs the whole suite (the ``full``
    tier); an empty *test_paths* without coverage is a no-op success.

    Args:
        repo_root: Git repo root (pytest's working directory).
        test_paths: Repo-relative test file paths; empty means "whole suite".
        coverage: Enable ``--cov`` (ignored with a notice when ``pytest-cov``
            is absent).
        telemetry: Sample resource usage during the run via
            ``forge.telemetry`` (degrades to an unprofiled run with a
            notice when ``psutil`` is absent — a missing profiler must
            never fail the test run).
        label: Telemetry run label forwarded to
            :func:`forge.telemetry.run_command`, so each depth tier of a
            multi-tier run keeps its own artifacts instead of clobbering
            the previous tier's (#376).

    Returns:
        ``(exit_code, combined_output)``. Exit code 5 ("no tests collected")
        is normalized to 0 only for a whole-suite run; for named
        *test_paths* it stays a failure.
    """
    if not test_paths and not coverage:
        return 0, "(no tests selected — nothing to run)\n"

    cmd = [sys.executable, "-m", "pytest", "-q"]
    notice = ""
    if coverage:
        if _coverage_available():
            cmd += ["--cov", "--cov-report=term-missing"]
        else:
            notice = "(pytest-cov not installed — running without coverage)\n"
    cmd += sorted(test_paths)

    if telemetry:
        if telemetry_mod.telemetry_available():
            code, output = telemetry_mod.run_command(
                cmd, repo_root, capture=True, cwd=repo_root, label=label
            )
            return _finalize(code, notice + output, selected=bool(test_paths))
        notice += (
            f"({missing_dependency_hint('psutil', extra='telemetry')} "
            "— running without telemetry)\n"
        )

    proc = subprocess.run(
        cmd, cwd=repo_root, capture_output=True, text=True, check=False
    )
    return _finalize(
        proc.returncode, notice + proc.stdout + proc.stderr, selected=bool(test_paths)
    )


def _finalize(code: int, output: str, *, selected: bool) -> tuple[int, str]:
    """Apply the "no tests collected" rule to a finished pytest run.

    Exit 5 is benign only for a whole-suite run (nothing named): a named
    selection that collects nothing means the selector picked files that
    hold no tests, and passing would report coverage that never ran.

    Args:
        code: pytest's exit code.
        output: The run's combined output.
        selected: Whether specific test files were passed to pytest.

    Returns:
        ``(exit_code, output)``, with an explanation appended when a
        selection collected nothing.
    """
    if code != _PYTEST_NO_TESTS:
        return code, output
    if not selected:
        return 0, output
    return code, output + (
        "\nsmart-test: the selected files collected no tests — failing rather "
        "than reporting a run that tested nothing.\n"
    )
