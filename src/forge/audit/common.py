"""Shared helpers for the forge-audit-* CLI scripts.

Provides:
    - ``Scope`` enum for ``--scope full|changed`` flag.
    - ``iter_files()`` for walking the repo with scope + extension filters.
    - ``Severity`` + ``Finding`` for structured per-audit output.
    - ``write_log()`` for the ``code_health/audit_<name>.log`` convention.
    - ``make_audit_parser()`` for the shared CLI surface.

Every audit script uses these so the on-disk log format is uniform, and
agents can parse any ``audit_*.log`` with one schema.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from forge.git_utils import get_modified_files, repo_root


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


logger = logging.getLogger(__name__)


CODE_HEALTH_DIR = "code_health"

DEFAULT_ROOTS: tuple[str, ...] = (
    "src",
    "scripts",
    "tools",
    "projects",
    "tests",
    "test",
    "agents",
    "lib",
    "docs",
    "config",
    "data",
)

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    ".tox",
    "build",
    "dist",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".egg-info",
)


class Scope(StrEnum):
    """Audit scope selector."""

    FULL = "full"
    CHANGED = "changed"


class Severity(StrEnum):
    """Finding severity tier.

    Used for downstream sorting and report rendering. Agents may surface
    ``CRITICAL`` findings as blockers, ``HIGH`` as required fixes, and
    ``MEDIUM`` / ``LOW`` as informational.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    REVIEW = "review"


@dataclass(frozen=True)
class Finding:
    """One audit observation with provenance.

    Attributes:
        audit: Audit script name (e.g. ``"dup"``, ``"deps"``).
        severity: ``Severity`` tier.
        path: Repo-relative path to the file (``str`` for log stability).
        line: 1-based line number, or ``0`` if file-level.
        message: One-line human-readable summary.
        evidence: Optional multi-line context (code snippet, related paths).
    """

    audit: str
    severity: Severity
    path: str
    line: int
    message: str
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        """Render this finding as a single block in the log file.

        Returns:
            Multi-line string ending with a blank line.
        """
        head = f"[{self.severity.value.upper()}] {self.path}:{self.line} {self.message}"
        if not self.evidence:
            return head + "\n\n"
        body = "\n".join(f"    {line}" for line in self.evidence)
        return f"{head}\n{body}\n\n"


def make_audit_parser(prog: str, description: str) -> argparse.ArgumentParser:
    """Build the shared CLI surface for an audit script.

    Args:
        prog: Console-script name (e.g. ``"forge-audit-dup"``).
        description: One-line description shown in ``--help``.

    Returns:
        Parser with ``--scope``, ``--roots``, ``--output`` registered.
    """
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--scope",
        choices=[s.value for s in Scope],
        default=Scope.FULL.value,
        help="Audit scope. 'full' scans roots; 'changed' scans modified files vs main.",
    )
    parser.add_argument(
        "--roots",
        nargs="*",
        default=None,
        help="Source dirs to scan when --scope=full. Auto-detected if omitted.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override log path. Defaults to code_health/audit_<name>.log.",
    )
    return parser


def resolve_roots(roots: list[str] | None) -> list[Path]:
    """Resolve the effective scan roots.

    Args:
        roots: Explicit list from ``--roots``, or ``None`` for auto-detect.

    Returns:
        Existing absolute directories under the repo root.
    """
    root = repo_root()
    if roots:
        return [(root / r).resolve() for r in roots if (root / r).is_dir()]
    return [(root / r).resolve() for r in DEFAULT_ROOTS if (root / r).is_dir()]


def _is_excluded(path: Path) -> bool:
    """Return ``True`` if ``path`` lies under any default-excluded directory.

    Args:
        path: Absolute path to test.

    Returns:
        Whether the path should be skipped.
    """
    parts = set(path.parts)
    return any(ex in parts for ex in DEFAULT_EXCLUDES)


def iter_files(
    scope: Scope,
    roots: list[Path],
    *,
    suffix: str = ".py",
) -> Iterator[Path]:
    """Yield matching files under ``roots`` respecting ``scope``.

    For ``Scope.CHANGED``, defers to ``git_utils.get_modified_files`` so the
    list matches what pre-commit sees on a feature branch.

    Args:
        scope: ``FULL`` or ``CHANGED``.
        roots: Directories to walk (only used for ``FULL``).
        suffix: File extension filter (include the dot, e.g. ``".py"``).

    Yields:
        Absolute paths to matching files.
    """
    if scope is Scope.CHANGED:
        root = repo_root()
        for rel in get_modified_files(suffix=suffix):
            abs_path = (root / rel).resolve()
            if abs_path.is_file() and not _is_excluded(abs_path):
                yield abs_path
        return

    for r in roots:
        for path in r.rglob(f"*{suffix}"):
            if path.is_file() and not _is_excluded(path):
                yield path


def relpath(path: Path) -> str:
    """Render ``path`` relative to the repo root for log stability.

    Args:
        path: Absolute path.

    Returns:
        Repo-relative POSIX string. Falls back to ``str(path)`` if outside.
    """
    try:
        return path.resolve().relative_to(repo_root()).as_posix()
    except ValueError:
        return str(path)


def write_log(
    name: str,
    findings: Iterable[Finding],
    summary: str,
    *,
    output: Path | None = None,
) -> Path:
    """Write findings + summary to ``code_health/audit_<name>.log``.

    Output is overwritten on every run. The header includes a UTC timestamp
    so agents can detect staleness vs the newest source file.

    Args:
        name: Audit short name (e.g. ``"dup"``, ``"deps"``).
        findings: Iterable of ``Finding`` records, severity-ordered upstream.
        summary: One-paragraph wrap-up rendered above the per-finding list.
        output: Override path. Defaults to ``code_health/audit_<name>.log``.

    Returns:
        Path to the written log.
    """
    root = repo_root()
    log_dir = root / CODE_HEALTH_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = output if output is not None else log_dir / f"audit_{name}.log"

    findings_list = list(findings)
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")

    lines = [
        f"# forge-audit-{name}",
        f"# generated: {timestamp}",
        f"# findings: {len(findings_list)}",
        "",
        "## Summary",
        summary.strip() or "(no summary)",
        "",
        "## Findings",
        "",
    ]
    body = "".join(f.render() for f in findings_list) or "(none)\n"
    log_path.write_text("\n".join(lines) + body, encoding="utf-8")
    logger.info("wrote %s (%d findings)", log_path, len(findings_list))
    return log_path


def exit_code_for(findings: Iterable[Finding]) -> int:
    """Map findings to a process exit code.

    Args:
        findings: Iterable of ``Finding`` records produced by an audit.

    Returns:
        ``0`` if all findings are ``REVIEW`` / ``LOW`` (informational), else
        ``1``. This lets pre-commit hooks gate on substantive findings without
        blocking on every claim-extraction candidate.
    """
    blocking = {Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM}
    return 1 if any(f.severity in blocking for f in findings) else 0


def count_by_severity(findings: Iterable[Finding]) -> dict[Severity, int]:
    """Tally findings per severity tier.

    Args:
        findings: Iterable of ``Finding`` records.

    Returns:
        Mapping from every ``Severity`` value to its count. Tiers with no
        findings map to ``0``, so callers can index without guarding.
    """
    counts = dict.fromkeys(Severity, 0)
    for f in findings:
        counts[f.severity] += 1
    return counts
