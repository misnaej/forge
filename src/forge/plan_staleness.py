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
    posted (or *--since*), that touched a file the plan names — falling
    back to the files the issue body names, and refusing when neither
    names any. Also refuses an issue labelled both ``blocked`` and
    ``plan-ready``.
``overlap <merged-pr>``
    Lists open issues whose body or authenticated plan names a file the
    merged PR changed, skipping the issues that PR itself closes, and
    marks the ones carrying ``plan-ready``.

A plan comment counts only when it opens with ``[issue-triage]
plan-validated:`` and its author has write, maintain or admin access per
the ``collaborators/<login>/permission`` call; the newest such comment
wins. Every GitHub or JSON failure is *unknown*, never clean. Output is
plain lines a skill can relay: GitHub-derived text is limited to numbers,
dates and validated paths, or fenced.

Exit codes:
    0  clean
    1  finding — an unmet prerequisite, drift, or an overlapping issue
    2  unknown or refused — a GitHub/JSON failure, an unparseable
       ``Requires:`` line, no named files, both labels present, an
       unresolvable base ref, or a PR that is not merged
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from forge.config import load_config
from forge.gh_comments import GH_LIST_TIMEOUT, parse_paged_json
from forge.git_utils import (
    configure_cli_logging,
    emit,
    fetch_quietly,
    gh_api,
    repo_root,
    resolve_base_branch_ref,
    run_git,
    wrap_in_code_fence,
)
from forge.pr_delta import strip_fences


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)

PLAN_MARKER: Final[str] = "[issue-triage] plan-validated:"
PLAN_READY: Final[str] = "plan-ready"
BLOCKED: Final[str] = "blocked"
WRITE_PERMISSIONS: Final[frozenset[str]] = frozenset({"admin", "maintain", "write"})

EXIT_CLEAN: Final[int] = 0
EXIT_FINDING: Final[int] = 1
EXIT_UNKNOWN: Final[int] = 2

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
    r"^[A-Za-z0-9_.][A-Za-z0-9_.\-/]*\.[A-Za-z0-9]{1,10}$"
)
# Extensions that make a slash-free token a file (`pyproject.toml`), not
# prose ("e.g.") or a dotted module name.
_BARE_FILE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {"md", "py", "toml", "yml", "yaml", "json", "sh", "cfg", "txt", "ini", "lock"}
)
_LOGIN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_PR_SUBJECT_RE: Final[re.Pattern[str]] = re.compile(
    r"\(#(\d{1,7})\)\s*$|^Merge pull request #(\d{1,7})\b"
)


@dataclass(frozen=True)
class Comment:
    """One issue comment as the checks need it."""

    id: int
    body: str
    created_at: str
    author: str


@dataclass(frozen=True)
class Issue:
    """One issue; ``comments`` is filled only by the open-issue listing."""

    number: int
    body: str
    labels: tuple[str, ...]
    created_at: str
    comments: tuple[Comment, ...] = ()


@dataclass(frozen=True)
class RefStatus:
    """What GitHub reports about one ``Requires:`` reference."""

    number: int
    is_pr: bool = field(kw_only=True)
    state: str = field(kw_only=True)
    state_reason: str | None = field(kw_only=True)
    merged: bool = field(kw_only=True)


@dataclass(frozen=True)
class MergedPR:
    """A PR's merge state, changed files and the issues it closes."""

    number: int
    merged: bool = field(kw_only=True)
    merge_sha: str = field(kw_only=True)
    closes: tuple[int, ...] = field(kw_only=True)
    files: tuple[str, ...] = field(kw_only=True)


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
        """Return every open issue, comments included."""


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
            if not (_FILE_RE.match(token) and is_safe_path(token)):
                continue
            extension = token.rsplit(".", 1)[-1].lower()
            if "/" not in token and extension not in _BARE_FILE_EXTENSIONS:
                continue
            if token not in paths:
                paths.append(token)
    return paths


def authenticated_plan(
    comments: Iterable[Comment], is_writer: Callable[[str], bool]
) -> Comment | None:
    """Return the newest plan comment whose author has write access.

    A comment counts only when its body opens with :data:`PLAN_MARKER`;
    anyone can type the marker, so a candidate from an author without
    write access is skipped — never trusted, never a veto.

    Args:
        comments: The issue's comments.
        is_writer: Permission predicate for a login.

    Returns:
        The qualifying comment, or ``None`` when none qualifies.
    """
    candidates = sorted(
        (c for c in comments if c.body.lstrip().startswith(PLAN_MARKER)),
        key=lambda c: c.created_at,
        reverse=True,
    )
    for comment in candidates:
        if is_writer(comment.author):
            return comment
        logger.warning(
            "plan-check: ignoring plan comment %s — author lacks write access",
            comment.id,
        )
    return None


