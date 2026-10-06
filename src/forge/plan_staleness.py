"""forge-plan-check — mechanical premise and prerequisite checks for validated plans.

A plan validated for unattended execution describes the code as it stood
when it was approved. These checks ask, without judgment, whether that is
still true (FOUNDATION §14 "Plan-readiness pipeline"):

``prerequisites <issue>``
    Every ``Requires:`` entry — in the issue body and in its authenticated
    plan — must have *landed*: a merged PR, or a closed issue a merged PR
    closed. An issue closed as not planned, or closed by hand with no
    merged PR behind it, still blocks; ``Requires: nothing`` is satisfied.
``drift <issue> [--since ISO]``
    Lists merges on the base branch, since the authenticated plan was
    posted (or *--since*), that touched a file the plan names. Merges
    that deliver the issue's own ``Requires:`` entries are expected and
    reported as notes, not drift. Refuses an issue labelled both
    ``blocked`` and ``plan-ready``.
``overlap <merged-pr>``
    Lists open issues whose authenticated plan names a file the merged PR
    changed, skipping the issues that PR itself closes, and marks the
    ones carrying ``plan-ready``.

Both ``drift`` and ``overlap`` take an issue's files from one rule
(:func:`plan_paths`): the authenticated plan's, or the issue body's only
when the plan names none — said in a ``note:`` line when it happens.

A plan comment counts only when it opens with ``[issue-triage]
plan-validated:`` and its author has write, maintain or admin access per
the ``collaborators/<login>/permission`` call; the newest such comment
wins. A candidate whose author's access cannot be looked up makes the
answer unknown — it might be the real plan. Every GitHub or JSON failure
is *unknown*, never clean. Output is plain lines a skill can relay:
GitHub-derived text is limited to numbers, dates and validated paths, or
fenced and capped. GitHub reads live in :mod:`forge.plan_staleness_gh`.

Exit codes:
    0  clean
    1  finding — an unmet prerequisite, drift, or an overlapping issue
    2  unknown or refused — a GitHub/JSON failure, an unparseable or
       missing ``Requires:`` line, a plan author whose access cannot be
       checked, no named files, both labels present, an unresolvable or
       unfetchable base ref, or a PR that is not merged
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from forge.config import load_config
from forge.git_utils import (
    configure_cli_logging,
    emit,
    fetch_quietly,
    repo_root,
    resolve_base_branch_ref,
    run_git,
    wrap_in_code_fence,
)
from forge.plan_staleness_gh import WRITE_PERMISSIONS, GhSource
from forge.pr_delta import strip_fences


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from forge.plan_staleness_gh import Comment, Issue, MergedPR, RefStatus


configure_cli_logging()
logger = logging.getLogger(__name__)

PLAN_MARKER: Final[str] = "[issue-triage] plan-validated:"
PLAN_READY: Final[str] = "plan-ready"
BLOCKED: Final[str] = "blocked"

EXIT_CLEAN: Final[int] = 0
EXIT_FINDING: Final[int] = 1
EXIT_UNKNOWN: Final[int] = 2

# Longest stretch of issue text echoed back (inside a fence).
ECHO_CAP: Final[int] = 200

# A `Requires:` line at the start of a line (after list/quote/bold markers),
# or opening a bold span mid-line — the form recorded plans use. A
# mention inside backticks ("the `Requires:` line") matches neither.
_REQUIRES_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^[\s>*\-]*|\*\*)Requires:(?P<rest>.*)$"
)
_REF_RE: Final[re.Pattern[str]] = re.compile(r"(?:\bPR\s*)?#(\d{1,7})\b")
_PAREN_RE: Final[re.Pattern[str]] = re.compile(r"\([^()]*\)")
# What may sit between references without making the entry unparseable.
_FILLER_RE: Final[re.Pattern[str]] = re.compile(
    r"[,;&.+]|\b(?:and|plus|merged)\b", re.IGNORECASE
)
_NOTHING_RE: Final[re.Pattern[str]] = re.compile(r"^(?:nothing|none)$", re.IGNORECASE)

# Path extraction: split on whitespace and markdown punctuation, then keep
# tokens shaped like a repo-relative file. `<` and `>` stay inside tokens so
# a placeholder path (`changelog.d/<slug>.md`) is discarded whole rather
# than leaving a fragment that looks like a file.
_TOKEN_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[\s`()\[\]\"'|,;]+")
_LINE_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r":\d+(?::\d+)?$")
_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_.][A-Za-z0-9_.\-/]*\.[A-Za-z0-9]{1,10}"
)
# Extensions that make a slash-free token a file (`pyproject.toml`), not
# prose ("e.g.") or a dotted module name.
_BARE_FILE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {"md", "py", "toml", "yml", "yaml", "json", "sh", "cfg", "txt", "ini", "lock"}
)
_LOGIN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_PR_SUBJECT_RE: Final[re.Pattern[str]] = re.compile(
    r"\(#(\d{1,7})\)\s*$|^Merge pull request #(\d{1,7})\b"
)


@dataclass(frozen=True)
class Merge:
    """One first-parent commit on the base branch touching named paths."""

    sha: str
    date: datetime
    pr: int | None
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Requires:
    """The ``Requires:`` entries parsed out of one or more texts."""

    found: bool
    refs: tuple[int, ...]
    unparsed: tuple[str, ...]


@dataclass(frozen=True)
class PlanLookup:
    """The outcome of looking for an issue's authenticated plan.

    ``unresolved`` is the id of a plan-marker comment, newer than any
    confirmed plan, whose author's access could not be looked up — while
    it is set the plan is unknown. ``ignored`` lists comments skipped
    because their authors confirmedly lack write access.
    """

    plan: Comment | None
    unresolved: int | None = None
    ignored: tuple[int, ...] = ()


class GitHubSource(Protocol):
    """The GitHub reads the checks perform; every method returns None on failure."""

    def issue(self, number: int) -> Issue | None:
        """Return issue *number* (body, labels, creation time).

        Args:
            number: Issue or pull-request number.

        Returns:
            The issue, or None when it could not be read.
        """

    def comments(self, number: int) -> list[Comment] | None:
        """Return every comment on issue *number*, an empty list when none.

        Args:
            number: Issue or pull-request number.

        Returns:
            The comments, or None when they could not be read.
        """

    def permission(self, login: str) -> str | None:
        """Return *login*'s permission on the repo (``admin``, ``write``, ...).

        Args:
            login: GitHub login to look up.

        Returns:
            The permission name, or None when it could not be read.
        """

    def ref_status(self, number: int) -> RefStatus | None:
        """Return the landing status of issue-or-PR *number*.

        Args:
            number: Issue or pull-request number.

        Returns:
            The status, or None when it could not be read.
        """

    def pull_request(self, number: int) -> MergedPR | None:
        """Return PR *number*'s merge state, changed files and closed issues.

        Args:
            number: Issue or pull-request number.

        Returns:
            The merged-PR record, or None when it could not be read.
        """

    def open_issues(self) -> list[Issue] | None:
        """Return every open issue, comments included.

        Returns:
            Every open issue with its full comment list, or None when the
            listing could not be read completely.
        """


@dataclass
class Report:
    """Lines to emit plus the counts that decide the exit code."""

    lines: list[str] = field(default_factory=list)
    findings: int = 0
    unknowns: int = 0

    def note(self, line: str) -> None:
        """Record an informational *line*.

        Args:
            line: Text to emit.
        """
        self.lines.append(line)

    def finding(self, line: str) -> None:
        """Record a finding *line*.

        Args:
            line: Text to emit.
        """
        self.findings += 1
        self.lines.append(line)

    def unknown(self, line: str) -> None:
        """Record an unknown-or-refused *line*.

        Args:
            line: Text to emit.
        """
        self.unknowns += 1
        self.lines.append(line)

    @property
    def exit_code(self) -> int:
        """Return 2 on any unknown, else 1 on any finding, else 0."""
        if self.unknowns:
            return EXIT_UNKNOWN
        return EXIT_FINDING if self.findings else EXIT_CLEAN

    def render(self) -> str:
        """Return the report lines followed by one verdict line.

        Returns:
            The text to print.
        """
        verdict = {EXIT_CLEAN: "clean", EXIT_FINDING: "finding"}.get(
            self.exit_code, "unknown"
        )
        return "\n".join([*self.lines, f"verdict: {verdict}"])


# --------------------------------------------------------------------------
# Pure parsing and decisions
# --------------------------------------------------------------------------


def _requires_rest(line: str) -> str | None:
    """Return the text after a ``Requires:`` marker on *line*, or None.

    Args:
        line: One line of issue text.

    Returns:
        The text following the marker, or None when there is no marker.
    """
    match = _REQUIRES_RE.search(line)
    if match is None:
        return None
    return match.group("rest").lstrip("*").split("**")[0].replace("`", "")


def parse_requires(texts: Iterable[str]) -> Requires:
    """Parse every ``Requires:`` line in *texts*.

    An entry is understood when, once parentheticals and connecting words
    are removed, only ``#N`` / ``PR #N`` references — or a lone
    ``nothing`` — remain. Anything else is kept verbatim as unparsed, so
    the caller fails closed on a prerequisite it cannot check.

    Args:
        texts: Markdown bodies to scan (issue body, plan payload).

    Returns:
        Whether any line was found, the referenced numbers in order, and
        the entries that could not be parsed.
    """
    found = False
    refs: list[int] = []
    unparsed: list[str] = []
    for text in texts:
        for line in strip_fences(text.splitlines()):
            rest = _requires_rest(line)
            if rest is None:
                continue
            found = True
            stripped = _PAREN_RE.sub(" ", rest)
            numbers = [int(n) for n in _REF_RE.findall(stripped)]
            residue = _FILLER_RE.sub(" ", _REF_RE.sub(" ", stripped)).strip()
            if numbers and not residue:
                refs.extend(n for n in numbers if n not in refs)
            elif not numbers and _NOTHING_RE.match(residue):
                continue
            else:
                unparsed.append(rest.strip())
    return Requires(found=found, refs=tuple(refs), unparsed=tuple(unparsed))


def prerequisite_verdict(status: RefStatus) -> tuple[bool, str]:
    """Decide whether one prerequisite has landed.

    The question is whether the work merged, not whether the issue
    closed: an issue closed as not planned, or closed with no merged PR
    behind it, still blocks.

    Args:
        status: GitHub's report on the referenced issue or PR.

    Returns:
        ``(satisfied, reason)``.
    """
    if status.is_pr:
        if status.merged:
            return True, "merged PR"
        return False, f"PR not merged ({status.state.lower()})"
    if status.state == "OPEN":
        return False, "issue still open"
    if status.state_reason == "NOT_PLANNED":
        return False, "issue closed as not planned"
    if status.merged:
        return True, "issue closed by a merged PR"
    return False, "issue closed without a merged PR"


def delivering_prs(statuses: Mapping[int, RefStatus]) -> dict[int, int]:
    """Map each merged PR that delivered a prerequisite to that prerequisite.

    A prerequisite closed as not planned delivered nothing, whatever PR is
    linked to it.

    Args:
        statuses: Prerequisite number to its status.

    Returns:
        Merged PR number to the prerequisite number it delivered.
    """
    delivered: dict[int, int] = {}
    for ref, status in statuses.items():
        if not prerequisite_verdict(status)[0]:
            continue
        for pr in status.landed_by:
            delivered.setdefault(pr, ref)
    return delivered


def capped_echo(text: str) -> str:
    """Return *text* fenced and capped at :data:`ECHO_CAP` characters.

    Args:
        text: Issue-derived text to show.

    Returns:
        A fenced block, ending in ``…[truncated]`` when cut.
    """
    if len(text) > ECHO_CAP:
        text = text[:ECHO_CAP] + " …[truncated]"
    return wrap_in_code_fence(text)


def is_safe_path(path: str) -> bool:
    """Return whether *path* may be handed to git as a repo-relative pathspec.

    Paths come from issue text anyone can write, so they are data: an
    absolute path, a ``..`` component, a leading ``-`` (an option to git),
    a URL or a home-relative path is refused outright.

    Args:
        path: Candidate path.

    Returns:
        ``True`` only for a plain repo-relative path.
    """
    if not path or path[0] in "/-~" or "\\" in path or ":" in path:
        return False
    if "\x00" in path:
        return False
    return ".." not in path.split("/")


def named_paths(text: str) -> list[str]:
    """Return the repo-relative file paths *text* names, outside code fences.

    Args:
        text: Markdown body.

    Returns:
        Paths in first-appearance order, without duplicates.
    """
    paths: list[str] = []
    for line in strip_fences(text.splitlines()):
        for raw in _TOKEN_SPLIT_RE.split(line):
            token = _LINE_SUFFIX_RE.sub("", raw.strip("*").rstrip(".:!?"))
            if not (_FILE_RE.fullmatch(token) and is_safe_path(token)):
                continue
            extension = token.rsplit(".", 1)[-1].lower()
            if "/" not in token and extension not in _BARE_FILE_EXTENSIONS:
                continue
            if token not in paths:
                paths.append(token)
    return paths


def authenticated_plan(
    comments: Iterable[Comment], is_writer: Callable[[str], bool | None]
) -> PlanLookup:
    """Find the newest plan comment whose author has write access.

    A comment counts only when its body opens with :data:`PLAN_MARKER`;
    anyone can type the marker, so a candidate from a confirmed non-writer
    is skipped — never trusted, never a veto. A candidate whose author's
    access cannot be looked up stops the search: it may be the real plan,
    so the answer is unknown rather than an older or absent plan.

    Args:
        comments: The issue's comments.
        is_writer: Tri-state permission predicate for a login (``None``
            when the lookup failed).

    Returns:
        The lookup outcome.
    """
    candidates = sorted(
        (c for c in comments if c.body.lstrip().startswith(PLAN_MARKER)),
        key=lambda c: c.created_at,
        reverse=True,
    )
    ignored: list[int] = []
    for comment in candidates:
        verdict = is_writer(comment.author)
        if verdict is None:
            return PlanLookup(None, unresolved=comment.id, ignored=tuple(ignored))
        if verdict:
            return PlanLookup(comment, ignored=tuple(ignored))
        ignored.append(comment.id)
    return PlanLookup(None, ignored=tuple(ignored))


def plan_paths(plan: Comment | None, body: str) -> tuple[list[str], bool]:
    """Return the files the plan names, falling back to the issue body's.

    The one path rule ``drift`` and ``overlap`` share.

    Args:
        plan: The authenticated plan comment, if any.
        body: The issue body.

    Returns:
        ``(paths, from_body)``; *paths* is empty when neither text names
        any file, and *from_body* says the body supplied them.
    """
    if plan is not None and (paths := named_paths(plan.body)):
        return paths, False
    body_paths = named_paths(body)
    return body_paths, bool(body_paths)


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, reading a naive one as UTC.

    Args:
        value: Timestamp text (``Z`` suffix accepted).

    Returns:
        A timezone-aware datetime.

    Raises:
        ValueError: When *value* is not ISO-8601.
    """
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_merge_log(raw: str) -> list[Merge]:
    """Parse ``git log --name-only --format=%x1e%H%x1f%cI%x1f%s`` output.

    Args:
        raw: The log output.

    Returns:
        One :class:`Merge` per commit, newest first.
    """
    merges: list[Merge] = []
    for record in raw.split("\x1e"):
        lines = [line for line in record.splitlines() if line.strip()]
        if not lines:
            continue
        sha, date, subject = ([*lines[0].split("\x1f"), "", ""])[:3]
        match = _PR_SUBJECT_RE.search(subject)
        pr = int(match.group(1) or match.group(2)) if match else None
        when = parse_timestamp(date).astimezone(UTC)
        merges.append(Merge(sha, when, pr, tuple(lines[1:])))
    return merges


