"""forge-pr-wrapup — compose, validate and post the PR wrap-up, retiring older ones.

The wrap-up is the verification record a reviewer trusts most: which
reporters ran at which commit, what they found, how each finding was
dispositioned. Two things go wrong when it is posted by hand. It grows —
``_TEMPLATE.md``'s report-by-exception rule ("a clean check is ONE line")
lives in agent prose, and prose is a remembered step — and it never gets
retired, so every earlier wrap-up on the PR keeps reading as a current
attestation of a tree that no longer exists. This CLI makes both
mechanical: ``validate`` refuses a body that narrates clean checks or
exceeds its budget, and ``post`` stamps the new wrap-up with a marker,
collapses every earlier marker-carrying wrap-up into a ``<details>``
block (history kept, one live attestation visible), and re-posts the
squash-merge comment so it stays the PR's newest.

The header contract (``verified-at:`` first line, optional
``wrapup-mode:`` and ``prior-art-searched:`` lines) is
``agents/_TEMPLATE.md``'s; ``block_unverified_pr_create`` and
``forge-pr-plan --freshness`` read the same lines, so the collapse keeps
the original body — header included — verbatim inside the details block.

``compose`` renders the mechanical parts (header, mode line, skipped or
clean reporter sections, Issue Management, Code Quality, CI Status) into
``code_health/pr_wrapup.md`` and marks the judgment parts with
``<!-- forge:fill … -->`` slots (``forge.pr_wrapup_compose``). ``post``
then absorbs the rest of the posting task: it refuses a stale or
out-of-date publication, refreshes CI Status and Issue Management from
GitHub, posts, and appends the CONTINUATION record.

Usage:

- ``forge-pr-wrapup compose --base REF [--pr N] [--plan FILE] [--design FILE]
  [--security FILE] [--docs FILE] [--prior-art FILE]`` — write the wrap-up
  with slots; exit 2 when required evidence is missing
- ``forge-pr-wrapup validate <file>`` — exit 2 listing every violation
- ``forge-pr-wrapup post --pr N [--body-file FILE] [--no-continuation]`` —
  validate, gate (exit 3 on refusal), refresh, post, collapse superseded
  wrap-ups, keep the squash comment last, append the CONTINUATION record
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from forge import continuation_append
from forge.emergency_state import armed_state, read_state
from forge.gh_comments import (
    ValidationError,
    list_marker_comments,
    patch_comment,
    post_new_comment,
    validate_no_ai_attribution,
)
from forge.git_utils import (
    behind_ahead,
    configure_cli_logging,
    emit,
    fetch_quietly,
    repo_root,
    run_gate_evidence,
    run_git,
)
from forge.pr_delta import (
    PROVENANCE_GATE_STEPS,
    VERIFIED_AT_RE,
    extract_verified_shas,
    fenced_line_indexes,
    find_closing_refs,
    non_fragment_adds,
    strip_fences,
)
from forge.pr_plan import added_paths, classify, gh_pr_view, wrapup_freshness
from forge.pr_squash_comment import ensure_last
from forge.pr_wrapup_compose import (
    PENDING_CI,
    ComposeError,
    ComposeInputs,
    evidence_fence,
    pytest_summary_line,
    render_code_quality,
    render_issue_management,
    render_wrapup,
    summarize_rollup,
    unfilled_slots,
)
from forge.precommit import freshness_verdicts, resolve_steps


if TYPE_CHECKING:
    from collections.abc import Mapping


configure_cli_logging()
logger = logging.getLogger(__name__)

# Invisible in rendered markdown, greppable in the raw body: how a later
# post recognizes the wrap-ups it supersedes. Same convention as the
# squash-comment marker.
WRAPUP_MARKER: Final[str] = "<!-- forge:pr-wrapup -->"

# Sections every wrap-up carries (pr-manager's posting contract). Extra
# sections (Issue Management, Testing, ...) are allowed; these must exist.
REQUIRED_SECTIONS: Final[tuple[str, ...]] = (
    "Design Check",
    "Security Review",
    "Documentation Check",
    "Code Quality",
    "CI Status",
    "Recommendation",
)

# Sections that are status lines by nature — one line whatever they say.
STATUS_SECTIONS: Final[tuple[str, ...]] = (
    "Code Quality",
    "CI Status",
    "Recommendation",
)

# Report by exception, made numeric: an all-clean wrap-up is header + one
# summary line + one line per section — 120 words is generous for that;
# every findings section (a reporter that did not simply PASS) earns 40
# more for what/where/disposition prose.
WORD_BUDGET: Final[int] = 120
FINDING_ALLOWANCE: Final[int] = 40

# A section opening with one of these is clean and must be exactly one line.
_CLEAN_PREFIXES: Final[tuple[str, ...]] = ("pass", "skipped", "n/a")

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_MODE_RE: Final[re.Pattern[str]] = re.compile(r"^wrapup-mode:\s*\S+", re.IGNORECASE)
_PRIOR_ART_RE: Final[re.Pattern[str]] = re.compile(r"^prior-art-searched:")

SUPERSEDED_SUMMARY: Final[str] = (
    "Superseded by verified-at {new} — this wrap-up verified {old}"
)

WRAPUP_PATH: Final[Path] = Path("code_health") / "pr_wrapup.md"

# Exit code for a publication `post` refuses (stale head, conflict, behind
# base) — distinct from 2 (invalid body) so a caller can tell a fixable
# text problem from a branch that needs re-verification.
EXIT_REFUSED: Final[int] = 3

_EMERGENCY_RE: Final[re.Pattern[str]] = re.compile(
    r"^wrapup-mode:\s*emergency\s*$", re.IGNORECASE | re.MULTILINE
)

# compose flag → forge-pr-plan reporter name.
_REPORT_FLAGS: Final[tuple[tuple[str, str], ...]] = (
    ("design", "design-checker"),
    ("security", "security-checker"),
    ("docs", "docs-types-checker"),
)

_PR_VIEW_FIELDS: Final[str] = (
    "number,headRefOid,baseRefName,mergeable,statusCheckRollup,body,title"
)


def _split(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split the body into its head and its ``## `` sections, in order.

    Args:
        text: The wrap-up markdown.

    Returns:
        ``(head_lines, [(title, body_lines), ...])`` with fenced blocks
        already removed — fences carry quoted tool output, never prose
        the budget should count.
    """
    head: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in strip_fences(text.splitlines()):
        match = _HEADING_RE.match(line)
        if match:
            current = []
            sections.append((match.group("title"), current))
        elif current is None:
            head.append(line)
        else:
            current.append(line)
    return head, sections


