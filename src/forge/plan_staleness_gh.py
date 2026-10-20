"""GitHub reads for ``forge-plan-check``: the records and the ``gh api`` adapter.

:mod:`forge.plan_staleness` owns the checks and the ``GitHubSource``
Protocol they run against; this module owns what GitHub reports — the
record types — and :class:`GhSource`, the production adapter that fills
them. The dependency runs one way (the checks import this module, never
the reverse), so :mod:`forge.plan_check_cli` can default to
:class:`GhSource` without an import cycle.

Every read fails closed: a failed ``gh`` call, bad JSON, a missing field
or a listing that could not be read completely returns ``None``, which
the checks report as *unknown*.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, TypeVar

from forge.gh_comments import GH_LIST_TIMEOUT, parse_paged_json
from forge.git_utils import gh_api


if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)

WRITE_PERMISSIONS: Final[frozenset[str]] = frozenset({"admin", "maintain", "write"})

_Record = TypeVar("_Record")


@dataclass(frozen=True)
class Comment:
    """One issue comment as the checks need it."""

    id: int
    body: str
    created_at: str
    author: str


@dataclass(frozen=True)
class Issue:
    """One issue; comments, title and update time come only from the open listing.

    ``author`` is the login that opened the issue — what the §14
    eligibility rule asks about — and is empty when GitHub reports none
    (a deleted account).
    """

    number: int
    body: str
    labels: tuple[str, ...]
    created_at: str
    comments: tuple[Comment, ...] = ()
    title: str = ""
    updated_at: str = ""
    author: str = ""


@dataclass(frozen=True)
class RefStatus:
    """What GitHub reports about one ``Requires:`` reference.

    ``landed_by`` names the merged PRs that delivered the work: the PR
    itself for a merged PR reference, or the merged PRs that closed (or
    are linked to close) an issue reference.
    """

    number: int
    is_pr: bool = field(kw_only=True)
    state: str = field(kw_only=True)
    state_reason: str | None = field(kw_only=True)
    merged: bool = field(kw_only=True)
    landed_by: tuple[int, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class MergedPR:
    """A PR's merge state, changed files and the issues it closes."""

    number: int
    merged: bool = field(kw_only=True)
    merge_sha: str = field(kw_only=True)
    closes: tuple[int, ...] = field(kw_only=True)
    files: tuple[str, ...] = field(kw_only=True)


@dataclass(frozen=True)
class OpenPR:
    """An open PR as the set checks need it: where it points and what it touches."""

    number: int
    head_sha: str = field(kw_only=True)
    base_ref: str = field(kw_only=True)
    draft: bool = field(kw_only=True)
    closes: tuple[int, ...] = field(kw_only=True)
    files: tuple[str, ...] = field(kw_only=True)


_STATUS_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name)"
    "{issueOrPullRequest(number:$n){__typename"
    " ... on PullRequest{state merged}"
    " ... on Issue{state stateReason"
    " closedByPullRequestsReferences(first:20,includeClosedPrs:true)"
    "{nodes{number merged}}"
    " timelineItems(itemTypes:[CLOSED_EVENT],last:1){nodes{... on ClosedEvent"
    "{closer{__typename ... on PullRequest{number merged}}}}}}}}}"
)
_PR_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$n){merged mergeCommit{oid}"
    " closingIssuesReferences(first:50){totalCount nodes{number}}}}}"
)
_OPEN_ISSUES_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$endCursor:String)"
    "{repository(owner:$owner,name:$name){issues(states:OPEN,first:50,"
    "after:$endCursor){pageInfo{hasNextPage endCursor} nodes{number title body"
    " createdAt"
    " updatedAt"
    " author{login}"
    " labels(first:50){nodes{name}} comments(last:100){totalCount"
    " nodes{databaseId body createdAt author{login}}}}}}}"
)
_OPEN_ISSUES_JQ: Final[str] = (
    "[.data.repository.issues.nodes[] | {number, title, body, created_at: .createdAt,"
    ' updated_at: .updatedAt, author: (.author.login // ""),'
    " labels: [.labels.nodes[].name], total: .comments.totalCount,"
    " comments: [.comments.nodes[] | {id: .databaseId, body, created_at: .createdAt,"
    ' author: (.author.login // "")}]}]'
)
_OPEN_ISSUE_COUNT_QUERY: Final[str] = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name)"
    "{issues(states:OPEN){totalCount}}}"
)
_OPEN_PRS_QUERY: Final[str] = (
    "query($owner:String!,$name:String!,$endCursor:String)"
    "{repository(owner:$owner,name:$name){pullRequests(states:OPEN,first:50,"
    "after:$endCursor){pageInfo{hasNextPage endCursor} nodes{number headRefOid"
    " baseRefName isDraft closingIssuesReferences(first:50){totalCount nodes{number}}"
    " files(first:100){totalCount nodes{path}}}}}}"
)
_OPEN_PRS_JQ: Final[str] = (
    "[.data.repository.pullRequests.nodes[] | {number, head_sha: .headRefOid,"
    " base_ref: .baseRefName, draft: .isDraft,"
    " closes: [.closingIssuesReferences.nodes[].number],"
    " closes_total: .closingIssuesReferences.totalCount,"
    " total: .files.totalCount, files: [.files.nodes[].path]}]"
)
_COMMENTS_JQ: Final[str] = (
    '[.[] | {id, body: (.body // ""), created_at, author: (.user.login // "")}]'
)
_REPO_FIELDS: Final[tuple[str, ...]] = ("-F", "owner={owner}", "-F", "name={repo}")


