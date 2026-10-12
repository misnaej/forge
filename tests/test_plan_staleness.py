"""Behaviour tests for ``forge-plan-check`` (``forge.plan_staleness``).

# MOCKING STRATEGY:
# The checks run against ``FakeGitHub``, a Fake implementing the
# ``GitHubSource`` Protocol from in-memory records; a method returning
# ``None`` is a failed call. The production adapter ``GhSource`` is
# exercised through its injected ``run`` seam with ``RecordedGh``, which
# replays canned ``gh api`` stdout (or ``None`` for a failed call). The
# git half runs against a real throwaway repository built in ``tmp_path``
# with fixed commit dates; ``main``'s base-ref plumbing is monkeypatched.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from forge import plan_staleness as ps
from forge.plan_staleness_gh import Comment, GhSource, Issue, MergedPR, RefStatus
from tests.conftest import GIT_ENV, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


PLAN_TIME = "2026-09-14T20:00:00Z"


@dataclass
class FakeGitHub:
    """In-memory ``GitHubSource``; a missing record reads as a failed call."""

    issues: dict[int, Issue] = field(default_factory=dict)
    comment_lists: dict[int, list[Comment]] = field(default_factory=dict)
    permissions: dict[str, str] = field(default_factory=dict)
    statuses: dict[int, RefStatus] = field(default_factory=dict)
    prs: dict[int, MergedPR] = field(default_factory=dict)
    open_listing: list[Issue] | None = None

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


@dataclass
class RecordedGh:
    """A ``gh api`` runner replaying canned stdout keyed by an argv substring."""

    replies: dict[str, str | None]
    calls: list[str] = field(default_factory=list)
    timeouts: list[int] = field(default_factory=list)

    def __call__(self, *args: str, timeout: int = 10) -> str | None:
        """Return the reply whose key occurs in the joined argv.

        The keyword matches ``gh_api``'s own, so a call site passing the
        wrong name fails here exactly as it would in production.

        Args:
            *args: ``gh api`` arguments.
            timeout: Recorded, never enforced.

        Returns:
            The canned stdout, or None when no key matches.
        """
        joined = " ".join(args)
        self.calls.append(joined)
        self.timeouts.append(timeout)
        for key, reply in self.replies.items():
            if key in joined:
                return reply
        return None


def _issue(number: int, body: str, *labels: str) -> Issue:
    """Build an issue record.

    Args:
        number: Issue number.
        body: Issue body text.
        *labels: Label names on the issue.

    Returns:
        The issue, created at a fixed time.
    """
    return Issue(number, body, labels, "2026-09-10T07:00:00Z")


def _plan(
    body: str, *, author: str = "maintainer", at: str = PLAN_TIME, cid: int = 1
) -> Comment:
    """Build a plan-validated comment.

    Args:
        body: Plan text following the marker.
        author: Comment author login.
        at: Comment creation timestamp.
        cid: Comment id.

    Returns:
        The comment.
    """
    return Comment(cid, f"{ps.PLAN_MARKER}\n\n{body}", at, author)


def _source(body: str, comments: list[Comment], *labels: str) -> FakeGitHub:
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


def _merge(day: str, *paths: str, pr: int | None = 1) -> ps.Merge:
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


def _merged_pr(number: int, *, landed_by: tuple[int, ...] = ()) -> RefStatus:
    """Build a merged-PR status.

    Args:
        number: PR number.
        landed_by: Merged PRs that delivered it.

    Returns:
        The status.
    """
    return RefStatus(
        number,
        is_pr=True,
        state="MERGED",
        state_reason=None,
        merged=True,
        landed_by=landed_by,
    )


def _closed_issue(
    number: int,
    *,
    state_reason: str = "COMPLETED",
    merged: bool = False,
    landed_by: tuple[int, ...] = (),
) -> RefStatus:
    """Build a closed-issue status.

    Args:
        number: Issue number.
        state_reason: GitHub state reason.
        merged: Whether the issue was closed by a merged PR.
        landed_by: PRs that closed the issue.

    Returns:
        The status.
    """
    return RefStatus(
        number,
        is_pr=False,
        state="CLOSED",
        state_reason=state_reason,
        merged=merged,
        landed_by=landed_by,
    )


def _status(
    number: int,
    *,
    is_pr: bool = False,
    state: str = "OPEN",
    merged: bool = False,
    landed_by: tuple[int, ...] = (),
) -> RefStatus:
    """Build a status; defaults describe an open issue.

    Args:
        number: Issue or PR number.
        is_pr: Whether the reference is a PR.
        state: GitHub state.
        merged: Whether the work merged.
        landed_by: Merged PRs that delivered it.

    Returns:
        The status.
    """
    return RefStatus(
        number,
        is_pr=is_pr,
        state=state,
        state_reason=None,
        merged=merged,
        landed_by=landed_by,
    )


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


def _lines(report: ps.Report, prefix: str) -> list[str]:
    """Return the report lines starting with *prefix*.

    Args:
        report: The report.
        prefix: Line prefix.

    Returns:
        Matching lines.
    """
    return [line for line in report.lines if line.startswith(prefix)]


# --------------------------------------------------------------------------
# prerequisites
# --------------------------------------------------------------------------


def test_merged_pr_prerequisite_is_satisfied() -> None:
    """A `Requires: PR #N` whose PR merged leaves the issue clean."""
    gh = _source("Requires: PR #5\n\nbody", [])
    gh.statuses[5] = _status(5, is_pr=True, state="MERGED", merged=True)
    report = ps.check_prerequisites(gh, 10)
    assert report.exit_code == ps.EXIT_CLEAN


@pytest.mark.parametrize(
    "status",
    [
        _closed_issue(5, state_reason="NOT_PLANNED"),
        _closed_issue(5, state_reason="COMPLETED"),
    ],
    ids=["closed-not-planned", "closed-by-hand"],
)
def test_closed_issue_without_landed_work_still_blocks(status: RefStatus) -> None:
    """A closed prerequisite blocks unless a merged PR closed it.

    Args:
        status: Closed-issue status with no merged PR behind it.
    """
    gh = _source("Requires: #5", [])
    gh.statuses[5] = status
    report = ps.check_prerequisites(gh, 10)
    assert report.exit_code == ps.EXIT_FINDING
    assert _lines(report, "blocked: #5")


def test_requires_nothing_is_satisfied() -> None:
    """`Requires: nothing` needs no GitHub lookup and is clean."""
    report = ps.check_prerequisites(_source("Requires: nothing", []), 10)
    assert report.exit_code == ps.EXIT_CLEAN


def test_prerequisites_notes_but_does_not_refuse_both_labels() -> None:
    """Both labels are drift's refusal; prerequisites notes it and still answers."""
    gh = _source("Requires: nothing", [], "blocked", "plan-ready")
    report = ps.check_prerequisites(gh, 10)
    assert report.exit_code == ps.EXIT_CLEAN
    assert _lines(report, "note: #10 carries both blocked and plan-ready")