def _prior_art_block(head: list[str]) -> set[int]:
    """Return the indexes of the ``prior-art-searched:`` block within *head*.

    The block is the ``prior-art-searched:`` line and every following
    line up to the next blank one — the reporter's evidence, which the
    budget does not count.

    Args:
        head: Header lines before the first section.

    Returns:
        Set of line indexes in the prior-art block.
    """
    indexes: set[int] = set()
    inside = False
    for i, line in enumerate(head):
        if _PRIOR_ART_RE.match(line):
            inside = True
        elif inside and not line.strip():
            inside = False
        if inside:
            indexes.add(i)
    return indexes


def _check_head(head: list[str]) -> list[str]:
    """Validate the header contract and the single summary line.

    Args:
        head: Lines before the first ``## `` section.

    Returns:
        Violations, empty when the head is well-formed.
    """
    problems: list[str] = []
    if not head or not VERIFIED_AT_RE.match(head[0]):
        problems.append("first line must be the `verified-at: <sha>` header")
    evidence = _prior_art_block(head)
    summary_lines = [
        line
        for i, line in enumerate(head[1:], start=1)
        if line.strip()
        and i not in evidence
        and not _MODE_RE.match(line)
        and not line.startswith("# ")
        and not line.startswith("<!--")
    ]
    if len(summary_lines) != 1:
        problems.append(
            "exactly one summary line belongs between the header and the first "
            f"section (found {len(summary_lines)}); the rest is narration"
        )
    return problems


