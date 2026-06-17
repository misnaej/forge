"""forge-audit-suppressions: list every lint / type / coverage suppression.

Every ``# noqa`` is a place where a developer pushed back on a rule.
This audit surfaces those decisions so reviewers can ask whether the
rule was right and the code is wrong.

Scans every ``.py`` file for these patterns:

    * ``# noqa[: CODE, CODE, ...]`` — ruff / pyflakes silencer
    * ``# type: ignore[...]`` — type-checker silencer
    * ``# pragma: no cover`` — coverage skip

For each ``# noqa`` rule code we resolve the canonical rule name and
one-line summary by invoking ``ruff rule <CODE> --output-format=json``
(cached per code per run). Bare suppressions (no code listed) are
escalated because they silence *every* rule on that line.

Severity:

    * HIGH   — ``# noqa`` with no specific code
    * MEDIUM — ``# noqa: CODE`` (specific but still suspicious)
    * MEDIUM — ``# type: ignore`` with no specific code
    * LOW    — ``# type: ignore[...]`` (specific)
    * LOW    — ``# pragma: no cover``

The agent reading this log is expected to articulate, for each entry,
whether the rule being suppressed hides a design problem (e.g. PLR0913
hides missing dataclasses, F841 hides dead code).
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from typing import TYPE_CHECKING

from forge.audit.common import (
    Finding,
    Scope,
    Severity,
    count_by_severity,
    exit_code_for,
    iter_files,
    make_audit_parser,
    relpath,
    resolve_roots,
    write_log,
)
from forge.git_utils import configure_cli_logging, require_cli


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


_NOQA_RE = re.compile(r"#\s*noqa(?:\s*:\s*([A-Za-z0-9_,\s]+))?", re.IGNORECASE)
_TYPE_IGNORE_RE = re.compile(r"#\s*type:\s*ignore(?:\[([^\]]+)\])?", re.IGNORECASE)
_PRAGMA_NO_COVER_RE = re.compile(r"#\s*pragma:\s*no[\s-]?cover", re.IGNORECASE)


@dataclass(frozen=True)
class SuppressionsConfig:
    """Tunable knobs for the suppressions audit.

    Attributes:
        output: Optional log-path override.
    """

    output: Path | None = None


def _parse_codes(raw: str | None) -> list[str]:
    """Split a comma-separated suppression-code string into trimmed codes.

    Args:
        raw: Raw match group (may be ``None`` or empty).

    Returns:
        Upper-cased non-empty code tokens.
    """
    if not raw:
        return []
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def resolve_ruff_rule(
    code: str,
    cache: dict[str, tuple[str, str] | None],
) -> tuple[str, str] | None:
    """Return ``(name, summary)`` for a ruff rule code, or ``None`` if unknown.

    Cached across calls so we hit the ``ruff rule`` CLI at most once per
    distinct code per audit run.

    Args:
        code: Rule code (e.g. ``"E501"``, ``"PLR0913"``).
        cache: Shared cache mapping code → resolved tuple (or ``None``).

    Returns:
        Tuple ``(rule_name, one_line_summary)`` or ``None`` if the rule
        cannot be resolved (unknown code, ruff missing, parse failure).
    """
    if code in cache:
        return cache[code]
    try:
        proc = subprocess.run(
            ["ruff", "rule", code, "--output-format=json"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        logger.debug("ruff invocation failed for %s: %s", code, exc)
        cache[code] = None
        return None
    if proc.returncode != 0:
        cache[code] = None
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        cache[code] = None
        return None
    name = str(data.get("name", code))
    summary = str(data.get("summary", "")).strip()
    cache[code] = (name, summary)
    return cache[code]


def _noqa_findings(
    path: str,
    line_no: int,
    line: str,
    rule_cache: dict[str, tuple[str, str] | None],
) -> list[Finding]:
    """Build findings for any ``# noqa`` directive on ``line``.

    Args:
        path: Repo-relative source path.
        line_no: 1-based line number.
        line: The full source line text.
        rule_cache: Shared ``ruff rule`` resolution cache.

    Returns:
        Zero or more findings — one per ``# noqa`` occurrence on the line.
    """
    findings: list[Finding] = []
    for match in _NOQA_RE.finditer(line):
        codes = _parse_codes(match.group(1))
        if not codes:
            findings.append(
                Finding(
                    audit="suppressions",
                    severity=Severity.HIGH,
                    path=path,
                    line=line_no,
                    message="bare `# noqa` silences every rule on this line",
                    evidence=(line.rstrip(),),
                ),
            )
            continue
        for code in codes:
            resolved = resolve_ruff_rule(code, rule_cache)
            if resolved is not None:
                descriptor = f"{code} ({resolved[0]}): {resolved[1]}"
            else:
                descriptor = f"{code} (rule unresolved)"
            findings.append(
                Finding(
                    audit="suppressions",
                    severity=Severity.MEDIUM,
                    path=path,
                    line=line_no,
                    message=(
                        f"`# noqa: {code}` — does suppressing "
                        "this hide a design problem?"
                    ),
                    evidence=(line.rstrip(), descriptor),
                ),
            )
    return findings


def _type_ignore_findings(path: str, line_no: int, line: str) -> list[Finding]:
    """Build findings for any ``# type: ignore`` directive on ``line``.

    Args:
        path: Repo-relative source path.
        line_no: 1-based line number.
        line: The full source line text.

    Returns:
        Zero or more findings — one per occurrence on the line.
    """
    findings: list[Finding] = []
    for match in _TYPE_IGNORE_RE.finditer(line):
        codes = match.group(1)
        if not codes:
            findings.append(
                Finding(
                    audit="suppressions",
                    severity=Severity.MEDIUM,
                    path=path,
                    line=line_no,
                    message="bare `# type: ignore` — silences every type error",
                    evidence=(line.rstrip(),),
                ),
            )
        else:
            findings.append(
                Finding(
                    audit="suppressions",
                    severity=Severity.LOW,
                    path=path,
                    line=line_no,
                    message=f"`# type: ignore[{codes}]`",
                    evidence=(line.rstrip(),),
                ),
            )
    return findings


def _pragma_findings(path: str, line_no: int, line: str) -> list[Finding]:
    """Build findings for ``# pragma: no cover`` directives on ``line``.

    Args:
        path: Repo-relative source path.
        line_no: 1-based line number.
        line: The full source line text.

    Returns:
        Zero or one finding per pragma occurrence on the line.
    """
    if not _PRAGMA_NO_COVER_RE.search(line):
        return []
    return [
        Finding(
            audit="suppressions",
            severity=Severity.LOW,
            path=path,
            line=line_no,
            message="`# pragma: no cover` — coverage exempted, is it tested elsewhere?",
            evidence=(line.rstrip(),),
        ),
    ]


def _iter_comments(text: str) -> list[tuple[int, str]]:
    """Yield ``(line_no, line_text)`` for every line that holds a COMMENT.

    Uses ``tokenize`` so suppression-like substrings inside string literals,
    docstrings, or regex source are ignored. Important: our own source
    references ``# noqa`` in docstrings and regexes; without this filter
    the audit reports itself.

    Args:
        text: Full file contents.

    Returns:
        List of ``(line_no, line_text)`` tuples for lines that contain a
        Python COMMENT token.
    """
    pairs: list[tuple[int, str]] = []
    seen_lines: set[int] = set()
    source_lines = text.splitlines()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            line_no = tok.start[0]
            if line_no in seen_lines:
                continue
            seen_lines.add(line_no)
            if 1 <= line_no <= len(source_lines):
                pairs.append((line_no, source_lines[line_no - 1]))
    except tokenize.TokenError as exc:
        logger.debug("tokenize failed: %s", exc)
    return pairs


def _scan_file(
    path: Path,
    rule_cache: dict[str, tuple[str, str] | None],
) -> list[Finding]:
    """Scan one source file for suppression directives.

    Args:
        path: Absolute file path.
        rule_cache: Shared ``ruff rule`` cache.

    Returns:
        All findings discovered in this file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("skipping %s: %s", path, exc)
        return []
    rel = relpath(path)
    findings: list[Finding] = []
    for line_no, line in _iter_comments(text):
        findings.extend(_noqa_findings(rel, line_no, line, rule_cache))
        findings.extend(_type_ignore_findings(rel, line_no, line))
        findings.extend(_pragma_findings(rel, line_no, line))
    return findings


