"""Behaviour tests for ``forge-plan-check`` (``forge.plan_staleness``).

# MOCKING STRATEGY:
# GitHub is replaced by ``FakeGitHub``, a Fake implementing the
# ``GitHubSource`` Protocol from in-memory records shaped like the
# projected ``gh api`` JSON. A method returning ``None`` is a failed call.
# The git half runs against a real throwaway repository built in
# ``tmp_path`` with fixed commit dates.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from forge import plan_staleness as ps
from tests.conftest import GIT_ENV, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


PLAN_TIME = "2026-09-14T20:00:00Z"


@dataclass
class FakeGitHub:
    """In-memory ``GitHubSource``; a missing record reads as a failed call."""

    issues: dict[int, ps.Issue] = field(default_factory=dict)
    comment_lists: dict[int, list[ps.Comment]] = field(default_factory=dict)
    permissions: dict[str, str] = field(default_factory=dict)
    statuses: dict[int, ps.RefStatus] = field(default_factory=dict)
    prs: dict[int, ps.MergedPR] = field(default_factory=dict)
    open_listing: list[ps.Issue] | None = None

    def issue(self, number: int) -> ps.Issue | None:
        """Return the recorded issue.

        Args:
            number: Issue number.

        Returns:
            The recorded issue, or None when unrecorded.
        """
        return self.issues.get(number)

    def comments(self, number: int) -> list[ps.Comment] | None:
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

    def ref_status(self, number: int) -> ps.RefStatus | None:
        """Return the recorded status.

        Args:
            number: Issue or pull-request number.

        Returns:
            The recorded status, or None when unrecorded.
        """
        return self.statuses.get(number)

    def pull_request(self, number: int) -> ps.MergedPR | None:
        """Return the recorded PR.

        Args:
            number: Pull-request number.

        Returns:
            The recorded PR, or None when unrecorded.
        """
        return self.prs.get(number)

    def open_issues(self) -> list[ps.Issue] | None:
        """Return the recorded open-issue listing."""
        return self.open_listing


def _issue(number: int, body: str, *labels: str) -> ps.Issue:
    """Build an issue record.

    Args:
        number: Issue number.
        body: Issue body text.
        *labels: Label names on the issue.

    Returns:
        The issue, created at a fixed time.
    """
    return ps.Issue(number, body, labels, "2026-09-10T07:00:00Z")


def _plan(body: str, *, author: str = "maintainer", at: str = PLAN_TIME) -> ps.Comment:
    """Build a plan-validated comment.

    Args:
        body: Plan text following the marker.
        author: Comment author login.
        at: Comment creation timestamp.

    Returns:
        The comment.
    """
    return ps.Comment(1, f"{ps.PLAN_MARKER}\n\n{body}", at, author)


def _source(body: str, comments: list[ps.Comment], *labels: str) -> FakeGitHub:
    """Build a fake holding issue 10 with *comments* and a writing maintainer.

    Args:
        body: Issue body text.
        comments: Comments on issue 10.
        *labels: Label names on the issue.

    Returns:
        The configured fake.
    """
    return FakeGitHub(
        issues={10: _issue(10, body, *labels)},
        comment_lists={10: comments},
        permissions={"maintainer": "write", "stranger": "read"},
    )


def _merge(day: str, *paths: str, pr: int = 1) -> ps.Merge:
    """Build a merge record on 2026-09-<day>.

    Args:
        day: Two-digit day of the month.
        *paths: Paths the merge touched.
        pr: Pull-request number of the merge.

    Returns:
        The merge record.
    """
    return ps.Merge(
        "a" * 40, datetime.fromisoformat(f"2026-09-{day}T12:00:00+00:00"), pr, paths
    )


# --------------------------------------------------------------------------
# prerequisites
# --------------------------------------------------------------------------


def test_merged_pr_prerequisite_is_satisfied() -> None:
    """A `Requires: PR #N` whose PR merged leaves the issue clean."""
    gh = _source("Requires: PR #5\n\nbody", [])
    gh.statuses[5] = ps.RefStatus(
        5, is_pr=True, state="MERGED", state_reason=None, merged=True
    )
    report = ps.check_prerequisites(gh, 10)
    assert report.exit_code == ps.EXIT_CLEAN


@pytest.mark.parametrize(
    "status",
    [
        ps.RefStatus(
            5, is_pr=False, state="CLOSED", state_reason="NOT_PLANNED", merged=False
        ),
        ps.RefStatus(
            5, is_pr=False, state="CLOSED", state_reason="COMPLETED", merged=False
        ),
    ],
    ids=["closed-not-planned", "closed-by-hand"],
)
def test_closed_issue_without_landed_work_still_blocks(status: ps.RefStatus) -> None:
    """A closed prerequisite blocks unless a merged PR closed it.

    Args:
        status: Closed-issue status with no merged PR behind it.
    """
    gh = _source("Requires: #5", [])
    gh.statuses[5] = status
    report = ps.check_prerequisites(gh, 10)
    assert report.exit_code == ps.EXIT_FINDING
    assert any(line.startswith("blocked: #5") for line in report.lines)


def test_requires_nothing_is_satisfied() -> None:
    """`Requires: nothing` needs no GitHub lookup and is clean."""
    report = ps.check_prerequisites(_source("Requires: nothing", []), 10)
    assert report.exit_code == ps.EXIT_CLEAN


def test_plan_requires_line_is_checked_too() -> None:
    """A prerequisite only the authenticated plan names still blocks.

    The bold mid-line form is how recorded plans write it.
    """
    gh = _source(
        "Requires: nothing", [_plan("Covers it. **Requires: PR #7** (merge first).")]
    )
    gh.statuses[7] = ps.RefStatus(
        7, is_pr=True, state="OPEN", state_reason=None, merged=False
    )
    assert ps.check_prerequisites(gh, 10).exit_code == ps.EXIT_FINDING


@pytest.mark.parametrize(
    "body", ["Requires: the guard PR merged", "no prerequisite line at all"]
)
def test_unparseable_or_missing_requires_is_unknown(body: str) -> None:
    """A prerequisite the CLI cannot check is unknown, never clean.

    Args:
        body: Issue body.
    """
    assert ps.check_prerequisites(_source(body, []), 10).exit_code == ps.EXIT_UNKNOWN


def test_github_failure_is_unknown() -> None:
    """A failed GitHub read exits 2, for the issue and for a prerequisite."""
    assert ps.check_prerequisites(FakeGitHub(), 10).exit_code == ps.EXIT_UNKNOWN
    gh = _source("Requires: #5", [])  # no status recorded for #5
    assert ps.check_prerequisites(gh, 10).exit_code == ps.EXIT_UNKNOWN


def test_main_exit_code_follows_report(capsys: pytest.CaptureFixture[str]) -> None:
    """`main` prints the verdict and returns the report's exit code."""
    assert ps.main(["prerequisites", "10"], source=FakeGitHub()) == ps.EXIT_UNKNOWN
    assert capsys.readouterr().out.rstrip().endswith("verdict: unknown")