# --------------------------------------------------------------------------
# The three checks
# --------------------------------------------------------------------------


class WriterCheck:
    """Tri-state write-access predicate over a :class:`GitHubSource`, cached."""

    def __init__(self, source: GitHubSource) -> None:
        """Bind the predicate to *source*.

        Args:
            source: Where permission lookups go.
        """
        self._source = source
        self._cache: dict[str, bool | None] = {}

    def __call__(self, login: str) -> bool | None:
        """Return whether *login* has write, maintain or admin access.

        A login that is not a plain GitHub username is confirmedly not a
        writer (no lookup is made). A lookup that fails is unknown, never
        "no access": a failure must not quietly demote the real plan.

        Args:
            login: GitHub login.

        Returns:
            ``True`` for write-level access, ``False`` for any lesser
            permission or a malformed login, ``None`` when the lookup failed.
        """
        if login not in self._cache:
            if _LOGIN_RE.fullmatch(login) is None:
                self._cache[login] = False
            else:
                permission = self._source.permission(login)
                self._cache[login] = (
                    None if permission is None else permission in WRITE_PERMISSIONS
                )
        return self._cache[login]


def _record_lookup(lookup: PlanLookup, number: int, report: Report) -> bool:
    """Write a plan lookup's notes into *report*; ``False`` when it is unknown.

    Args:
        lookup: The lookup outcome.
        number: Issue number.
        report: Report to write into.

    Returns:
        Whether the lookup is usable.
    """
    for comment_id in lookup.ignored:
        report.note(
            f"note: ignored plan comment {comment_id} on #{number}; "
            "its author lacks write access"
        )
    if lookup.unresolved is not None:
        report.unknown(
            f"unknown: could not check the access of plan comment "
            f"{lookup.unresolved}'s author on #{number}"
        )
        return False
    return True


