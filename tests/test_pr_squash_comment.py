"""Unit tests for forge.pr_squash_comment — validators, body builder, CLI.

# MOCKING STRATEGY: two seams, never the network. ``gh_api`` is patched
# in the module namespace to serve canned GitHub listings (the reads);
# ``subprocess.run`` is patched to capture argv for the writes (``gh pr
# comment``, ``gh api -X DELETE``). Listing fakes dispatch on the
# endpoint path AND the ``--jq`` expression, because ``ensure_last``
# reads the same endpoint twice for different fields.
"""

from __future__ import annotations

import json
import sys

import pytest

from forge import pr_squash_comment as mod
from tests.conftest import FakeProc


VALID_BULLETS = [
    "bullet alpha description",
    "bullet beta description",
    "bullet gamma description",
]

OLD_SQUASH_COMMENT = {
    "id": 555,
    "body": f"{mod.SQUASH_MARKER}\nold body",
    "created_at": "2026-01-01T00:00:00Z",
}

UNRELATED_COMMENT = {
    "id": 777,
    "body": "a human wrote this",
    "created_at": "2026-01-02T00:00:00Z",
}


def _page(*comments: dict[str, object]) -> str:
    """Render one ``gh api --paginate --jq '[...]'`` output page.

    Args:
        *comments: Comment mappings the fake endpoint should return.

    Returns:
        A single JSON array line, as gh emits per page.
    """
    return json.dumps(list(comments))


# ---------------------------------------------------------------------------
# Bullet validation
# ---------------------------------------------------------------------------


def test_validate_bullets_accepts_min_count() -> None:
    """Exactly MIN_BULLETS passes."""
    mod._validate_bullets(["a", "b", "c"])


def test_validate_bullets_accepts_max_count() -> None:
    """Exactly MAX_BULLETS passes."""
    mod._validate_bullets(["a", "b", "c", "d", "e"])


@pytest.mark.parametrize("n", [0, 1, 2, 6, 7])
def test_validate_bullets_rejects_out_of_range(n: int) -> None:
    """Counts outside [MIN_BULLETS, MAX_BULLETS] raise.

    Args:
        n: Number of bullets to test (out-of-range value).
    """
    with pytest.raises(mod.ValidationError):
        mod._validate_bullets([f"bullet {i}" for i in range(n)])


def test_validate_bullets_rejects_whitespace_only_entry() -> None:
    """An all-whitespace bullet is treated as empty and raises."""
    with pytest.raises(mod.ValidationError):
        mod._validate_bullets(["real", "  ", "also real"])


# ---------------------------------------------------------------------------
# Word count validation
# ---------------------------------------------------------------------------


def test_validate_word_count_accepts_at_cap() -> None:
    """Exactly MAX_WORDS across the bullets passes (cap is inclusive)."""
    bullets = [
        " ".join(["word"] * 20),
        " ".join(["word"] * 20),
        " ".join(["word"] * 10),
    ]
    mod._validate_word_count(bullets)


def test_validate_word_count_rejects_above_cap() -> None:
    """MAX_WORDS + 1 words raises, naming the observed count."""
    bullets = [
        " ".join(["word"] * 20),
        " ".join(["word"] * 20),
        " ".join(["word"] * 11),
    ]
    with pytest.raises(mod.ValidationError, match=r"51 words"):
        mod._validate_word_count(bullets)


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
    """Any AI-attribution phrase in the bullets raises via the phrase layer.

    Args:
        blob: A string containing a known AI-attribution phrase.
    """
    with pytest.raises(mod.ValidationError, match="pattern detected"):
        mod._validate_no_ai_attribution([*VALID_BULLETS, blob])


def test_validate_no_ai_attribution_accepts_clean_message() -> None:
    """A message free of attribution patterns passes."""
    mod._validate_no_ai_attribution(VALID_BULLETS)


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
    mod._validate_no_ai_attribution([*VALID_BULLETS, blob])


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
        mod._validate_no_ai_attribution([*VALID_BULLETS, blob])


# ---------------------------------------------------------------------------
# Body builder
# ---------------------------------------------------------------------------


def test_build_body_wraps_in_literal_triple_backtick_fence() -> None:
    """The body contains a real ``` fence — not escaped backticks."""
    body = mod.build_body(VALID_BULLETS)
    assert "```" in body
    assert r"\`\`\`" not in body


def test_build_body_includes_every_bullet() -> None:
    """Each bullet appears in the rendered body."""
    body = mod.build_body(VALID_BULLETS)
    for b in VALID_BULLETS:
        assert f"- {b}" in body


