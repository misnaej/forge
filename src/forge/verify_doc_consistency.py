"""verify-forge-doc-consistency — check machine-checkable doc claims vs repo state.

Backs the opt-in ``doc_consistency`` pre-commit step. Nothing in forge
otherwise checks documentation for factual drift against the repo: a doc
can list a CLI that no longer exists (or omit a new one) and only a
careful human reread catches it. This CLI closes the **structured,
NLP-free subset** of that hole.

Check (self-skips when its inputs are absent, so the CLI is safe in any
repo):

- **CLI coverage** — every ``[project.scripts]`` entry name appears at
  least once in ``docs/cli-reference.md``. A CLI added or removed without
  a matching doc line is drift.

Scope is deliberately conservative for v1: the one robust, no-NLP,
no-maintenance check. Name-list/count checks that depend on prose
phrasing, internal-link validation, and repo-state facts (visibility,
default branch) are intentionally out of scope — tracked separately.

Exit code: ``0`` when consistent or nothing to check, ``1`` when any
drift is found. The ``doc_consistency`` step renders a non-zero result as
a non-blocking ``WARN``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from typing import TYPE_CHECKING

from forge.git_utils import configure_cli_logging
from forge.git_utils import repo_root as get_repo_root


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


def _check_cli_coverage(repo_root: Path) -> list[str]:
    """Return findings for ``[project.scripts]`` names missing from the CLI reference.

    Skips silently when ``pyproject.toml`` has no ``[project.scripts]``
    table or ``docs/cli-reference.md`` is absent — a repo without either
    has nothing to drift.

    Args:
        repo_root: Git repo root.

    Returns:
        One finding string per script name absent from the reference doc;
        empty when consistent or not applicable.
    """
    pyproject = repo_root / "pyproject.toml"
    reference = repo_root / "docs" / "cli-reference.md"
    if not pyproject.is_file() or not reference.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    scripts = (data.get("project") or {}).get("scripts") or {}
    if not scripts:
        return []
    reference_text = reference.read_text(encoding="utf-8")
    return [
        f"docs/cli-reference.md: no entry for [project.scripts] CLI '{name}'"
        for name in sorted(scripts)
        if name not in reference_text
    ]


def main() -> int:
    """CLI entry point.

    Returns:
        ``0`` when the check is consistent (or nothing applies); ``1`` when
        drift is found.
    """
    argparse.ArgumentParser(
        prog="verify-forge-doc-consistency",
        description=(
            "Check that every [project.scripts] CLI is documented in "
            "docs/cli-reference.md. Non-blocking reporter for the "
            "doc_consistency pre-commit step."
        ),
    ).parse_args()

    findings = _check_cli_coverage(get_repo_root())
    if findings:
        logger.error("Documentation drift detected:")
        for finding in findings:
            logger.error("  - %s", finding)
        return 1
    logger.info("Documentation claims consistent with repo state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