def _issue_and_plan(
    source: GitHubSource, number: int, report: Report
) -> tuple[Issue, Comment | None] | None:
    """Fetch issue *number* and its authenticated plan, recording failures.

    Args:
        source: GitHub reader to query.
        number: Issue or pull-request number.
        report: Report that receives an unknown line on failure.

    Returns:
        The issue and its authenticated plan comment (None when absent), or
        None when the issue or its plan could not be determined.
    """
    issue = source.issue(number)
    comments = source.comments(number) if issue is not None else None
    if issue is None or comments is None:
        report.unknown(f"unknown: could not read issue #{number} from GitHub")
        return None
    lookup = authenticated_plan(comments, WriterCheck(source))
    if not _record_lookup(lookup, number, report):
        return None
    if lookup.plan is None:
        report.note(f"plan: no authenticated plan-validated comment on #{number}")
    else:
        report.note(f"plan: comment {lookup.plan.id} posted {lookup.plan.created_at}")
    return issue, lookup.plan


def _requires_texts(issue: Issue, plan: Comment | None) -> list[str]:
    """Return the texts whose ``Requires:`` lines count: body, then plan.

    Args:
        issue: The issue.
        plan: Its authenticated plan, if any.

    Returns:
        The texts to parse.
    """
    return [issue.body] + ([plan.body] if plan is not None else [])


