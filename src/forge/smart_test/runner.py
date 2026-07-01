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


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


# pytest's "no tests collected" exit code — treated as success for a batch
# that legitimately selected nothing (the orchestrator decides whether an
# empty selection should even call pytest).
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

    Returns:
        ``(exit_code, combined_output)``. Exit code 5 ("no tests collected")
        is normalized to 0 — an empty batch is not a failure.
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

    proc = subprocess.run(
        cmd, cwd=repo_root, capture_output=True, text=True, check=False
    )
    code = 0 if proc.returncode == _PYTEST_NO_TESTS else proc.returncode
    return code, notice + proc.stdout + proc.stderr
