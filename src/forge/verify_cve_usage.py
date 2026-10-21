"""verify-forge-cve-usage — second-stage CVE filter on top of pip-audit.

``pip-audit`` flags vulnerable **packages** — every CVE in a dependency's
advisory database, whether or not the project uses the vulnerable code
path. On a large tree that is chronic noise: a CVE in a function the
project never calls is reported every run and trains contributors to
ignore the step. This CLI adds the second stage — it flags vulnerable
**usage**:

1. Run ``pip-audit`` and collect the **live** advisories.
2. Read the consumer's ``cve_usage_patterns.toml`` — a map of
   ``CVE-ID → {package, patterns, risk, mitigation}``.
3. For every CVE that is *both* live **and** in the map, grep the source
   roots for the patterns and report only real matches, with ``file:line``,
   the risk, and the mitigation.

The check can only search for a CVE someone wrote a pattern for, so the
report says how many of the reported CVEs it actually checked and names
every one it did not: a CVE with no map entry, an entry with no usable
pattern (not a list, empty, or a regex that does not compile). Any
unchecked CVE exits 1, so the step renders WARN rather than a clean pass
that looked at nothing.

**Self-pruning, not self-maintaining:** a pattern is checked only while its
CVE is *currently* reported by pip-audit. Upgrade the package → the CVE
leaves the report → its pattern stops being evaluated, so a resolved CVE
leaves no stale entry to prune. A *newly* reported CVE is the opposite
case: nothing checks it until someone writes its pattern, which is why the
report names it.

Forge ships the **engine**; the pattern map is **consumer config** (every
repo's vulnerable surface differs). The CLI exits 0 without checking when
the map is absent or pip-audit is unavailable — advisory, never a hard fail
(FOUNDATION §15) — and says so with the
:data:`forge.pip_audit_json.CVE_USAGE_SKIP_MARKER` line. Writes
``code_health/cve_usage.log``.

Usage:

- ``verify-forge-cve-usage`` — scan and write the log.
- ``verify-forge-cve-usage --audit-json code_health/pip_audit.json`` — reuse
  the ``pip_audit`` step's findings instead of invoking pip-audit again, so the
  two steps share one scan per commit.
- ``verify-forge-cve-usage --list-inactive`` — report mapped CVEs no longer in
  pip-audit's live report (dormant prune candidates). Read-only, exits 0, never
  edits the map.
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
    code_health_dir,
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


class Coverage(NamedTuple):
    """Which reported advisories the pattern map let the scan check.

    Attributes:
        checked: Advisories with a map entry holding a usable pattern.
        unchecked: Every other reported advisory — nothing searched for it.
    """

    checked: list[pip_audit_json.Advisory]
    unchecked: list[pip_audit_json.Advisory]


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


def _load_audit_data(root: Path, audit_json: Path | None) -> dict | None:
    """Return pip-audit's parsed report, from the sidecar or a live run.

    Args:
        root: Repo root (pip-audit runs against the active environment; a
            relative *audit_json* path resolves against it).
        audit_json: Optional path to a pip-audit JSON sidecar written by the
            ``pip_audit`` pre-commit step. When given, its contents are read
            instead of invoking pip-audit, so the two steps share **one** scan
            per commit. A relative path resolves against *root*; a path that
            resolves outside the repo (and outside the log directory) is
            refused so the public flag cannot read arbitrary files.

    Returns:
        The parsed report, or ``None`` when the sidecar is absent /
        unparseable / outside the repo, or pip-audit is missing /
        unparseable — the signal to skip cleanly (FOUNDATION §15). Each
        case logs why.
    """
    if audit_json is not None:
        # Resolve (collapsing any ``..``) and confine the read to the repo:
        # ``--audit-json`` is a public CLI flag, and the sidecar it names only
        # ever lives under the repo. An out-of-repo path skips cleanly rather
        # than turning the JSON read into an arbitrary-file-read oracle.
        candidate = audit_json if audit_json.is_absolute() else root / audit_json
        path = candidate.resolve()
        # The log directory may be relocated outside the repo
        # (FORGE_CODE_HEALTH_DIR); the sidecar lives there, so it is the
        # one other place a read is allowed.
        allowed = (root.resolve(), code_health_dir(root).resolve())
        if not any(path.is_relative_to(base) for base in allowed):
            logger.info("(--audit-json %s is outside the repo — skipped)", audit_json)
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.info("(no usable pip-audit sidecar at %s — skipped)", path)
            return None
        return data if isinstance(data, dict) else None
    run = pip_audit_json.run_json(root)
    if run is None:
        logger.info("(pip-audit not on PATH — skipped)")
        return None
    if run.data is None:
        logger.info("(pip-audit produced no parseable JSON — skipped)")
        return None
    return run.data


def active_cve_ids(root: Path, audit_json: Path | None = None) -> set[str] | None:
    """Return the advisory / CVE IDs pip-audit currently reports.

    Args:
        root: Repo root (see :func:`_load_audit_data`).
        audit_json: Optional pip-audit JSON sidecar to read instead of
            invoking pip-audit (see :func:`_load_audit_data`).

    Returns:
        The set of live IDs (each ``id`` plus its ``aliases``, so a CVE-keyed
        map matches a PYSEC-keyed report and vice versa), or ``None`` when
        no report could be loaded — the signal to skip cleanly.
    """
    data = _load_audit_data(root, audit_json)
    if data is None:
        return None
    return pip_audit_json.ids_from_data(data)


def _usable_patterns(entry: dict[str, object]) -> list[re.Pattern[str]]:
    """Return an entry's patterns that can actually be searched for, compiled.

    The single judgement of "this entry can check its CVE", shared by
    :func:`scan` and :func:`coverage` so the report cannot call a CVE
    checked that the scan never searched. A ``patterns`` value that is not
    a list, a non-string item, and a regex that does not compile are all
    unusable; a bad regex is reported through coverage instead of crashing
    the step.

    Patterns come from the consumer's committed ``cve_usage_patterns.toml``
    — same trust level as ``pyproject.toml``. No runtime sanitization: a
    pathological regex would only slow the committer's own pre-commit.

    Args:
        entry: One pattern-map entry.

    Returns:
        The compiled usable patterns, in map order; ``[]`` when none.
    """
    raw_patterns = entry.get("patterns", [])
    if not isinstance(raw_patterns, list):
        return []
    compiled: list[re.Pattern[str]] = []
    for raw in raw_patterns:
        if not isinstance(raw, str):
            continue
        try:
            compiled.append(re.compile(raw))
        except re.error:
            continue
    return compiled


def coverage(
    patterns: dict[str, dict[str, object]],
    advisories: list[pip_audit_json.Advisory],
) -> Coverage:
    """Split the reported advisories into the ones the map can check and the rest.

    An advisory is checked when any ID it answers to
    (:meth:`forge.pip_audit_json.Advisory.ids` — the same set :func:`scan`
    matches map keys against) keys an entry with at least one usable
    pattern.

    Args:
        patterns: The consumer ``CVE-ID → entry`` map.
        advisories: Every advisory pip-audit reported.

    Returns:
        The checked and unchecked advisories, each in report order.
    """
    searchable = {cve for cve, entry in patterns.items() if _usable_patterns(entry)}
    checked: list[pip_audit_json.Advisory] = []
    unchecked: list[pip_audit_json.Advisory] = []
    for advisory in advisories:
        (checked if advisory.ids() & searchable else unchecked).append(advisory)
    return Coverage(checked=checked, unchecked=unchecked)


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
    checked, each with its :func:`_usable_patterns`. Comment lines (first
    non-space char ``#``) are skipped — a pattern string inside a comment
    is not real usage.

    Args:
        root: Repo root.
        patterns: The consumer ``CVE-ID → entry`` map.
        active: Advisory / CVE IDs pip-audit currently reports.

    Returns:
        One :class:`Finding` per matched line, in scan order.
    """
    compiled = [
        (cve, entry, rx)
        for cve, entry in patterns.items()
        if cve in active
        for rx in _usable_patterns(entry)
    ]
    if not compiled:
        return []
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