def test_plan_requires_line_is_checked_too() -> None:
    """A prerequisite only the authenticated plan names still blocks.

    The bold mid-line form is how recorded plans write it.
    """
    gh = _source(
        "Requires: nothing", [_plan("Covers it. **Requires: PR #7** (merge first).")]
    )
    gh.statuses[7] = _status(7, is_pr=True)
    assert ps.check_prerequisites(gh, 10).exit_code == ps.EXIT_FINDING


def test_unparseable_requires_is_unknown_and_echo_is_capped() -> None:
    """An entry the CLI cannot check is unknown; its echo is fenced and capped."""
    report = ps.check_prerequisites(
        _source("Requires: the guard PR merged " + "x" * 300, []), 10
    )
    assert report.exit_code == ps.EXIT_UNKNOWN
    echo = next(line for line in report.lines if line.startswith("````"))
    assert "…[truncated]" in echo
    assert len(echo) < ps.ECHO_CAP + 40


def test_missing_requires_line_points_at_adding_one() -> None:
    """No `Requires:` line is unknown, and the output says to add one."""
    report = ps.check_prerequisites(_source("no prerequisite line at all", []), 10)
    assert report.exit_code == ps.EXIT_UNKNOWN
    assert "add one" in _lines(report, "unknown:")[0]


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


