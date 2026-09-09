"""Unit tests for forge.gh_comments — the shared PR-comment plumbing.

# MOCKING STRATEGY: two seams, never the network. ``gh_api`` is patched in
# the module namespace to serve canned GitHub listings (the reads);
# ``subprocess.run`` is patched (via the shared ``subprocess`` module
# object — see ``tests/test_pr_squash_comment.py``'s module docstring) to
# capture argv and inputs for the writes (post, delete, patch). Callers of
# this module (``pr_squash_comment``, ``pr_wrapup``) each keep only a thin
# delegation test — the paging/filtering/attribution contract lives here.
"""

from __future__ import annotations

import logging

import pytest

from forge import gh_comments as mod
from tests.conftest import CapturedCalls, FakeProc, make_fake_run, page_json


VALID_TEXT_LINES = [
    "feat(#42): example squash title",
    "bullet alpha description",
    "bullet beta description",
    "bullet gamma description",
]


def _with_extra(blob: str) -> str:
    """Join the baseline lines with *blob* appended.

    Mirrors how a real caller (``pr_squash_comment``, ``pr_wrapup``)
    assembles the text it validates — a title/body join with one extra
    line under test.

    Args:
        blob: The line under test.

    Returns:
        The joined text.
    """
    return "\n".join([*VALID_TEXT_LINES, blob])


# ---------------------------------------------------------------------------
# AI attribution validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blob",
    [
        "Generated with Claude",
        "Co-authored-by: Claude <noreply@anthropic.com>",
        "🤖",
        "AI-generated info",
        "Assisted by AI on this PR",
        "Paired with Claude Code today",
        "This commit was authored by Claude",
    ],
)
def test_validate_no_ai_attribution_rejects_known_patterns(blob: str) -> None:
    """Any AI-attribution phrase anywhere in the text raises via the phrase layer.

    Args:
        blob: A string containing a known AI-attribution phrase.
    """
    with pytest.raises(mod.ValidationError, match="pattern detected"):
        mod.validate_no_ai_attribution(_with_extra(blob))


@pytest.mark.parametrize(
    "blob",
    [
        "See CLAUDE.md for the exact policy",
        "Path is .claude/settings.json",
        "Regenerate `CLAUDE.md`.",
        "Wrappers live under .claude",
        "Hooks live in claude-hooks/block_claude_attribution.sh",
        "Config lives in anthropic.yml",
    ],
)
def test_validate_no_ai_attribution_accepts_path_shaped_mentions(blob: str) -> None:
    """A path- or filename-shaped mention of a vendor term does not raise.

    Args:
        blob: A string containing a path- or filename-shaped vendor mention.
    """
    mod.validate_no_ai_attribution(_with_extra(blob))


@pytest.mark.parametrize(
    "blob",
    [
        "Thanks Claude.",
        "(Claude)",
        "Built with Anthropic",
        "Thanks Anthropic!",
        "See generated-with/claude for context",
        "Made-by.claude helped here",
        "credit/anthropic.ai assisted",
        "Refactored via Claude.ai suggestions",
        "Built with Anthropic.Claude",
        "This was co.authored.by.Claude",
    ],
)
def test_validate_no_ai_attribution_rejects_bare_vendor_mentions(blob: str) -> None:
    """A bare (non-path-shaped) vendor mention raises via the token backstop.

    None of these match an :data:`AI_ATTRIBUTION_PATTERNS` phrase — the
    backstop's ``(in '<token>')`` message detail distinguishes the layer.

    Args:
        blob: A string containing a bare AI-vendor mention.
    """
    with pytest.raises(mod.ValidationError, match=r"\(in '"):
        mod.validate_no_ai_attribution(_with_extra(blob))


# ---------------------------------------------------------------------------
# Comment listing
# ---------------------------------------------------------------------------

MARKER = "<!-- test:marker -->"

MARKED_COMMENT = {
    "id": 555,
    "body": f"{MARKER}\nold body",
    "created_at": "2026-01-01T00:00:00Z",
    "author": "octocat",
}

UNRELATED_COMMENT = {
    "id": 777,
    "body": "a human wrote this",
    "created_at": "2026-01-02T00:00:00Z",
    "author": "octocat",
}


def test_list_marker_comments_keeps_only_marked_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: a PR carries one marker-carrying comment among human ones.

    MOCK SETUP: ``gh_api`` returns a single page holding both; ``own_login``
    reports the identity both fixture comments already carry.
    EXPECTED BEHAVIOR: only the marker-carrying comment comes back.
    """
    monkeypatch.setattr(mod, "own_login", lambda: "octocat")
    monkeypatch.setattr(
        mod, "gh_api", lambda *_a, **_kw: page_json(MARKED_COMMENT, UNRELATED_COMMENT)
    )
    found = mod.list_marker_comments(61, MARKER)
    assert found is not None
    assert [c["id"] for c in found] == [555]


def test_list_marker_comments_spans_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCENARIO: ``--paginate`` emits one JSON array per page.

    MOCK SETUP: ``gh_api`` returns two array lines, one marked comment each;
    ``own_login`` reports the identity both pages' comments carry.
    EXPECTED BEHAVIOR: both pages are flattened into one ordered list.
    """
    monkeypatch.setattr(mod, "own_login", lambda: "octocat")
    second = {**MARKED_COMMENT, "id": 556, "created_at": "2026-01-03T00:00:00Z"}
    monkeypatch.setattr(
        mod,
        "gh_api",
        lambda *_a, **_kw: f"{page_json(MARKED_COMMENT)}\n{page_json(second)}",
    )
    found = mod.list_marker_comments(61, MARKER)
    assert found is not None
    assert [c["id"] for c in found] == [555, 556]


