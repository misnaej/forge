"""verify-forge-doc-consistency — check machine-checkable doc claims vs repo state.

Backs the opt-in ``doc_consistency`` pre-commit step. Nothing in forge
otherwise checks documentation for factual drift against the repo: a doc
can claim "ten foundation agents" or list a CLI that no longer exists,
and only a careful human reread catches it. This CLI closes the
**structured, NLP-free subset** of that hole — names and counts a doc
asserts about the repo's own inventory, verified against the filesystem
and ``pyproject.toml``.

Checks (each self-skips when its inputs are absent, so the CLI is safe in
any repo):

1. **CLI coverage** — every ``[project.scripts]`` entry name appears at
   least once in ``docs/cli-reference.md``. A new CLI added without a doc
   line is drift.
2. **Agent count** — a ``"<N> foundation agents"`` claim in
   ``FOUNDATION.md`` matches the number of ``agents/*.md`` files
   (excluding the underscore-prefixed ``_TEMPLATE.md``).

Scope is deliberately conservative for v1: name-lists and counts only.
Internal-link validation and repo-state facts (visibility, default
branch) are intentionally out of scope — they are tracked separately.

Exit code: ``0`` when consistent or nothing to check, ``1`` when any
drift is found. The ``doc_consistency`` step renders a non-zero result as
a non-blocking ``WARN``.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tomllib
from typing import TYPE_CHECKING

from forge.git_utils import configure_cli_logging
from forge.git_utils import repo_root as get_repo_root


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


# Spelled-out cardinals forge docs actually use for inventory counts.
# Digits are matched directly; this map covers the prose form.
_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

# "<count> foundation agents" — count as a digit run or a spelled cardinal.
_AGENT_COUNT_RE = re.compile(
    r"\b(\d+|" + "|".join(_NUMBER_WORDS) + r")\s+foundation agents\b",
    re.IGNORECASE,
)


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


def _check_agent_count(repo_root: Path) -> list[str]:
    """Return a finding when FOUNDATION's agent-count claim disagrees with disk.

    Counts ``agents/*.md`` files (excluding underscore-prefixed templates)
    and compares against the highest ``"<N> foundation agents"`` claim in
    ``FOUNDATION.md``. Skips silently when the ``agents/`` directory or the
    claim is absent.

    Args:
        repo_root: Git repo root.

    Returns:
        A single-element finding list on mismatch; empty when consistent
        or not applicable.
    """
    agents_dir = repo_root / "agents"
    foundation = repo_root / "FOUNDATION.md"
    if not agents_dir.is_dir() or not foundation.is_file():
        return []
    actual = sum(1 for path in agents_dir.glob("*.md") if not path.name.startswith("_"))
    match = _AGENT_COUNT_RE.search(foundation.read_text(encoding="utf-8"))
    if match is None:
        return []
    token = match.group(1).lower()
    claimed = int(token) if token.isdigit() else _NUMBER_WORDS[token]
    if claimed != actual:
        return [
            f"FOUNDATION.md claims {claimed} foundation agents, "
            f"but agents/ holds {actual}"
        ]
    return []


def main() -> int:
    """CLI entry point.

    Returns:
        ``0`` when every applicable check is consistent (or nothing
        applies); ``1`` when any drift is found.
    """
    argparse.ArgumentParser(
        prog="verify-forge-doc-consistency",
        description=(
            "Check machine-checkable documentation claims (CLI name-lists, "
            "agent counts) against the actual repo state. Non-blocking "
            "reporter for the doc_consistency pre-commit step."
        ),
    ).parse_args()

    repo_root = get_repo_root()
    findings = _check_cli_coverage(repo_root) + _check_agent_count(repo_root)
    if findings:
        logger.error("Documentation drift detected:")
        for finding in findings:
            logger.error("  - %s", finding)
        return 1
    logger.info("Documentation claims consistent with repo state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