@pytest.mark.parametrize(
    ("login", "permission", "expected"),
    [
        ("alice", None, None),
        ("alice", "read", False),
        ("alice", "triage", False),
        ("alice", "write", True),
        ("alice", "maintain", True),
        ("alice", "admin", True),
        ("bad login!", "admin", False),
        ("alice\n", "admin", False),
    ],
)
def test_writer_check_is_tri_state(
    login: str, permission: str | None, *, expected: bool | None
) -> None:
    """Write-level roles pass, lesser ones fail, a failed lookup is unknown.

    Args:
        login: Login to check.
        permission: What the source reports for it (None: lookup failed).
        expected: The predicate's answer.
    """
    gh = FakeGitHub(permissions={} if permission is None else {login: permission})
    assert ps.WriterCheck(gh)(login) is expected


def test_plan_from_non_writer_is_ignored() -> None:
    """A newer plan comment by an author without write access never counts."""
    real = _plan("touches `src/forge/a.py`", at="2026-09-01T00:00:00Z")
    spoof = _plan("touches `src/forge/b.py`", author="stranger", cid=2)
    gh = _source("", [real, spoof])
    lookup = ps.authenticated_plan(gh.comment_lists[10], ps.WriterCheck(gh))
    assert lookup.plan is real
    assert lookup.ignored == (2,)


def test_unresolvable_plan_author_is_unknown_everywhere() -> None:
    """A plan whose author's access cannot be looked up makes every check unknown.

    SCENARIO: the permission call fails for the newest plan comment's
    author. That comment may be the real plan, so treating the author as
    a non-writer would silently drop its `Requires:` entries and files.
    """
    plan = _plan("**Requires: PR #7** Edit `src/forge/a.py`.", author="ghost")
    gh = _source("Requires: nothing", [plan])
    assert ps.check_prerequisites(gh, 10).exit_code == ps.EXIT_UNKNOWN
    drift = ps.check_drift(gh, 10, since=None, log_merges=LogRecorder([]))
    assert drift.exit_code == ps.EXIT_UNKNOWN

    gh.prs[50] = MergedPR(
        50, merged=True, merge_sha="c" * 40, closes=(), files=("src/forge/a.py",)
    )
    gh.open_listing = [Issue(10, "no files", (), PLAN_TIME, (plan,))]
    assert ps.check_overlap(gh, 50).exit_code == ps.EXIT_UNKNOWN


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
    log = LogRecorder(
        [
            _merge("20", "src/forge/a.py", pr=9),
            _merge("19", "src/forge/a.py", pr=None),
            _merge("01", "src/forge/a.py"),
        ]
    )
    report = ps.check_drift(gh, 10, since=None, log_merges=log)
    assert log.seen == [["src/forge/a.py"]]
    assert report.exit_code == ps.EXIT_FINDING
    assert _lines(report, "drift:") == [
        f"drift: {'a' * 12} 2026-09-20T12:00:00+00:00 PR #9 changed src/forge/a.py",
        (
            f"drift: {'a' * 12} 2026-09-19T12:00:00+00:00 no PR number in subject "
            "changed src/forge/a.py"
        ),
    ]


def test_drift_without_merges_is_clean() -> None:
    """No merge after the plan on its files exits 0."""
    gh = _source("", [_plan("Edit `src/forge/a.py`.")])
    report = ps.check_drift(gh, 10, since=None, log_merges=LogRecorder([]))
    assert report.exit_code == ps.EXIT_CLEAN


def test_body_only_requires_cannot_hide_drift() -> None:
    """A body `Requires:` added after validation does not excuse a merge.

    SCENARIO: a plan exists and names its files; the body later gains
    `Requires: #9`, naming a merged PR that changed a plan file after
    approval. Only the plan's entries excuse merges, so that merge is
    still drift.
    """
    gh = _source("Requires: #9", [_plan("Edit `src/forge/a.py`.")])
    gh.statuses[9] = _status(9, is_pr=True, state="MERGED", merged=True, landed_by=(9,))
    log = LogRecorder([_merge("20", "src/forge/a.py", pr=9)])
    report = ps.check_drift(gh, 10, since=None, log_merges=log)
    assert report.exit_code == ps.EXIT_FINDING
    assert _lines(report, "drift:")