def _is_clean(body: list[str]) -> bool:
    """Return whether a section reports a clean check (its first line says so).

    Args:
        body: Section body lines.

    Returns:
        True when the first non-blank line starts with a clean prefix.
    """
    first = next((line.strip() for line in body if line.strip()), "")
    return first.lower().startswith(_CLEAN_PREFIXES)


def _check_sections(sections: list[tuple[str, list[str]]]) -> list[str]:
    """Validate presence and one-line shape of the required sections.

    Args:
        sections: ``(title, body_lines)`` pairs in document order.

    Returns:
        Violations, empty when every required section is present and
        every clean or status section is exactly one line.
    """
    problems: list[str] = []
    titles = {title.casefold(): body for title, body in sections}
    for required in REQUIRED_SECTIONS:
        body = titles.get(required.casefold())
        if body is None:
            problems.append(f"missing section `## {required}`")
            continue
        n_lines = sum(1 for line in body if line.strip())
        if n_lines <= 1:
            continue
        if required in STATUS_SECTIONS:
            problems.append(
                f"`## {required}` is a status line — one line, found {n_lines}"
            )
        elif _is_clean(body):
            problems.append(
                f"`## {required}` is clean (PASS) but runs {n_lines} lines — a clean "
                "check is ONE line; prose is for findings"
            )
    return problems


def _count_words(head: list[str], sections: list[tuple[str, list[str]]]) -> int:
    """Count the words the budget applies to (prose, not headers or evidence).

    Args:
        head: Header lines before the first section.
        sections: List of (title, body_lines) section tuples.

    Returns:
        Total word count of budgeted prose.
    """
    evidence = _prior_art_block(head)
    counted: list[str] = [
        line
        for i, line in enumerate(head)
        if i not in evidence
        and not VERIFIED_AT_RE.match(line)
        and not _MODE_RE.match(line)
        and not line.startswith("<!--")
    ]
    for _title, body in sections:
        counted.extend(line for line in body if not line.startswith("<!--"))
    return sum(len(line.split()) for line in counted)


def _check_budget(head: list[str], sections: list[tuple[str, list[str]]]) -> list[str]:
    """Enforce the word budget, scaled by the number of findings sections.

    Args:
        head: Lines before the first section.
        sections: ``(title, body_lines)`` pairs in document order.

    Returns:
        One violation when the body exceeds ``WORD_BUDGET`` plus
        ``FINDING_ALLOWANCE`` per required non-clean, non-status section.
    """
    titles = {title.casefold(): body for title, body in sections}
    findings = sum(
        1
        for required in REQUIRED_SECTIONS
        if required not in STATUS_SECTIONS
        and (body := titles.get(required.casefold())) is not None
        and not _is_clean(body)
    )
    budget = WORD_BUDGET + FINDING_ALLOWANCE * findings
    words = _count_words(head, sections)
    if words > budget:
        return [
            (
                f"{words} words over a budget of {budget} ({WORD_BUDGET} + "
                f"{FINDING_ALLOWANCE} x {findings} findings); clean checks "
                "are one line"
            )
        ]
    return []


def _check_attribution(text: str) -> list[str]:
    """Wrap the shared FOUNDATION §2 attribution gate as a violation list.

    Args:
        text: The wrap-up markdown to validate.

    Returns:
        Violations if AI attribution is detected, empty list otherwise.
    """
    try:
        validate_no_ai_attribution(text)
    except ValidationError as exc:
        return [str(exc)]
    return []


