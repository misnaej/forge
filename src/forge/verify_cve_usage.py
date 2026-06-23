"""verify-forge-cve-usage — second-stage CVE filter on top of pip-audit.

``pip-audit`` flags vulnerable **packages** — every CVE in a dependency's
advisory database, whether or not the project uses the vulnerable code
path. On a large tree that is chronic noise: a CVE in a function the
project never calls is reported every run and trains contributors to
ignore the step. This CLI adds the second stage — it flags vulnerable
**usage**:

1. Run ``pip-audit`` and collect the **live** set of advisory / CVE IDs.
2. Read the consumer's ``cve_usage_patterns.toml`` — a map of
   ``CVE-ID → {package, patterns, risk, mitigation}``.
3. For every CVE that is *both* live **and** in the map, grep the source
   roots for the patterns and report only real matches, with ``file:line``,
   the risk, and the mitigation.

**Self-maintaining:** a pattern is checked only while its CVE is *currently*
reported by pip-audit. Upgrade the package → the CVE leaves the report →
the pattern is skipped → the warning disappears. No stale list to prune.

Forge ships the **engine**; the pattern map is **consumer config** (every
repo's vulnerable surface differs). The CLI skips cleanly (exit 0) when the
map is absent or pip-audit is unavailable — advisory, never a hard fail
(FOUNDATION §15). Writes ``code_health/cve_usage.log``.

Usage:

- ``verify-forge-cve-usage`` — scan and write the log.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import tomllib
from typing import TYPE_CHECKING, NamedTuple

from forge.config import resolve_tool_roots
from forge.git_utils import (
    capturing_to_step_log,
    configure_cli_logging,
    repo_root,
)


if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


PATTERN_FILE = "cve_usage_patterns.toml"


class Finding(NamedTuple):
    """One matched vulnerable-usage occurrence.

    Attributes:
        cve: The advisory / CVE ID (the pattern-map key).
        package: The vulnerable package the CVE belongs to.
        path: Repo-relative file path where a pattern matched.
        line: 1-based line number of the match.
        risk: Human-readable note on when the usage is actually exploitable.
        mitigation: How to neutralize the risk without upgrading, if possible.
    """

    cve: str
    package: str
    path: str
    line: int
    risk: str
    mitigation: str


def load_patterns(root: Path) -> dict[str, dict[str, object]] | None:
    """Load the consumer's ``cve_usage_patterns.toml`` map.

    Args:
        root: Repo root the pattern file sits at.

    Returns:
        The parsed ``CVE-ID → entry`` map, or ``None`` when the file is
        absent (the signal to skip the check) or cannot be parsed.
    """
    path = root / PATTERN_FILE
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("cve_usage: could not parse %s", path)
        return None
    # Accept either a top-level table of CVE entries or a nested
    # [cve] / [patterns] wrapper; normalize to the flat CVE-keyed map.
    section = data.get("cve_usage", data)
    return {k: v for k, v in section.items() if isinstance(v, dict)}


def active_cve_ids(root: Path) -> set[str] | None:
    """Return the advisory / CVE IDs pip-audit currently reports.

    Args:
        root: Repo root (pip-audit runs against the active environment).

    Returns:
        The set of live IDs (each ``id`` plus its ``aliases``, so a
        CVE-keyed map matches a PYSEC-keyed report and vice versa), or
        ``None`` when ``pip-audit`` is missing or its output is unparseable
        — the signal to skip cleanly (FOUNDATION §15).
    """
    if shutil.which("pip-audit") is None:
        logger.info("(pip-audit not on PATH — skipped)")
        return None
    proc = subprocess.run(
        ["pip-audit", "--skip-editable", "--format=json"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.info("(pip-audit produced no parseable JSON — skipped)")
        return None
    ids: set[str] = set()
    for dep in data.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            if vuln.get("id"):
                ids.add(vuln["id"])
            ids.update(a for a in vuln.get("aliases", []) if a)
    return ids


def _iter_source_lines(root: Path, exclude: str) -> Iterable[tuple[str, int, str]]:
    """Yield ``(repo_relative_path, line_no, text)`` for every source line.

    Walks the layout-aware scan roots (the shared
    :func:`forge.config.resolve_tool_roots`, so it honors
    ``[tool.forge].source_dirs`` / ``[tool.forge.cve_usage].paths``), reading
    ``.py`` files. The pattern file itself is excluded — it contains the
    patterns verbatim and would self-match.

    Args:
        root: Repo root.
        exclude: Repo-relative path to skip (the pattern config file).

    Yields:
        One ``(path, 1-based line number, line text)`` per source line.
    """
    for rel_root in resolve_tool_roots(root, "cve_usage", include_tests=True):
        for py in sorted((root / rel_root).rglob("*.py")):
            rel = str(py.relative_to(root))
            if rel == exclude:
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                yield rel, lineno, line


def scan(
    root: Path,
    patterns: dict[str, dict[str, object]],
    active: set[str],
) -> list[Finding]:
    """Grep the source for the patterns of every active, mapped CVE.

    Only CVEs that are both live (in *active*) and present in *patterns* are
    checked. Comment lines (first non-space char ``#``) are skipped — a
    pattern string inside a comment is not real usage.

    Args:
        root: Repo root.
        patterns: The consumer ``CVE-ID → entry`` map.
        active: Advisory / CVE IDs pip-audit currently reports.

    Returns:
        One :class:`Finding` per matched line, in scan order.
    """
    checked = {cve: patterns[cve] for cve in patterns if cve in active}
    if not checked:
        return []
    compiled: list[tuple[str, dict[str, object], re.Pattern[str]]] = []
    for cve, entry in checked.items():
        raw_patterns = entry.get("patterns", [])
        if not isinstance(raw_patterns, list):
            continue
        compiled.extend(
            (cve, entry, re.compile(raw))
            for raw in raw_patterns
            if isinstance(raw, str)
        )
    findings: list[Finding] = []
    for rel, lineno, line in _iter_source_lines(root, PATTERN_FILE):
        if line.lstrip().startswith("#"):
            continue
        for cve, entry, rx in compiled:
            if rx.search(line):
                findings.append(
                    Finding(
                        cve=cve,
                        package=str(entry.get("package", "?")),
                        path=rel,
                        line=lineno,
                        risk=str(entry.get("risk", "")),
                        mitigation=str(entry.get("mitigation", "")),
                    )
                )
    return findings


def _render(findings: list[Finding]) -> str:
    """Render findings as the ``code_health/cve_usage.log`` body.

    Args:
        findings: The matched usages.

    Returns:
        A human-readable report; a clean one-liner when nothing matched.
    """
    if not findings:
        return "No vulnerable usage found for any currently-reported CVE."
    lines = [f"{len(findings)} vulnerable-usage finding(s):", ""]
    for f in findings:
        lines.append(f"⚠️  {f.cve} ({f.package}) — {f.path}:{f.line}")
        if f.risk:
            lines.append(f"    risk:       {f.risk}")
        if f.mitigation:
            lines.append(f"    mitigation: {f.mitigation}")
    return "\n".join(lines)


def main() -> int:
    """CLI entry point.

    Returns:
        ``1`` when vulnerable usage is found (so the pre-commit step and CI
        render a WARN), ``0`` when clean or skipped (no pattern file, or
        pip-audit unavailable). The check is **advisory** — the step marks it
        non-blocking regardless; the exit code only signals findings.
    """
    argparse.ArgumentParser(
        prog="verify-forge-cve-usage",
        description=(
            "Second-stage CVE filter: report only CVEs whose vulnerable "
            "code path is actually used. Reads cve_usage_patterns.toml; "
            "skips cleanly when absent or pip-audit is unavailable."
        ),
    ).parse_args()

    root = repo_root()
    with capturing_to_step_log(root, "cve_usage"):
        patterns = load_patterns(root)
        if patterns is None:
            logger.info("(no %s — skipped)", PATTERN_FILE)
            return 0
        active = active_cve_ids(root)
        if active is None:
            return 0
        findings = scan(root, patterns, active)
        logger.info("%s", _render(findings))
        return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
