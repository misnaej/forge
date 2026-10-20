"""Behaviour tests for ``forge.plan_tracks`` — the set checks of ``forge-plan-check``.

# MOCKING STRATEGY:
# GitHub reads come from ``FakeGitHub`` (the Fake shared with
# ``tests/conftest.py``); base-branch history from its
# ``LogRecorder``; git's merge verdicts from ``ProbeStub``, a Fake over the
# ``(left, right) -> bool | None`` probe seam. ``merge_tree_clean`` itself
# is tested against a real repository in ``tests/test_git_utils.py``.
# Assertions read finding kinds and counts, not full prose.
"""

from __future__ import annotations

from datetime import UTC, datetime

from forge import plan_staleness as ps
from forge import plan_tracks as pt
from tests.conftest import (
    PLAN_PERMISSIONS,
    FakeGitHub,
    LogRecorder,
    ProbeStub,
    make_open_issue,
    make_open_pr,
    pr_head,
    report_lines,
)


BASE = "origin/main"

# --------------------------------------------------------------------------
# tracks
# --------------------------------------------------------------------------


def test_tracks_reports_shared_files_pairs_and_labels_but_nothing_for_a_loner() -> None:
    """Behavior: two issues naming one file are paired and listed with their label.

    The disjoint third issue produces no finding and is not named in any
    shared-file line.
    """
    gh = FakeGitHub(
        open_listing=[
            make_open_issue(1, "src/x/a.py", labels=("track:alpha",)),
            make_open_issue(2, "src/x/a.py", "src/x/b.py"),
            make_open_issue(3, "src/y/c.py"),
        ],
        open_prs=[],
        permissions=PLAN_PERMISSIONS,
    )

    report = pt.check_tracks(gh)

    assert report.exit_code == ps.EXIT_FINDING
    assert report.findings == 1
    assert report_lines(report, "overlap:")[0].startswith("overlap: #1 and #2")
    shared = report_lines(report, "shared:")
    assert len(shared) == 1
    assert "src/x/a.py" in shared[0]
    assert "track:alpha" in shared[0]
    assert "#3" not in shared[0]


def test_tracks_open_pr_on_an_issues_file_is_a_collision_unless_it_delivers_it() -> (
    None
):
    """Behavior: an open PR touching an issue's file collides unless it delivers it."""
    gh = FakeGitHub(
        open_listing=[
            make_open_issue(1, "src/x/a.py"),
            make_open_issue(2, "src/x/b.py"),
        ],
        open_prs=[make_open_pr(50, "src/x/a.py", "src/x/b.py", closes=(2,))],
        permissions=PLAN_PERMISSIONS,
    )

    report = pt.check_tracks(gh)

    assert report.findings == 1
    assert "PR #50" in report_lines(report, "pr-overlap:")[0]
    assert "#1" in report_lines(report, "pr-overlap:")[0]
    assert any("PR #50 delivers #2" in line for line in report.lines)


def test_tracks_unreadable_listing_is_unknown() -> None:
    """Behavior: a failed GitHub listing is unknown, never an empty (clean) backlog."""
    report = pt.check_tracks(FakeGitHub(open_listing=[], open_prs=None))
    assert report.exit_code == ps.EXIT_UNKNOWN


def _chain_listing() -> FakeGitHub:
    """Build a backlog where #1/#2 share one file and #2/#3 share another.

    Returns:
        The GitHub fake, with one open PR touching both shared files.
    """
    return FakeGitHub(
        open_listing=[
            make_open_issue(1, "src/x/a.py"),
            make_open_issue(2, "src/x/a.py", "src/x/b.py"),
            make_open_issue(3, "src/x/b.py"),
        ],
        open_prs=[make_open_pr(50, "src/x/a.py", "src/x/b.py")],
        permissions=PLAN_PERMISSIONS,
    )


def test_tracks_focus_reports_only_lines_involving_the_focused_issue() -> None:
    """Behavior: focus on #1 keeps its overlap, shared file and PR line, drops #2/#3."""
    report = pt.check_tracks(_chain_listing(), focus=[1])

    overlaps = report_lines(report, "overlap:")
    assert len(overlaps) == 1
    assert overlaps[0].startswith("overlap: #1 and #2")
    shared = report_lines(report, "shared:")
    assert len(shared) == 1
    assert "src/x/a.py" in shared[0]
    pr_lines = report_lines(report, "pr-overlap:")
    assert len(pr_lines) == 1
    assert "named by #1" in pr_lines[0]
    assert report.exit_code == ps.EXIT_FINDING


def test_tracks_focus_on_an_issue_that_is_not_open_is_refused() -> None:
    """Behavior: a focus number outside the open listing is unknown, never clean."""
    report = pt.check_tracks(_chain_listing(), focus=[99])

    assert report.exit_code == ps.EXIT_UNKNOWN
    assert "refused: #99 is not an open issue" in report.lines
    assert report_lines(report, "overlap:") == []