def validate_wrapup(text: str) -> list[str]:
    """Return every rule the wrap-up *text* breaks (empty means valid).

    Pure: text in, violations out — the rules are ``_TEMPLATE.md``'s
    header contract and report-by-exception paragraph, made checkable.
    Unfilled ``compose`` slots are reported first: they are the concrete
    cause of the vaguer header and budget messages that follow them.

    Args:
        text: The wrap-up markdown as it would be posted.

    Returns:
        Human-readable violations in document order.
    """
    slots = [
        f"unfilled slot `{name}` — replace its `<!-- forge:fill … -->` line"
        for name in unfilled_slots(text)
    ]
    head, sections = _split(text)
    return (
        slots
        + _check_head(head)
        + _check_sections(sections)
        + _check_budget(head, sections)
        + _check_attribution(text)
    )


def _collapse(existing: list[dict[str, object]], new_sha: str) -> int:
    """Fold every earlier wrap-up into a ``<details>`` block, once.

    Args:
        existing: Marker-carrying wrap-ups captured *before* the new post.
        new_sha: The new wrap-up's ``verified-at`` SHA, named in the summary.

    Returns:
        How many comments were collapsed; already-collapsed ones are skipped.
    """
    collapsed = 0
    for comment in existing:
        body = str(comment.get("body", ""))
        if body.lstrip().startswith("<details>"):
            continue
        shas = extract_verified_shas(body)
        old = shas[0] if shas else "?"
        summary = SUPERSEDED_SUMMARY.format(new=new_sha, old=old)
        new_body = f"<details><summary>{summary}</summary>\n\n{body}\n\n</details>\n"
        if patch_comment(int(str(comment["id"])), new_body):
            collapsed += 1
    return collapsed


def post_wrapup(pr_number: int, body: str) -> int:
    """Post *body* as the PR's wrap-up, retiring the ones it supersedes.

    Order matters: the earlier wrap-ups are listed before the post, so
    the new one can never be mistaken for a superseded one; the collapse
    runs after the post is live, so a failure there leaves an extra
    visible attestation rather than a PR with none; the squash comment is
    re-posted last (its own CLI's invariant) because a wrap-up posted
    through this CLI is invisible to the ``keep_squash_comment_last``
    hook, which only sees the outer Bash command.

    Args:
        pr_number: GitHub PR number.
        body: The validated wrap-up markdown (marker appended here).

    Returns:
        ``0`` when the post succeeded; the ``gh`` exit code otherwise.
    """
    existing = list_marker_comments(pr_number, WRAPUP_MARKER)
    stamped = body.rstrip("\n") + "\n\n" + WRAPUP_MARKER + "\n"
    rc = post_new_comment(pr_number, stamped)
    if rc != 0:
        return rc
    shas = extract_verified_shas(body)
    new_sha = shas[0] if shas else "?"
    if existing is None:
        logger.warning("could not list earlier wrap-ups; none were collapsed")
    else:
        n = _collapse(existing, new_sha)
        emit(
            f"Posted wrap-up (verified-at {new_sha}); "
            f"collapsed {n} superseded wrap-up(s)."
        )
    # Keeps the squash comment newest; a PR with no squash comment yet
    # just logs — that is the normal first-wrap-up case, not a failure.
    ensure_last(pr_number)
    return 0


def _section_bounds(lines: list[str], title: str) -> tuple[int, int] | None:
    """Return the ``(heading, end)`` line indexes of section *title*.

    A heading-shaped line inside a fence (a quoted report, gate output) is
    not a section — the same reading ``_split`` gives ``validate_wrapup``,
    so a refresh never splices into a fence.

    Args:
        lines: The wrap-up split on newlines.
        title: Section title (case-insensitive).

    Returns:
        Heading index and the index of the next heading (or the end), or
        ``None`` when the section is absent.
    """
    fenced = fenced_line_indexes(lines)
    headings = [
        i for i, line in enumerate(lines) if i not in fenced and _HEADING_RE.match(line)
    ]
    start = next(
        (
            i
            for i in headings
            if (m := _HEADING_RE.match(lines[i]))
            and m.group("title").casefold() == title.casefold()
        ),
        None,
    )
    if start is None:
        return None
    end = next((i for i in headings if i > start), len(lines))
    return start, end