# --------------------------------------------------------------------------
# plan authenticity and paths
# --------------------------------------------------------------------------


def test_plan_from_non_writer_is_ignored() -> None:
    """A newer plan comment by an author without write access never counts."""
    real = _plan("touches `src/forge/a.py`", at="2026-09-01T00:00:00Z")
    spoof = _plan("touches `src/forge/b.py`", author="stranger")
    gh = _source("", [real, spoof])
    plan = ps.authenticated_plan(gh.comment_lists[10], ps.WriterCheck(gh))
    assert plan is real


@pytest.mark.parametrize(
    "path", ["../etc/passwd", "/etc/passwd", "-rf", "https://x.io/a.md", "a/../b.py"]
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    """Paths from issue text never reach git as options or outside the repo.

    Args:
        path: Hostile candidate.
    """
    assert not ps.is_safe_path(path)
    assert path not in ps.named_paths(f"see {path} here")


def test_named_paths_keeps_real_files_only() -> None:
    """Repo files are kept; placeholders, prose and fenced text are not."""
    text = (
        "Edit `skills/pr/SKILL.md`, src/forge/x.py:12 and pyproject.toml.\n"
        "Not `changelog.d/<slug>.md`, e.g. this, or forge.pr_delta.\n"
        "```\nsrc/forge/quoted.py\n```"
    )
    assert ps.named_paths(text) == [
        "skills/pr/SKILL.md",
        "src/forge/x.py",
        "pyproject.toml",
    ]


# --------------------------------------------------------------------------
# drift
# --------------------------------------------------------------------------


def test_drift_reports_merge_after_plan_time() -> None:
    """Only merges after the plan was posted, on its named files, are drift."""
    gh = _source("", [_plan("Edit `src/forge/a.py`.")])
    seen: list[list[str]] = []

    def log(paths: list[str]) -> list[ps.Merge]:
        """Record *paths* and return two canned merges.

        Args:
            paths: File paths to log.

        Returns:
            Two canned merge records.
        """
        seen.append(paths)
        return [_merge("20", "src/forge/a.py", pr=9), _merge("01", "src/forge/a.py")]

    report = ps.check_drift(gh, 10, since=None, log_merges=log)
    assert seen == [["src/forge/a.py"]]
    assert report.exit_code == ps.EXIT_FINDING
    assert [line for line in report.lines if line.startswith("drift:")] == [
        f"drift: {'a' * 12} 2026-09-20T12:00:00+00:00 PR #9 changed src/forge/a.py"
    ]


def test_drift_falls_back_to_body_then_refuses() -> None:
    """A plan naming no files uses the body's; neither naming any refuses."""
    gh = _source("Touches `agents/x.md`.", [_plan("No files here.")])
    seen: list[list[str]] = []
    ps.check_drift(gh, 10, since=None, log_merges=lambda p: seen.append(p) or [])
    assert seen == [["agents/x.md"]]

    gh = _source("Nothing named.", [_plan("No files here.")])
    report = ps.check_drift(gh, 10, since=None, log_merges=lambda _: [])
    assert report.exit_code == ps.EXIT_UNKNOWN


def test_drift_refuses_blocked_and_plan_ready_together() -> None:
    """An issue labelled both `blocked` and `plan-ready` is refused."""
    gh = _source("`a/b.py`", [_plan("`a/b.py`")], "blocked", "plan-ready")
    report = ps.check_drift(gh, 10, since=None, log_merges=lambda _: [])
    assert report.exit_code == ps.EXIT_UNKNOWN


def _commit(repo: Path, path: str, when: str, message: str) -> None:
    """Write *path* and commit it with a fixed committer date.

    Args:
        repo: Repository to commit in.
        path: Repository-relative file to write.
        when: Committer and author date.
        message: Commit message, also written as the file content.
    """
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(message)
    env = {**GIT_ENV, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=env, check=True
    )


def test_incident_replay_drift_flags_premise_changed_before_approval(
    tmp_path: Path,
) -> None:
    """Replay: a merge changed the file a plan's premise rested on before approval.

    SCENARIO: an issue claimed an agent only executes what it is handed.
    Hours before its plan was approved, a merge gave that agent real work
    by editing ``agents/git-commit-push.md``; nobody compared the two, and
    the plan ran unattended on a false premise. Checked from the issue's
    filing time (what ``/plan-issue`` shows), drift flags that merge; the
    post-merge overlap sweep lists the issue too.
    """
    init_git_repo(tmp_path)
    _commit(tmp_path, "README.md", "2026-09-09T10:00:00+00:00", "unrelated (#1)")
    _commit(
        tmp_path,
        "agents/git-commit-push.md",
        "2026-09-14T12:00:00+00:00",
        "feat: commit agent stages work (#2)",
    )
    body = "The agent in `agents/git-commit-push.md` only executes what it is handed."
    gh = _source(body, [_plan("Delete `agents/git-commit-push.md`.")])

    report = ps.check_drift(
        gh,
        10,
        since=ps.parse_timestamp("2026-09-10T07:00:00Z"),
        log_merges=ps.git_merge_log(tmp_path, "main"),
    )
    drift = [line for line in report.lines if line.startswith("drift:")]
    assert len(drift) == 1
    assert "PR #2 changed agents/git-commit-push.md" in drift[0]

    gh.prs[2] = ps.MergedPR(
        2,
        merged=True,
        merge_sha="b" * 40,
        closes=(),
        files=("agents/git-commit-push.md",),
    )
    gh.open_listing = [ps.Issue(10, body, (), PLAN_TIME, ())]
    assert ps.check_overlap(gh, 2).exit_code == ps.EXIT_FINDING


def test_git_merge_log_reads_first_parent_history(tmp_path: Path) -> None:
    """The git adapter lists only commits touching the paths, with UTC dates."""
    init_git_repo(tmp_path)
    _commit(tmp_path, "a.md", "2026-09-01T10:00:00+03:00", "x (#3)")
    _commit(tmp_path, "b.md", "2026-09-02T10:00:00+00:00", "y")
    merges = ps.git_merge_log(tmp_path, "main")(["a.md"])
    assert merges == [
        ps.Merge(merges[0].sha, datetime(2026, 9, 1, 7, tzinfo=UTC), 3, ("a.md",))
    ]
    assert ps.git_merge_log(tmp_path, "no-such-ref")(["a.md"]) is None


# --------------------------------------------------------------------------
# overlap
# --------------------------------------------------------------------------


def test_overlap_lists_issues_naming_changed_files() -> None:
    """Open issues naming a changed file are listed; the PR's own are skipped."""
    plan_only = ps.Issue(
        2,
        "no files",
        ("plan-ready",),
        PLAN_TIME,
        (_plan("Edit `src/forge/a.py`."),),
    )
    gh = FakeGitHub(
        permissions={"maintainer": "admin"},
        prs={
            50: ps.MergedPR(
                50,
                merged=True,
                merge_sha="c" * 40,
                closes=(1,),
                files=("src/forge/a.py",),
            )
        },
        open_listing=[
            _issue(1, "Fixes `src/forge/a.py`."),  # closed by the PR itself
            plan_only,
            _issue(3, "Mentions `src/forge/other.py`."),
            _issue(4, "Also `src/forge/a.py`."),
        ],
    )
    report = ps.check_overlap(gh, 50)
    assert [line for line in report.lines if line.startswith("overlap:")] == [
        "overlap: #2 plan-ready names src/forge/a.py",
        "overlap: #4 names src/forge/a.py",
    ]
    assert report.exit_code == ps.EXIT_FINDING


def test_overlap_refuses_unmerged_pr_and_failed_listing() -> None:
    """An unmerged PR is refused; a failed issue listing is unknown."""
    gh = FakeGitHub(
        prs={
            50: ps.MergedPR(
                50, merged=False, merge_sha="", closes=(), files=("a/b.py",)
            )
        }
    )
    assert ps.check_overlap(gh, 50).exit_code == ps.EXIT_UNKNOWN
    gh.prs[50] = ps.MergedPR(
        50, merged=True, merge_sha="d" * 40, closes=(), files=("a/b.py",)
    )
    assert ps.check_overlap(gh, 50).exit_code == ps.EXIT_UNKNOWN


def test_requires_ignores_backticked_mentions() -> None:
    """Prose about the `Requires:` convention is not a prerequisite line."""
    parsed = ps.parse_requires(["Every issue opens with a `Requires:` line."])
    assert not parsed.found