def _statuses(
    source: GitHubSource, refs: Iterable[int], report: Report
) -> dict[int, RefStatus]:
    """Look up each prerequisite, recording an unknown line per failure.

    Args:
        source: GitHub reads.
        refs: Prerequisite numbers.
        report: Report that receives the failures.

    Returns:
        The statuses that could be read.
    """
    statuses: dict[int, RefStatus] = {}
    for ref in refs:
        status = source.ref_status(ref)
        if status is None:
            report.unknown(f"unknown: could not read #{ref} from GitHub")
        else:
            statuses[ref] = status
    return statuses


def check_prerequisites(source: GitHubSource, number: int) -> Report:
    """Check that every prerequisite of issue *number* has landed.

    Args:
        source: GitHub reads.
        number: Issue number.

    Returns:
        The report; a finding per unmet prerequisite.
    """
    report = Report()
    fetched = _issue_and_plan(source, number, report)
    if fetched is None:
        return report
    issue, plan = fetched
    if BLOCKED in issue.labels and PLAN_READY in issue.labels:
        report.note(
            f"note: #{number} carries both blocked and plan-ready; drift refuses it"
        )
    requires = parse_requires(_requires_texts(issue, plan))
    if not requires.found:
        report.unknown(
            f"unknown: #{number} has no Requires: line; add one "
            "(Requires: nothing, or the PR or issue it waits on)"
        )
    for entry in requires.unparsed:
        report.unknown("unknown: a Requires: entry could not be parsed:")
        report.note(capped_echo(entry))
    if requires.found and not requires.refs and not requires.unparsed:
        report.note("prerequisite: Requires: nothing")
    for ref, status in _statuses(source, requires.refs, report).items():
        satisfied, reason = prerequisite_verdict(status)
        if satisfied:
            report.note(f"prerequisite: #{ref} landed — {reason}")
        else:
            report.finding(f"blocked: #{ref} — {reason}")
    return report