def test_list_marker_comments_reports_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed listing returns None — distinct from 'no marked comment'."""
    monkeypatch.setattr(mod, "own_login", lambda: "octocat")
    monkeypatch.setattr(mod, "gh_api", lambda *_a, **_kw: None)
    assert mod.list_marker_comments(61, MARKER) is None


def test_list_marker_comments_skips_foreign_author_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: a stranger plants the marker on a public PR.

    MOCK SETUP: ``own_login`` reports ``"octocat"``; the page holds a
    marker-carrying comment authored by someone else.
    EXPECTED BEHAVIOR: the foreign comment is excluded and a warning names
    it — callers must never mutate a comment they didn't post.
    """
    monkeypatch.setattr(mod, "own_login", lambda: "octocat")
    foreign = {**MARKED_COMMENT, "id": 999, "author": "impersonator"}
    monkeypatch.setattr(mod, "gh_api", lambda *_a, **_kw: page_json(foreign))
    with caplog.at_level(logging.WARNING):
        found = mod.list_marker_comments(61, MARKER)
    assert found == []
    assert "999" in caplog.text
    assert "impersonator" in caplog.text


def test_list_marker_comments_returns_none_when_identity_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: ``gh`` cannot report who it's authenticated as.

    MOCK SETUP: ``own_login`` returns ``None``; ``gh_api`` is a recording
    fake that fails the test if it's ever called.
    EXPECTED BEHAVIOR: the listing call is skipped entirely and ``None``
    comes back — "cannot verify" must short-circuit before any network call.
    """
    monkeypatch.setattr(mod, "own_login", lambda: None)

    def _unexpected_gh_api(*_a: object, **_kw: object) -> str:
        pytest.fail("gh_api must not be called when the identity is unknown")

    monkeypatch.setattr(mod, "gh_api", _unexpected_gh_api)
    assert mod.list_marker_comments(61, MARKER) is None


# ---------------------------------------------------------------------------
# parse_paged_json
# ---------------------------------------------------------------------------


def test_parse_paged_json_flattens_pages_in_order() -> None:
    """Two page lines concatenate into one ordered list."""
    raw = f"{page_json({'id': 1})}\n{page_json({'id': 2}, {'id': 3})}"
    assert mod.parse_paged_json(raw) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_parse_paged_json_skips_malformed_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-JSON line is skipped with a warning; the good pages still parse."""
    raw = f"{page_json({'id': 1})}\nnot json\n{page_json({'id': 2})}"
    with caplog.at_level(logging.WARNING):
        items = mod.parse_paged_json(raw)
    assert items == [{"id": 1}, {"id": 2}]
    assert "unparseable" in caplog.text


# ---------------------------------------------------------------------------
# post_new_comment
# ---------------------------------------------------------------------------


def test_post_new_comment_argv_shape_and_rc_pass_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comment is posted via stdin, and the gh exit code passes through."""
    captured = CapturedCalls()
    monkeypatch.setattr(
        "forge.gh_comments.subprocess.run",
        make_fake_run(returncode=7, captured=captured),
    )
    assert mod.post_new_comment(61, "hello body") == 7
    assert captured.calls == [["gh", "pr", "comment", "61", "--body-file", "-"]]


# ---------------------------------------------------------------------------
# delete_comment
# ---------------------------------------------------------------------------


def test_delete_comment_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero-exit DELETE call reports success."""
    monkeypatch.setattr("forge.gh_comments.subprocess.run", make_fake_run(returncode=0))
    assert mod.delete_comment(555) is True


def test_delete_comment_false_and_warns_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected DELETE call reports failure and logs a warning."""
    monkeypatch.setattr("forge.gh_comments.subprocess.run", make_fake_run(returncode=1))
    with caplog.at_level(logging.WARNING):
        result = mod.delete_comment(555)
    assert result is False
    assert "could not delete" in caplog.text


# ---------------------------------------------------------------------------
# patch_comment
# ---------------------------------------------------------------------------


def test_patch_comment_true_with_expected_argv_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edit runs `-X PATCH ... -f body=@-` with the new body on stdin."""
    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> FakeProc:
        """Capture cmd and stdin input; return a zero-exit stub.

        Args:
            cmd: Command list to capture.
            **kwargs: Keyword arguments passed to ``subprocess.run``;
                only ``input`` is recorded.

        Returns:
            A FakeProc with returncode 0.
        """
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return FakeProc(returncode=0)

    monkeypatch.setattr("forge.gh_comments.subprocess.run", _fake_run)
    assert mod.patch_comment(555, "new body") is True
    assert captured["cmd"] == [
        "gh",
        "api",
        "-X",
        "PATCH",
        "repos/{owner}/{repo}/issues/comments/555",
        "-f",
        "body=@-",
    ]
    assert captured["input"] == "new body"


def test_patch_comment_false_and_warns_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected PATCH call reports failure and logs a warning."""
    monkeypatch.setattr("forge.gh_comments.subprocess.run", make_fake_run(returncode=1))
    with caplog.at_level(logging.WARNING):
        result = mod.patch_comment(555, "new body")
    assert result is False
    assert "could not edit" in caplog.text
