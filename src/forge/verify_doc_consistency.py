"""verify-forge-doc-consistency — check machine-checkable doc claims vs repo state.

Backs the opt-in ``doc_consistency`` pre-commit step. Nothing in forge
otherwise checks documentation for factual drift against the repo: a doc
can list a CLI that no longer exists (or omit a new one) and only a
careful human reread catches it. This CLI closes the **structured,
NLP-free subset** of that hole.

Checks (each self-skips when its inputs are absent, so the CLI is safe in
any repo):

- **CLI coverage** — every ``[project.scripts]`` entry name appears at
  least once in ``docs/cli-reference.md``. A CLI added or removed without
  a matching doc line is drift.
- **Provenance gate names** — every step in
  ``pr_delta.PROVENANCE_GATE_STEPS`` appears in each prose surface that
  hand-names the gates (the files in :data:`_PROVENANCE_PROSE_FILES`),
  and no ``*_check`` token named in those files' provenance prose is
  absent from the constant (a stale name).

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
import re
import sys
import tomllib
from typing import TYPE_CHECKING

from forge.git_utils import configure_cli_logging
from forge.git_utils import repo_root as get_repo_root
from forge.pr_delta import PROVENANCE_GATE_STEPS


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


# Step-name-shaped token, matched only inside provenance-adjacent prose.
_CHECK_TOKEN_RE = re.compile(r"\b[a-z0-9_]+_check\b")
# Lines this close to a "provenance" mention count as provenance prose.
# Sized to span forge-docs/configuration.md's gate bullet list, whose last
# token sits 9 lines below its "provenance gates" anchor line.
_PROVENANCE_WINDOW = 9

# The surfaces that hand-name the provenance gate steps in prose.
_PROVENANCE_PROSE_FILES = (
    "src/forge/precommit.py",
    "skills/pr/SKILL.md",
    "forge-docs/configuration.md",
)


def _provenance_prose_tokens(text: str) -> set[str]:
    """Collect ``*_check`` tokens from *text*'s provenance-adjacent lines.

    Scoping to a window around the word "provenance" keeps the stale-name
    check from matching the many unrelated ``*_check`` step names both
    files legitimately contain.

    Args:
        text: Full file text.

    Returns:
        The ``*_check`` tokens found within :data:`_PROVENANCE_WINDOW`
        lines of any line mentioning "provenance" (case-insensitive).
    """
    lines = text.splitlines()
    anchor_indices = [i for i, line in enumerate(lines) if "provenance" in line.lower()]
    tokens: set[str] = set()
    for anchor in anchor_indices:
        lo = max(0, anchor - _PROVENANCE_WINDOW)
        hi = min(len(lines), anchor + _PROVENANCE_WINDOW + 1)
        for line in lines[lo:hi]:
            tokens.update(_CHECK_TOKEN_RE.findall(line))
    return tokens


def _check_provenance_gate_names(repo_root: Path) -> list[str]:
    """Return findings for provenance gate-step names drifting from the constant.

    ``pr_delta.PROVENANCE_GATE_STEPS`` is the single source of truth; the
    files in :data:`_PROVENANCE_PROSE_FILES` hand-name the same steps in
    prose. Two drift directions are flagged per file: a constant
    step missing from the file entirely, and a ``*_check`` token in the
    file's provenance prose that the constant does not contain (a stale
    name left behind by a rename).

    Args:
        repo_root: Git repo root.

    Returns:
        One finding string per drift; empty when consistent or when a
        prose surface is absent (self-skip).
    """
    findings: list[str] = []
    for rel_path in _PROVENANCE_PROSE_FILES:
        path = repo_root / rel_path
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # Word-boundary match, same tokenizer as the stale direction — a
        # step name buried inside a longer identifier is not a mention.
        mentioned = set(_CHECK_TOKEN_RE.findall(text))
        findings.extend(
            f"{rel_path}: provenance gate step '{step}' "
            "(pr_delta.PROVENANCE_GATE_STEPS) is not mentioned"
            for step in PROVENANCE_GATE_STEPS
            if step not in mentioned
        )
        # A `step_<name>` token is the step FUNCTION for `<name>` — strip
        # the prefix so function references validate against the constant.
        findings.extend(
            f"{rel_path}: provenance prose names '{token}', which is not in "
            "pr_delta.PROVENANCE_GATE_STEPS (stale step name?)"
            for token in sorted(_provenance_prose_tokens(text))
            if token.removeprefix("step_") not in PROVENANCE_GATE_STEPS
        )
    return findings


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

    root = get_repo_root()
    findings = _check_cli_coverage(root) + _check_provenance_gate_names(root)
    if findings:
        logger.error("Documentation drift detected:")
        for finding in findings:
            logger.error("  - %s", finding)
        return 1
    logger.info("Documentation claims consistent with repo state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