def _drift_window(
    issue: Issue, plan: Comment | None, since: datetime | None, report: Report
) -> tuple[list[str], datetime] | None:
    """Return the paths and cut-off drift checks, or None after a refusal.

    Args:
        issue: The issue.
        plan: Its authenticated plan, if any.
        since: Caller's cut-off, or None for the plan's post time.
        report: Report that receives refusals and notes.

    Returns:
        ``(paths, cut-off)``, or None when the check is refused.
    """
    number = issue.number
    if BLOCKED in issue.labels and PLAN_READY in issue.labels:
        report.unknown(f"refused: #{number} is labelled both blocked and plan-ready")
        return None
    paths, from_body = plan_paths(plan, issue.body)
    if not paths:
        report.unknown(f"refused: neither the plan nor #{number} names any file")
        return None
    if from_body:
        report.note(
            f"note: #{number} paths come from the issue body, not an authenticated plan"
        )
    if since is None:
        if plan is None:
            report.unknown("refused: no authenticated plan to date from; pass --since")
            return None
        since = parse_timestamp(plan.created_at)
    return paths, since


def _report_merges(
    merges: Iterable[Merge],
    since: datetime,
    delivered: Mapping[int, int],
    report: Report,
) -> None:
    """Record each merge after *since*: a note when it delivers a prerequisite.

    Args:
        merges: Base-branch commits touching the plan's files.
        since: Cut-off.
        delivered: Merged PR number to the prerequisite it delivered.
        report: Report to write into.
    """
    for merge in merges:
        if merge.date <= since:
            continue
        head = f"{merge.sha[:12]} {merge.date.isoformat()}"
        if merge.pr is not None and merge.pr in delivered:
            report.note(
                f"note: {head} PR #{merge.pr} delivers prerequisite "
                f"#{delivered[merge.pr]}; not drift"
            )
            continue
        pr = f"PR #{merge.pr}" if merge.pr else "no PR number in subject"
        report.finding(f"drift: {head} {pr} changed {', '.join(merge.paths)}")