def plan_paths(plan: Comment | None, body: str) -> list[str]:
    """Return the files the plan names, falling back to the issue body's.

    Args:
        plan: The authenticated plan comment, if any.
        body: The issue body.

    Returns:
        Named paths; empty when neither text names any.
    """
    if plan is not None and (paths := named_paths(plan.body)):
        return paths
    return named_paths(body)


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
    """Write-access predicate over a :class:`GitHubSource`, cached per login."""

    def __init__(self, source: GitHubSource) -> None:
        """Bind the predicate to *source*.

        Args:
            source: Where permission lookups go.
        """
        self._source = source
        self._cache: dict[str, bool] = {}

    def __call__(self, login: str) -> bool:
        """Return whether *login* has write, maintain or admin access.

        A login that is not a plain GitHub username, or a lookup that
        fails, counts as no access (FOUNDATION §14: fail closed).

        Args:
            login: GitHub login.

        Returns:
            ``True`` only on a confirmed write-level permission.
        """
        if login not in self._cache:
            permission = (
                self._source.permission(login) if _LOGIN_RE.match(login) else None
            )
            self._cache[login] = permission in WRITE_PERMISSIONS
        return self._cache[login]


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
        None when the issue could not be read.
    """
    issue = source.issue(number)
    comments = source.comments(number) if issue is not None else None
    if issue is None or comments is None:
        report.unknown(f"unknown: could not read issue #{number} from GitHub")
        return None
    plan = authenticated_plan(comments, WriterCheck(source))
    if plan is None:
        report.note(f"plan: no authenticated plan-validated comment on #{number}")
    else:
        report.note(f"plan: comment {plan.id} posted {plan.created_at}")
    return issue, plan


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
    texts = [issue.body] + ([plan.body] if plan is not None else [])
    requires = parse_requires(texts)
    if not requires.found:
        report.unknown(f"unknown: #{number} has no Requires: line")
    for entry in requires.unparsed:
        report.unknown("unknown: a Requires: entry could not be parsed:")
        report.note(wrap_in_code_fence(entry))
    if requires.found and not requires.refs and not requires.unparsed:
        report.note("prerequisite: Requires: nothing")
    for ref in requires.refs:
        status = source.ref_status(ref)
        if status is None:
            report.unknown(f"unknown: could not read #{ref} from GitHub")
            continue
        satisfied, reason = prerequisite_verdict(status)
        if satisfied:
            report.note(f"prerequisite: #{ref} landed — {reason}")
        else:
            report.finding(f"blocked: #{ref} — {reason}")
    return report


def check_drift(
    source: GitHubSource,
    number: int,
    *,
    since: datetime | None,
    log_merges: Callable[[list[str]], list[Merge] | None],
) -> Report:
    """List base-branch merges that touched the plan's files since *since*.

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
    if BLOCKED in issue.labels and PLAN_READY in issue.labels:
        report.unknown(f"refused: #{number} is labelled both blocked and plan-ready")
        return report
    paths = plan_paths(plan, issue.body)
    if not paths:
        report.unknown(f"refused: neither the plan nor #{number} names any file")
        return report
    if since is None:
        if plan is None:
            report.unknown("refused: no authenticated plan to date from; pass --since")
            return report
        since = parse_timestamp(plan.created_at)
    cutoff = since.astimezone(UTC).isoformat()
    report.note(f"since: {cutoff}; files: {', '.join(paths)}")
    merges = log_merges(paths)
    if merges is None:
        report.unknown("unknown: git could not list the base branch history")
        return report
    for merge in merges:
        if merge.date > since:
            pr = f"PR #{merge.pr}" if merge.pr else "direct commit"
            report.finding(
                f"drift: {merge.sha[:12]} {merge.date.isoformat()} {pr} "
                f"changed {', '.join(merge.paths)}"
            )
    return report


def _overlap_line(issue: Issue, files: set[str], is_writer: WriterCheck) -> str | None:
    """Return the finding line when *issue* names any of *files*.

    Args:
        issue: Open issue to inspect.
        files: Paths changed by the work being checked.
        is_writer: Check deciding whether a plan author has write access.

    Returns:
        The overlap line, or None when the issue names none of *files*.
    """
    plan = authenticated_plan(issue.comments, is_writer)
    named = named_paths(issue.body) + (named_paths(plan.body) if plan else [])
    hits = sorted(files.intersection(named))
    if not hits:
        return None
    marker = " plan-ready" if PLAN_READY in issue.labels else ""
    return f"overlap: #{issue.number}{marker} names {', '.join(hits)}"


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
    report.note(
        f"merge: PR #{pr_number} {pr.merge_sha[:12]} changed {len(pr.files)} file(s)"
    )
    files = set(pr.files)
    is_writer = WriterCheck(source)
    for issue in issues:
        if issue.number in pr.closes:
            continue
        line = _overlap_line(issue, files, is_writer)
        if line is not None:
            report.finding(line)
    return report


