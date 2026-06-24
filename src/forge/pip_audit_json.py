"""pip_audit_json — one shared pip-audit JSON invocation for both CVE steps.

Both the base ``pip_audit`` pre-commit step (:func:`forge.precommit.step_pip_audit`)
and the usage-scoped second stage (:mod:`forge.verify_cve_usage`) need
pip-audit's findings. Running it independently in each meant **two** OSV
network round-trips per commit (#78). This module is the single seam both
depend on: one ``pip-audit --skip-editable --format=json`` invocation, parsed
once, with separate accessors for the human log (:func:`render_report`) and the
live advisory-ID set (:func:`ids_from_data`).

Neutral low-level seam (DIP): the base step must not import the ``cve_usage``
extension to reuse its parsing, so the shared logic lives here — a stdlib-only
leaf both callers depend on independently, never the reverse.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING, NamedTuple


if TYPE_CHECKING:
    from pathlib import Path


# pip-audit in JSON mode against the active env, skipping the editable repo
# itself (whose own source is not a pinned third-party dependency).
PIP_AUDIT_CMD = ["pip-audit", "--skip-editable", "--format=json"]


class AuditRun(NamedTuple):
    """One completed pip-audit invocation.

    Attributes:
        data: Parsed pip-audit JSON object, or ``None`` when stdout was not
            parseable JSON (e.g. an operational error printed a bare message).
        stderr: Captured standard error, surfaced in the step log when a run
            fails without producing parseable findings.
        returncode: pip-audit's exit status — non-zero both for findings and
            for operational errors, so ``data`` is what disambiguates the two.
    """

    data: dict | None
    stderr: str
    returncode: int


def run_json(root: Path) -> AuditRun | None:
    """Run pip-audit once in JSON mode against *root*'s environment.

    Args:
        root: Working directory for the scan (the repo root).

    Returns:
        An :class:`AuditRun`, or ``None`` when the ``pip-audit`` binary is not
        on ``PATH`` — the signal callers translate into a loud "scanner
        missing" warning (the step) or a clean skip (the CVE-usage filter),
        per FOUNDATION §15.
    """
    if shutil.which("pip-audit") is None:
        return None
    proc = subprocess.run(
        PIP_AUDIT_CMD,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = None
    return AuditRun(data=data, stderr=proc.stderr, returncode=proc.returncode)


def _dependencies(data: dict) -> list[dict]:
    """Return the well-formed dependency objects in parsed pip-audit JSON.

    Args:
        data: Parsed pip-audit JSON.

    Returns:
        The ``dependencies`` list filtered to ``dict`` entries (a malformed
        or unexpected shape yields an empty list rather than raising).
    """
    deps = data.get("dependencies", [])
    if not isinstance(deps, list):
        return []
    return [dep for dep in deps if isinstance(dep, dict)]


def _vulns(dep: dict) -> list[dict]:
    """Return the well-formed vulnerability objects of one dependency.

    Args:
        dep: One dependency object from parsed pip-audit JSON.

    Returns:
        The ``vulns`` list filtered to ``dict`` entries.
    """
    vulns = dep.get("vulns", [])
    if not isinstance(vulns, list):
        return []
    return [vuln for vuln in vulns if isinstance(vuln, dict)]


def ids_from_data(data: dict) -> set[str]:
    """Collect the advisory / CVE IDs from parsed pip-audit JSON.

    Each vulnerability contributes its primary ``id`` plus every ``alias``, so
    a CVE-keyed consumer map matches a PYSEC-keyed report and vice versa.

    Args:
        data: Parsed pip-audit JSON (the :attr:`AuditRun.data` object).

    Returns:
        The set of live advisory IDs across every scanned dependency.
    """
    ids: set[str] = set()
    for dep in _dependencies(data):
        for vuln in _vulns(dep):
            vid = vuln.get("id")
            if isinstance(vid, str) and vid:
                ids.add(vid)
            aliases = vuln.get("aliases", [])
            if isinstance(aliases, list):
                ids.update(a for a in aliases if isinstance(a, str) and a)
    return ids


def has_vulns(data: dict) -> bool:
    """Report whether any scanned dependency carries a vulnerability.

    Args:
        data: Parsed pip-audit JSON.

    Returns:
        ``True`` if at least one dependency has a non-empty ``vulns`` list.
    """
    return any(_vulns(dep) for dep in _dependencies(data))


def render_report(data: dict) -> str:
    """Render parsed pip-audit JSON as the human-readable step-log body.

    One line per vulnerability, carrying the **primary** ``id`` only (aliases
    are deliberately omitted so :func:`forge.precommit._count_pip_audit_advisories`
    counts exactly one advisory per finding), followed by the package, its fix
    versions, and the first line of the description.

    Args:
        data: Parsed pip-audit JSON.

    Returns:
        A multi-line report, or a clean one-liner when nothing was found.
    """
    lines: list[str] = []
    for dep in _dependencies(data):
        name = dep.get("name", "?")
        version = dep.get("version", "?")
        for vuln in _vulns(dep):
            vid = vuln.get("id", "?")
            fix_versions = vuln.get("fix_versions", [])
            fixes = ", ".join(fix_versions) if isinstance(fix_versions, list) else ""
            description = vuln.get("description") or ""
            summary = description.strip().splitlines()
            line = f"{vid}  {name} {version}  (fix: {fixes or 'none'})"
            if summary:
                line += f"  — {summary[0]}"
            lines.append(line)
    if not lines:
        return "No known vulnerabilities found in non-editable dependencies."
    header = f"{len(lines)} dependency vulnerability advisory(ies):"
    return "\n".join([header, "", *lines])