# --------------------------------------------------------------------------
# current
# --------------------------------------------------------------------------


def test_current_lists_history_once_and_flags_only_the_drifted_issue() -> None:
    """Behavior: one batched history read; a merge on one issue's file flags only it."""
    gh = FakeGitHub(
        issues={
            10: make_open_issue(10, "src/x/a.py"),
            11: make_open_issue(11, "src/y/b.py"),
        },
        comment_lists={
            10: list(make_open_issue(10, "src/x/a.py").comments),
            11: list(make_open_issue(11, "src/y/b.py").comments),
        },
        permissions=PLAN_PERMISSIONS,
    )
    landed = ps.Merge(
        "a" * 40, datetime(2026, 9, 12, 12, tzinfo=UTC), 5, ("src/x/a.py",)
    )
    history = LogRecorder([landed])

    report = pt.check_current(gh, [10, 11], log_merges=history)

    assert len(history.seen) == 1
    assert sorted(history.seen[0]) == ["src/x/a.py", "src/y/b.py"]
    assert report.findings == 1
    assert report.exit_code == ps.EXIT_FINDING
    assert report_lines(report, "current: #10")[0].endswith("re-check it")
    assert "no merge touched" in report_lines(report, "current: #11")[0]


# --------------------------------------------------------------------------
# merge-order
# --------------------------------------------------------------------------


def test_merge_order_conflicting_pair_is_a_finding_and_the_later_pr_refreshes() -> None:
    """Behavior: a conflict between two PRs is a finding; the later refreshes after."""
    probe = ProbeStub({(pr_head(1), pr_head(2)): False})

    report = pt.check_merge_order(
        [make_open_pr(1, "a.py"), make_open_pr(2, "b.py")], BASE, probe
    )

    assert report.findings == 1
    assert report.exit_code == ps.EXIT_FINDING
    first, second = report_lines(report, "order:")
    assert "refresh after" not in first
    assert "PR #2" in second
    assert "refresh after PR #1 (conflict)" in second


def test_merge_order_unprobeable_pair_is_unknown_never_clean() -> None:
    """Behavior: a probe answering None makes the run unknown, never clean."""
    probe = ProbeStub({(pr_head(1), pr_head(2)): None})

    report = pt.check_merge_order(
        [make_open_pr(1, "a.py"), make_open_pr(2, "b.py")], BASE, probe
    )

    assert report.exit_code == ps.EXIT_UNKNOWN
    assert report.findings == 0
    assert "refresh after PR #1 (unknown)" in report_lines(report, "order:")[1]


def test_merge_order_clean_merge_over_shared_files_still_entangles() -> None:
    """Behavior: shared files order PRs without a finding; disjoint PRs do not."""
    sharing = pt.check_merge_order(
        [make_open_pr(1, "a.py"), make_open_pr(2, "a.py")], BASE, ProbeStub()
    )
    disjoint = pt.check_merge_order(
        [make_open_pr(1, "a.py"), make_open_pr(2, "b.py")], BASE, ProbeStub()
    )

    assert sharing.exit_code == ps.EXIT_CLEAN
    assert "refresh after PR #1 (shared files)" in report_lines(sharing, "order:")[1]
    assert disjoint.exit_code == ps.EXIT_CLEAN
    assert not any("refresh after" in line for line in disjoint.lines)


def test_merge_order_conflict_with_base_is_a_finding_naming_the_remedy() -> None:
    """Behavior: a base conflict is a finding whose remedy is a base merge."""
    probe = ProbeStub({(BASE, pr_head(1)): False})

    report = pt.check_merge_order([make_open_pr(1, "a.py")], BASE, probe)

    assert report.findings == 1
    assert f"git merge {BASE}" in report_lines(report, "conflict:")[0]


def test_merge_order_cap_probes_only_the_lowest_numbered_prs_and_says_so() -> None:
    """Behavior: past the cap a finding names the cut; no probe touches a skipped PR."""
    probe = ProbeStub()
    prs = [make_open_pr(3, "c.py"), make_open_pr(1, "a.py"), make_open_pr(2, "b.py")]

    report = pt.check_merge_order(prs, BASE, probe, cap=2)

    cap_line = report_lines(report, "cap:")[0]
    assert "3 PRs" in cap_line
    assert "cap of 2" in cap_line
    assert report.exit_code == ps.EXIT_FINDING
    assert all(pr_head(3) not in call for call in probe.calls)


def test_merge_order_malformed_head_is_unknown_and_not_probed() -> None:
    """Behavior: a PR without a full commit SHA is reported and never reaches git."""
    probe = ProbeStub()

    report = pt.check_merge_order(
        [make_open_pr(1, "a.py", head="--output=x")], BASE, probe
    )

    assert report.exit_code == ps.EXIT_UNKNOWN
    assert probe.calls == []
