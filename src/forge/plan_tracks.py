"""plan_tracks — ``forge-plan-check``'s checks across a set of issues or PRs.

:mod:`forge.plan_staleness` asks whether *one* plan still holds. Clearing
a backlog asks the same questions of a set, plus two only a set has:
which issues touch the same files and so belong in one track, and in
which order open PRs should merge so one does not invalidate another.

``tracks [issue ...] [--focus ISSUE ... | --from-pr N]``
    For the open issues (all, or the ones named): each issue's files by
    the shared rule (:func:`~forge.plan_staleness.plan_paths`), every pair
    of issues naming a common file, every open PR changing a file an
    issue names, the ``track:<name>`` labels already applied, and each
    file more than one issue names with its issues — the evidence a
    track proposal is judged from. ``--focus`` reports only the lines
    involving the named issues; ``--from-pr`` takes them from the
    ``Follow-ups:`` line of PR *N*'s newest wrap-up by a write-access
    author. Either prints ``placement: none`` when no line involves them.
``track-members <name>``
    Who belongs to track *name*: an open issue whose authenticated plan
    carries the line ``track: <name>`` and whose author has write access.
    The ``track:<name>`` label only nominates candidates; every labelled
    issue failing either test is reported excluded with its reason, and
    a plan naming the track on an unlabelled issue is reported too.
``current <issue> [issue ...]``
    The drift check of each issue since it was *filed*: merges on the base
    branch that touched its files, so a fix that already landed shows up
    before anyone plans it. The base history is listed once for the union
    of every issue's files.
``merge-order [pr ...]``
    Probes every open PR against the base and against each other with
    ``git merge-tree`` (nothing is checked out or written to a ref) and
    suggests an order: PRs entangled with nothing first, the most
    entangled last, each naming the earlier PRs it must refresh after.
    At most :data:`MERGE_ORDER_CAP` PRs are probed, the probes growing with
    the square of the count; past it is a finding.

Output and exit codes follow :mod:`forge.plan_staleness`: plain lines,
GitHub-derived text limited to numbers and validated paths, 0 clean,
1 finding, 2 unknown or refused.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from functools import partial
from itertools import combinations
from typing import TYPE_CHECKING, Final

from forge.git_utils import fetch_quietly, merge_tree_clean
from forge.plan_staleness import (
    PLAN_MARKER,
    PLAN_READY,
    Report,
    WriterCheck,
    authenticated_comment,
    authenticated_plan,
    drift_from,
    issue_and_plan,
    parse_timestamp,
    plan_paths,
    record_lookup,
)
from forge.pr_delta import (
    FOLLOW_UPS_LABEL,
    ISSUE_REF_RE,
    REGEN_COMMANDS,
    fenced_line_indexes,
    strip_fences,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from forge.plan_staleness import GitHubSource, Merge
    from forge.plan_staleness_gh import Comment, Issue, OpenPR


logger = logging.getLogger(__name__)

TRACK_PREFIX: Final[str] = "track:"
# The one shape a track name takes — in a ``track:<name>`` label and in
# a plan's ``track: <name>`` line. Anything else is never echoed.
TRACK_NAME_PATTERN: Final[str] = r"[a-z0-9][a-z0-9._-]{0,49}"
# A wrap-up comment opens with its reporter-contract header.
WRAPUP_HEADER: Final[str] = "verified-at:"
# Pairwise probes grow with the square of the PR count: 20 PRs is 190
# pair probes plus 20 against the base.
MERGE_ORDER_CAP: Final[int] = 20

_TRACK_NAME_RE: Final[re.Pattern[str]] = re.compile(TRACK_NAME_PATTERN)
_TRACK_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    re.escape(TRACK_PREFIX) + TRACK_NAME_PATTERN
)
# A plan's track line, read before the name is validated.
_PLAN_TRACK_LINE_RE: Final[re.Pattern[str]] = re.compile(r"track: (?P<name>\S+)")
_SHA_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}")
# The lines that place an issue next to another issue or an open PR.
_PLACEMENT_PREFIXES: Final[tuple[str, ...]] = ("overlap:", "pr-overlap:", "shared:")
# Generated artifacts are a pure function of the merged tree (regenerated,
# never hand-merged), so two PRs both touching one do not entangle.
_GENERATED_PATHS: Final[frozenset[str]] = frozenset(REGEN_COMMANDS)


@dataclass(frozen=True)
class IssueFiles:
    """One open issue reduced to what grouping needs."""

    number: int
    paths: frozenset[str]
    tracks: tuple[str, ...]
    plan_ready: bool


# --------------------------------------------------------------------------
# tracks
# --------------------------------------------------------------------------


def _track_labels(issue: Issue, report: Report) -> tuple[str, ...]:
    """Return *issue*'s well-formed ``track:`` labels, noting any malformed one.

    Args:
        issue: The issue.
        report: Report that receives the note.

    Returns:
        The labels that may be echoed, in label order.
    """
    labels = [label for label in issue.labels if label.startswith(TRACK_PREFIX)]
    shown = tuple(label for label in labels if _TRACK_LABEL_RE.fullmatch(label))
    if len(shown) < len(labels):
        report.note(
            f"note: #{issue.number} carries {len(labels) - len(shown)} track "
            "label(s) with an unexpected name; not shown"
        )
    return shown


def _issue_files(
    issue: Issue, is_writer: WriterCheck, report: Report
) -> IssueFiles | None:
    """Reduce *issue* to its files and track labels, recording notes.

    Args:
        issue: Open issue from the listing (comments included).
        is_writer: Plan-author access check.
        report: Report to write into.

    Returns:
        The reduced issue, or None when its plan is unknown.
    """
    lookup = authenticated_plan(issue.comments, is_writer)
    if not record_lookup(lookup, issue.number, report):
        return None
    paths, from_body = plan_paths(lookup.plan, issue.body)
    tracks = _track_labels(issue, report)
    if not paths:
        report.note(f"note: #{issue.number} names no file; place it by judgment")
    elif from_body:
        report.note(
            f"note: #{issue.number} paths come from the issue body, "
            "not an authenticated plan"
        )
    if tracks:
        report.note(f"track: #{issue.number} carries {', '.join(tracks)}")
    return IssueFiles(
        issue.number, frozenset(paths), tracks, plan_ready=PLAN_READY in issue.labels
    )


def _select(
    issues: Sequence[Issue], numbers: Sequence[int], report: Report
) -> list[Issue]:
    """Return the listed issues the caller named (all when none), refusing others.

    Args:
        issues: Every open issue.
        numbers: Issue numbers the caller named; empty for all.
        report: Report that receives a refusal per number not open.

    Returns:
        The selected issues in listing order.
    """
    if not numbers:
        return list(issues)
    listed = {issue.number for issue in issues}
    for number in numbers:
        if number not in listed:
            report.unknown(f"refused: #{number} is not an open issue")
    return [issue for issue in issues if issue.number in numbers]


def _report_pairs(
    files: Sequence[IssueFiles], report: Report, focus: frozenset[int]
) -> None:
    """Record every pair of issues naming a common file.

    Args:
        files: The reduced issues.
        report: Report to write into.
        focus: When non-empty, only pairs with a member in it are reported.
    """
    for left, right in combinations(files, 2):
        if focus and left.number not in focus and right.number not in focus:
            continue
        shared = sorted(left.paths & right.paths)
        if shared:
            report.finding(
                f"overlap: #{left.number} and #{right.number} both name "
                f"{', '.join(shared)}"
            )


def _report_prs(
    files: Sequence[IssueFiles],
    prs: Sequence[OpenPR],
    report: Report,
    focus: frozenset[int],
) -> None:
    """Record every open PR changing a file an issue names.

    A PR that closes the issue is its delivery, not a collision: noted.

    Args:
        files: The reduced issues.
        prs: Open PRs.
        report: Report to write into.
        focus: When non-empty, only these issues are matched against PRs.
    """
    for pr in prs:
        changed = set(pr.files)
        for issue in files:
            if focus and issue.number not in focus:
                continue
            hits = sorted(issue.paths & changed)
            if not hits:
                continue
            if issue.number in pr.closes:
                report.note(f"pr: PR #{pr.number} delivers #{issue.number}")
                continue
            report.finding(
                f"pr-overlap: PR #{pr.number} changes {', '.join(hits)}, "
                f"named by #{issue.number}"
            )


def _report_shared_files(
    files: Sequence[IssueFiles], report: Report, focus: frozenset[int]
) -> None:
    """Record each file more than one issue names, most-named first.

    Deliberately not a transitive grouping: files every change touches (a
    manifest, a generated reference, the rule documents) would chain a
    whole backlog into one "track". Per file, the reader sees which files
    are hubs and which bind a real cluster, and judges the tracks.

    Args:
        files: The reduced issues.
        report: Report to write into.
        focus: When non-empty, only files a focus issue names are reported.
    """
    naming: dict[str, list[IssueFiles]] = {}
    for issue in files:
        for path in issue.paths:
            naming.setdefault(path, []).append(issue)
    shared = sorted(
        ((path, issues) for path, issues in naming.items() if len(issues) > 1),
        key=lambda item: (-len(item[1]), item[0]),
    )
    for path, issues in shared:
        if focus and not any(issue.number in focus for issue in issues):
            continue
        members = " ".join(f"#{issue.number}" for issue in issues)
        tracks = sorted({track for issue in issues for track in issue.tracks})
        ready = [f"#{issue.number}" for issue in issues if issue.plan_ready]
        line = f"shared: {path} — {len(issues)} issues: {members}"
        line += f"; tracks: {', '.join(tracks) or 'none'}"
        if ready:
            line += f"; plan-ready: {' '.join(ready)}"
        report.note(line)


def _open_focus(
    issues: Sequence[Issue], focus: Sequence[int], report: Report
) -> list[int]:
    """Return the focus issues still open, noting each one that is not.

    Args:
        issues: Every open issue.
        focus: Issue numbers to keep.
        report: Report that receives a note per issue skipped.

    Returns:
        The open focus issues, in *focus* order.
    """
    listed = {issue.number for issue in issues}
    for number in focus:
        if number not in listed:
            report.note(f"note: #{number} is not an open issue; skipped")
    return [number for number in focus if number in listed]


def _report_placement_none(
    files: Sequence[IssueFiles], report: Report, focus: frozenset[int]
) -> None:
    """Say ``placement: none`` when nothing places the focus issues.

    Only when the answer is established: no unknown in the run, every
    focus issue reduced, and each naming at least one file — an issue
    naming none was already handed to judgment by its own note.

    Args:
        files: The reduced issues.
        report: Report to write into, placement lines already recorded.
        focus: The focus issues.
    """
    if report.unknowns:
        return
    reduced = {issue.number: issue for issue in files if issue.number in focus}
    if set(reduced) != focus or not all(issue.paths for issue in reduced.values()):
        return
    if not any(line.startswith(_PLACEMENT_PREFIXES) for line in report.lines):
        report.note("placement: none")


def check_tracks(
    source: GitHubSource,
    numbers: Sequence[int] = (),
    *,
    focus: Sequence[int] = (),
    skip_closed_focus: bool = False,
) -> Report:
    """Report which open issues share files with each other and with open PRs.

    *focus* answers the per-PR placement question — where do these newly
    filed issues fit? — by comparing them against every considered issue
    while reporting only the lines that involve them, and ends in
    ``placement: none`` when no line does.

    Args:
        source: GitHub reads.
        numbers: Issues to consider; empty for every open issue.
        focus: Issues whose overlaps alone are reported; empty for all.
        skip_closed_focus: Skip a focus issue that is not open with a
            note (a follow-up closed since it was filed) instead of
            refusing it.

    Returns:
        The report; a finding per overlapping pair and per PR collision.
    """
    report = Report()
    issues = source.open_issues()
    prs = source.open_pull_requests()
    if issues is None or prs is None:
        report.unknown("unknown: could not list open issues and PRs from GitHub")
        return report
    if skip_closed_focus and focus:
        focus = _open_focus(issues, focus, report)
        if not focus:
            report.note("placement: none")
            return report
    selected = _select(issues, numbers, report)
    focused = frozenset(issue.number for issue in _select(issues, focus, report))
    if focus and not focused:
        return report
    if focused:
        # A focus issue is compared against the others, so it is
        # considered even when *numbers* leaves it out.
        selected += [i for i in issues if i.number in focused and i not in selected]
    is_writer = WriterCheck(source)
    files = [
        reduced
        for issue in selected
        if (reduced := _issue_files(issue, is_writer, report)) is not None
    ]
    report.note(f"issues: {len(files)} checked; open PRs: {len(prs)}")
    _report_pairs(files, report, focused)
    _report_prs(files, prs, report, focused)
    _report_shared_files(files, report, focused)
    if focused:
        _report_placement_none(files, report, focused)
    return report


def _follow_ups(text: str) -> list[int]:
    """Return the issues the wrap-up's ``Follow-ups:`` line names.

    Only the Issue Management section counts, outside fences: that is
    where ``forge-pr-wrapup`` writes the line, and nothing quoted
    elsewhere in the wrap-up can add an issue to placement.

    Args:
        text: The wrap-up comment body.

    Returns:
        Issue numbers in first-appearance order, without duplicates.
    """
    lines = text.split("\n")
    fenced = fenced_line_indexes(lines)
    refs: list[int] = []
    inside = False
    for i, line in enumerate(lines):
        if i in fenced:
            continue
        if line.startswith("## "):
            inside = line.strip() == "## Issue Management"
            continue
        if inside and line.startswith(FOLLOW_UPS_LABEL):
            for number in ISSUE_REF_RE.findall(line[len(FOLLOW_UPS_LABEL) :]):
                if int(number) not in refs:
                    refs.append(int(number))
    return refs


def wrapup_follow_ups(
    source: GitHubSource, pr_number: int, report: Report
) -> list[int] | None:
    """Return the follow-ups PR *pr_number*'s newest trusted wrap-up names.

    The wrap-up is the newest comment opening with :data:`WRAPUP_HEADER`
    whose author has write access — the same trust rule a plan comment
    meets, since what it names is handed to placement.

    Args:
        source: GitHub reads.
        pr_number: The PR whose wrap-up to read.
        report: Report that receives the notes and any unknown line.

    Returns:
        The follow-up issue numbers (possibly none), or None when the
        wrap-up could not be read or authenticated.
    """
    comments = source.comments(pr_number)
    if comments is None:
        report.unknown(f"unknown: could not read PR #{pr_number}'s comments")
        return None
    lookup = authenticated_comment(comments, WriterCheck(source), WRAPUP_HEADER)
    for comment_id in lookup.ignored:
        report.note(
            f"note: ignored wrap-up comment {comment_id} on PR #{pr_number}; "
            "its author lacks write access"
        )
    if lookup.unresolved is not None:
        report.unknown(
            f"unknown: could not check the access of wrap-up comment "
            f"{lookup.unresolved}'s author on PR #{pr_number}"
        )
        return None
    if lookup.plan is None:
        report.unknown(
            f"unknown: PR #{pr_number} has no wrap-up by a write-access author"
        )
        return None
    refs = _follow_ups(lookup.plan.body)
    named = " ".join(f"#{ref}" for ref in refs) or "none"
    report.note(f"wrap-up: comment {lookup.plan.id} on PR #{pr_number}")
    report.note(f"follow-ups: {named}")
    return refs


def check_tracks_from_pr(
    source: GitHubSource, pr_number: int, numbers: Sequence[int] = ()
) -> Report:
    """Run :func:`check_tracks` focused on the follow-ups PR *pr_number* names.

    Args:
        source: GitHub reads.
        pr_number: The PR whose wrap-up names the follow-ups.
        numbers: Issues to consider; empty for every open issue.

    Returns:
        The report: ``placement: none`` when the wrap-up names no
        follow-up, unknown when it cannot be read.
    """
    report = Report()
    refs = wrapup_follow_ups(source, pr_number, report)
    if refs is None:
        return report
    if not refs:
        report.note("placement: none")
        return report
    report.absorb(check_tracks(source, numbers, focus=refs, skip_closed_focus=True))
    return report


# --------------------------------------------------------------------------
# track-members
# --------------------------------------------------------------------------


def plan_track_names(plan_body: str) -> set[str]:
    """Return every name a plan's ``track: <name>`` lines give, outside fences.

    Names are returned raw so a malformed one still counts as "another
    track" rather than none; callers never echo them.

    Args:
        plan_body: The plan-validated comment body.

    Returns:
        The names, possibly empty.
    """
    return {
        match.group("name")
        for line in strip_fences(plan_body.splitlines())
        if (match := _PLAN_TRACK_LINE_RE.fullmatch(line.strip())) is not None
    }


def _plan_reason(names: set[str], track: str) -> str | None:
    """Return why a plan naming *names* does not place its issue in *track*.

    Args:
        names: The track names the plan gives.
        track: The track asked about.

    Returns:
        The reason, or None when the plan names exactly *track*.
    """
    if names == {track}:
        return None
    if not names:
        return "its plan names no track"
    if track in names:
        return "its plan names more than one track"
    return "its plan names another track"


def _exclusions(
    issue: Issue, track: str, is_writer: WriterCheck, report: Report
) -> list[str] | None:
    """Return every reason *issue* is not a member of *track*.

    Args:
        issue: Candidate issue (comments included).
        track: Validated track name.
        is_writer: Write-access check, shared across candidates.
        report: Report that receives notes and unknown lines.

    Returns:
        The reasons (empty for a member), or None when membership is unknown.
    """
    lookup = authenticated_plan(issue.comments, is_writer)
    if not record_lookup(lookup, issue.number, report):
        return None
    reasons: list[str] = []
    if lookup.plan is None:
        reasons.append("no authenticated plan")
    elif (why := _plan_reason(plan_track_names(lookup.plan.body), track)) is not None:
        reasons.append(why)
    author = is_writer(issue.author)
    if author is None:
        report.unknown(
            f"unknown: could not check the access of #{issue.number}'s author"
        )
        return None
    if not author:
        reasons.append(
            "its author lacks write access (eligibility: endorsement not checked)"
        )
    return reasons


def _names_track_in_text(issue: Issue, track: str) -> bool:
    """Return whether any plan-marker comment on *issue* has *track*'s line.

    A cheap text filter before authentication, so an unlabelled issue
    costs no permission lookup unless some plan comment names the track.

    Args:
        issue: The issue.
        track: Validated track name.

    Returns:
        Whether a candidate comment carries the line.
    """
    return any(
        comment.body.lstrip().startswith(PLAN_MARKER)
        and track in plan_track_names(comment.body)
        for comment in issue.comments
    )


def check_track_members(source: GitHubSource, track: str) -> Report:
    """Report which open issues belong to track *track*, and why others do not.

    Args:
        source: GitHub reads.
        track: Track name, without the ``track:`` prefix.

    Returns:
        The report: a ``member:`` note per member, a finding per labelled
        issue excluded and per member missing the label, unknown when a
        read or access check fails or the name is malformed.
    """
    report = Report()
    if _TRACK_NAME_RE.fullmatch(track) is None:
        report.unknown(f"refused: a track name must match {TRACK_NAME_PATTERN}")
        return report
    issues = source.open_issues()
    if issues is None:
        report.unknown("unknown: could not list open issues from GitHub")
        return report
    label = f"{TRACK_PREFIX}{track}"
    is_writer = WriterCheck(source)
    members = excluded = 0
    for issue in issues:
        labelled = label in issue.labels
        if not labelled and not _names_track_in_text(issue, track):
            continue
        reasons = _exclusions(issue, track, is_writer, report)
        if reasons is None:
            continue
        if reasons and labelled:
            excluded += 1
            report.finding(f"excluded: #{issue.number} — {'; '.join(reasons)}")
        elif not reasons:
            members += 1
            report.note(f"member: #{issue.number}")
            if not labelled:
                report.finding(
                    f"unlabelled: #{issue.number} — its plan names the track "
                    f"but it lacks the {label} label"
                )
    if not members and not excluded and not report.unknowns:
        report.unknown(f"refused: no open issue carries {label} or names it in a plan")
        return report
    report.note(f"track-members: {track} — {members} member(s), {excluded} excluded")
    return report


# --------------------------------------------------------------------------
# current
# --------------------------------------------------------------------------


def _restricted(
    merges: list[Merge] | None,
) -> Callable[[list[str]], list[Merge] | None]:
    """Return a ``log_merges`` callable answering from one listing.

    Args:
        merges: The base history for the union of every issue's files, or
            None when git failed.

    Returns:
        A callable keeping the merges that touched the given paths, each
        narrowed to those paths, or returning None when *merges* is None.
    """

    def log_merges(paths: list[str]) -> list[Merge] | None:
        """Return the listed merges that touched *paths*.

        Args:
            paths: The issue's files.

        Returns:
            The narrowed merges, or None when the listing failed.
        """
        if merges is None:
            return None
        wanted = set(paths)
        narrowed = [
            replace(m, paths=tuple(p for p in m.paths if p in wanted)) for m in merges
        ]
        return [m for m in narrowed if m.paths]

    return log_merges


def _verdict_line(number: int, part: Report) -> str:
    """Return the one-line per-issue verdict of the ``current`` check.

    Args:
        number: Issue number.
        part: That issue's report.

    Returns:
        The verdict line.
    """
    if part.unknowns:
        return f"current: #{number} unknown"
    if part.findings:
        return f"current: #{number} files changed since filing; re-check it"
    return f"current: #{number} no merge touched its files since filing"


def check_current(
    source: GitHubSource,
    numbers: Sequence[int],
    *,
    log_merges: Callable[[list[str]], list[Merge] | None],
) -> Report:
    """Run the drift check of each issue since it was filed, listing history once.

    Args:
        source: GitHub reads.
        numbers: Issues to check.
        log_merges: Base-branch history reader (see
            :func:`~forge.plan_staleness.check_drift`); called once.

    Returns:
        The report: each issue's lines under an ``issue:`` line, closed by
        a ``current:`` verdict line.
    """
    fetched: list[tuple[int, Report, tuple[Issue, Comment | None] | None]] = []
    union: list[str] = []
    for number in numbers:
        part = Report()
        pair = issue_and_plan(source, number, part)
        if pair is not None:
            paths = plan_paths(pair[1], pair[0].body)[0]
            union += [p for p in paths if p not in union]
        fetched.append((number, part, pair))
    reader = _restricted(log_merges(union) if union else [])
    report = Report()
    for number, part, pair in fetched:
        if pair is not None:
            issue, plan = pair
            try:
                filed = parse_timestamp(issue.created_at)
            except ValueError:
                part.unknown(f"unknown: #{number} has an unreadable creation time")
            else:
                part.absorb(
                    drift_from(source, issue, plan, since=filed, log_merges=reader)
                )
        report.note(f"issue: #{number}")
        report.absorb(part)
        report.note(_verdict_line(number, part))
    return report


# --------------------------------------------------------------------------
# merge-order
# --------------------------------------------------------------------------


@dataclass
class _Entanglement:
    """What the probes found about one PR: partner PR number to the reason."""

    pr: OpenPR
    base_clean: bool | None = None
    partners: dict[int, str] = field(default_factory=dict)

    def weight(self) -> tuple[int, int, int]:
        """Order PRs entangled with nothing first and the most entangled last.

        Returns:
            Partner count, then 1 when the base probe was not clean, then
            the PR number.
        """
        trouble = int(self.base_clean is not True)
        return len(self.partners), trouble, self.pr.number


def _probe_base(
    pr: OpenPR, base_ref: str, probe: Callable[[str, str], bool | None], report: Report
) -> bool | None:
    """Probe *pr* against the base, recording the outcome.

    Args:
        pr: The PR.
        base_ref: Resolved base ref.
        probe: Merge probe (:func:`~forge.git_utils.merge_tree_clean` bound
            to a repo root).
        report: Report to write into.

    Returns:
        The probe's verdict.
    """
    clean = probe(base_ref, pr.head_sha)
    if clean is None:
        report.unknown(
            f"unknown: git could not probe PR #{pr.number} against {base_ref}"
        )
    elif clean:
        report.note(f"base: PR #{pr.number} merges cleanly into {base_ref}")
    else:
        report.finding(
            f"conflict: PR #{pr.number} conflicts with {base_ref}; "
            f"it needs `git merge {base_ref}` first"
        )
    return clean


def _probe_pairs(
    found: Sequence[_Entanglement],
    probe: Callable[[str, str], bool | None],
    report: Report,
) -> None:
    """Probe every pair of PRs, recording each entanglement on both sides.

    A pair that merges cleanly but changes common files still entangles:
    whichever merges second was verified against a base without the other.
    Generated artifacts (:data:`~forge.pr_delta.REGEN_COMMANDS`) do not
    count towards that: their merged content is regenerated, never
    reconciled. A conflict still counts wherever it arises.

    Args:
        found: One entry per PR.
        probe: Merge probe.
        report: Report to write into.
    """
    for left, right in combinations(found, 2):
        a, b = left.pr, right.pr
        clean = probe(a.head_sha, b.head_sha)
        shared = len((set(a.files) & set(b.files)) - _GENERATED_PATHS)
        if clean is None:
            report.unknown(
                f"unknown: git could not probe PR #{a.number} with PR #{b.number}"
            )
            why = "unknown"
        elif not clean:
            report.finding(f"conflict: PR #{a.number} and PR #{b.number} conflict")
            why = "conflict"
        elif shared:
            report.note(
                f"shared: PR #{a.number} and PR #{b.number} merge cleanly but "
                f"change {shared} common file(s)"
            )
            why = "shared files"
        else:
            continue
        left.partners[b.number] = why
        right.partners[a.number] = why


def _report_order(found: Sequence[_Entanglement], report: Report) -> None:
    """Record the suggested merge order with its refresh-after notes.

    Args:
        found: One entry per PR, probes done.
        report: Report to write into.
    """
    done: list[int] = []
    for position, entry in enumerate(sorted(found, key=_Entanglement.weight), 1):
        partners = entry.partners
        after = [f"PR #{n} ({partners[n]})" for n in done if n in partners]
        line = f"order: {position} PR #{entry.pr.number}"
        if entry.pr.draft:
            line += " (draft)"
        if after:
            line += f" — refresh after {', '.join(after)}"
        report.note(line)
        done.append(entry.pr.number)


def _usable(prs: Sequence[OpenPR], report: Report, *, cap: int) -> list[OpenPR]:
    """Return the PRs to probe: well-formed heads, at most *cap* of them.

    Args:
        prs: Candidate PRs, already restricted to the base branch.
        report: Report that receives a refusal per malformed head and a
            finding when the cap cuts the set.
        cap: Most PRs to probe.

    Returns:
        The lowest-numbered *cap* PRs with a full commit SHA head.
    """
    usable = []
    for pr in sorted(prs, key=lambda p: p.number):
        if _SHA_RE.fullmatch(pr.head_sha):
            usable.append(pr)
        else:
            report.unknown(f"unknown: PR #{pr.number} has no readable head commit")
    if len(usable) > cap:
        report.finding(
            f"cap: {len(usable)} PRs exceed the merge-order cap of {cap}; probed "
            f"the {cap} lowest-numbered — name PRs to choose others"
        )
        usable = usable[:cap]
    return usable


def check_merge_order(
    prs: Sequence[OpenPR],
    base_ref: str,
    probe: Callable[[str, str], bool | None],
    *,
    cap: int = MERGE_ORDER_CAP,
) -> Report:
    """Probe *prs* against the base and each other, then suggest a merge order.

    Args:
        prs: Open PRs targeting the base branch, heads fetched.
        base_ref: Resolved base ref (e.g. ``origin/main``).
        probe: Merge probe returning True clean, False conflicts, None
            unknown.
        cap: Most PRs to probe.

    Returns:
        The report; a finding per conflict and when the cap cuts the set.
    """
    report = Report()
    usable = _usable(prs, report, cap=cap)
    if not usable:
        report.note(f"merge-order: no open PR to order against {base_ref}")
        return report
    found = [_Entanglement(pr) for pr in usable]
    for entry in found:
        entry.base_clean = _probe_base(entry.pr, base_ref, probe, report)
    _probe_pairs(found, probe, report)
    _report_order(found, report)
    return report


def git_merge_probe(root: Path) -> Callable[[str, str], bool | None]:
    """Return a merge probe over *root*'s object store.

    Args:
        root: Repository root.

    Returns:
        :func:`~forge.git_utils.merge_tree_clean` bound to *root*.
    """
    return partial(merge_tree_clean, root)


def fetch_pr_heads(root: Path, prs: Sequence[OpenPR], report: Report) -> list[OpenPR]:
    """Fetch each PR's head commit so it can be probed; drop the failures.

    Only the objects arrive: ``pull/<N>/head`` is fetched without a
    destination, so no local ref is created or moved.

    Args:
        root: Repository root.
        prs: PRs to fetch.
        report: Report that receives an unknown line per failed fetch.

    Returns:
        The PRs whose heads were fetched.
    """
    fetched = []
    for pr in prs:
        if fetch_quietly(root, "origin", f"pull/{pr.number}/head"):
            fetched.append(pr)
        else:
            report.unknown(f"unknown: could not fetch PR #{pr.number}'s head")
    return fetched
