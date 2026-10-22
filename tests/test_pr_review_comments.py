"""Tests for forge.pr_review_comments — ``forge-pr-review-comments``.

# MOCKING STRATEGY: one seam, never the network. ``gh_api`` is replaced in
# ``forge.gh_comments`` (where ``list_review_comments`` looks it up) by a
# fake that records its argv and returns canned ``--paginate`` output —
# one ``page_json`` line per page, as ``gh`` emits it.
"""

from __future__ import annotations

import json

import pytest

from forge import pr_review_comments as mod
from tests.conftest import page_json


def _comment(cid: int, reply_to: int | None = None) -> dict[str, object]:
    """Build one projected review comment.

    Args:
        cid: Comment id.
        reply_to: The thread's first comment id, or ``None`` for a
            top-level comment.

    Returns:
        A mapping shaped like ``list_review_comments``' projection.
    """
    return {
        "id": cid,
        "path": "src/x.py",
        "line": cid,
        "body": f"comment {cid}",
        "in_reply_to_id": reply_to,
    }


def _serve(monkeypatch: pytest.MonkeyPatch, raw: str | None) -> list[tuple[str, ...]]:
    """Replace ``gh_api`` with a fake returning *raw*; return its recorded argvs.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        raw: The fake's stdout, or ``None`` for a failed call.

    Returns:
        The list each call's argv is appended to.
    """
    calls: list[tuple[str, ...]] = []

    def _fake_gh_api(*args: str, **_kw: object) -> str | None:
        calls.append(args)
        return raw

    monkeypatch.setattr("forge.gh_comments.gh_api", _fake_gh_api)
    return calls


def test_list_prints_every_comment_from_every_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SCENARIO: a review spans two pages.

    MOCK SETUP: ``gh_api`` serves two page lines.

    EXPECTED BEHAVIOR: exit 0, one JSON object per comment from both
    pages, read from the review-comment endpoint with ``--paginate``.
    """
    pages = "\n".join([page_json(_comment(1), _comment(2, 1)), page_json(_comment(3))])
    calls = _serve(monkeypatch, pages)

    assert mod.main(["list", "--pr", "7"]) == 0

    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [c["id"] for c in out] == [1, 2, 3]
    assert out[1]["in_reply_to_id"] == 1
    (argv,) = calls
    assert argv[0] == "repos/{owner}/{repo}/pulls/7/comments"
    assert "--paginate" in argv


def test_unreplied_finds_thread_on_second_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SCENARIO: thread 1 is answered; thread 3, on page 2, is not.

    MOCK SETUP: ``gh_api`` serves two page lines.

    EXPECTED BEHAVIOR: exit 1 printing only id 3 — the comment a
    first-page-only read would never have seen.
    """
    pages = "\n".join([page_json(_comment(1), _comment(2, 1)), page_json(_comment(3))])
    _serve(monkeypatch, pages)

    assert mod.main(["unreplied", "--pr", "7"]) == 1
    assert capsys.readouterr().out.split() == ["3"]


def test_unreplied_exits_zero_when_every_thread_has_a_reply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SCENARIO: the reply to thread 1 sits on a later page than the thread.

    MOCK SETUP: ``gh_api`` serves two page lines.

    EXPECTED BEHAVIOR: exit 0 and nothing printed.
    """
    pages = "\n".join([page_json(_comment(1)), page_json(_comment(2, 1))])
    _serve(monkeypatch, pages)

    assert mod.main(["unreplied", "--pr", "7"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["list", "unreplied"])
def test_exits_two_when_github_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """SCENARIO: the ``gh`` call fails (auth, network, timeout).

    MOCK SETUP: ``gh_api`` returns ``None``.

    EXPECTED BEHAVIOR: exit 2 with nothing on stdout — unknown, never an
    empty review or "none unreplied".

    Args:
        command: The subcommand under test.
    """
    _serve(monkeypatch, None)

    assert mod.main([command, "--pr", "7"]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "bad_page",
    ["not json", '{"id": 2, "body": "x"}', "5", "[1, 2]", '"abc"'],
    ids=["not-json", "object-page", "scalar-page", "non-object-items", "string"],
)
def test_malformed_page_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_page: str,
) -> None:
    """SCENARIO: page 2 of the listing is not a JSON list of comment objects.

    MOCK SETUP: ``gh_api`` serves a valid page holding an unanswered
    thread followed by the malformed page.

    EXPECTED BEHAVIOR: exit 2, no traceback and no ids printed — a
    partial or garbled listing is never reported as the review.

    Args:
        bad_page: The malformed second page.
    """
    _serve(monkeypatch, page_json(_comment(1)) + "\n" + bad_page)

    assert mod.main(["unreplied", "--pr", "7"]) == 2
    assert capsys.readouterr().out == ""