def run(scope: Scope, roots: list[Path], config: SuppressionsConfig) -> int:
    """Execute the suppressions audit.

    Args:
        scope: ``FULL`` or ``CHANGED``.
        roots: Scan roots for ``FULL`` scope.
        config: Tunable knobs (currently just log-path override).

    Returns:
        Process exit code (0 = clean / LOW-only, 1 otherwise).
    """
    rule_cache: dict[str, tuple[str, str] | None] = {}
    findings: list[Finding] = []
    for path in iter_files(scope, roots):
        findings.extend(_scan_file(path, rule_cache))
    counts = count_by_severity(findings)
    summary = (
        f"Found {len(findings)} suppression(s): "
        f"{counts[Severity.HIGH]} HIGH (bare), "
        f"{counts[Severity.MEDIUM]} MEDIUM, "
        f"{counts[Severity.LOW]} LOW."
    )
    write_log("suppressions", findings, summary, output=config.output)
    return exit_code_for(findings)


def main() -> int:
    """CLI entry point for ``forge-audit-suppressions``.

    Returns:
        Process exit code.
    """
    parser = make_audit_parser(
        prog="forge-audit-suppressions",
        description="List lint/type/coverage suppressions and resolve rule names.",
    )
    args = parser.parse_args()
    require_cli("ruff", caller="forge-audit-suppressions")
    scope = Scope(args.scope)
    roots = resolve_roots(args.roots)
    config = SuppressionsConfig(output=args.output)
    return run(scope, roots, config)


if __name__ == "__main__":
    sys.exit(main())
