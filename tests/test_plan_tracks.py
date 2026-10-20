"""Behaviour tests for ``forge.plan_tracks`` — the set checks of ``forge-plan-check``.

# MOCKING STRATEGY:
# GitHub reads come from ``FakeGitHub`` (the Fake shared in
# ``tests/_plan_fakes.py``); base-branch history from its
# ``LogRecorder``; git's merge verdicts from ``ProbeStub``, a Fake over the
# ``(left, right) -> bool | None`` probe seam. ``merge_tree_clean`` itself
# is tested against a real repository in ``tests/test_git_utils.py``.
# Assertions read finding kinds and counts, not full prose.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forge import plan_staleness as ps
from forge import plan_tracks as pt
from forge.plan_staleness_gh import Comment, Issue
from tests._plan_fakes import (
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


def test_tracks_focus_with_nothing_in_common_says_placement_none() -> None:
    """Behavior: a focus issue sharing no file with anything ends `placement: none`."""
    gh = _chain_listing()
    assert gh.open_listing is not None
    gh.open_listing.append(make_open_issue(4, "src/z/d.py"))

    report = pt.check_tracks(gh, focus=[4])

    assert report.exit_code == ps.EXIT_CLEAN
    assert report_lines(report, "placement:") == ["placement: none"]


def test_tracks_focus_placement_none_is_withheld_when_not_established() -> None:
    """Behavior: an overlap, or a focus issue naming no file, prints no verdict."""
    overlapping = pt.check_tracks(_chain_listing(), focus=[1])
    gh = _chain_listing()
    assert gh.open_listing is not None
    gh.open_listing.append(make_open_issue(4))

    fileless = pt.check_tracks(gh, focus=[4])

    assert report_lines(overlapping, "placement:") == []
    assert report_lines(fileless, "placement:") == []
    assert any("#4 names no file" in line for line in fileless.lines)


# --------------------------------------------------------------------------
# tracks --from-pr
# --------------------------------------------------------------------------

_HEAD = "verified-at: 7ab3e4e"


def _wrapup(follow_ups: str, *, author: str = "maintainer") -> Comment:
    """Build a PR wrap-up comment whose Issue Management names *follow_ups*.

    Args:
        follow_ups: The `Follow-ups:` line's tail; empty for no line.
        author: Comment author.

    Returns:
        The comment.
    """
    line = f"Follow-ups: {follow_ups}\n" if follow_ups else ""
    body = (
        f"{_HEAD}\n\n## Design Check\nFollow-ups: #77\n\n"
        f"## Issue Management\n\nCloses #1\n{line}"
        "```\nFollow-ups: #88\n```\n\n## Code Quality\nPASS\n"
    )
    return Comment(900, body, "2026-10-01T00:00:00Z", author)


def test_from_pr_focuses_on_the_issue_management_follow_ups_only() -> None:
    """Behavior: only the Issue Management line counts; fenced and other text don't."""
    gh = _chain_listing()
    gh.comment_lists[60] = [_wrapup("#1, #99")]

    report = pt.check_tracks_from_pr(gh, 60)

    assert "follow-ups: #1 #99" in report.lines
    assert "note: #99 is not an open issue; skipped" in report.lines
    assert report_lines(report, "overlap:")[0].startswith("overlap: #1 and #2")
    assert report.exit_code == ps.EXIT_FINDING


def test_from_pr_without_follow_ups_is_placement_none_and_clean() -> None:
    """Behavior: a trusted wrap-up naming no follow-up exits 0 with the verdict."""
    gh = _chain_listing()
    gh.comment_lists[60] = [_wrapup("")]

    report = pt.check_tracks_from_pr(gh, 60)

    assert report.exit_code == ps.EXIT_CLEAN
    assert report_lines(report, "placement:") == ["placement: none"]


def test_from_pr_ignores_a_strangers_newer_wrap_up() -> None:
    """Behavior: a non-writer's wrap-up is skipped; the writer's older one is read."""
    gh = _chain_listing()
    gh.permissions = {**PLAN_PERMISSIONS, "stranger": "read"}
    newer = Comment(
        901,
        f"{_HEAD}\n\n## Issue Management\n\nFollow-ups: #3\n",
        "2026-10-02T00:00:00Z",
        "stranger",
    )
    gh.comment_lists[60] = [_wrapup("#1"), newer]

    report = pt.check_tracks_from_pr(gh, 60)

    assert "follow-ups: #1" in report.lines
    assert any("ignored wrap-up comment 901" in line for line in report.lines)


@pytest.mark.parametrize(
    "comments",
    [None, [], [_wrapup("#1", author="ghost-login")]],
    ids=["unreadable", "no-wrap-up", "author-unknown"],
)
def test_from_pr_unreadable_or_untrusted_wrap_up_is_unknown(
    comments: list[Comment] | None,
) -> None:
    """Behavior: no readable write-access wrap-up is unknown (2), never placement.

    Args:
        comments: The PR's comments (None: the read failed).
    """
    gh = _chain_listing()
    if comments is not None:
        gh.comment_lists[60] = comments

    report = pt.check_tracks_from_pr(gh, 60)

    assert report.exit_code == ps.EXIT_UNKNOWN
    assert report_lines(report, "placement:") == []


# --------------------------------------------------------------------------
# track-members
# --------------------------------------------------------------------------

_LABEL = "track:alpha"
_LINE = "track: alpha"


def _members(*issues: Issue) -> FakeGitHub:
    """Build a GitHub fake listing *issues*, `maintainer` and `alice` writers.

    Args:
        *issues: Open issues.

    Returns:
        The fake.
    """
    return FakeGitHub(
        open_listing=list(issues),
        permissions={**PLAN_PERMISSIONS, "alice": "write", "outsider": "read"},
    )


