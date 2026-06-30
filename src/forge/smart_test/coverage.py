"""Coverage-validated test selection (Gap 2, opt-in).

Static import analysis can't see test↔code links created at runtime —
fixture-injected collaborators, dynamic dispatch, ``getattr`` /
``importlib`` indirection. This stage reads a coverage map recorded with
**per-test dynamic contexts** (``pytest --cov-context=test`` /
``coverage run --context=…``) and returns the tests whose contexts touch a
changed file, so the static selection can be unioned with them rather than
under-selecting. It is a belt-and-suspenders tier: keep the static pass the
fast default and reconcile against coverage when a fresh map exists (a
stale map under-selects — regenerate it on ``full`` runs).

Two input forms are supported: a ``coverage json --show-contexts`` export
(dependency-free) and a ``.coverage`` SQLite DB (read via the ``coverage``
library when installed; a missing library is a loud-but-empty skip, not a
crash).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from forge.git_utils import configure_cli_logging


if TYPE_CHECKING:
    from collections.abc import Iterable


configure_cli_logging()
logger = logging.getLogger(__name__)


def _context_to_test(context: str) -> str | None:
    """Reduce a coverage context to a repo-relative test file path.

    A per-test context is the pytest node id (``tests/test_x.py::test_fn``);
    the file path is the segment before ``::``. The empty context (code run
    outside any test) and non-``.py`` contexts yield ``None``.

    Args:
        context: A coverage dynamic-context string.

    Returns:
        The test file path, or ``None`` when the context names no test file.
    """
    if not context:
        return None
    head = context.split("::", 1)[0]
    return head if head.endswith(".py") else None


def _from_json(path: Path, changed: set[str]) -> set[str]:
    """Extract covering tests from a ``coverage json --show-contexts`` export.

    Args:
        path: Path to the coverage JSON file.
        changed: Repo-relative changed source paths to intersect against.

    Returns:
        Repo-relative test files whose contexts cover a changed file.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("coverage: could not read %s: %s", path, exc)
        return set()
    tests: set[str] = set()
    for fname, info in (data.get("files") or {}).items():
        if fname not in changed:
            continue
        for ctx_list in (info.get("contexts") or {}).values():
            for ctx in ctx_list:
                if (test := _context_to_test(ctx)) is not None:
                    tests.add(test)
    return tests


def _from_sqlite(path: Path, changed: set[str], repo_root: Path) -> set[str]:
    """Extract covering tests from a ``.coverage`` SQLite DB.

    Uses the ``coverage`` library's ``CoverageData`` API; when it is not
    installed this logs a warning and returns an empty set (the union with
    the static selection then simply adds nothing).

    Args:
        path: Path to the ``.coverage`` DB.
        changed: Repo-relative changed source paths.
        repo_root: Git repo root, to relativize the DB's absolute paths.

    Returns:
        Repo-relative test files whose contexts cover a changed file.
    """
    try:
        from coverage import CoverageData  # noqa: PLC0415 — optional dependency
    except ImportError:
        logger.warning(
            "coverage: --coverage-db is a SQLite DB but the `coverage` library "
            "is not installed; skipping coverage validation."
        )
        return set()
    data = CoverageData(basename=str(path))
    data.read()
    tests: set[str] = set()
    for measured in data.measured_files():
        try:
            rel = Path(measured).resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            continue
        if rel not in changed:
            continue
        for ctx_list in data.contexts_by_lineno(measured).values():
            for ctx in ctx_list:
                if (test := _context_to_test(ctx)) is not None:
                    tests.add(test)
    return tests


def tests_covering(
    coverage_path: Path, changed_files: Iterable[str], repo_root: Path
) -> set[str]:
    """Return repo-relative test files whose coverage touches a changed file.

    Dispatches on the path suffix: ``.json`` is parsed directly; anything
    else is treated as a ``.coverage`` SQLite DB. A missing file yields an
    empty set with a warning — coverage validation never hard-fails the run.

    Args:
        coverage_path: Path to the coverage JSON export or ``.coverage`` DB.
        changed_files: Repo-relative changed source paths.
        repo_root: Git repo root.

    Returns:
        Repo-relative test files to union into the static selection.
    """
    if not coverage_path.is_file():
        logger.warning(
            "coverage: %s not found — skipping coverage validation.", coverage_path
        )
        return set()
    changed = set(changed_files)
    if coverage_path.suffix == ".json":
        return _from_json(coverage_path, changed)
    return _from_sqlite(coverage_path, changed, repo_root)
