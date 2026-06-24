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
- ``verify-forge-cve-usage --audit-json code_health/pip_audit.json`` — reuse
  the ``pip_audit`` step's findings instead of invoking pip-audit again, so the
  two steps share one scan per commit (#78).
- ``verify-forge-cve-usage --list-inactive`` — report mapped CVEs no longer in
  pip-audit's live report (dormant prune candidates). Read-only, exits 0, never
  edits the map (#80).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from forge import pip_audit_json
from forge.config import resolve_tool_roots
from forge.git_utils import (
    capturing_to_step_log,
    configure_cli_logging,
    repo_root,
)


if TYPE_CHECKING:
    from collections.abc import Iterable


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


def active_cve_ids(root: Path, audit_json: Path | None = None) -> set[str] | None:
    """Return the advisory / CVE IDs pip-audit currently reports.

    Args:
        root: Repo root (pip-audit runs against the active environment; a
            relative *audit_json* path resolves against it).
        audit_json: Optional path to a pip-audit JSON sidecar written by the
            ``pip_audit`` pre-commit step. When given, its contents are read
            instead of invoking pip-audit, so the two steps share **one** scan
            per commit (#78). A relative path resolves against *root*.

    Returns:
        The set of live IDs (each ``id`` plus its ``aliases``, so a CVE-keyed
        map matches a PYSEC-keyed report and vice versa), or ``None`` when the
        sidecar is absent / unparseable, or pip-audit is missing / unparseable
        — the signal to skip cleanly (FOUNDATION §15).
    """
    if audit_json is not None:
        path = audit_json if audit_json.is_absolute() else root / audit_json
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.info("(no usable pip-audit sidecar at %s — skipped)", path)
            return None
        return pip_audit_json.ids_from_data(data)
    run = pip_audit_json.run_json(root)
    if run is None:
        logger.info("(pip-audit not on PATH — skipped)")
        return None
    if run.data is None:
        logger.info("(pip-audit produced no parseable JSON — skipped)")
        return None
    return pip_audit_json.ids_from_data(run.data)


def _iter_source_lines(root: Path) -> Iterable[tuple[str, int, str]]:
    """Yield ``(repo_relative_path, line_no, text)`` for every source line.

    Walks the layout-aware scan roots (the shared
    :func:`forge.config.resolve_tool_roots`, so it honors
    ``[tool.forge].source_dirs`` / ``[tool.forge.cve_usage].paths``), reading
    only ``.py`` files — so the ``.toml`` pattern map is inherently never
    scanned (it would otherwise self-match its own patterns).

    Args:
        root: Repo root.

    Yields:
        One ``(path, 1-based line number, line text)`` per source line.
    """
    for rel_root in resolve_tool_roots(root, "cve_usage", include_tests=True):
        for py in sorted((root / rel_root).rglob("*.py")):
            rel = str(py.relative_to(root))
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
        # Patterns come from the consumer's committed cve_usage_patterns.toml —
        # same trust level as pyproject.toml. No runtime sanitization: a
        # pathological regex would only slow the committer's own pre-commit.
        compiled.extend(
            (cve, entry, re.compile(raw))
            for raw in raw_patterns
            if isinstance(raw, str)
        )
    findings: list[Finding] = []
    for rel, lineno, line in _iter_source_lines(root):
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


def inactive_cves(
    patterns: dict[str, dict[str, object]],
    active: set[str],
) -> list[tuple[str, str]]:
    """Return mapped CVE IDs that pip-audit is *not* currently reporting.

    These are the dormant entries a maintainer may want to prune from
    ``cve_usage_patterns.toml`` — patched/upgraded away, so never evaluated.
    The tool only *reports* them: a CVE can drop off pip-audit's report
    transiently (a different env, pip-audit offline, a temporary downgrade),
    so auto-deletion on this signal would be unsafe (#80).

    Args:
        patterns: The consumer ``CVE-ID → entry`` map.
        active: Advisory / CVE IDs pip-audit currently reports.

    Returns:
        Sorted ``(cve, package)`` pairs for each mapped CVE absent from
        *active*.
    """
    dormant = [
        (cve, str(entry.get("package", "?")))
        for cve, entry in patterns.items()
        if cve not in active
    ]
    return sorted(dormant)


def _render_inactive(dormant: list[tuple[str, str]]) -> str:
    """Render the ``--list-inactive`` report body.

    Args:
        dormant: Mapped-but-not-live ``(cve, package)`` pairs.

    Returns:
        A human-readable list, or a clean one-liner when every mapped CVE is
        still live.
    """
    if not dormant:
        return "All mapped CVEs are currently live in pip-audit's report."
    lines = [
        f"{len(dormant)} mapped CVE(s) not in the current pip-audit report "
        "(prune candidates — verify before removing):",
        "",
    ]
    lines.extend(f"  {cve} ({package})" for cve, package in dormant)
    return "\n".join(lines)


def _run_list_inactive(root: Path, audit_json: Path | None) -> int:
    """Print dormant mapped CVEs; never mutates the map. Always exits 0.

    Args:
        root: Repo root.
        audit_json: Optional pip-audit JSON sidecar to read instead of
            invoking pip-audit (see :func:`active_cve_ids`).

    Returns:
        Always ``0`` — purely informational; writes and edits nothing.
    """
    patterns = load_patterns(root)
    if patterns is None:
        logger.info("(no %s — nothing to list)", PATTERN_FILE)
        return 0
    active = active_cve_ids(root, audit_json)
    if active is None:
        logger.info("(pip-audit unavailable — cannot determine inactive CVEs)")
        return 0
    logger.info("%s", _render_inactive(inactive_cves(patterns, active)))
    return 0


def main() -> int:
    """CLI entry point.

    Returns:
        ``1`` when vulnerable usage is found (so the pre-commit step and CI
        render a WARN), ``0`` when clean, skipped (no pattern file, or
        pip-audit unavailable), or run in ``--list-inactive`` mode. The check
        is **advisory** — the step marks it non-blocking regardless; the exit
        code only signals findings.
    """
    parser = argparse.ArgumentParser(
        prog="verify-forge-cve-usage",
        description=(
            "Second-stage CVE filter: report only CVEs whose vulnerable "
            "code path is actually used. Reads cve_usage_patterns.toml; "
            "skips cleanly when absent or pip-audit is unavailable."
        ),
    )
    parser.add_argument(
        "--audit-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Read pip-audit findings from this JSON sidecar instead of "
        "invoking pip-audit (shares one scan with the pip_audit step).",
    )
    parser.add_argument(
        "--list-inactive",
        action="store_true",
        help="Report mapped CVEs no longer in pip-audit's live report "
        "(prune candidates). Read-only, exits 0, never edits the map.",
    )
    args = parser.parse_args()

    root = repo_root()
    if args.list_inactive:
        return _run_list_inactive(root, args.audit_json)
    with capturing_to_step_log(root, "cve_usage"):
        patterns = load_patterns(root)
        if patterns is None:
            logger.info("(no %s — skipped)", PATTERN_FILE)
            return 0
        active = active_cve_ids(root, args.audit_json)
        if active is None:
            return 0
        findings = scan(root, patterns, active)
        logger.info("%s", _render(findings))
        return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