def test_build_body_fenced_block_holds_bullets_only() -> None:
    """The fenced region is exactly the bullet lines — no title line.

    The title field of GitHub's squash dialog prefills from the PR
    title; a title inside the fence would have to be deleted by hand
    after pasting.
    """
    body = mod.build_body(VALID_BULLETS)
    fenced = body.split("```")[1].strip().splitlines()
    assert fenced == [f"- {b}" for b in VALID_BULLETS]


def test_build_body_carries_marker_for_later_runs() -> None:
    """The marker is present so a later run can find and supersede it."""
    assert mod.SQUASH_MARKER in mod.build_body(VALID_BULLETS)


def test_build_body_has_copy_verbatim_cue() -> None:
    """The 'copy verbatim' header is included so the user can act on it."""
    assert "copy verbatim" in mod.build_body(VALID_BULLETS).lower()


def test_build_body_fence_appears_exactly_twice() -> None:
    """One opening fence, one closing fence — no extras (no inner fence)."""
    assert mod.build_body(VALID_BULLETS).count("```") == 2


# ---------------------------------------------------------------------------
# Comment listing
# ---------------------------------------------------------------------------


def test_list_squash_comments_keeps_only_marked_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: a PR carries one forge squash comment among human ones.

    MOCK SETUP: ``gh_api`` returns a single page holding both.
    EXPECTED BEHAVIOR: only the marker-carrying comment comes back.
    """
    monkeypatch.setattr(
        mod, "gh_api", lambda *_a, **_kw: _page(OLD_SQUASH_COMMENT, UNRELATED_COMMENT)
    )
    found = mod._list_squash_comments(61)
    assert found is not None
    assert [c["id"] for c in found] == [555]


def test_list_squash_comments_spans_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCENARIO: ``--paginate`` emits one JSON array per page.

    MOCK SETUP: ``gh_api`` returns two array lines, one marked comment each.
    EXPECTED BEHAVIOR: both pages are flattened into one ordered list.
    """
    second = {**OLD_SQUASH_COMMENT, "id": 556, "created_at": "2026-01-03T00:00:00Z"}
    monkeypatch.setattr(
        mod,
        "gh_api",
        lambda *_a, **_kw: f"{_page(OLD_SQUASH_COMMENT)}\n{_page(second)}",
    )
    found = mod._list_squash_comments(61)
    assert found is not None
    assert [c["id"] for c in found] == [555, 556]


def test_list_squash_comments_reports_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed listing returns None — distinct from 'no squash comment'."""
    monkeypatch.setattr(mod, "gh_api", lambda *_a, **_kw: None)
    assert mod._list_squash_comments(61) is None


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture
def _cli_argv(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub ``sys.argv`` for ``main()`` invocations.

    Returns:
        The mutable argv list. Tests append to it before calling
        ``mod.main()``.
    """
    argv = ["forge-pr-squash-comment"]
    monkeypatch.setattr("sys.argv", argv)
    return argv


@pytest.mark.usefixtures("_cli_argv")
def test_main_dry_run_prints_body_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` writes the wrapped body to stdout, exit 0."""
    sys.argv.extend(
        ["--dry-run", *(item for b in VALID_BULLETS for item in ("--bullet", b))]
    )
    assert mod.main() == 0
    captured = capsys.readouterr()
    assert "```" in captured.out
    assert f"- {VALID_BULLETS[0]}" in captured.out


@pytest.mark.usefixtures("_cli_argv")
def test_main_validation_failure_returns_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Too few bullets fails validation; main returns 1, stderr names the rule."""
    sys.argv.extend(["--dry-run", "--bullet", "only one"])
    assert mod.main() == 1
    assert "requires 3-5" in capsys.readouterr().err


@pytest.mark.usefixtures("_cli_argv")
def test_main_pr_mode_posts_then_deletes_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: a PR already carries an older forge squash comment.

    MOCK SETUP: ``gh_api`` serves that comment; ``subprocess.run``
    captures the post and the delete.
    EXPECTED BEHAVIOR: the new comment is posted FIRST (a failed
    cleanup must never leave the PR without a squash message), then the
    superseded comment is deleted by id.
    """
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: object) -> FakeProc:
        """Capture cmd; return a zero-exit stub.

        Args:
            cmd: Command list to capture.
            **_kw: Additional keyword arguments (unused).

        Returns:
            A FakeProc with returncode 0.
        """
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(mod, "gh_api", lambda *_a, **_kw: _page(OLD_SQUASH_COMMENT))
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    sys.argv.extend(
        ["--pr", "61", *(item for b in VALID_BULLETS for item in ("--bullet", b))]
    )
    assert mod.main() == 0
    assert calls[0][:4] == ["gh", "pr", "comment", "61"]
    assert "--body-file" in calls[0]
    assert calls[1][:4] == ["gh", "api", "-X", "DELETE"]
    assert calls[1][-1].endswith("/issues/comments/555")