def check_drift(
    source: GitHubSource,
    number: int,
    *,
    since: datetime | None,
    log_merges: Callable[[list[str]], list[Merge] | None],
) -> Report:
    """List base-branch merges that touched the plan's files since *since*.

    Merges delivering the issue's own prerequisites are expected — the
    plan was written to build on them — so they are noted, not counted.

    Args:
        source: GitHub reads.
        number: Issue number.
        since: Cut-off; ``None`` uses the authenticated plan's post time.
        log_merges: Returns the base branch's first-parent commits touching
            the given paths, or ``None`` when git failed.

    Returns:
        The report; a finding per merge.
    """
    report = Report()
    fetched = _issue_and_plan(source, number, report)
    if fetched is None:
        return report
    issue, plan = fetched
    window = _drift_window(issue, plan, since, report)
    if window is None:
        return report
    paths, cutoff = window
    refs = parse_requires(_requires_texts(issue, plan)).refs
    delivered = delivering_prs(_statuses(source, refs, report))
    if report.unknowns:
        return report
    start = cutoff.astimezone(UTC).isoformat()
    report.note(f"since: {start}; files: {', '.join(paths)}")
    merges = log_merges(paths)
    if merges is None:
        report.unknown("unknown: git could not list the base branch history")
        return report
    _report_merges(merges, cutoff, delivered, report)
    return report


def _check_overlap_issue(
    issue: Issue, files: set[str], is_writer: WriterCheck, report: Report
) -> None:
    """Record whether open *issue*'s files intersect *files*.

    Args:
        issue: Open issue to inspect.
        files: Paths the merged PR changed.
        is_writer: Plan-author access check.
        report: Report to write into.
    """
    lookup = authenticated_plan(issue.comments, is_writer)
    if not _record_lookup(lookup, issue.number, report):
        return
    paths, from_body = plan_paths(lookup.plan, issue.body)
    hits = sorted(files.intersection(paths))
    if not hits:
        return
    marker = " plan-ready" if PLAN_READY in issue.labels else ""
    report.finding(f"overlap: #{issue.number}{marker} names {', '.join(hits)}")
    if from_body:
        report.note(
            f"note: #{issue.number} paths come from the issue body, "
            "not an authenticated plan"
        )


def check_overlap(source: GitHubSource, pr_number: int) -> Report:
    """List open issues naming a file merged PR *pr_number* changed.

    Args:
        source: GitHub reads.
        pr_number: The merged PR.

    Returns:
        The report; a finding per overlapping issue.
    """
    report = Report()
    pr = source.pull_request(pr_number)
    if pr is None:
        report.unknown(f"unknown: could not read PR #{pr_number} from GitHub")
        return report
    if not pr.merged:
        report.unknown(f"refused: PR #{pr_number} is not merged")
        return report
    issues = source.open_issues()
    if issues is None:
        report.unknown("unknown: could not list open issues from GitHub")
        return report
    sha = f" {pr.merge_sha[:12]}" if pr.merge_sha else ""
    report.note(f"merge: PR #{pr_number}{sha} changed {len(pr.files)} file(s)")
    files = set(pr.files)
    is_writer = WriterCheck(source)
    for issue in issues:
        if issue.number not in pr.closes:
            _check_overlap_issue(issue, files, is_writer, report)
    return report


