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
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from forge.config import declared_layout_dirs, load_config, summarize_paths
from forge.git_utils import (
    code_health_dir,
    get_modified_files,
    get_untracked_files,
    produced_at_stamp,
    repo_root,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


logger = logging.getLogger(__name__)


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


def sanitize_log_text(text: str) -> str:
    """Escape non-printable characters so a value cannot forge log lines.

    ``code_health/*.log`` files are trusted ground truth for agents
    (FOUNDATION §13), while finding paths and messages can carry
    untrusted content — git filenames, or config-supplied strings like
    layer names. A newline or ANSI escape embedded there could inject a
    spoofed finding line; escaping to ``repr``-style sequences keeps
    every finding on its own line. Tabs and printable text pass through.

    Args:
        text: Raw text destined for a log line.

    Returns:
        The text with every non-printable character (except tab) escaped —
        notably control characters and the Unicode line separators
        ``str.splitlines`` also breaks on.
    """
    return "".join(
        ch if ch == "\t" or ch.isprintable() else repr(ch)[1:-1] for ch in text
    )


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
        key: Optional stable identity for this finding, chosen by the
            audit to survive edits elsewhere in the file (never
            ``path:line``, whose line half shifts on any insertion above
            it). Rendered as a ``key=`` line in the log block so agents
            and humans can address one finding across runs. Empty when
            the audit defines no key.
    """

    audit: str
    severity: Severity
    path: str
    line: int
    message: str
    evidence: tuple[str, ...] = field(default_factory=tuple)
    key: str = ""

    def render(self) -> str:
        """Render this finding as a single block in the log file.

        Path, message, evidence, and key are control-character-escaped
        via :func:`sanitize_log_text` — untrusted content cannot inject
        a spoofed finding line.

        Returns:
            Multi-line string ending with a blank line.
        """
        head = (
            f"[{self.severity.value.upper()}] "
            f"{sanitize_log_text(self.path)}:{self.line} "
            f"{sanitize_log_text(self.message)}"
        )
        parts = [head]
        if self.key:
            parts.append(f"    key={sanitize_log_text(self.key)}")
        parts.extend(f"    {sanitize_log_text(line)}" for line in self.evidence)
        return "\n".join(parts) + "\n\n"


def under_module_prefix(module: str, prefix: str) -> bool:
    """Return whether *module* equals *prefix* or is a dotted child of it.

    The one prefix matcher shared by every module-grouping consumer
    (``forge-gen-c4`` components, ``forge-audit-layering`` layers) — a
    ``forge.audit`` prefix matches ``forge.audit`` and ``forge.audit.deps``
    but not ``forge.auditor``.

    Args:
        module: Dotted module name (e.g. ``"forge.audit.deps"``).
        prefix: Dotted package prefix (e.g. ``"forge.audit"``).

    Returns:
        True when *module* is *prefix* itself or nested beneath it.
    """
    return module == prefix or module.startswith(f"{prefix}.")


def make_audit_parser(
    prog: str, description: str, *, honours_scope: bool = True
) -> argparse.ArgumentParser:
    """Build the shared CLI surface for an audit script.

    Args:
        prog: Console-script name (e.g. ``"forge-audit-dup"``).
        description: One-line description shown in ``--help``.
        honours_scope: Whether ``--scope changed`` narrows this audit.
            ``False`` keeps the flag for a uniform CLI but says, truthfully,
            that the audit reads everything either way.

    Returns:
        Parser with ``--scope``, ``--roots``, ``--output`` registered.
    """
    parser = argparse.ArgumentParser(prog=prog, description=description)
    scope_help = (
        (
            "Audit scope. 'full' scans roots; 'changed' limits the report to "
            "tracked files modified vs the configured base branch (committed, "
            "staged or not); untracked files are never treated as changed, "
            "and the log summary names them."
        )
        if honours_scope
        else (
            "Accepted for parity with the other forge-audit-* CLIs; this "
            "audit reads every file it covers, untracked ones included, at "
            "either scope."
        )
    )
    parser.add_argument(
        "--scope",
        choices=[s.value for s in Scope],
        default=Scope.FULL.value,
        help=scope_help,
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
        help="Override log path. Defaults to audit_<name>.log in the log "
        "directory (code_health/, or $FORGE_CODE_HEALTH_DIR when set).",
    )
    return parser


def resolve_roots(roots: list[str] | None) -> list[Path]:
    """Resolve the effective scan roots.

    A repo that declared its layout gets exactly that layout: when
    ``[tool.forge].source_dirs`` is set, the default scan is the declared
    ``source_dirs`` + ``test_dirs`` — not the broad ``DEFAULT_ROOTS``
    guess, whose extra directories (docs, config, data, vendored trees)
    are where spurious audit findings live. The guess remains only for
    repos with no declared layout.

    Args:
        roots: Explicit list from ``--roots``, or ``None`` for the
            declared-layout / auto-detect fallback.

    Returns:
        Existing absolute directories under the repo root.
    """
    root = repo_root()
    if roots:
        return [(root / r).resolve() for r in roots if (root / r).is_dir()]
    declared = declared_layout_dirs(root)
    if declared is not None:
        return [(root / r).resolve() for r in declared]
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
    list matches what pre-commit sees on a feature branch — untracked files
    included in neither; :func:`untracked_summary_line` names them.

    Args:
        scope: ``FULL`` or ``CHANGED``.
        roots: Directories to walk (only used for ``FULL``).
        suffix: File extension filter (include the dot, e.g. ``".py"``).

    Yields:
        Absolute paths to matching files.
    """
    if scope is Scope.CHANGED:
        root = repo_root()
        base_branch = load_config(root).base_branch
        for rel in get_modified_files(
            suffix=suffix, repo_root=root, base_branch=base_branch
        ):
            abs_path = (root / rel).resolve()
            if abs_path.is_file() and not _is_excluded(abs_path):
                yield abs_path
        return

    for r in roots:
        for path in r.rglob(f"*{suffix}"):
            if path.is_file() and not _is_excluded(path):
                yield path


def select_like_audit(
    root: Path,
    rels: list[str],
    *,
    suffix: str | tuple[str, ...] = ".py",
    roots: list[Path] | None = None,
) -> list[str]:
    """Keep the *rels* an audit's file selection would include.

    The filters mirror the audit's own: *suffix*, the default-excluded
    directories (as :func:`iter_files` applies them), and — for an audit
    whose findings come from a walk of its roots — those *roots*.

    Args:
        root: Git repo root the paths are relative to.
        rels: Repo-relative candidate paths.
        suffix: File extension(s) the audit reads, with the dot.
        roots: Absolute scan roots to keep files under; ``None`` keeps
            files anywhere (an audit reading the whole diff).

    Returns:
        The kept paths, in input order.
    """
    kept: list[str] = []
    for rel in rels:
        abs_path = (root / rel).resolve()
        if not rel.endswith(suffix) or _is_excluded(abs_path):
            continue
        if roots is not None and not any(abs_path.is_relative_to(r) for r in roots):
            continue
        kept.append(rel)
    return kept


def untracked_summary_line(
    scope: Scope,
    *,
    suffix: str | tuple[str, ...] = ".py",
    roots: list[Path] | None = None,
    root: Path | None = None,
) -> str:
    """Return the note naming untracked files a changed-files run left out.

    A changed-files selection is git's diff, which never lists an
    untracked file, so such a file is never treated as changed — most
    audits then do not read it at all — and saying so beats a clean log a
    reader takes to cover it. A full run walks the disk and sees untracked
    files, so it gets no note. Each audit appends the result to its own
    summary, passing its own suffix and roots.

    Args:
        scope: The scope the audit ran at.
        suffix: File extension(s) the audit reads, with the dot.
        roots: As for :func:`select_like_audit`.
        root: Git repo root; defaults to the process-wide repo root the
            audit itself runs against.

    Returns:
        ``""`` for a full run, when nothing is left out, or outside a git
        work tree (the ``git`` query fails there and yields no output);
        otherwise the note, starting with a newline so it appends to a
        summary as is. Gitignored files are never named.
    """
    if scope is not Scope.CHANGED:
        return ""
    root = root if root is not None else repo_root()
    skipped = select_like_audit(
        root,
        get_untracked_files(suffix="", repo_root=root),
        suffix=suffix,
        roots=roots,
    )
    if not skipped:
        return ""
    return (
        f"\nUntracked, not treated as changed: {len(skipped)} — "
        f"{summarize_paths(skipped)}. Changed mode reads git's diff, which "
        "lists no untracked file; add them if they belong to this work, "
        "leave them out if not."
    )


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


def read_finding_count(log_text: str) -> int:
    """Return the ``# findings: N`` count :func:`write_log` puts in a log header.

    Args:
        log_text: Full log contents.

    Returns:
        The count, or ``-1`` when the header is missing or malformed.
    """
    for line in log_text.splitlines()[:10]:
        if line.startswith("# findings:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return -1
    return -1


def read_scope(log_text: str) -> str | None:
    """Return the ``# scope:`` value :func:`write_log` puts in a log header.

    Args:
        log_text: Full log contents.

    Returns:
        ``"full"`` / ``"changed"``, or ``None`` when the log predates the
        line or was written without a scope — unknown, never assumed full.
    """
    for line in log_text.splitlines()[:10]:
        if line.startswith("# scope:"):
            return line.split(":", 1)[1].strip() or None
    return None


def write_log(
    name: str,
    findings: Iterable[Finding],
    summary: str,
    *,
    output: Path | None = None,
    scope: Scope | None = None,
) -> Path:
    """Write findings + summary to ``code_health/audit_<name>.log``.

    Output is overwritten on every run. The first line is the
    :func:`forge.git_utils.produced_at_stamp` naming the tree the findings
    describe, so a reader judges freshness by tree identity, never by
    comparing timestamps.

    Args:
        name: Audit short name (e.g. ``"dup"``, ``"deps"``).
        findings: Iterable of ``Finding`` records, severity-ordered upstream.
        summary: One-paragraph wrap-up rendered above the per-finding list.
        output: Override path. Defaults to ``code_health/audit_<name>.log``.
        scope: The scope the audit ran at. Written as a ``# scope:`` header
            line, because a changed-files run writes the same file a full
            run does and a reader cannot tell them apart otherwise
            (FOUNDATION §13). Placed within the first ten lines, which is
            all :func:`read_finding_count` scans.

    Returns:
        Path to the written log.
    """
    root = repo_root()
    log_dir = code_health_dir(root)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = output if output is not None else log_dir / f"audit_{name}.log"

    findings_list = list(findings)

    lines = [
        produced_at_stamp(root),
        f"# forge-audit-{name}",
        *([f"# scope: {scope.value}"] if scope is not None else []),
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