def refresh_sections(text: str, *, ci_status: str, issue_management: str) -> str:
    """Replace the CI Status and Issue Management bodies, leaving the rest as is.

    The two sections whose truth changes after authoring — CI finishes, the
    PR body gains its closing keyword — are rewritten at posting time;
    every other byte of the authored wrap-up is kept.

    Args:
        text: The authored wrap-up.
        ci_status: New one-line CI Status.
        issue_management: New Issue Management text.

    Returns:
        The refreshed wrap-up. A missing Issue Management section is
        inserted before Code Quality (or appended when that is absent too).
    """
    lines = text.split("\n")
    for title, value in (
        ("CI Status", ci_status),
        ("Issue Management", issue_management),
    ):
        bounds = _section_bounds(lines, title)
        if bounds is not None:
            start, end = bounds
            lines[start + 1 : end] = ["", value, ""]
            continue
        anchor = _section_bounds(lines, "Code Quality")
        at = anchor[0] if anchor is not None else len(lines)
        lines[at:at] = [f"## {title}", "", value, ""]
    return "\n".join(lines)


def post_gates(
    view: Mapping[str, object],
    verified_sha: str,
    *,
    behind: int | None,
    emergency: bool,
) -> tuple[list[str], list[str]]:
    """Decide whether a wrap-up may be published on the PR as it is now.

    A merge by the tool would move HEAD and make the wrap-up stale on
    arrival, so every problem is a refusal naming the fix, never a repair.

    Args:
        view: ``gh pr view --json`` fields (``headRefOid``, ``baseRefName``,
            ``mergeable``, optionally ``number``).
        verified_sha: The wrap-up's ``verified-at:`` SHA.
        behind: Commits the branch is behind ``origin/<base>``, or ``None``
            when that could not be determined.
        emergency: Whether the wrap-up is the recorded emergency PR's,
            which may be behind base (never conflicting).

    Returns:
        ``(refusals, notes)`` — any refusal blocks the post; notes only inform.
    """
    refusals: list[str] = []
    notes: list[str] = []
    head = str(view.get("headRefOid") or "")
    base = str(view.get("baseRefName") or "main")
    rerun = f"/pr {view.get('number') or '<N>'}"
    if not head.startswith(verified_sha):
        refusals.append(
            f"the wrap-up verifies {verified_sha} but the PR head is "
            f"{head[:12] or 'unknown'} — re-verify with {rerun} (delta)"
        )
    mergeable = str(view.get("mergeable") or "")
    if mergeable == "CONFLICTING":
        refusals.append(
            f"the branch conflicts with {base}: run `git merge origin/{base}` "
            "(only generated files conflict: `forge-resync --resolve-conflicts`), "
            f"then re-verify with {rerun}"
        )
    elif mergeable == "UNKNOWN":
        notes.append("GitHub has not computed mergeability yet (UNKNOWN)")
    if behind is None:
        notes.append(f"could not compare with origin/{base}; behind-base check skipped")
    elif behind > 0 and emergency:
        notes.append(
            f"the branch is {behind} commit(s) behind origin/{base}; allowed "
            "for the recorded emergency PR"
        )
    elif behind > 0:
        refusals.append(
            f"the branch is {behind} commit(s) behind origin/{base}: run "
            f"`git merge origin/{base}`, then re-verify with {rerun}"
        )
    return refusals, notes


def _read_optional(path: Path | None) -> str | None:
    """Read *path* when given.

    Args:
        path: File to read, or ``None``.

    Returns:
        The file text, or ``None`` when no path was given.

    Raises:
        ComposeError: When the file cannot be read.
    """
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise ComposeError(msg) from exc


