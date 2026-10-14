"""Behaviour tests for ``forge-backlog-index`` (``forge.backlog_index``).

# MOCKING STRATEGY:
# The render and run paths read GitHub only through ``BacklogSource``, so
# ``FakeBacklog`` stands in for it from in-memory records (``None`` is a
# failed read). The Index writer is the ``write`` callable ``run`` takes;
# ``Writes`` records its calls instead of shelling out to ``gh``. Output is
# read with ``capsys`` because ``emit`` writes to stdout. No ``Mock`` is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from forge import backlog_index as bi
from forge import plan_staleness as ps
from forge.plan_staleness_gh import Comment, Issue


if TYPE_CHECKING:
    from collections.abc import Sequence


TODAY = "2026-10-14"
PLAN_TIME = "2026-09-14T20:00:00Z"
LONG_TITLE = "x" * 200


@dataclass
class FakeBacklog:
    """In-memory ``BacklogSource``; a ``None`` record is a failed read."""

    issues: list[Issue] | None = field(default_factory=list)
    count: int | None = None
    slug: str | None = None
    permissions: dict[str, str] = field(default_factory=dict)

    def open_issues(self) -> list[Issue] | None:
        """Return the recorded listing.

        Returns:
            The recorded issues, or None for a failed read.
        """
        return self.issues

    def open_issue_count(self) -> int | None:
        """Return the recorded total, defaulting to the listing length.

        Returns:
            The total, or None for a failed read.
        """
        if self.count is None and self.issues is not None:
            return len(self.issues)
        return self.count

    def repo_slug(self) -> str | None:
        """Return the recorded slug.

        Returns:
            The slug, or None when unrecorded.
        """
        return self.slug

    def permission(self, login: str) -> str | None:
        """Return the recorded permission.

        Args:
            login: GitHub login.

        Returns:
            The permission, or None when unrecorded.
        """
        return self.permissions.get(login)


@dataclass
class Writes:
    """A recording Index writer returning a fixed exit code."""

    rc: int = 0
    calls: list[tuple[int, str]] = field(default_factory=list)

    def __call__(self, number: int, body: str) -> int:
        """Record the write.

        Args:
            number: Issue number written to.
            body: Body written.

        Returns:
            The configured exit code.
        """
        self.calls.append((number, body))
        return self.rc


def _issue(
    number: int,
    title: str = "t",
    *labels: str,
    updated: str = "2026-10-01T00:00:00Z",
    body: str = "",
    comments: Sequence[Comment] = (),
) -> Issue:
    """Build a listed issue.

    Args:
        number: Issue number.
        title: Issue title.
        *labels: Label names.
        updated: Last-activity timestamp.
        body: Issue body.
        comments: Issue comments.

    Returns:
        The issue.
    """
    return Issue(
        number,
        body,
        labels,
        "2026-09-10T07:00:00Z",
        tuple(comments),
        title,
        updated,
    )


def _index(number: int = 1) -> Issue:
    """Build the Index issue itself.

    Args:
        number: Issue number.

    Returns:
        An issue titled :data:`bi.INDEX_TITLE`.
    """
    return _issue(number, bi.INDEX_TITLE)


def _inputs(**kwargs: object) -> bi.IndexInputs:
    """Build render inputs dated :data:`TODAY`.

    Args:
        **kwargs: ``IndexInputs`` overrides.

    Returns:
        The inputs.
    """
    return bi.IndexInputs(today=TODAY, **kwargs)  # type: ignore[arg-type]


def _run(source: FakeBacklog, write: Writes, **opts: object) -> int:
    """Run the Index against *source*.

    Args:
        source: The fake backlog.
        write: Recording writer.
        **opts: ``RunOptions`` overrides; ``inputs`` defaults to :func:`_inputs`.

    Returns:
        The exit code.
    """
    opts.setdefault("inputs", _inputs())
    return bi.run(source, bi.RunOptions(**opts), write)  # type: ignore[arg-type]


def _body(issues: Sequence[Issue], **kwargs: object) -> str:
    """Render *issues* and return the body.

    Args:
        issues: Issues to list.
        **kwargs: ``IndexInputs`` overrides.

    Returns:
        The rendered body.
    """
    rendered = bi.render(issues, _inputs(**kwargs))
    assert rendered is not None
    return rendered.body


def _line_of(body: str, number: int) -> str:
    """Return the first body line starting with issue *number*'s head.

    Args:
        body: Rendered body.
        number: Issue number.

    Returns:
        The line.
    """
    return next(ln for ln in body.splitlines() if ln.startswith(f"- #{number} "))


def test_tier_line_omits_stated_labels_keeps_type_and_activity() -> None:
    """Behavior: a tier line omits labels the page already states."""
    body = _body(
        [
            _issue(2, "a", "tier-2-high", "bug", updated="2026-10-03T09:00:00Z"),
            _issue(3, "b", "tier-2-high"),
            _issue(4, "c", "tier-2-high", updated=""),
        ]
    )
    line = _line_of(body, 2)
    assert "`bug`" in line
    assert "tier-2-high" not in line
    assert line.endswith("_activity: 2026-10-03_")
    assert _line_of(body, 4).endswith("_activity: unknown_")


def test_exclusive_lane_issue_listed_only_in_its_lane() -> None:
    """Behavior: a laned issue leaves the tier sections and the tier count."""
    lane = bi.Lane("research", "🔬 Research")
    body = _body(
        [
            _issue(2, "laned", "tier-3-standard", "research"),
            _issue(3, "plain", "tier-3-standard"),
        ],
        lanes=(lane,),
    )
    tier, _, rest = body.partition("## 🔬 Research")
    assert "## 📋 Tier 3 — Standard (1)" in tier
    assert "- #2 " not in tier
    assert "- #3 " in tier
    assert "- #2 " in rest


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        (_issue(2, "p", "tier-1-critical", "plan-ready"), "validated: unknown"),
        (
            _issue(3, "b", "tier-1-critical", "blocked", body="Requires: #7"),
            "blocker: #7",
        ),
        (_issue(4, "w", "tier-1-critical", "waiting-upstream"), "blocker: external"),
        (
            _issue(5, "e", "tier-1-critical", "needs-endorsement"),
            "## 🤝 Needs Endorsement (1)",
        ),
        (_issue(6, "u"), "## 🆕 Needs Triage (1)"),
    ],
    ids=["plan-ready", "blocked", "waiting-upstream", "endorsement", "untiered"],
)
def test_status_lanes_and_untiered(issue: Issue, expected: str) -> None:
    """Behavior: each status label lands in its lane with the lane's detail.

    Args:
        issue: Issue carrying the status label under test.
        expected: Text the rendered body must contain.
    """
    assert expected in _body([issue])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@octocat hi", "@\u200boctocat hi"),
        ("fixes #12", "fixes #\u200b12"),
        ("[x](http://e)", "[x]\u200b(http://e)"),
        ("a\nb  c", "a b c"),
    ],
)
def test_titles_are_neutralised(raw: str, expected: str) -> None:
    """Behavior: hostile titles cannot ping, reference, link or break lines.

    Args:
        raw: Title as an issue author wrote it.
        expected: The neutralised title.
    """
    assert bi.safe_title(raw) == expected
    assert f"- #2 — {expected}" in _body([_issue(2, raw, "tier-1-critical")])


@pytest.mark.parametrize(
    ("author", "permissions", "expected"),
    [
        ("maintainer", {"maintainer": "write"}, "_validated: 2026-09-14_"),
        ("stranger", {"stranger": "read"}, "_validated: unknown_"),
        ("ghost", {}, "_validated: unknown_"),
    ],
    ids=["writer", "non-writer", "unresolved"],
)
def test_plan_ready_date_from_authenticated_plan(
    author: str,
    permissions: dict[str, str],
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: only a write-access author's plan dates the validation.

    Args:
        author: Login that wrote the plan comment.
        permissions: Login-to-permission map the fake source answers from.
        expected: Validation text the rendered body must contain.
    """
    plan = Comment(1, f"{ps.PLAN_MARKER}\n\nplan", PLAN_TIME, author)
    source = FakeBacklog(
        issues=[
            _index(),
            _issue(2, "p", "tier-1-critical", "plan-ready", comments=[plan]),
        ],
        permissions=permissions,
    )
    writes = Writes()
    assert _run(source, writes, dry_run=True) == bi.EXIT_OK
    assert expected in capsys.readouterr().out
    assert writes.calls == []