def test_drift_notes_merges_that_deliver_its_own_prerequisites() -> None:
    """Merges landing the issue's prerequisites are expected, not drift.

    SCENARIO: the plan names a PR it waits on and an issue a merged PR
    closed; both merges touch the plan's files after approval. Counting
    them would hard-skip every plan whose prerequisite shares its files.
    """
    plan = _plan("**Requires: PR #7, #8** Edit `src/forge/a.py`.")
    gh = _source("Requires: nothing", [plan])
    gh.statuses[7] = _merged_pr(7, landed_by=(7,))
    gh.statuses[8] = _closed_issue(
        8, state_reason="COMPLETED", merged=True, landed_by=(30,)
    )
    log = LogRecorder(
        [_merge("20", "src/forge/a.py", pr=7), _merge("21", "src/forge/a.py", pr=30)]
    )
    report = ps.check_drift(gh, 10, since=None, log_merges=log)
    assert report.exit_code == ps.EXIT_CLEAN
    notes = _lines(report, "note:")
    assert any("PR #7 delivers prerequisite #7" in n for n in notes)
    assert any("PR #30 delivers prerequisite #8" in n for n in notes)


def test_drift_falls_back_to_body_with_a_note_then_refuses() -> None:
    """A plan naming no files uses the body's, saying so; neither naming any refuses."""
    gh = _source("Touches `agents/x.md`.", [_plan("No files here.")])
    log = LogRecorder([])
    report = ps.check_drift(gh, 10, since=None, log_merges=log)
    assert log.seen == [["agents/x.md"]]
    assert _lines(report, "note: #10 paths come from the issue body")

    gh = _source("Nothing named.", [_plan("No files here.")])
    report = ps.check_drift(gh, 10, since=None, log_merges=LogRecorder([]))
    assert report.exit_code == ps.EXIT_UNKNOWN


def test_drift_ignores_a_stranger_only_plan() -> None:
    """A plan from a non-writer supplies neither files nor a start time."""
    spoof = _plan("Edit `src/forge/spoof.py`.", author="stranger")
    gh = _source("Touches `src/forge/body.py`.", [spoof])
    log = LogRecorder([])
    since = ps.parse_timestamp("2026-09-01T00:00:00Z")
    report = ps.check_drift(gh, 10, since=since, log_merges=log)
    assert log.seen == [["src/forge/body.py"]]
    assert _lines(report, "note: ignored plan comment 1")
    no_since = ps.check_drift(gh, 10, since=None, log_merges=LogRecorder([]))
    assert no_since.exit_code == ps.EXIT_UNKNOWN


def test_drift_refuses_blocked_and_plan_ready_together() -> None:
    """An issue labelled both `blocked` and `plan-ready` is refused."""
    gh = _source("`a/b.py`", [_plan("`a/b.py`")], "blocked", "plan-ready")
    report = ps.check_drift(gh, 10, since=None, log_merges=LogRecorder([]))
    assert report.exit_code == ps.EXIT_UNKNOWN


def test_drift_git_failure_is_unknown() -> None:
    """A `log_merges` that cannot read history exits 2."""
    gh = _source("", [_plan("Edit `src/forge/a.py`.")])
    report = ps.check_drift(gh, 10, since=None, log_merges=LogRecorder(None))
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
    drift = _lines(report, "drift:")
    assert len(drift) == 1
    assert "PR #2 changed agents/git-commit-push.md" in drift[0]

    gh.prs[2] = MergedPR(
        2,
        merged=True,
        merge_sha="b" * 40,
        closes=(),
        files=("agents/git-commit-push.md",),
    )
    gh.open_listing = [Issue(10, body, (), PLAN_TIME, ())]
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
# main drift / overlap plumbing
# --------------------------------------------------------------------------


