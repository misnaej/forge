"""Fakes and builders shared by the ``forge-plan-check`` suites.

Imported explicitly by the plan_staleness, plan_tracks and plan_check_cli
suites rather than living in ``tests/conftest.py``: none of it is a
fixture, and an explicit import is an edge the smart-test selector sees,
so editing these fakes reruns exactly the suites that use them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forge import plan_staleness as ps
from forge.plan_staleness_gh import Comment, Issue, MergedPR, OpenPR, RefStatus


PLAN_PERMISSIONS = {"maintainer": "write"}


@dataclass
class FakeGitHub:
    """In-memory ``GitHubSource``; a missing record reads as a failed call."""

    issues: dict[int, Issue] = field(default_factory=dict)
    comment_lists: dict[int, list[Comment]] = field(default_factory=dict)
    permissions: dict[str, str] = field(default_factory=dict)
    statuses: dict[int, RefStatus] = field(default_factory=dict)
    prs: dict[int, MergedPR] = field(default_factory=dict)
    open_listing: list[Issue] | None = None
    open_prs: list[OpenPR] | None = None

    def issue(self, number: int) -> Issue | None:
        """Return the recorded issue.

        Args:
            number: Issue number.

        Returns:
            The recorded issue, or None when unrecorded.
        """
        return self.issues.get(number)

    def comments(self, number: int) -> list[Comment] | None:
        """Return the recorded comments.

        Args:
            number: Issue number.

        Returns:
            The recorded comments, or None when unrecorded.
        """
        return self.comment_lists.get(number)

    def permission(self, login: str) -> str | None:
        """Return the recorded permission.

        Args:
            login: GitHub login.

        Returns:
            The recorded permission, or None when unrecorded.
        """
        return self.permissions.get(login)

    def ref_status(self, number: int) -> RefStatus | None:
        """Return the recorded status.

        Args:
            number: Issue or pull-request number.

        Returns:
            The recorded status, or None when unrecorded.
        """
        return self.statuses.get(number)

    def pull_request(self, number: int) -> MergedPR | None:
        """Return the recorded PR.

        Args:
            number: Pull-request number.

        Returns:
            The recorded PR, or None when unrecorded.
        """
        return self.prs.get(number)

    def open_issues(self) -> list[Issue] | None:
        """Return the recorded open-issue listing.

        Returns:
            The recorded listing, or None when unrecorded.
        """
        return self.open_listing

    def open_pull_requests(self) -> list[OpenPR] | None:
        """Return the recorded open-PR listing.

        Returns:
            The recorded listing, or None when unrecorded.
        """
        return self.open_prs


class LogRecorder:
    """A ``log_merges`` stand-in recording the paths it was asked about."""

    def __init__(self, merges: list[ps.Merge] | None = None) -> None:
        """Set the merges to return.

        Args:
            merges: Merges every call returns; None means git failed.
        """
        self.merges = merges
        self.seen: list[list[str]] = []

    def __call__(self, paths: list[str]) -> list[ps.Merge] | None:
        """Record *paths* and return the canned merges.

        Args:
            paths: File paths to log.

        Returns:
            The canned merges.
        """
        self.seen.append(paths)
        return self.merges


class ProbeStub:
    """Merge probe Fake: answers from a table, True otherwise, recording calls."""

    def __init__(
        self,
        answers: dict[tuple[str, str], bool | None] | None = None,
    ) -> None:
        """Set the verdicts by ``(left, right)``.

        Args:
            answers: Verdict per pair; unlisted pairs merge cleanly.
        """
        self.answers = answers or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, left: str, right: str) -> bool | None:
        """Record the pair and return its verdict.

        Args:
            left: One side.
            right: The other side.

        Returns:
            The tabled verdict, or True.
        """
        self.calls.append((left, right))
        return self.answers.get((left, right), True)


def report_lines(report: ps.Report, prefix: str) -> list[str]:
    """Return the report lines starting with *prefix*.

    Args:
        report: The report.
        prefix: Line prefix.

    Returns:
        Matching lines.
    """
    return [line for line in report.lines if line.startswith(prefix)]


def make_open_issue(
    number: int,
    *paths: str,
    labels: tuple[str, ...] = (),
    plan_extra: str = "",
    author: str = "maintainer",
) -> Issue:
    """Build an open issue whose authenticated plan names *paths*.

    Args:
        number: Issue number.
        *paths: Files the plan names.
        labels: Label names on the issue.
        plan_extra: Lines appended to the plan payload (e.g. a track line).
        author: Login that opened the issue.

    Returns:
        The issue, comments included.
    """
    named = " ".join(f"`{p}`" for p in paths)
    plan = Comment(
        number,
        f"{ps.PLAN_MARKER}\n\nEdit {named}.\n{plan_extra}",
        "2026-09-14T20:00:00Z",
        "maintainer",
    )
    return Issue(
        number,
        "Requires: nothing",
        labels,
        "2026-09-10T07:00:00Z",
        (plan,),
        author=author,
    )


def make_open_pr(
    number: int, *files: str, closes: tuple[int, ...] = (), head: str | None = None
) -> OpenPR:
    """Build an open PR with a full-length head SHA.

    Args:
        number: PR number.
        *files: Files the PR changes.
        closes: Issues the PR closes.
        head: Head SHA; defaults to one derived from *number*.

    Returns:
        The PR.
    """
    return OpenPR(
        number,
        head_sha=head or pr_head(number),
        base_ref="main",
        draft=False,
        closes=closes,
        files=files,
    )


def pr_head(number: int) -> str:
    """Return the full-length head SHA `make_open_pr` derives for PR *number*.

    Args:
        number: PR number, zero-padded to 40 hex digits to form the SHA.

    Returns:
        The 40-character hex head SHA.
    """
    return f"{number:040x}"