def test_recheck_rendering() -> None:
    """Behavior: a mapped recheck names the PR; a bare label lists the head alone."""
    body = _body(
        [
            _issue(5, "mapped", "tier-2-high"),
            _issue(6, "labelled", "tier-2-high", "needs-recheck"),
        ],
        recheck={5: 700},
    )
    assert "- #5 — mapped — _touched by PR #700_" in body
    recheck_section = body.partition("## 🔁 Needs Recheck")[2].partition("## 🤝")[0]
    assert "- #6 — labelled\n" in recheck_section


@pytest.mark.parametrize(
    ("source", "code", "expected"),
    [
        (FakeBacklog(issues=None), bi.EXIT_UNREADABLE, "unknown: could not read"),
        (
            FakeBacklog(issues=[_index(), _issue(2)], count=3),
            bi.EXIT_REFUSED,
            "refused: the listing returned 2 of 3",
        ),
    ],
    ids=["issues-unreadable", "short-listing"],
)
def test_run_refuses_short_listing_and_never_writes(
    source: FakeBacklog, code: int, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: an unreadable or short listing never reaches the writer.

    Args:
        source: Fake backlog whose listing is unreadable or short.
        code: Exit code the run must return.
        expected: Text the run must print.
    """
    writes = Writes()
    assert _run(source, writes, dry_run=True) == code
    assert expected in capsys.readouterr().out
    assert writes.calls == []


def test_run_unreadable_count_is_exit_two(capsys: pytest.CaptureFixture[str]) -> None:
    """Behavior: a total that cannot be read is GitHub-unreadable, not a refusal."""

    class NoCount(FakeBacklog):
        """Stand-in whose total cannot be read."""

        def open_issue_count(self) -> int | None:
            """Return None, as if the count query failed."""
            return None

    writes = Writes()
    assert _run(NoCount(issues=[_index()]), writes) == bi.EXIT_UNREADABLE
    assert "unknown" in capsys.readouterr().out
    assert writes.calls == []


@pytest.mark.parametrize(
    ("issues", "named"),
    [
        ([_issue(2)], "none"),
        ([_index(1), _index(4), _issue(2)], "#1, #4"),
    ],
    ids=["zero", "two"],
)
def test_run_requires_exactly_one_index(
    issues: list[Issue], named: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: zero or several Index issues refuse and name what was found.

    Args:
        issues: Open issues the fake source lists.
        named: Description of the found Index issues the refusal must print.
    """
    writes = Writes()
    assert _run(FakeBacklog(issues=issues), writes) == bi.EXIT_REFUSED
    assert named in capsys.readouterr().out
    assert writes.calls == []


def test_run_writes_to_the_index_and_excludes_it_from_its_own_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: the body goes to the Index issue and never lists it."""
    source = FakeBacklog(
        issues=[_index(), _issue(2, "a", "tier-1-critical"), _issue(3, "b")]
    )
    writes = Writes()
    assert _run(source, writes) == bi.EXIT_OK
    assert len(writes.calls) == 1
    number, body = writes.calls[0]
    assert number == 1
    assert "#1 —" not in body
    assert capsys.readouterr().out.startswith("wrote: Index #1")


def test_dry_run_prints_body_and_size_without_writing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: --dry-run shows the page and its size, and writes nothing."""
    source = FakeBacklog(issues=[_index(), _issue(2, "a", "tier-1-critical")])
    writes = Writes()
    assert _run(source, writes, dry_run=True) == bi.EXIT_OK
    out = capsys.readouterr().out
    assert "- #2 — a" in out
    assert f"/{bi.BODY_LIMIT}" in out.rsplit("chars: ", 1)[1]
    assert "trimmed:" not in out
    assert writes.calls == []


def test_run_refuses_when_write_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """Behavior: a failed write is a refusal that names the Index."""
    writes = Writes(rc=1)
    assert _run(FakeBacklog(issues=[_index()]), writes) == bi.EXIT_REFUSED
    assert "writing Index" in capsys.readouterr().out


def test_empty_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavior: a blank rendered body is never written over the old Index."""
    monkeypatch.setattr(
        bi, "render", lambda *_a, **_k: bi.Rendered(body="  ", trimmed={})
    )
    writes = Writes()
    assert _run(FakeBacklog(issues=[_index()]), writes) == bi.EXIT_REFUSED
    assert writes.calls == []


def _trim_fixture() -> list[Issue]:
    """Return three tier-4 and two tier-3 issues with long titles.

    Returns:
        The issues; activity rises with the issue number.
    """
    return [
        *(
            _issue(n, LONG_TITLE, "tier-4-low", updated=f"2026-10-0{n - 9}T00:00:00Z")
            for n in (10, 11, 12)
        ),
        *(
            _issue(
                n, LONG_TITLE, "tier-3-standard", updated=f"2026-10-0{n - 19}T00:00:00Z"
            )
            for n in (20, 21)
        ),
        _issue(1, "keep", "tier-1-critical"),
    ]


def test_trimming_drops_tier4_then_tier3_oldest_first() -> None:
    """Behavior: an oversized body sheds tier 4 then tier 3, oldest activity first."""
    issues = _trim_fixture()
    full = len(_body(issues))

    partial_max = full - len(LONG_TITLE) // 2
    partial = bi.render(issues, _inputs(), max_chars=partial_max)
    assert partial is not None
    assert partial.trimmed == {"tier-4-low": 1}
    assert len(partial.body) <= partial_max
    assert "- #10 " not in partial.body
    assert "- #12 " in partial.body
    assert "## 🌱 Tier 4 — Low Priority (3)" in partial.body
    assert "…and 1 more" in partial.body
    assert "- #20 " in partial.body
    assert "- #21 " in partial.body

    tight_max = full - 5 * len(LONG_TITLE)
    tight = bi.render(issues, _inputs(), max_chars=tight_max)
    assert tight is not None
    assert set(tight.trimmed) == {"tier-4-low", "tier-3-standard"}
    assert len(tight.body) <= tight_max
    assert "gh issue list --label tier-4-low" in tight.body

    linked = bi.render(issues, _inputs(slug="o/r"), max_chars=partial_max)
    assert linked is not None
    assert "[full list](https://github.com/o/r/issues?q=" in linked.body


def test_trimming_reports_in_run_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Behavior: the run output tells the caller which tiers were shortened."""
    issues = _trim_fixture()
    full = len(_body(issues))
    source = FakeBacklog(issues=[_index(), *issues])
    writes = Writes()
    assert (
        _run(source, writes, dry_run=True, max_chars=full - len(LONG_TITLE) // 2)
        == bi.EXIT_OK
    )
    assert "trimmed: tier-4-low 1" in capsys.readouterr().out


def test_refuses_when_tiers_one_and_two_alone_exceed_limit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: tiers 1-2 are never trimmed; an impossible fit leaves the old Index."""
    issues = [_issue(2, LONG_TITLE, "tier-1-critical")]
    assert bi.render(issues, _inputs(), max_chars=50) is None
    writes = Writes()
    assert (
        _run(FakeBacklog(issues=[_index(), *issues]), writes, max_chars=50)
        == bi.EXIT_REFUSED
    )
    assert "tiers 1\N{EN DASH}2 alone" in capsys.readouterr().out
    assert writes.calls == []


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--lane", "a=B c"], "## B c (1)"),
        (["--recheck", "5=700"], "touched by PR #700"),
        (["--recheck", "#5=#700"], "touched by PR #700"),
    ],
)
def test_cli_accepts_lane_and_recheck_forms(
    argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: the documented --lane and --recheck forms parse and shape the page.

    Args:
        argv: Command-line options under test.
        expected: Text the rendered body must contain.
    """
    source = FakeBacklog(issues=[_index(), _issue(5, "t", "tier-2-high", "a")])
    writes = Writes()
    assert bi.main([*argv, "--dry-run"], source=source, write=writes) == bi.EXIT_OK
    assert expected in capsys.readouterr().out
    assert writes.calls == []


@pytest.mark.parametrize(
    "argv",
    [["--lane", "noequals"], ["--recheck", "x=y"], ["--recheck", "5"]],
)
def test_cli_rejects_malformed_pairs(argv: list[str]) -> None:
    """Behavior: a malformed pair is a usage error, not a silent default.

    Args:
        argv: Malformed command-line options.
    """
    with pytest.raises(SystemExit) as excinfo:
        bi.main([*argv, "--dry-run"], source=FakeBacklog(), write=Writes())
    assert excinfo.value.code == 2
