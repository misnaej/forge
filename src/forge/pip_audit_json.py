"""pip_audit_json — one shared pip-audit JSON invocation for both CVE steps.

Both the base ``pip_audit`` pre-commit step (:func:`forge.precommit.step_pip_audit`)
and the usage-scoped second stage (:mod:`forge.verify_cve_usage`) need
pip-audit's findings. Running it independently in each meant **two** OSV
network round-trips per commit (#78). This module is the single seam both
depend on: one ``pip-audit --skip-editable --format=json`` invocation, parsed
once, with separate accessors for the human log (:func:`render_report`), the
per-advisory records (:func:`advisories`) and the live advisory-ID set
(:func:`ids_from_data`).

It also owns the ``code_health/pip_audit.json`` sidecar's read side — the
report plus the time the scan ran (:func:`sidecar_scanned_at`) — because
three callers judge that scan: the step's reuse cadence, the usage filter,
and ``forge-pr-create``'s refusal of a branch with no scan since it left its
base (:func:`scan_postdates_fork`). Keeping the reader here lets the publish
gate ask the question without importing the whole pre-commit runner.

Neutral low-level seam (DIP): the base step must not import the ``cve_usage``
extension to reuse its parsing, so the shared logic lives here — a leaf that
depends only on :mod:`forge.git_utils` for the log directory, which every
caller depends on independently, never the reverse.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from forge.git_utils import code_health_dir


if TYPE_CHECKING:
    from pathlib import Path


# pip-audit in JSON mode against the active env, skipping the editable repo
# itself (whose own source is not a pinned third-party dependency).
PIP_AUDIT_CMD = ["pip-audit", "--skip-editable", "--format=json"]

#: The sidecar the ``pip_audit`` step writes under ``code_health/``.
SIDECAR_NAME = "pip_audit.json"
#: Sidecar field recording when the scan ran (epoch seconds, UTC).
SCANNED_AT_KEY = "scanned_at"
#: A stamp dated further ahead than this is treated as invalid rather
#: than allowed to suppress scans indefinitely (mirrors smart_test).
FUTURE_TOLERANCE_S = 300.0
#: The exact line ``verify-forge-cve-usage`` prints when it did not run the
#: check. The pre-commit step matches the whole line, so no package name or
#: advisory text inside a longer line can mark a real run as skipped.
CVE_USAGE_SKIP_MARKER = "cve_usage-status: not-run"


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


def _dict_list(obj: dict, key: str) -> list[dict]:
    """Return ``obj[key]`` filtered to ``dict`` entries, or ``[]``.

    The shared shape-guard behind the ``dependencies`` and ``vulns`` walks:
    a malformed or unexpected JSON shape yields an empty list rather than
    raising, so the public functions stay total over any parsed input.

    Args:
        obj: A parsed-JSON object (the report root or one dependency).
        key: The list-valued key to extract (``"dependencies"`` / ``"vulns"``).

    Returns:
        The value at *key* filtered to ``dict`` entries; ``[]`` when absent
        or not a list.
    """
    value = obj.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


class Advisory(NamedTuple):
    """One vulnerability pip-audit reported against one dependency.

    Attributes:
        package: The dependency's name (``"?"`` when the report omits it).
        id: The primary advisory ID, or ``""`` when the report carries none
            usable (missing, empty, or not a string).
        aliases: The usable alias IDs, in report order.
    """

    package: str
    id: str
    aliases: tuple[str, ...]

    def ids(self) -> frozenset[str]:
        """Return every ID this advisory answers to — the primary plus aliases.

        The one definition of "this advisory is the CVE named X": a
        CVE-keyed consumer map matches a PYSEC-keyed report and vice versa.

        Returns:
            The non-empty IDs.
        """
        return frozenset({self.id, *self.aliases} - {""})


def advisories(data: dict) -> list[Advisory]:
    """Return one :class:`Advisory` per reported vulnerability, in report order.

    Malformed entries are tolerated the same way everywhere in this module:
    a non-dict dependency or vulnerability is dropped, an unusable primary
    ID becomes ``""``, and a non-list ``aliases`` reads as none. A
    vulnerability with no usable ID at all is still returned — it was
    reported, so a caller counting what it checked must count it.

    Args:
        data: Parsed pip-audit JSON (the :attr:`AuditRun.data` object).

    Returns:
        The advisories across every scanned dependency.
    """
    found: list[Advisory] = []
    for dep in _dict_list(data, "dependencies"):
        package = str(dep.get("name", "?"))
        for vuln in _dict_list(dep, "vulns"):
            vid = vuln.get("id")
            raw_aliases = vuln.get("aliases", [])
            aliases = (
                tuple(a for a in raw_aliases if isinstance(a, str) and a)
                if isinstance(raw_aliases, list)
                else ()
            )
            found.append(
                Advisory(
                    package=package,
                    id=vid if isinstance(vid, str) else "",
                    aliases=aliases,
                )
            )
    return found


def ids_from_data(data: dict) -> set[str]:
    """Collect the advisory / CVE IDs from parsed pip-audit JSON.

    The union of :meth:`Advisory.ids` over :func:`advisories`.

    Args:
        data: Parsed pip-audit JSON (the :attr:`AuditRun.data` object).

    Returns:
        The set of live advisory IDs across every scanned dependency.
    """
    ids: set[str] = set()
    for advisory in advisories(data):
        ids.update(advisory.ids())
    return ids


def has_vulns(data: dict) -> bool:
    """Report whether any scanned dependency carries a vulnerability.

    Args:
        data: Parsed pip-audit JSON.

    Returns:
        ``True`` if at least one dependency has a non-empty ``vulns`` list.
    """
    return any(_dict_list(dep, "vulns") for dep in _dict_list(data, "dependencies"))


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
    for dep in _dict_list(data, "dependencies"):
        name = dep.get("name", "?")
        version = dep.get("version", "?")
        for vuln in _dict_list(dep, "vulns"):
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


def read_sidecar(repo_root: Path) -> dict | None:
    """Return the sidecar's parsed scan, or ``None`` when it cannot be trusted.

    Missing, unreadable, malformed, or not a pip-audit report (a JSON
    object with a ``dependencies`` list) all read as ``None`` — the
    caller then scans, the same safe direction an unreadable timestamp
    takes. A skeletal ``{}`` would otherwise render as a clean scan.

    Args:
        repo_root: Git repo root.

    Returns:
        The parsed scan data, or ``None``.
    """
    sidecar = code_health_dir(repo_root) / SIDECAR_NAME
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("dependencies"), list):
        return data
    return None


def _scan_time_value(value: object) -> float | None:
    """Return *value* as epoch seconds, or ``None`` when it is not a usable time.

    Args:
        value: The sidecar's recorded ``scanned_at`` field.

    Returns:
        A finite float, or ``None`` for a bool, a non-number, a non-finite
        number, or an integer too large to be a float.
    """
    # bool is an int subclass: `true` must not read as 1970-01-01T00:00:01.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) else None


def sidecar_scanned_at(repo_root: Path) -> float | None:
    """Return when the last CVE scan ran, the one reader every age check uses.

    The time recorded in the payload wins because a file's modification
    time does not survive a copy or a CI cache restore — both reset it to
    the moment of the copy and manufacture a fresh-looking scan that never
    ran. The modification time is used only for a sidecar written before
    the field existed; a field that is present but unusable is not a
    reason to trust the file's timestamp instead.

    Args:
        repo_root: Git repo root.

    Returns:
        Epoch seconds, or ``None`` when the sidecar is unreadable, its
        recorded time is unusable, or the time is dated further ahead than
        :data:`FUTURE_TOLERANCE_S` — each means unknown, and every caller
        treats unknown as "no scan", the safe direction (a future stamp
        would otherwise suppress scans indefinitely).
    """
    data = read_sidecar(repo_root)
    if data is None:
        return None
    if SCANNED_AT_KEY in data:
        scanned_at = _scan_time_value(data[SCANNED_AT_KEY])
        if scanned_at is None:
            return None
    else:
        sidecar = code_health_dir(repo_root) / SIDECAR_NAME
        try:
            scanned_at = sidecar.stat().st_mtime
        except OSError:
            return None
    if scanned_at - datetime.now(tz=UTC).timestamp() > FUTURE_TOLERANCE_S:
        return None
    return scanned_at


def scan_postdates_fork(scanned_at: float | None, forked_at: int | None) -> bool:
    """Whether a scan ran after the branch left its base.

    The single definition both the step's branch cadence and
    ``forge-pr-create``'s refusal use, so the two cannot disagree about
    whether a branch was scanned. A scan in the fork's own second does
    not count: the fork time has one-second resolution, so equality
    cannot say which came first.

    The two timestamps come from different clocks — the scan's from the
    machine that ran it, the fork's from whoever authored that commit — so
    a clock running ahead could date a scan after a fork it actually
    preceded. :func:`sidecar_scanned_at` rejects a future-dated scan,
    which bounds that window.

    Args:
        scanned_at: Epoch seconds from :func:`sidecar_scanned_at`.
        forked_at: Fork-point commit time from
            :func:`forge.git_utils.fork_point_time`.

    Returns:
        ``True`` only when both times are known and the scan is later.
    """
    if scanned_at is None or forked_at is None:
        return False
    return scanned_at > forked_at