def _load_plan(
    root: Path, args: argparse.Namespace
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return the plan's ``(mode, reporters, reasons)``.

    Args:
        root: Repo root.
        args: Parsed ``compose`` arguments.

    Returns:
        From ``--plan`` JSON when given, else a fresh ``forge-pr-plan``
        classification.

    Raises:
        ComposeError: When the plan file is unreadable or the base cannot
            be classified.
    """
    try:
        if args.plan is not None:
            data = json.loads(args.plan.read_text(encoding="utf-8"))
            return (
                str(data["mode"]),
                tuple(data.get("reporters", ())),
                tuple(data.get("reasons", ())),
            )
        plan = classify(root, args.base, args.pr)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        msg = f"cannot read the plan: {exc}"
        raise ComposeError(msg) from exc
    except subprocess.CalledProcessError as exc:
        msg = f"cannot classify against {args.base}: git exited {exc.returncode}"
        raise ComposeError(msg) from exc
    return plan.mode, tuple(plan.reporters), tuple(plan.reasons)


def _code_quality(root: Path) -> str:
    """Render Code Quality from the timing log and each log's freshness.

    Args:
        root: Repo root.

    Returns:
        The one-line Code Quality status.
    """
    health = root / "code_health"
    timing = health / "precommit_timing.log"
    timing_text = timing.read_text(encoding="utf-8") if timing.is_file() else None
    verdicts = freshness_verdicts(root)
    try:
        steps = resolve_steps(root)
    except ValueError:
        steps = []
    smart = health / "smart_test.log"
    smart_text = smart.read_text(encoding="utf-8") if smart.is_file() else None
    return render_code_quality(
        timing_text,
        verdicts,
        [step.name for step in steps],
        pytest_line=pytest_summary_line(smart_text),
        pytest_verdict=verdicts.get("smart_test"),
    )


def _ci_status(pr_number: int | None, view: Mapping[str, object] | None) -> str:
    """Return the CI Status line for the PR as *view* shows it.

    Only a PR that does not exist yet is pending publication; an existing
    PR whose view could not be read says so instead of claiming it is
    unpublished.

    Args:
        pr_number: The PR, or ``None`` before it exists.
        view: ``gh pr view --json`` fields including ``statusCheckRollup``,
            or ``None`` when the PR could not be read.

    Returns:
        The one-line CI Status.
    """
    if pr_number is None:
        return PENDING_CI
    if view is None:
        return f"unknown — could not read PR #{pr_number}"
    rollup = view.get("statusCheckRollup")
    return (
        summarize_rollup(rollup) if isinstance(rollup, list) else "no checks reported"
    )


def _branch_messages(root: Path, base_ref: str) -> str:
    """Return the commit messages on HEAD since *base_ref*.

    Args:
        root: Repo root.
        base_ref: Base the branch forked from.

    Returns:
        Concatenated messages, or ``""`` when git fails.
    """
    if base_ref.startswith("-"):
        return ""
    return run_git(
        "log",
        "--format=%B",
        f"{base_ref}..HEAD",
        cwd=root,
        check=False,
        log_errors=False,
    )


def _gather_inputs(root: Path, args: argparse.Namespace) -> ComposeInputs:
    """Collect everything ``compose`` renders from.

    Args:
        root: Repo root.
        args: Parsed ``compose`` arguments.

    Returns:
        The compose inputs.

    Raises:
        ComposeError: On unreadable inputs or failed light-regen gates.
    """
    mode, reporters, reasons = _load_plan(root, args)
    reports = {
        name: text
        for flag, name in _REPORT_FLAGS
        if (text := _read_optional(getattr(args, flag))) is not None
    }
    view = gh_pr_view(args.pr, "body,statusCheckRollup") if args.pr else None
    body = str(view.get("body") or "") if view else ""
    refs = find_closing_refs(f"{body}\n{_branch_messages(root, args.base)}")
    emergency = armed_state(root)
    evidence = None
    if mode == "light-regen":
        passed, block = run_gate_evidence(
            root,
            ",".join(PROVENANCE_GATE_STEPS),
            success_headline="Provenance gates pass.",
            failure_headline="Provenance gates FAILED.",
            section_title="Provenance gates",
        )
        if not passed:
            msg = "light-regen provenance gates failed: run the full reporter round"
            raise ComposeError(msg)
        evidence = evidence_fence(block)
    return ComposeInputs(
        head_sha=run_git(
            "rev-parse", "--short", "HEAD", cwd=root, check=False, log_errors=False
        ),
        mode=mode,
        reasons=reasons,
        reporters=reporters,
        reports=reports,
        prior_art_report=_read_optional(args.prior_art),
        added_non_fragment_paths=tuple(
            non_fragment_adds(added_paths(root, f"{args.base}...HEAD"))
        ),
        issue_management=render_issue_management(
            refs, pr_body_checked=view is not None
        ),
        code_quality=_code_quality(root),
        ci_status=_ci_status(args.pr, view),
        emergency_ledger=emergency.ledger_issue if emergency else None,
        delta_prior_sha=(
            wrapup_freshness(args.pr).latest_verified_at
            if mode == "delta" and args.pr
            else None
        ),
        light_regen_evidence=evidence,
    )


def _cmd_compose(args: argparse.Namespace) -> int:
    """Write ``code_health/pr_wrapup.md`` with slots for the author to fill.

    Args:
        args: Parsed ``compose`` arguments.

    Returns:
        ``0`` when written; ``2`` when evidence is missing or unreadable,
        or ``--base`` looks like an option.
    """
    if args.base.startswith("-"):
        emit(f"pr-wrapup: invalid --base {args.base!r}")
        return 2
    root = repo_root()
    try:
        text = render_wrapup(_gather_inputs(root, args))
    except ComposeError as exc:
        emit(f"pr-wrapup: {exc}")
        return 2
    path = root / WRAPUP_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    slots = unfilled_slots(text)
    todo = (
        f"{len(slots)} slot(s) to fill: {', '.join(slots)}"
        if slots
        else "no slots to fill"
    )
    emit(f"pr-wrapup: wrote {path}; {todo}")
    return 0


def _is_emergency_post(root: Path, text: str, pr: int) -> bool:
    """Return whether *text* is the recorded emergency PR's own wrap-up.

    The head's ``wrapup-mode: emergency`` line alone is text any author —
    or a filled slot quoting it — can write, so waiving the behind-base
    refusal also needs the sentinel's structural record of this PR
    (``forge-emergency record-pr``), the same evidence repayment trusts.

    Args:
        root: Repo root.
        text: The wrap-up.
        pr: The PR being posted to.

    Returns:
        ``True`` only when the head declares emergency mode and the
        sentinel records *pr*.
    """
    head, _sections = _split(text)
    if not any(_EMERGENCY_RE.match(line) for line in head):
        return False
    state = read_state(root)
    return state is not None and state.pr_number == pr


def _cmd_post(args: argparse.Namespace, text: str, path: Path) -> int:
    """Gate, refresh, post and record a validated wrap-up.

    Args:
        args: Parsed ``post`` arguments.
        text: The validated wrap-up.
        path: Where the wrap-up lives (rewritten with the refreshed text
            once it validates).

    Returns:
        ``0`` when posted; ``2`` when the refreshed body fails validation;
        ``3`` when a gate refuses or the PR cannot be read; ``gh``'s exit
        code when the post fails.
    """
    root = repo_root()
    view = gh_pr_view(args.pr, _PR_VIEW_FIELDS)
    if view is None:
        emit(f"pr-wrapup: refused: cannot read PR #{args.pr} with gh; nothing posted")
        return EXIT_REFUSED
    base = str(view.get("baseRefName") or "main")
    counts = (
        behind_ahead(root, f"origin/{base}")
        if fetch_quietly(root, "origin", base)
        else None
    )
    refusals, notes = post_gates(
        view,
        extract_verified_shas(text)[0],
        behind=counts[0] if counts is not None else None,
        emergency=_is_emergency_post(root, text, args.pr),
    )
    for note in notes:
        emit(f"pr-wrapup: note: {note}")
    if refusals:
        for refusal in refusals:
            emit(f"pr-wrapup: refused: {refusal}")
        return EXIT_REFUSED
    refs = find_closing_refs(
        f"{view.get('body') or ''}\n{_branch_messages(root, f'origin/{base}')}"
    )
    refreshed = refresh_sections(
        text,
        ci_status=_ci_status(args.pr, view),
        issue_management=render_issue_management(refs, pr_body_checked=True),
    )
    problems = validate_wrapup(refreshed)
    if problems:
        for problem in problems:
            emit(f"pr-wrapup: {problem}")
        return 2
    path.write_text(refreshed, encoding="utf-8")
    rc = post_wrapup(args.pr, refreshed)
    if rc != 0:
        return rc
    if not args.no_continuation:
        title = str(view.get("title") or f"PR #{args.pr}")
        # "--": a PR title may start with "-" and must never parse as a flag.
        continuation_append.main(["--pr", str(args.pr), "--", title], repo_root=root)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``forge-pr-wrapup`` argument parser.

    Returns:
        The parser with ``compose``, ``validate`` and ``post`` subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="forge-pr-wrapup",
        description=(
            "Compose, validate and post the PR wrap-up comment (FOUNDATION §6)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    comp = sub.add_parser(
        "compose",
        help="write code_health/pr_wrapup.md with slots for the judgment parts",
    )
    comp.add_argument("--base", required=True, metavar="REF", help="base ref")
    comp.add_argument("--pr", type=int, metavar="N", help="PR number, once it exists")
    comp.add_argument(
        "--plan",
        type=Path,
        metavar="FILE",
        help="forge-pr-plan JSON (default: classify)",
    )
    for flag, name in _REPORT_FLAGS:
        comp.add_argument(f"--{flag}", type=Path, metavar="FILE", help=f"{name} report")
    comp.add_argument(
        "--prior-art", type=Path, metavar="FILE", help="forge:prior-art report"
    )
    val = sub.add_parser(
        "validate", help="check a wrap-up body; exit 2 listing violations"
    )
    val.add_argument(
        "file", type=Path, help="wrap-up markdown (code_health/pr_wrapup.md)"
    )
    post = sub.add_parser(
        "post",
        help=(
            "validate, refuse a stale/conflicting/behind branch (exit 3), refresh "
            "CI Status and Issue Management, post, append the CONTINUATION record"
        ),
    )
    post.add_argument("--pr", type=int, required=True, metavar="N", help="PR number")
    post.add_argument(
        "--body-file",
        type=Path,
        metavar="FILE",
        help="wrap-up markdown (default: code_health/pr_wrapup.md)",
    )
    post.add_argument(
        "--no-continuation",
        action="store_true",
        help="do not append the CONTINUATION record",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ``compose``, ``validate`` or ``post``.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        ``0`` on success; ``2`` when validation fails (every violation
        printed), a body file is unreadable, or compose lacks evidence or
        gets an option-like ``--base``;
        ``3`` when ``post`` refuses the publication; ``gh``'s exit code
        when the post fails.
    """
    args = _build_parser().parse_args(argv)
    if args.command == "compose":
        return _cmd_compose(args)
    if args.command == "validate":
        path: Path = args.file
    else:
        path = args.body_file or repo_root() / WRAPUP_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("pr-wrapup: cannot read %s", path)
        return 2
    problems = validate_wrapup(text)
    if problems:
        for problem in problems:
            emit(f"pr-wrapup: {problem}")
        return 2
    if args.command == "validate":
        emit(f"pr-wrapup: {path} is valid.")
        return 0
    return _cmd_post(args, text, path)


if __name__ == "__main__":
    sys.exit(main())