def _advisory_label(advisory: pip_audit_json.Advisory) -> str:
    """Name an advisory for the report: its primary ID, else its first alias.

    Args:
        advisory: The advisory to name.

    Returns:
        ``"<ID> (<package>)"``, with ``?`` for an advisory carrying no ID.
    """
    name = advisory.id or next(iter(advisory.aliases), "?")
    return f"{name} ({advisory.package})"


def _render(findings: list[Finding], cov: Coverage) -> str:
    """Render the ``code_health/cve_usage.log`` body.

    The all-clear sentence appears only when every reported CVE was
    checked; a partial check never reads as a clean one.

    Args:
        findings: The matched usages.
        cov: Which reported advisories were checked.

    Returns:
        A human-readable report.
    """
    total = len(cov.checked) + len(cov.unchecked)
    if total == 0:
        return "pip-audit reports no CVEs — nothing to check."
    head = f"Checked {len(cov.checked)} of {total} reported CVEs against {PATTERN_FILE}"
    lines: list[str] = []
    if findings:
        lines += [f"{head}: {len(findings)} vulnerable-usage finding(s):", ""]
        for f in findings:
            lines.append(f"⚠️  {f.cve} ({f.package}) — {f.path}:{f.line}")
            if f.risk:
                lines.append(f"    risk:       {f.risk}")
            if f.mitigation:
                lines.append(f"    mitigation: {f.mitigation}")
    elif not cov.unchecked:
        lines.append(f"{head} — no vulnerable usage found.")
    else:
        lines.append(f"{head} — no match among the ones checked.")
    if cov.unchecked:
        names = ", ".join(_advisory_label(a) for a in cov.unchecked)
        lines += [
            "",
            (
                f"No usable pattern for: {names} — NOT checked. Add an entry with "
                f"at least one valid regex to {PATTERN_FILE} to check it."
            ),
        ]
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
    so auto-deletion on this signal would be unsafe.

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
        (
            f"{len(dormant)} mapped CVE(s) not in the current pip-audit report "
            "(prune candidates — verify before removing):"
        ),
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