def test_track_members_member_needs_the_plan_line_and_a_writer_author() -> None:
    """Behavior: each failing labelled candidate is excluded with its reason."""
    gh = _members(
        make_open_issue(1, "a.py", labels=(_LABEL,), plan_extra=_LINE),
        make_open_issue(2, "a.py", labels=(_LABEL,)),
        make_open_issue(3, "a.py", labels=(_LABEL,), plan_extra="track: beta"),
        make_open_issue(
            4, "a.py", labels=(_LABEL,), plan_extra=_LINE, author="outsider"
        ),
    )

    report = pt.check_track_members(gh, "alpha")

    assert report_lines(report, "member:") == ["member: #1"]
    excluded = report_lines(report, "excluded:")
    assert excluded[0] == "excluded: #2 — its plan names no track"
    assert excluded[1] == "excluded: #3 — its plan names another track"
    assert "endorsement not checked" in excluded[2]
    assert report.exit_code == ps.EXIT_FINDING


def test_track_members_all_members_is_clean_and_fenced_lines_do_not_count() -> None:
    """Behavior: every candidate a member exits 0; a fenced track line is not one."""
    fenced = make_open_issue(
        2, "a.py", labels=(_LABEL,), plan_extra=f"```\n{_LINE}\n```"
    )
    clean = pt.check_track_members(
        _members(make_open_issue(1, "a.py", labels=(_LABEL,), plan_extra=_LINE)),
        "alpha",
    )
    with_fence = pt.check_track_members(_members(fenced), "alpha")

    assert clean.exit_code == ps.EXIT_CLEAN
    assert report_lines(clean, "track-members:") == [
        "track-members: alpha — 1 member(s), 0 excluded"
    ]
    assert report_lines(with_fence, "excluded:") == [
        "excluded: #2 — its plan names no track"
    ]


def test_track_members_reports_a_plan_naming_the_track_on_an_unlabelled_issue() -> None:
    """Behavior: the label is a hint only; a plan-named member lacking it is flagged."""
    gh = _members(make_open_issue(5, "a.py", plan_extra=_LINE))

    report = pt.check_track_members(gh, "alpha")

    assert report_lines(report, "member:") == ["member: #5"]
    assert report_lines(report, "unlabelled:")[0].startswith("unlabelled: #5")
    assert report.exit_code == ps.EXIT_FINDING


def test_track_members_a_strangers_plan_comment_makes_no_member() -> None:
    """Behavior: a track line a non-writer typed on an unlabelled issue is ignored."""
    forged = Comment(
        1, f"{ps.PLAN_MARKER}\n{_LINE}\n", "2026-10-01T00:00:00Z", "outsider"
    )
    issue = Issue(6, "", (), "2026-09-10T07:00:00Z", (forged,), author="alice")

    report = pt.check_track_members(_members(issue), "alpha")

    assert report_lines(report, "member:") == []
    assert report.exit_code == ps.EXIT_UNKNOWN


@pytest.mark.parametrize(
    ("name", "listing"),
    [("Alpha", True), ("-x", True), ("alpha", False), ("alpha", True)],
    ids=["uppercase", "dash-first", "unreadable", "no-candidate"],
)
def test_track_members_refuses_bad_names_unreadable_listings_and_empty_tracks(
    name: str, *, listing: bool
) -> None:
    """Behavior: a malformed name, failed read or empty track exits 2.

    Args:
        name: Track name asked about.
        listing: Whether the open-issue listing is readable.
    """
    gh = _members(make_open_issue(1, "a.py"))
    if not listing:
        gh.open_listing = None

    report = pt.check_track_members(gh, name)

    assert report.exit_code == ps.EXIT_UNKNOWN
    assert report_lines(report, "member:") == []


def test_track_members_unknown_author_access_is_unknown() -> None:
    """Behavior: an author whose access lookup fails is unknown, never excluded."""
    gh = _members(
        make_open_issue(1, "a.py", labels=(_LABEL,), plan_extra=_LINE, author="bob")
    )

    report = pt.check_track_members(gh, "alpha")

    assert report.exit_code == ps.EXIT_UNKNOWN
    assert report_lines(report, "excluded:") == []


def test_track_labels_must_be_lowercase_to_be_echoed() -> None:
    """Behavior: a `track:` label outside the documented name rule is never shown."""
    gh = FakeGitHub(
        open_listing=[
            make_open_issue(1, "a.py", labels=("track:Alpha",)),
            make_open_issue(2, "a.py", labels=("track:alpha",)),
        ],
        open_prs=[],
        permissions=PLAN_PERMISSIONS,
    )

    report = pt.check_tracks(gh)

    assert report_lines(report, "track: #1") == []
    assert report_lines(report, "track: #2") == ["track: #2 carries track:alpha"]
    assert any("#1 carries 1 track label(s)" in line for line in report.lines)


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


def test_merge_order_shared_generated_artifacts_do_not_entangle() -> None:
    """Behavior: two PRs sharing only a regenerated file are independent.

    A conflict there still counts: the verdict is git's, not the file list's.
    """
    generated = "docs/api-digest.md"
    clean = pt.check_merge_order(
        [make_open_pr(1, generated), make_open_pr(2, generated)], BASE, ProbeStub()
    )
    conflicting = pt.check_merge_order(
        [make_open_pr(1, generated), make_open_pr(2, generated)],
        BASE,
        ProbeStub({(pr_head(1), pr_head(2)): False}),
    )

    assert not any("refresh after" in line for line in clean.lines)
    assert report_lines(clean, "shared:") == []
    assert conflicting.exit_code == ps.EXIT_FINDING


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