# --------------------------------------------------------------------------
# Git adapter
# --------------------------------------------------------------------------


def git_merge_log(
    root: Path, base_ref: str
) -> Callable[[list[str]], list[Merge] | None]:
    """Return a ``log_merges`` callable over *base_ref*'s first-parent history.

    Args:
        root: Repository root.
        base_ref: Resolved base ref (e.g. ``origin/main``).

    Returns:
        A callable listing the commits that touched the given paths, or
        returning ``None`` when git fails.
    """

    def log_merges(paths: list[str]) -> list[Merge] | None:
        """List first-parent commits that touched any of *paths*.

        Args:
            paths: Repository-relative paths; unsafe ones are dropped.

        Returns:
            The parsed merges, or None when git fails.
        """
        safe = [p for p in paths if is_safe_path(p)]
        try:
            raw = run_git(
                "log",
                "--first-parent",
                "--name-only",
                "--format=%x1e%H%x1f%cI%x1f%s",
                "--end-of-options",
                base_ref,
                "--",
                *safe,
                cwd=root,
            )
            return parse_merge_log(raw)
        except (subprocess.CalledProcessError, ValueError):
            return None

    return log_merges


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _since_arg(value: str) -> datetime:
    """Argparse type for ``--since``: an ISO-8601 timestamp.

    Args:
        value: Command-line text to parse.

    Returns:
        The parsed timestamp.
    """
    try:
        return parse_timestamp(value)
    except ValueError as exc:
        msg = f"not an ISO-8601 timestamp: {value!r}"
        raise argparse.ArgumentTypeError(msg) from exc


def _build_parser() -> argparse.ArgumentParser:
    """Return the ``forge-plan-check`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="forge-plan-check",
        description="Mechanical premise and prerequisite checks for a plan "
        "(exit 0 clean, 1 finding, 2 unknown or refused).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prereq = sub.add_parser("prerequisites", help="Have all Requires: entries landed?")
    prereq.add_argument("issue", type=int)
    drift = sub.add_parser("drift", help="Merges touching the plan's files since.")
    drift.add_argument("issue", type=int)
    drift.add_argument(
        "--since",
        type=_since_arg,
        help="ISO-8601 cut-off (default: when the authenticated plan was posted).",
    )
    drift.add_argument(
        "--base", help="Base branch (default: [tool.forge].base_branch)."
    )
    overlap = sub.add_parser("overlap", help="Open issues naming a merged PR's files.")
    overlap.add_argument("pr", type=int)
    return parser


def _run_drift(args: argparse.Namespace, source: GitHubSource) -> Report:
    """Resolve and refresh the base ref, then run the drift check.

    Args:
        args: Parsed command-line arguments.
        source: GitHub reader to query.

    Returns:
        The drift report.
    """
    root = repo_root()
    base_branch = args.base or load_config(root).base_branch
    base_ref = resolve_base_branch_ref(root, base_branch)
    if base_ref is None:
        report = Report()
        report.unknown(f"refused: base branch {base_branch!r} does not resolve")
        return report
    remote = base_ref.startswith("origin/")
    if remote and not fetch_quietly(root, "origin", base_branch):
        report = Report()
        report.unknown(f"unknown: could not fetch {base_ref}; history may be stale")
        return report
    report = check_drift(
        source, args.issue, since=args.since, log_merges=git_merge_log(root, base_ref)
    )
    if not remote:
        report.lines.insert(
            0, f"note: history read from local {base_ref}; it was not fetched"
        )
    return report


def main(argv: list[str] | None = None, source: GitHubSource | None = None) -> int:
    """Run one ``forge-plan-check`` subcommand and print its report.

    Args:
        argv: Argument vector (defaults to ``sys.argv``).
        source: GitHub reads; defaults to :class:`GhSource`.

    Returns:
        0 clean, 1 finding, 2 unknown or refused.
    """
    args = _build_parser().parse_args(argv)
    gh: GitHubSource = source if source is not None else GhSource()
    if args.command == "prerequisites":
        report = check_prerequisites(gh, args.issue)
    elif args.command == "drift":
        report = _run_drift(args, gh)
    else:
        report = check_overlap(gh, args.pr)
    emit(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