@pytest.mark.usefixtures("_cli_argv")
def test_main_pr_mode_survives_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: the delete call is rejected after a successful post.

    MOCK SETUP: ``subprocess.run`` returns 0 for the post, 1 for the delete.
    EXPECTED BEHAVIOR: exit 0 — the squash comment is live and newest;
    a leftover duplicate is a warning, not a failed run.
    """

    def _fake_run(cmd: list[str], **_kw: object) -> FakeProc:
        """Succeed on the post, fail on the delete.

        Args:
            cmd: Command list under inspection.
            **_kw: Additional keyword arguments (unused).

        Returns:
            A FakeProc whose returncode depends on the command.
        """
        if cmd[:2] == ["gh", "api"]:
            return FakeProc(returncode=1, stderr="403")
        return FakeProc()

    monkeypatch.setattr(mod, "gh_api", lambda *_a, **_kw: _page(OLD_SQUASH_COMMENT))
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    sys.argv.extend(
        ["--pr", "61", *(item for b in VALID_BULLETS for item in ("--bullet", b))]
    )
    assert mod.main() == 0


@pytest.mark.usefixtures("_cli_argv")
def test_main_ensure_last_reposts_when_buried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: a review-thread reply landed under the squash comment.

    MOCK SETUP: the review-comment endpoint reports a newer timestamp
    than the squash comment's.
    EXPECTED BEHAVIOR: the existing body is re-posted verbatim and the
    buried copy deleted — no bullets needed on the command line.
    """
    calls: list[list[str]] = []

    def _fake_api(path: str, *args: str, **_kw: object) -> str | None:
        """Serve comment listings per endpoint and requested field.

        Args:
            path: The gh api endpoint path.
            *args: Trailing gh api arguments; the last is the jq filter.
            **_kw: Additional keyword arguments (unused).

        Returns:
            A canned JSON page for the endpoint under test.
        """
        jq = args[-1] if args else ""
        if "issues/61/comments" in path:
            return (
                _page(OLD_SQUASH_COMMENT)
                if "body" in jq
                else _page("2026-01-01T00:00:00Z")
            )
        if "pulls/61/comments" in path:
            return _page("2026-02-01T00:00:00Z")
        return _page()

    def _fake_run(cmd: list[str], **_kw: object) -> FakeProc:
        """Capture cmd; return a zero-exit stub.

        Args:
            cmd: Command list to capture.
            **_kw: Additional keyword arguments (unused).

        Returns:
            A FakeProc with returncode 0.
        """
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(mod, "gh_api", _fake_api)
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    sys.argv.extend(["--pr", "61"])
    assert mod.main() == 0
    assert calls[0][:4] == ["gh", "pr", "comment", "61"]
    assert calls[1][:4] == ["gh", "api", "-X", "DELETE"]


@pytest.mark.usefixtures("_cli_argv")
def test_main_ensure_last_is_quiet_noop_when_already_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: nothing has been posted since the squash comment.

    MOCK SETUP: every activity endpoint reports timestamps at or below
    the squash comment's own.
    EXPECTED BEHAVIOR: exit 0 with no gh writes — re-posting would only
    spam subscribers.
    """

    def _fake_api(path: str, *args: str, **_kw: object) -> str | None:
        """Report the squash comment as the newest activity.

        Args:
            path: The gh api endpoint path.
            *args: Trailing gh api arguments; the last is the jq filter.
            **_kw: Additional keyword arguments (unused).

        Returns:
            A canned JSON page for the endpoint under test.
        """
        jq = args[-1] if args else ""
        if "issues/61/comments" in path:
            return (
                _page(OLD_SQUASH_COMMENT)
                if "body" in jq
                else _page("2026-01-01T00:00:00Z")
            )
        return _page()

    def _fail(*_a: object, **_kw: object) -> FakeProc:
        """Fail the test if any gh write is attempted.

        Args:
            *_a: Positional arguments (unused).
            **_kw: Keyword arguments (unused).

        Returns:
            Never returns — always raises.

        Raises:
            AssertionError: Always.
        """
        msg = "no-op path must not shell out"
        raise AssertionError(msg)

    monkeypatch.setattr(mod, "gh_api", _fake_api)
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fail)
    sys.argv.extend(["--pr", "61"])
    assert mod.main() == 0


@pytest.mark.usefixtures("_cli_argv")
def test_main_ensure_last_errors_without_an_existing_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR with no forge squash comment cannot be re-ordered — exit 1."""
    monkeypatch.setattr(mod, "gh_api", lambda *_a, **_kw: _page())
    sys.argv.extend(["--pr", "61"])
    assert mod.main() == 1