# --------------------------------------------------------------------------
# I/O adapters
# --------------------------------------------------------------------------

_STATUS_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name)"
    "{issueOrPullRequest(number:$n){__typename"
    " ... on PullRequest{state merged}"
    " ... on Issue{state stateReason"
    " closedByPullRequestsReferences(first:20,includeClosedPrs:true){nodes{merged}}"
    " timelineItems(itemTypes:[CLOSED_EVENT],last:1){nodes{... on ClosedEvent"
    "{closer{__typename ... on PullRequest{merged}}}}}}}}}"
)
_PR_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$n){merged mergeCommit{oid}"
    " closingIssuesReferences(first:50){nodes{number}}}}}"
)
_OPEN_ISSUES_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$endCursor:String)"
    "{repository(owner:$owner,name:$name){issues(states:OPEN,first:50,"
    "after:$endCursor){pageInfo{hasNextPage endCursor} nodes{number body createdAt"
    " labels(first:50){nodes{name}} comments(last:100){totalCount"
    " nodes{databaseId body createdAt author{login}}}}}}}"
)
_OPEN_ISSUES_JQ: Final[str] = (
    "[.data.repository.issues.nodes[] | {number, body, created_at: .createdAt,"
    " labels: [.labels.nodes[].name], total: .comments.totalCount,"
    " comments: [.comments.nodes[] | {id: .databaseId, body, created_at: .createdAt,"
    ' author: (.author.login // "")}]}]'
)
_COMMENTS_JQ: Final[str] = (
    '[.[] | {id, body: (.body // ""), created_at, author: (.user.login // "")}]'
)
_REPO_FIELDS: Final[tuple[str, ...]] = ("-F", "owner={owner}", "-F", "name={repo}")


def _loads(raw: str | None) -> dict[str, Any] | None:
    """Decode one JSON document; ``None`` for a failed call or bad JSON.

    Args:
        raw: Raw ``gh`` output, or None when the call failed.

    Returns:
        The decoded document, or None.
    """
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("plan-check: unparseable gh output: %.60s", raw)
        return None


def _pages(raw: str | None) -> list[Any] | None:
    """Flatten paginated output, or ``None`` when the call or any page failed.

    Args:
        raw: Raw paginated ``gh`` output, or None when the call failed.

    Returns:
        The flattened items, or None.
    """
    if raw is None:
        return None
    try:
        return parse_paged_json(raw, strict=True)
    except (json.JSONDecodeError, TypeError):
        logger.warning("plan-check: unparseable gh page output")
        return None


def _comment(item: dict[str, Any]) -> Comment:
    """Build a :class:`Comment` from a projected JSON mapping.

    Args:
        item: Mapping with id, body, created_at and author keys.

    Returns:
        The comment.
    """
    return Comment(
        id=int(item["id"]),
        body=str(item.get("body") or ""),
        created_at=str(item["created_at"]),
        author=str(item.get("author") or ""),
    )


class _IncompleteListingError(Exception):
    """An open issue could not be read completely; the listing is unknown."""