def _run_check(root: Path, audit_json: Path | None) -> int:
    """Run the usage check and log its report; the body of :func:`main`.

    The audit report is loaded once and both the advisory list (for
    coverage) and the ID set (for the scan) are derived from it.

    Args:
        root: Repo root.
        audit_json: Optional pip-audit JSON sidecar (see
            :func:`_load_audit_data`).

    Returns:
        The exit code :func:`main` documents.
    """
    patterns = load_patterns(root)
    if patterns is None:
        logger.info("(no %s — skipped)", PATTERN_FILE)
        logger.info("%s", pip_audit_json.CVE_USAGE_SKIP_MARKER)
        return 0
    data = _load_audit_data(root, audit_json)
    if data is None:
        logger.info("%s", pip_audit_json.CVE_USAGE_SKIP_MARKER)
        return 0
    cov = coverage(patterns, pip_audit_json.advisories(data))
    findings = scan(root, patterns, pip_audit_json.ids_from_data(data))
    logger.info("%s", _render(findings, cov))
    return 1 if findings or cov.unchecked else 0


def main() -> int:
    """CLI entry point.

    Returns:
        ``1`` when vulnerable usage is found **or** any reported CVE was
        not checked (so the pre-commit step and CI render a WARN), ``0``
        when every reported CVE was checked and none matched, when
        pip-audit reports no CVEs, when the check did not run (no pattern
        file, or pip-audit unavailable — announced by the
        :data:`forge.pip_audit_json.CVE_USAGE_SKIP_MARKER` line), or in
        ``--list-inactive`` mode. The check is **advisory** — the step
        marks it non-blocking regardless; the exit code only signals what
        needs a look.
    """
    parser = argparse.ArgumentParser(
        prog="verify-forge-cve-usage",
        description=(
            "Second-stage CVE filter: report only CVEs whose vulnerable "
            "code path is actually used, and name every reported CVE no "
            "pattern checks. Reads cve_usage_patterns.toml; does not run "
            "when it is absent or pip-audit is unavailable."
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
        return _run_check(root, args.audit_json)


if __name__ == "__main__":
    raise SystemExit(main())
