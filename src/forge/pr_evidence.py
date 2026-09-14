"""Review evidence pack for ``forge-pr-plan --evidence``.

A reviewer of a PR otherwise re-derives what the tree already settled —
which ``code_health/`` logs describe this tree, whether the generated docs
match their generators, what the duplicate and layering audits found in
the changed files, which issues the PR closes. The pack gathers those once
into ``code_health/pr_evidence.log``, stamped with the tree it describes;
each reviewer starts from it (the reporter contract in
``agents/_TEMPLATE.md``) and re-derives only what it marks ``unavailable``
or stale.

Every item is bounded and isolated: a tool that fails, times out or is
missing renders ``unavailable: <reason>`` for that item alone. Nothing here
writes to stdout — ``forge-pr-plan``'s plan JSON and exit code are the
contract the publish hook parses. Untrusted text (filenames, commit
messages, the PR body, audit findings) is sanitized line by line and
fenced as data.

Gathering runs in a different order from rendering: the audits run first
(they rewrite their own logs), the code-health snapshot is taken next, and
the generated-artifact gates run last — ``forge-precommit --only``
overwrites ``precommit_timing.log``, which the snapshot must read before
that happens.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Final

from forge.audit.all import SUB_AUDITS
from forge.audit.common import CODE_HEALTH_DIR, read_finding_count, sanitize_log_text
from forge.changelog_fragments import branch_added_fragments
from forge.config import is_fragments_mode, load_config
from forge.git_utils import (
    EVIDENCE_OUTPUT_CAP,
    log_freshness,
    resolve_base_branch_ref,
    resolve_pr_base_ref,
    run_git,
    working_tree_sha,
    wrap_in_code_fence,
    write_step_log,
)
from forge.pr_delta import PROVENANCE_GATE_STEPS, find_closing_refs, regen_commands
from forge.pr_wrapup_compose import render_issue_management
from forge.precommit import freshness_verdicts, timing_markers


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path


logger = logging.getLogger(__name__)

EVIDENCE_LOG_NAME: Final[str] = "pr_evidence"

# Audits the pack runs itself, at changed scope. Their changed scope stays
# full-tree-aware (dup matches changed units against a whole-tree index,
# layering builds the whole module graph), so the result is exact for the
# diff; every other audit's is not, so the pack reports only its log.
RUN_AUDITS: Final[tuple[str, ...]] = ("dup", "layering")
REPORTED_AUDITS: Final[tuple[str, ...]] = tuple(
    name for name in SUB_AUDITS if name not in RUN_AUDITS
)

# An audit exits 1 when it found something: a result to embed, never a
# failure to hide behind "unavailable".
_AUDIT_RESULT_EXITS: Final[frozenset[int]] = frozenset({0, 1})

AUDIT_TIMEOUT_S: Final[float] = 300.0
GATE_TIMEOUT_S: Final[float] = 300.0

# The C4 model's `--check` step joins the provenance gates only where the
# model is configured.
_C4_GATE: Final[str] = "c4"
_C4_PATH: Final[str] = "docs/architecture.dsl"
_API_DIGEST: Final[str] = "docs/api-digest.md"
_API_DIGEST_GATE: Final[str] = "api_digest_check"

_FINDINGS_HEADING: Final[str] = "\n## Findings\n"


class _UnavailableError(ValueError):
    """An item that ran but produced no usable result."""


# Errors that make one pack item unavailable without touching the rest.
_ITEM_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    subprocess.SubprocessError,
    ValueError,
)


def _run_tool(
    argv: Sequence[str], *, cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run one pack tool — the module's single subprocess seam.

    Args:
        argv: Command and arguments.
        cwd: Working directory; the repo root, so the tool finds its own.
        timeout: Seconds before the item is abandoned.

    Returns:
        The completed process, output captured as text.
    """
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _reason(exc: Exception) -> str:
    """Describe in one line why an item is unavailable.

    Args:
        exc: The error the item raised.

    Returns:
        A short, sanitized reason.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        text = f"timed out after {exc.timeout:g}s"
    elif isinstance(exc, subprocess.CalledProcessError):
        text = f"exited {exc.returncode}"
    elif isinstance(exc, FileNotFoundError):
        text = f"{exc.filename or 'tool'} not found"
    else:
        text = next(iter(str(exc).splitlines()), "") or type(exc).__name__
    return sanitize_log_text(text)


def _data(text: str) -> list[str]:
    """Fence untrusted *text* as data: capped, then sanitized line by line.

    Args:
        text: Tool output, filenames or messages.

    Returns:
        The fenced block's lines.
    """
    if len(text) > EVIDENCE_OUTPUT_CAP:
        text = f"{text[:EVIDENCE_OUTPUT_CAP]}\n… (truncated)"
    clean = "\n".join(sanitize_log_text(line) for line in text.splitlines())
    return wrap_in_code_fence(clean).splitlines()


def _section(title: str, build: Callable[[], list[str]]) -> list[str]:
    """Render one pack section, or its ``unavailable`` line when *build* fails.

    Args:
        title: Section heading.
        build: Produces the section body.

    Returns:
        The section's lines, blank line included.
    """
    try:
        body = build()
    except _ITEM_ERRORS as exc:
        logger.warning("pr-evidence: %s unavailable (%s)", title, _reason(exc))
        body = [f"unavailable: {_reason(exc)}"]
    return [f"## {title}", "", *body, ""]


def _item(label: str, build: Callable[[], list[str]]) -> list[str]:
    """Render one bullet item, isolating its failure from its siblings.

    Args:
        label: The bullet's label.
        build: Produces the first line's text and any following lines.

    Returns:
        The item's lines.
    """
    try:
        first, *rest = build()
    except _ITEM_ERRORS as exc:
        logger.warning("pr-evidence: %s unavailable (%s)", label, _reason(exc))
        return [f"- {label}: unavailable: {_reason(exc)}"]
    return [f"- {label}: {first}", *rest]


def _pr_lines(
    root: Path, base: str, plan: Mapping[str, object], added: Sequence[str]
) -> list[str]:
    """Render the PR identity: head, base, mode, diff stat, added files.

    Args:
        root: Repo root.
        base: The classified base ref.
        plan: The emitted plan (``mode``, ``reasons``, ``classified_at``).
        added: Paths the branch adds.

    Returns:
        The section body.
    """
    head = run_git("rev-parse", "--short", "HEAD", cwd=root)
    stat = run_git("diff", "--stat", f"{base}...HEAD", cwd=root)
    reasons = plan.get("reasons")
    lines = [
        f"- head: {head}",
        f"- base: {sanitize_log_text(base)}",
        f"- mode: {sanitize_log_text(str(plan.get('mode')))}",
    ]
    if isinstance(reasons, list):
        lines += [f"  - {sanitize_log_text(str(reason))}" for reason in reasons]
    lines += ["- diff stat:", *_data(stat or "(no changes)")]
    lines += ["- added files:", *(_data("\n".join(added)) if added else ["  none"])]
    return lines


def _timing_snapshot(root: Path) -> dict[str, str]:
    """Return the step markers of the newest ``forge-precommit`` run.

    Args:
        root: Repo root.

    Returns:
        Step name → marker; empty when no run left a timing log.
    """
    timing = root / CODE_HEALTH_DIR / "precommit_timing.log"
    if not timing.is_file():
        return {}
    return timing_markers(timing.read_text(encoding="utf-8"))


def _health_lines(root: Path) -> list[str]:
    """Render each log's freshness beside the latest pre-commit step marker.

    Audit logs belong to the audit section, and a previous pack describes
    nothing, so both are left out.

    Args:
        root: Repo root.

    Returns:
        The section body.
    """
    verdicts = {
        name: verdict
        for name, verdict in freshness_verdicts(root).items()
        if not name.startswith("audit_") and name != EVIDENCE_LOG_NAME
    }
    markers = _timing_snapshot(root)
    lines = [f"- latest pre-commit run: {verdicts.pop('precommit_timing', 'none')}"]
    for name in sorted(set(verdicts) | set(markers)):
        marker = markers.get(name, "no step row")
        lines.append(
            f"- {sanitize_log_text(name)}: {marker}, log"
            f" {verdicts.get(name, 'missing')}"
        )
    return lines


def _audit_result(root: Path, name: str, current: str | None) -> list[str]:
    """Run one audit at changed scope and read its findings back.

    Args:
        root: Repo root.
        name: Audit short name.
        current: The working tree the log must describe.

    Returns:
        The finding count, then the fenced findings when there are any.

    Raises:
        _UnavailableError: When the audit crashed or left no log for this tree.
    """
    proc = _run_tool(
        [f"forge-audit-{name}", "--scope", "changed"], cwd=root, timeout=AUDIT_TIMEOUT_S
    )
    if proc.returncode not in _AUDIT_RESULT_EXITS:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        msg = f"exited {proc.returncode}" + (f": {detail[-1]}" if detail else "")
        raise _UnavailableError(msg)
    log = root / CODE_HEALTH_DIR / f"audit_{name}.log"
    verdict = log_freshness(log, current)
    if verdict != "fresh":
        msg = f"{log.name} is {verdict} after the run"
        raise _UnavailableError(msg)
    text = log.read_text(encoding="utf-8", errors="replace")
    count = read_finding_count(text)
    findings = text.partition(_FINDINGS_HEADING)[2].strip()
    first = f"{count} finding(s)" if count >= 0 else "no `# findings:` header"
    return [first, *_data(findings)] if findings and count != 0 else [first]


def _reported_audit(root: Path, name: str, current: str | None) -> list[str]:
    """Report an audit the pack does not run: its log's freshness and count.

    Args:
        root: Repo root.
        name: Audit short name.
        current: The working tree to judge the log against.

    Returns:
        One line.
    """
    log = root / CODE_HEALTH_DIR / f"audit_{name}.log"
    if not log.is_file():
        return [f"no log — run `forge-audit-{name} --scope full`"]
    verdict = log_freshness(log, current)
    if verdict != "fresh":
        return [f"{verdict} — re-run at full scope"]
    count = read_finding_count(log.read_text(encoding="utf-8", errors="replace"))
    return [f"fresh, {count if count >= 0 else '?'} finding(s)"]


def _audit_lines(root: Path, base: str, current: str | None) -> list[str]:
    """Run dup and layering at changed scope; report every other audit log.

    Args:
        root: Repo root.
        base: The classified base ref.
        current: The working tree the logs must describe.

    Returns:
        The section body.
    """
    config_base = load_config(root).base_branch
    audit_base = resolve_base_branch_ref(root, config_base) or config_base
    scope = f"- changed scope compares against {sanitize_log_text(audit_base)}"
    if audit_base != base:
        scope += (
            f" — not {sanitize_log_text(base)}, so commits between the two "
            "are in scope too"
        )
    lines = [scope]
    for name in RUN_AUDITS:
        lines += _item(
            f"forge-audit-{name} --scope changed",
            lambda name=name: _audit_result(root, name, current),
        )
    for name in REPORTED_AUDITS:
        lines += _item(
            f"audit_{name}", lambda name=name: _reported_audit(root, name, current)
        )
    return lines


def _gate_steps(root: Path) -> list[str]:
    """Return the ``forge-precommit`` steps that check the generated artifacts.

    Args:
        root: Repo root.

    Returns:
        The provenance gates, plus the C4 check where the model is configured.
    """
    steps = list(PROVENANCE_GATE_STEPS)
    if _C4_PATH in regen_commands(root):
        steps.append(_C4_GATE)
    return steps


def _gate_lines(root: Path) -> tuple[list[str], bool]:
    """Run the generated-artifact checks through ``forge-precommit --only``.

    The checks run through :func:`_run_tool` rather than
    ``git_utils.run_gate_evidence`` because the pack bounds every item with
    a timeout, which that shared seam does not take.

    Args:
        root: Repo root.

    Returns:
        ``(section lines, digest_checked)`` — whether the api-digest check
        itself passed, read from the timing log this run just wrote.
    """
    title = "Generated artifacts"
    try:
        proc = _run_tool(
            ["forge-precommit", "--only", ",".join(_gate_steps(root))],
            cwd=root,
            timeout=GATE_TIMEOUT_S,
        )
        digest_checked = _timing_snapshot(root).get(_API_DIGEST_GATE) == "PASS"
    except _ITEM_ERRORS as exc:
        logger.warning("pr-evidence: %s unavailable (%s)", title, _reason(exc))
        return [f"## {title}", "", f"unavailable: {_reason(exc)}", ""], False
    verdict = (
        "PASS — every generated artifact matches its generator."
        if proc.returncode == 0
        else "FAIL — a generated artifact is out of date or unverified."
    )
    output = "\n".join(
        part for part in (proc.stdout.strip(), proc.stderr.strip()) if part
    )
    return [f"## {title}", "", verdict, "", *_data(output or "(no output)"), ""], (
        digest_checked
    )


def _surface_lines(root: Path, base: str, *, digest_checked: bool) -> list[str]:
    """Render the committed api-digest's changed lines over ``base...HEAD``.

    Args:
        root: Repo root.
        base: The classified base ref.
        digest_checked: Whether the api-digest check passed on this tree.

    Returns:
        The section body.
    """
    if not (root / _API_DIGEST).is_file():
        return [f"no {_API_DIGEST} in this repo"]
    diff = run_git("diff", "--unified=0", f"{base}...HEAD", "--", _API_DIGEST, cwd=root)
    changes = [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    lines = []
    if not digest_checked:
        lines.append(f"⚠️ may be stale: the {_API_DIGEST_GATE} step did not pass")
    if not changes:
        return [*lines, f"no changed lines in {_API_DIGEST}"]
    return [
        *lines,
        f"{len(changes)} changed line(s) in {_API_DIGEST}:",
        *_data("\n".join(changes)),
    ]


def _fragment_line(root: Path, base: str) -> str:
    """Describe the branch's changelog fragments, naming the base compared with.

    Args:
        root: Repo root.
        base: The classified base ref.

    Returns:
        One line.
    """
    if not is_fragments_mode(root):
        return "fragments mode is off — see the changelog_updated step above"
    config_base = load_config(root).base_branch
    fragment_base = resolve_pr_base_ref(root, config_base) or config_base
    names = ", ".join(sanitize_log_text(path) for path in branch_added_fragments(root))
    line = f"fragments added: {names or 'none'}"
    if fragment_base != base:
        line += f" (compared with {sanitize_log_text(fragment_base)})"
    return line


def _wiring_lines(root: Path, base: str, pr_body: str | None) -> list[str]:
    """Render closing keywords and changelog-fragment presence.

    Args:
        root: Repo root.
        base: The classified base ref.
        pr_body: The PR body, or ``None`` when there is no readable PR.

    Returns:
        The section body.
    """
    messages = run_git("log", "--format=%B", f"{base}..HEAD", cwd=root)
    refs = find_closing_refs(f"{pr_body or ''}\n{messages}")
    closing = render_issue_management(refs, pr_body_checked=pr_body is not None)
    return [
        f"- closing keywords: {closing}",
        f"- changelog: {_fragment_line(root, base)}",
    ]


def build_pack(
    root: Path,
    *,
    base: str,
    plan: Mapping[str, object],
    added: Sequence[str],
    pr_body: str | None,
) -> str:
    """Gather the evidence and render the pack.

    Args:
        root: Repo root.
        base: The classified base ref.
        plan: The emitted plan (``mode``, ``reasons``, ``classified_at``).
        added: Paths the branch adds over ``base...HEAD``.
        pr_body: The PR body, or ``None`` when there is no readable PR.

    Returns:
        The pack's markdown, without the ``# produced-at:`` stamp.
    """
    current = working_tree_sha(root)
    audits = _section("Audits", lambda: _audit_lines(root, base, current))
    health = _section("Code health", lambda: _health_lines(root))
    gates, digest_checked = _gate_lines(root)
    surface = _section(
        "Public surface",
        lambda: _surface_lines(root, base, digest_checked=digest_checked),
    )
    wiring = _section("Wiring", lambda: _wiring_lines(root, base, pr_body))
    identity = _section("PR", lambda: _pr_lines(root, base, plan, added))
    return "\n".join(
        [
            "# forge-pr-plan evidence pack",
            "",
            *identity,
            *health,
            *audits,
            *gates,
            *surface,
            *wiring,
        ]
    )


def write_pack(
    root: Path,
    *,
    base: str,
    plan: Mapping[str, object],
    added: Sequence[str],
    pr_body: str | None,
) -> Path:
    """Build the pack and write it to ``code_health/pr_evidence.log``.

    Args:
        root: Repo root.
        base: The classified base ref.
        plan: The emitted plan (``mode``, ``reasons``, ``classified_at``).
        added: Paths the branch adds over ``base...HEAD``.
        pr_body: The PR body, or ``None`` when there is no readable PR.

    Returns:
        The written log's path.
    """
    pack = build_pack(root, base=base, plan=plan, added=added, pr_body=pr_body)
    return write_step_log(root, EVIDENCE_LOG_NAME, pack)