def _loads(raw: str | None) -> dict[str, Any] | None:
    """Decode one JSON object; ``None`` for a failed call, bad JSON or a non-object.

    Args:
        raw: Raw ``gh`` output, or None when the call failed.

    Returns:
        The decoded object, or None.
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("plan-check: unparseable gh output: %.60s", raw)
        return None
    return data if isinstance(data, dict) else None


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


def _issue_status(number: int, data: dict[str, Any]) -> RefStatus:
    """Map an ``Issue`` GraphQL node to a :class:`RefStatus`.

    The closer of the last close event comes first in ``landed_by``;
    merged linked PRs follow.

    Args:
        number: The issue number.
        data: The ``issueOrPullRequest`` node.

    Returns:
        The status.
    """
    closer = (data["timelineItems"]["nodes"] or [{}])[-1].get("closer") or {}
    landed: list[int] = [int(closer["number"])] if closer.get("merged") else []
    for node in data["closedByPullRequestsReferences"]["nodes"]:
        if node["merged"] and int(node["number"]) not in landed:
            landed.append(int(node["number"]))
    return RefStatus(
        number,
        is_pr=False,
        state=str(data["state"]),
        state_reason=data.get("stateReason"),
        merged=bool(landed),
        landed_by=tuple(landed),
    )


class _IncompleteListingError(Exception):
    """An open issue or PR could not be read completely; the listing is unknown."""


class GhSource:
    """Production ``GitHubSource`` over ``gh api``; ``None`` on any failure."""

    def __init__(self, run: Callable[..., str | None] = gh_api) -> None:
        """Bind the adapter to a ``gh api`` runner.

        Args:
            run: Called as ``run(*args, timeout=...)`` or ``run(*args)``;
                returns stdout, or None on any failure. Tests inject a
                recorded runner here.
        """
        self._run = run

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
            self._run(
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
            self._run(
                f"repos/{{owner}}/{{repo}}/issues/{number}",
                "--jq",
                '{number, body: (.body // ""), labels: [.labels[].name], created_at,'
                ' author: (.user.login // "")}',
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
                author=str(data.get("author") or ""),
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
            self._run(
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
            self._run(
                f"repos/{{owner}}/{{repo}}/collaborators/{login}/permission",
                "--jq",
                "{permission, role: .role_name}",
            )
        )
        if data is None or data.get("permission") is None:
            return None
        # `permission` folds maintain into write; `role_name` keeps it.
        role = data.get("role")
        return str(role) if role in WRITE_PERMISSIONS else str(data["permission"])

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
        if data is None:
            return None
        try:
            if data["__typename"] != "PullRequest":
                return _issue_status(number, data)
            merged = bool(data["merged"])
            return RefStatus(
                number,
                is_pr=True,
                state=str(data["state"]),
                state_reason=None,
                merged=merged,
                landed_by=(number,) if merged else (),
            )
        except (KeyError, TypeError, IndexError, AttributeError, ValueError):
            return None

    def pull_request(self, number: int) -> MergedPR | None:
        """Return PR *number*'s merge state, changed files and closed issues.

        Args:
            number: Issue or pull-request number.

        Returns:
            The merged-PR record, or None when it could not be read.
        """
        data = self._graphql(_PR_QUERY, number, ".data.repository.pullRequest")
        files = self._pr_files(number)
        if files is None or data is None:
            return None
        try:
            closing = data["closingIssuesReferences"]
            # A PR closing more issues than the page holds would hide the
            # rest from the overlap check's skip list: unknown, not partial.
            if int(closing["totalCount"]) > len(closing["nodes"]):
                return None
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

    def _pr_files(self, number: int) -> list[Any] | None:
        """Return every file PR *number* changes, a rename's old path included.

        Args:
            number: Pull-request number.

        Returns:
            The paths, or None when any page could not be read.
        """
        return _pages(
            self._run(
                f"repos/{{owner}}/{{repo}}/pulls/{number}/files",
                "--paginate",
                "--jq",
                "[.[] | .filename, (.previous_filename // empty)]",
                timeout=GH_LIST_TIMEOUT,
            )
        )

    def _listing(
        self, query: str, jq: str, build: Callable[[dict[str, Any]], _Record]
    ) -> list[_Record] | None:
        """Run one paginated GraphQL listing and build a record per entry.

        Args:
            query: GraphQL query text taking an ``$endCursor`` variable.
            jq: ``jq`` expression projecting each page to a list of entries.
            build: Builds one record, raising :class:`_IncompleteListingError`
                when its entry cannot be read completely.

        Returns:
            Every record, or None when the listing, any page of it, or any
            entry could not be read completely.
        """
        items = _pages(
            self._run(
                "graphql",
                "--paginate",
                *_REPO_FIELDS,
                "-f",
                f"query={query}",
                "--jq",
                jq,
                timeout=GH_LIST_TIMEOUT * 2,
            )
        )
        if items is None:
            return None
        try:
            return [build(item) for item in items]
        except _IncompleteListingError:
            return None

    def open_pull_requests(self) -> list[OpenPR] | None:
        """Return every open PR with its head, base, changed files and closed issues.

        Returns:
            Every open PR, or None when the listing, any page of it, any
            truncated PR's file refetch, or a PR closing more issues than
            the page holds could not be read completely.
        """
        return self._listing(_OPEN_PRS_QUERY, _OPEN_PRS_JQ, self._open_pr)

    def _open_pr(self, item: dict[str, Any]) -> OpenPR:
        """Build one listed PR, fetching every file when the page truncated them.

        Args:
            item: Projected listing entry for one open PR.

        Returns:
            The PR record.

        Raises:
            _IncompleteListingError: When the entry is malformed, its
                truncated file list could not be refetched, or it closes
                more issues than the page holds (a delivery the ``tracks``
                check would then misread as a collision).
        """
        try:
            number = int(item["number"])
            if int(item["closes_total"]) > len(item["closes"]):
                raise _IncompleteListingError
            files = [str(f) for f in item["files"]]
            if int(item["total"]) > len(files):
                fetched = self._pr_files(number)
                if fetched is None:
                    raise _IncompleteListingError
                files = [str(f) for f in fetched]
            return OpenPR(
                number,
                head_sha=str(item["head_sha"]),
                base_ref=str(item["base_ref"]),
                draft=bool(item["draft"]),
                closes=tuple(int(n) for n in item["closes"]),
                files=tuple(files),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _IncompleteListingError from exc

    def open_issues(self) -> list[Issue] | None:
        """Return every open issue, comments included.

        Returns:
            Every open issue with its full comment list, or None when the
            listing, any page of it, or any truncated issue's comment
            refetch could not be read.
        """
        return self._listing(_OPEN_ISSUES_QUERY, _OPEN_ISSUES_JQ, self._open_issue)

    def open_issue_count(self) -> int | None:
        """Return how many issues GitHub counts as open.

        Returns:
            The open-issue total, or None when it could not be read.
        """
        raw = self._run(
            "graphql",
            *_REPO_FIELDS,
            "-f",
            f"query={_OPEN_ISSUE_COUNT_QUERY}",
            "--jq",
            ".data.repository.issues.totalCount",
        )
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    def repo_slug(self) -> str | None:
        """Return the repository's ``owner/name``.

        Returns:
            The slug, or None when it could not be read.
        """
        raw = self._run("repos/{owner}/{repo}", "--jq", ".full_name")
        return raw or None

    def _open_issue(self, item: dict[str, Any]) -> Issue:
        """Build one listed issue, fetching all comments when the page truncated.

        Args:
            item: Projected listing entry for one open issue.

        Returns:
            The issue with its full comment list.

        Raises:
            _IncompleteListingError: When the entry is malformed or its
                truncated comments could not be refetched.
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
                title=str(item.get("title") or ""),
                updated_at=str(item.get("updated_at") or ""),
                author=str(item.get("author") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _IncompleteListingError from exc