class GhSource:
    """Production :class:`GitHubSource` over ``gh api``; ``None`` on any failure."""

    def _graphql(self, query: str, number: int, jq: str) -> dict[str, Any] | None:
        """Run one numbered GraphQL query and decode its ``--jq`` projection.

        Args:
            query: GraphQL query text taking a ``$n`` number variable.
            number: Issue or pull-request number.
            jq: ``jq`` expression projecting the response.

        Returns:
            The decoded projection, or None on failure.
        """
        return _loads(
            gh_api(
                "graphql",
                *_REPO_FIELDS,
                "-F",
                f"n={number}",
                "-f",
                f"query={query}",
                "--jq",
                jq,
            )
        )

    def issue(self, number: int) -> Issue | None:
        """Return issue *number* (body, labels, creation time).

        Args:
            number: Issue or pull-request number.

        Returns:
            The issue, or None when it could not be read.
        """
        data = _loads(
            gh_api(
                f"repos/{{owner}}/{{repo}}/issues/{number}",
                "--jq",
                '{number, body: (.body // ""), labels: [.labels[].name], created_at}',
            )
        )
        if data is None:
            return None
        try:
            return Issue(
                number=int(data["number"]),
                body=str(data["body"]),
                labels=tuple(str(n) for n in data["labels"]),
                created_at=str(data["created_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def comments(self, number: int) -> list[Comment] | None:
        """Return every comment on issue *number*, an empty list when none.

        Args:
            number: Issue or pull-request number.

        Returns:
            The comments, or None when they could not be read.
        """
        items = _pages(
            gh_api(
                f"repos/{{owner}}/{{repo}}/issues/{number}/comments",
                "--paginate",
                "--jq",
                _COMMENTS_JQ,
                timeout=GH_LIST_TIMEOUT,
            )
        )
        try:
            return None if items is None else [_comment(i) for i in items]
        except (KeyError, TypeError, ValueError):
            return None

    def permission(self, login: str) -> str | None:
        """Return *login*'s permission on the repo (``admin``, ``write``, ...).

        Args:
            login: GitHub login to look up.

        Returns:
            The permission name, or None when it could not be read.
        """
        data = _loads(
            gh_api(
                f"repos/{{owner}}/{{repo}}/collaborators/{login}/permission",
                "--jq",
                "{permission, role: .role_name}",
            )
        )
        if not isinstance(data, dict):
            return None
        # `permission` folds maintain into write; `role_name` keeps it.
        role = data.get("role")
        return str(role) if role in WRITE_PERMISSIONS else str(data.get("permission"))

    def ref_status(self, number: int) -> RefStatus | None:
        """Return the landing status of issue-or-PR *number*.

        Args:
            number: Issue or pull-request number.

        Returns:
            The status, or None when it could not be read.
        """
        data = self._graphql(
            _STATUS_QUERY, number, ".data.repository.issueOrPullRequest"
        )
        try:
            if data is not None and data["__typename"] == "PullRequest":
                return RefStatus(
                    number,
                    is_pr=True,
                    state=str(data["state"]),
                    state_reason=None,
                    merged=bool(data["merged"]),
                )
            if data is None:
                return None
            closer = (data["timelineItems"]["nodes"] or [{}])[-1].get("closer") or {}
            linked = data["closedByPullRequestsReferences"]["nodes"]
            merged = bool(closer.get("merged")) or any(n["merged"] for n in linked)
            return RefStatus(
                number,
                is_pr=False,
                state=str(data["state"]),
                state_reason=data.get("stateReason"),
                merged=merged,
            )
        except (KeyError, TypeError, IndexError, AttributeError):
            return None

    def pull_request(self, number: int) -> MergedPR | None:
        """Return PR *number*'s merge state, changed files and closed issues.

        Args:
            number: Issue or pull-request number.

        Returns:
            The merged-PR record, or None when it could not be read.
        """
        data = self._graphql(_PR_QUERY, number, ".data.repository.pullRequest")
        files = _pages(
            gh_api(
                f"repos/{{owner}}/{{repo}}/pulls/{number}/files",
                "--paginate",
                "--jq",
                "[.[] | .filename, (.previous_filename // empty)]",
                timeout=GH_LIST_TIMEOUT,
            )
        )
        if files is None or data is None:
            return None
        try:
            return MergedPR(
                number=number,
                merged=bool(data["merged"]),
                merge_sha=str((data.get("mergeCommit") or {}).get("oid") or ""),
                closes=tuple(
                    int(n["number"]) for n in data["closingIssuesReferences"]["nodes"]
                ),
                files=tuple(str(f) for f in files),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    def open_issues(self) -> list[Issue] | None:
        """Return every open issue, comments included."""
        items = _pages(
            gh_api(
                "graphql",
                "--paginate",
                *_REPO_FIELDS,
                "-f",
                f"query={_OPEN_ISSUES_QUERY}",
                "--jq",
                _OPEN_ISSUES_JQ,
                timeout=GH_LIST_TIMEOUT * 2,
            )
        )
        if items is None:
            return None
        try:
            return [self._open_issue(item) for item in items]
        except _IncompleteListingError:
            return None

    def _open_issue(self, item: dict[str, Any]) -> Issue:
        """Build one listed issue, fetching all comments when the page truncated.

        Args:
            item: Projected listing entry for one open issue.

        Returns:
            The issue with its full comment list.
        """
        try:
            comments = [_comment(c) for c in item["comments"]]
            number = int(item["number"])
            if int(item["total"]) > len(comments):
                fetched = self.comments(number)
                if fetched is None:
                    raise _IncompleteListingError
                comments = fetched
            return Issue(
                number=number,
                body=str(item.get("body") or ""),
                labels=tuple(str(n) for n in item["labels"]),
                created_at=str(item["created_at"]),
                comments=tuple(comments),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _IncompleteListingError from exc


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
    fetched = not base_ref.startswith("origin/") or fetch_quietly(
        root, "origin", base_branch
    )
    if not fetched:
        report = Report()
        report.unknown(f"unknown: could not fetch {base_ref}; history may be stale")
        return report
    return check_drift(
        source, args.issue, since=args.since, log_merges=git_merge_log(root, base_ref)
    )


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
