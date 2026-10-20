"""Behaviour tests for ``forge.plan_check_cli`` — the set subcommands' exit codes.

# MOCKING STRATEGY:
# ``main`` takes its GitHub source by injection (``FakeGitHub``). The git
# plumbing it reaches for the base-ref subcommands is patched in
# ``forge.plan_check_cli``'s own namespace: ``repo_root``,
# ``resolve_base_branch_ref``, ``fetch_quietly`` and ``git_merge_log``
# (history), plus ``fetch_pr_heads`` / ``git_merge_probe`` (merge-order).
# The checks behind them are covered in ``tests/test_plan_tracks.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from forge import plan_check_cli as cli
from forge import plan_staleness as ps
from forge.plan_staleness_gh import Comment
from tests._plan_fakes import (
    PLAN_PERMISSIONS,
    FakeGitHub,
    LogRecorder,
    ProbeStub,
    make_open_issue,
    make_open_pr,
    pr_head,
)


if TYPE_CHECKING:
    from pathlib import Path


def _arrange(
    command: str,
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[list[str], FakeGitHub]:
    """Wire the fakes so *command* ends in *scenario*.

    "Unusable" means the input the command needs cannot be read: the base
    ref is unresolved for ``current`` and ``merge-order``, and the open-issue
    listing is unreadable for ``tracks``.

    Args:
        command: ``tracks``, ``current`` or ``merge-order``.
        scenario: ``clean``, ``finding`` or ``unusable``.
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Scratch directory used as the repo root.

    Returns:
        The argument vector and the GitHub fake to pass to ``main``.
    """
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        cli,
        "resolve_base_branch_ref",
        lambda *_: None if scenario == "unusable" else "origin/main",
    )
    monkeypatch.setattr(cli, "fetch_quietly", lambda *_: True)
    shared = scenario == "finding"
    issues = [
        make_open_issue(1, "src/x/a.py"),
        make_open_issue(2, "src/x/a.py" if shared else "src/y/b.py"),
    ]
    gh = FakeGitHub(
        open_listing=issues,
        issues={i.number: i for i in issues},
        comment_lists={i.number: list(i.comments) for i in issues},
        open_prs=[make_open_pr(1, "a.py"), make_open_pr(2, "b.py")],
        permissions=PLAN_PERMISSIONS,
    )
    if command == "tracks":
        if scenario == "unusable":
            gh.open_listing = None
        return ["tracks"], gh
    if command == "current":
        when = datetime(2026, 9, 12, tzinfo=UTC)
        merges = [ps.Merge("a" * 40, when, 5, ("src/x/a.py",))]
        history = LogRecorder(merges if shared else [])
        monkeypatch.setattr(cli, "git_merge_log", lambda *_: history)
        return ["current", "1"], gh
    probe = ProbeStub({(pr_head(1), pr_head(2)): False} if shared else {})
    monkeypatch.setattr(cli, "fetch_pr_heads", lambda _root, prs, _report: list(prs))
    monkeypatch.setattr(cli, "git_merge_probe", lambda _root: probe)
    return ["merge-order"], gh


_OUTCOMES: dict[str, tuple[int, str]] = {
    "clean": (ps.EXIT_CLEAN, "verdict: clean"),
    "finding": (ps.EXIT_FINDING, "verdict: finding"),
    "unusable": (ps.EXIT_UNKNOWN, "verdict: unknown"),
}


@pytest.mark.parametrize("scenario", list(_OUTCOMES))
@pytest.mark.parametrize("command", ["tracks", "current", "merge-order"])
def test_set_subcommands_exit_codes(
    command: str,
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: each set subcommand exits 0 clean, 1 on a finding, 2 when unusable.

    Args:
        command: Subcommand under test.
        scenario: Which outcome the fakes arrange; keys `_OUTCOMES`.
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Scratch directory used as the repo root.
        capsys: Pytest capture fixture.
    """
    exit_code, verdict = _OUTCOMES[scenario]
    argv, gh = _arrange(command, scenario, monkeypatch, tmp_path)

    assert cli.main(argv, source=gh) == exit_code
    assert capsys.readouterr().out.rstrip().endswith(verdict)


def test_tracks_focus_flag_filters_and_unknown_focus_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: `tracks --focus` limits the report; an unopened focus issue exits 2."""
    issues = [
        make_open_issue(1, "a.py"),
        make_open_issue(2, "a.py", "b.py"),
        make_open_issue(3, "b.py"),
    ]
    gh = FakeGitHub(open_listing=issues, open_prs=[], permissions=PLAN_PERMISSIONS)

    assert cli.main(["tracks", "--focus", "1"], source=gh) == ps.EXIT_FINDING
    out = capsys.readouterr().out
    assert "#1 and #2" in out
    assert "#2 and #3" not in out
    assert cli.main(["tracks", "--focus", "99"], source=gh) == ps.EXIT_UNKNOWN


def test_tracks_from_pr_reads_the_wrap_up_and_excludes_focus(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: `--from-pr` focuses on a wrap-up's follow-ups; not with `--focus`."""
    issues = [make_open_issue(1, "a.py"), make_open_issue(2, "a.py")]
    wrapup = Comment(
        7,
        "verified-at: 7ab3e4e\n\n## Issue Management\n\nFollow-ups: #2\n",
        "2026-10-01T00:00:00Z",
        "maintainer",
    )
    gh = FakeGitHub(
        open_listing=issues,
        open_prs=[],
        permissions=PLAN_PERMISSIONS,
        comment_lists={60: [wrapup]},
    )

    assert cli.main(["tracks", "--from-pr", "60"], source=gh) == ps.EXIT_FINDING
    assert "overlap: #1 and #2" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["tracks", "--from-pr", "60", "--focus", "1"], source=gh)
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    ("plan_extra", "exit_code"),
    [("track: alpha", ps.EXIT_CLEAN), ("", ps.EXIT_FINDING)],
    ids=["member", "excluded"],
)
def test_track_members_exit_codes(
    plan_extra: str, exit_code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: `track-members` exits 0 when every candidate is a member, else 1.

    Args:
        plan_extra: Text appended to the labelled issue's plan.
        exit_code: Expected exit code.
        capsys: Pytest capture fixture.
    """
    issue = make_open_issue(1, "a.py", labels=("track:alpha",), plan_extra=plan_extra)
    gh = FakeGitHub(open_listing=[issue], permissions=PLAN_PERMISSIONS)

    assert cli.main(["track-members", "alpha"], source=gh) == exit_code
    assert "track-members: alpha" in capsys.readouterr().out


def test_bad_since_exits_2_before_any_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: an unparseable `--since` is a usage error (2), not a silent default."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["drift", "10", "--since", "yesterday"], source=FakeGitHub())

    assert exit_info.value.code == 2
    assert "ISO-8601" in capsys.readouterr().err