@pytest.fixture
def drift_base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Stub the repo root, base-ref resolution, fetch and git log for `main`.

    Returns:
        Mutable knobs: ``ref`` (resolved base ref or None) and ``fetch``
        (whether the fetch succeeds).
    """
    knobs: dict[str, object] = {"ref": "origin/main", "fetch": True}
    monkeypatch.setattr(ps, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(ps, "resolve_base_branch_ref", lambda *_: knobs["ref"])
    monkeypatch.setattr(ps, "fetch_quietly", lambda *_: knobs["fetch"])
    monkeypatch.setattr(ps, "git_merge_log", lambda *_: LogRecorder([]))
    return knobs


def test_main_drift_exit_codes(
    drift_base: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    """Clean drift exits 0; an unresolved base or failed fetch refuses with 2."""
    gh = _source("", [_plan("Edit `src/forge/a.py`.")])
    argv = ["drift", "10", "--base", "main"]
    assert ps.main(argv, source=gh) == ps.EXIT_CLEAN

    drift_base["ref"] = "main"
    assert ps.main(argv, source=gh) == ps.EXIT_CLEAN
    assert "note: history read from local main" in capsys.readouterr().out

    drift_base["ref"] = "origin/main"
    drift_base["fetch"] = False
    assert ps.main(argv, source=gh) == ps.EXIT_UNKNOWN

    drift_base["ref"] = None
    assert ps.main(argv, source=gh) == ps.EXIT_UNKNOWN


def test_main_overlap_exit_codes() -> None:
    """Overlap exits 0 with no overlapping issue and 2 when the PR cannot be read."""
    gh = FakeGitHub(
        prs={50: MergedPR(50, merged=True, merge_sha="", closes=(), files=("a.py",))},
        open_listing=[],
    )
    assert ps.main(["overlap", "50"], source=gh) == ps.EXIT_CLEAN
    assert ps.main(["overlap", "51"], source=gh) == ps.EXIT_UNKNOWN


# --------------------------------------------------------------------------
# overlap
# --------------------------------------------------------------------------


def test_overlap_lists_issues_naming_changed_files() -> None:
    """Open issues naming a changed file are listed; the PR's own are skipped.

    Both-labels issues are still listed and marked plan-ready — overlap
    reports, it does not refuse.
    """
    plan_only = Issue(
        2,
        "no files",
        ("blocked", "plan-ready"),
        PLAN_TIME,
        (_plan("Edit `src/forge/a.py`."),),
    )
    spoofed = Issue(
        5,
        "no files",
        (),
        PLAN_TIME,
        (_plan("Edit `src/forge/a.py`.", author="stranger"),),
    )
    gh = FakeGitHub(
        permissions={"maintainer": "admin", "stranger": "read"},
        prs={
            50: MergedPR(
                50,
                merged=True,
                merge_sha="",
                closes=(1,),
                files=("src/forge/a.py",),
            )
        },
        open_listing=[
            _issue(1, "Fixes `src/forge/a.py`."),  # closed by the PR itself
            plan_only,
            _issue(3, "Mentions `src/forge/other.py`."),
            _issue(4, "Also `src/forge/a.py`."),
            spoofed,
        ],
    )
    report = ps.check_overlap(gh, 50)
    assert _lines(report, "overlap:") == [
        "overlap: #2 plan-ready names src/forge/a.py",
        "overlap: #4 names src/forge/a.py",
    ]
    assert _lines(report, "note: #4 paths come from the issue body")
    assert _lines(report, "merge:") == ["merge: PR #50 changed 1 file(s)"]
    assert report.exit_code == ps.EXIT_FINDING


def test_overlap_refuses_unmerged_pr_and_failed_listing() -> None:
    """An unmerged PR is refused; a failed issue listing is unknown."""
    gh = FakeGitHub(
        prs={50: MergedPR(50, merged=False, merge_sha="", closes=(), files=("a/b.py",))}
    )
    assert ps.check_overlap(gh, 50).exit_code == ps.EXIT_UNKNOWN
    gh.prs[50] = MergedPR(
        50, merged=True, merge_sha="d" * 40, closes=(), files=("a/b.py",)
    )
    assert ps.check_overlap(gh, 50).exit_code == ps.EXIT_UNKNOWN


def test_requires_ignores_backticked_mentions() -> None:
    """Prose about the `Requires:` convention is not a prerequisite line."""
    parsed = ps.parse_requires(["Every issue opens with a `Requires:` line."])
    assert not parsed.found


# --------------------------------------------------------------------------
# GhSource adapter (fail closed)
# --------------------------------------------------------------------------


def _page(*items: object) -> str:
    """Return one compact JSON page.

    Args:
        *items: Page entries.

    Returns:
        The page as ``gh --paginate --jq`` prints it.
    """
    return json.dumps(list(items))


def _listed(number: int, total: int) -> dict[str, object]:
    """Return one open-issue listing entry carrying one comment.

    Args:
        number: Issue number.
        total: The comment total GitHub reports.

    Returns:
        The projected entry.
    """
    comment = {"id": 1, "body": "hi", "created_at": PLAN_TIME, "author": "a"}
    return {
        "number": number,
        "body": "b",
        "created_at": PLAN_TIME,
        "labels": [],
        "total": total,
        "comments": [comment],
    }


def test_gh_source_failed_call_and_bad_json_are_none() -> None:
    """A failed `gh` call or unparseable output is None, never an empty result."""
    failed = GhSource(run=RecordedGh({}))
    assert failed.issue(1) is None
    assert failed.comments(1) is None
    assert failed.ref_status(1) is None
    assert failed.pull_request(1) is None
    assert failed.open_issues() is None
    assert failed.permission("alice") is None

    garbled = GhSource(run=RecordedGh({"issues/1": "{not json", "graphql": "[]"}))
    assert garbled.issue(1) is None
    assert garbled.comments(1) is None
    assert garbled.ref_status(1) is None


def test_gh_source_empty_comment_listing_is_an_empty_list() -> None:
    """A successful call with no comments is distinct from a failed call."""
    assert GhSource(run=RecordedGh({"comments": "[]"})).comments(1) == []


def test_gh_source_incomplete_paged_listing_is_none() -> None:
    """One bad page makes the whole open-issue listing unknown."""
    raw = _page(_listed(1, 1)) + "\nnot json"
    assert GhSource(run=RecordedGh({"graphql": raw})).open_issues() is None


def test_gh_source_truncated_comments_refetch() -> None:
    """Comments beyond the listed nodes are refetched; a failed refetch is unknown."""
    listing = _page(_listed(1, 2))
    failing = GhSource(run=RecordedGh({"graphql": listing}))
    assert failing.open_issues() is None

    full = _page(
        {"id": 1, "body": "a", "created_at": PLAN_TIME, "author": "x"},
        {"id": 2, "body": "b", "created_at": PLAN_TIME, "author": "y"},
    )
    working = GhSource(run=RecordedGh({"issues/1/comments": full, "graphql": listing}))
    issues = working.open_issues()
    assert issues is not None
    assert [c.id for c in issues[0].comments] == [1, 2]


@pytest.mark.parametrize(
    ("node", "satisfied", "landed_by"),
    [
        (
            {
                "state": "CLOSED",
                "stateReason": "COMPLETED",
                "closedByPullRequestsReferences": {"nodes": []},
                "timelineItems": {
                    "nodes": [{"closer": {"number": 30, "merged": True}}]
                },
            },
            True,
            (30,),
        ),
        (
            {
                "state": "CLOSED",
                "stateReason": "COMPLETED",
                "closedByPullRequestsReferences": {
                    "nodes": [{"number": 31, "merged": False}]
                },
                "timelineItems": {"nodes": [{"closer": None}]},
            },
            False,
            (),
        ),
        (
            {
                "state": "CLOSED",
                "stateReason": "NOT_PLANNED",
                "closedByPullRequestsReferences": {"nodes": []},
                "timelineItems": {"nodes": [{"closer": None}]},
            },
            False,
            (),
        ),
    ],
    ids=["merged-pr-close", "hand-close", "not-planned"],
)
def test_gh_source_maps_issue_status(
    node: dict[str, object], *, satisfied: bool, landed_by: tuple[int, ...]
) -> None:
    """GraphQL issue nodes map to statuses the verdict reads correctly.

    Args:
        node: The ``issueOrPullRequest`` projection.
        satisfied: Whether the prerequisite counts as landed.
        landed_by: The merged PRs recorded as delivering it.
    """
    reply = json.dumps({"__typename": "Issue", **node})
    status = GhSource(run=RecordedGh({"graphql": reply})).ref_status(8)
    assert status is not None
    assert ps.prerequisite_verdict(status)[0] is satisfied
    assert status.landed_by == landed_by
