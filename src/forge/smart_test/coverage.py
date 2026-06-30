"""Coverage-validated test selection.

Reads a coverage JSON export (``coverage json --show-contexts``, recorded
with per-test contexts via ``pytest --cov-context=test``) and returns the
tests whose contexts touch a changed source line. The caller unions that
set with the static import selection so tests linked to changed code only
at runtime — fixtures, dynamic dispatch, ``importlib`` indirection — are
still selected. A stale export under-selects, so regenerate it when the
source tree changes.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from forge.git_utils import configure_cli_logging


if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


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


def _from_json(data: dict[str, object], changed: set[str]) -> set[str]:
    """Map a parsed coverage-JSON document to covering test files.

    Args:
        data: The parsed ``coverage json --show-contexts`` document.
        changed: Repo-relative changed source paths to intersect against.

    Returns:
        Repo-relative test files whose contexts cover a changed file.
    """
    files = data.get("files")
    tests: set[str] = set()
    for fname, info in (files if isinstance(files, dict) else {}).items():
        if fname not in changed or not isinstance(info, dict):
            continue
        contexts = info.get("contexts")
        for ctx_list in (contexts if isinstance(contexts, dict) else {}).values():
            for ctx in ctx_list:
                if (test := _context_to_test(ctx)) is not None:
                    tests.add(test)
    return tests


def tests_covering(coverage_json: Path, changed_files: Iterable[str]) -> set[str]:
    """Return repo-relative test files whose coverage touches a changed file.

    Parses a ``coverage json --show-contexts`` export and intersects its
    per-line contexts with *changed_files*. A missing or malformed file
    yields an empty set with a warning — coverage validation never
    hard-fails the run.

    Args:
        coverage_json: Path to the coverage JSON export.
        changed_files: Repo-relative changed source paths.

    Returns:
        Repo-relative test files to union into the static selection.
    """
    if not coverage_json.is_file():
        logger.warning(
            "coverage: %s not found — skipping coverage validation.", coverage_json
        )
        return set()
    try:
        data = json.loads(coverage_json.read_text(encoding="utf-8"))
    except ValueError as exc:
        logger.warning("coverage: could not parse %s: %s", coverage_json, exc)
        return set()
    return _from_json(data, set(changed_files))
