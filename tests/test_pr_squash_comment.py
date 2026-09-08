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


VALID_TITLE = "feat(#42): example squash title"
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
# Title validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "feat: simple subject",
        "fix(#1): scoped with one ref",
        "fix(#1, #2): scoped with two refs",
        "refactor(audit): named scope",
        "docs: lowercase subject is fine",
        "chore(#99): bump",
    ],
)
def test_validate_title_accepts_conventional_forms(title: str) -> None:
    """Conventional-commit titles in known shapes pass.

    Args:
        title: A conventional-commit format title string.
    """
    mod._validate_title(title)  # no raise


@pytest.mark.parametrize(
    "title",
    [
        "",
        "  ",
        "no type prefix here",
        "FEAT: uppercase type",
        "feat add subject without colon",
        "wip: not a conventional type",
        "feat: line one\nfeat: line two",
    ],
)
def test_validate_title_rejects_bad_forms(title: str) -> None:
    """Empty, multi-line, or non-conventional titles raise.

    Args:
        title: A malformed title string (empty, multi-line, or non-conventional).
    """
    with pytest.raises(mod.ValidationError):
        mod._validate_title(title)


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
    """Exactly MAX_WORDS across title + bullets passes (cap is inclusive)."""
    title = "feat: title with five words"  # 5 words
    bullets = [" ".join(["word"] * 15)] * 3  # 45 words
    mod._validate_word_count(title, bullets)


def test_validate_word_count_rejects_above_cap() -> None:
    """MAX_WORDS + 1 words raises, naming the observed count."""
    title = "feat: title with five words here"  # 6 words
    bullets = [" ".join(["word"] * 15)] * 3  # 45 words
    with pytest.raises(mod.ValidationError, match=r"51 words"):
        mod._validate_word_count(title, bullets)


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
    """Any AI-attribution phrase in title or bullets raises via the phrase layer.

    Args:
        blob: A string containing a known AI-attribution phrase.
    """
    with pytest.raises(mod.ValidationError, match="pattern detected"):
        mod._validate_no_ai_attribution(VALID_TITLE, [*VALID_BULLETS, blob])


def test_validate_no_ai_attribution_accepts_clean_message() -> None:
    """A message free of attribution patterns passes."""
    mod._validate_no_ai_attribution(VALID_TITLE, VALID_BULLETS)


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
    mod._validate_no_ai_attribution(VALID_TITLE, [*VALID_BULLETS, blob])


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
        mod._validate_no_ai_attribution(VALID_TITLE, [*VALID_BULLETS, blob])


# ---------------------------------------------------------------------------
# Body builder
# ---------------------------------------------------------------------------


def test_build_body_wraps_in_literal_triple_backtick_fence() -> None:
    """The body contains real ``` fences — not escaped backticks."""
    body = mod.build_body(VALID_TITLE, VALID_BULLETS)
    assert "```" in body
    assert r"\`\`\`" not in body


def test_build_body_includes_every_bullet() -> None:
    """Each bullet appears in the rendered body."""
    body = mod.build_body(VALID_TITLE, VALID_BULLETS)
    for b in VALID_BULLETS:
        assert f"- {b}" in body


def test_build_body_puts_title_and_body_in_separate_fences() -> None:
    """Two fences, one per field of the squash dialog — never one merged block.

    Each half is copied on its own, so a single fence holding both
    would have to be split by hand after pasting.
    """
    body = mod.build_body(VALID_TITLE, VALID_BULLETS)
    title_fence, body_fence = body.split("```")[1], body.split("```")[3]
    assert title_fence.strip().splitlines() == [VALID_TITLE]
    assert body_fence.strip().splitlines() == [f"- {b}" for b in VALID_BULLETS]


def test_build_body_carries_marker_for_later_runs() -> None:
    """The marker is present so a later run can find and supersede it."""
    assert mod.SQUASH_MARKER in mod.build_body(VALID_TITLE, VALID_BULLETS)


def test_build_body_has_copy_verbatim_cue() -> None:
    """The copy-verbatim header is included so the user can act on it."""
    assert "verbatim" in mod.build_body(VALID_TITLE, VALID_BULLETS).lower()


def test_build_body_has_exactly_two_fenced_blocks() -> None:
    """Four fence markers: title block open/close, body block open/close."""
    assert mod.build_body(VALID_TITLE, VALID_BULLETS).count("```") == 4


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


def _fake_api_with_title(pr_title: str) -> object:
    """Build a ``gh_api`` fake serving a PR title and one squash comment.

    Args:
        pr_title: What the PR endpoint should report as the current title.

    Returns:
        A callable dispatching on the endpoint path.
    """

    def _call(path: str, *_args: str, **_kw: object) -> str | None:
        """Serve the PR title read or the comment listing.

        Args:
            path: The gh api endpoint path.
            *_args: Trailing gh api arguments (unused).
            **_kw: Additional keyword arguments (unused).

        Returns:
            The canned response for the endpoint under test.
        """
        if path.endswith("/pulls/61"):
            return pr_title
        return _page(OLD_SQUASH_COMMENT)

    return _call


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
        [
            "--dry-run",
            "--title",
            VALID_TITLE,
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 0
    captured = capsys.readouterr()
    assert captured.out.count("```") == 4
    assert VALID_TITLE in captured.out
    assert f"- {VALID_BULLETS[0]}" in captured.out


@pytest.mark.usefixtures("_cli_argv")
def test_main_validation_failure_returns_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-conventional title fails validation; exit 1, stderr names the rule."""
    sys.argv.extend(
        [
            "--dry-run",
            "--title",
            "not conventional",
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 1
    assert "conventional-commit" in capsys.readouterr().err


@pytest.mark.usefixtures("_cli_argv")
def test_main_rejects_too_few_bullets(capsys: pytest.CaptureFixture[str]) -> None:
    """The 3-5 bullet rule is enforced before anything reaches gh."""
    sys.argv.extend(["--dry-run", "--title", VALID_TITLE, "--bullet", "only one"])
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

    monkeypatch.setattr(mod, "gh_api", _fake_api_with_title(VALID_TITLE))
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    sys.argv.extend(
        [
            "--pr",
            "61",
            "--title",
            VALID_TITLE,
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
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

    monkeypatch.setattr(mod, "gh_api", _fake_api_with_title(VALID_TITLE))
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    sys.argv.extend(
        [
            "--pr",
            "61",
            "--title",
            VALID_TITLE,
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 0


@pytest.mark.usefixtures("_cli_argv")
def test_main_pr_mode_forces_a_stale_pr_title_to_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: the PR title no longer matches the squash title.

    MOCK SETUP: the PR reports an older title; subprocess captures the calls.
    EXPECTED BEHAVIOR: `gh pr edit --title` runs BEFORE the comment is
    posted — GitHub prefills the squash dialog from the PR title, so a
    stale one would put the wrong line in the permanent `main` commit.
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

    monkeypatch.setattr(mod, "gh_api", _fake_api_with_title("feat: an older title"))
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    sys.argv.extend(
        [
            "--pr",
            "61",
            "--title",
            VALID_TITLE,
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 0
    assert calls[0] == ["gh", "pr", "edit", "61", "--title", VALID_TITLE]
    assert calls[1][:4] == ["gh", "pr", "comment", "61"]


@pytest.mark.usefixtures("_cli_argv")
def test_main_pr_mode_skips_the_edit_when_the_title_already_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-matching PR title takes no edit — no no-op timeline event."""
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

    monkeypatch.setattr(mod, "gh_api", _fake_api_with_title(VALID_TITLE))
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    sys.argv.extend(
        [
            "--pr",
            "61",
            "--title",
            VALID_TITLE,
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 0
    assert not any(cmd[:3] == ["gh", "pr", "edit"] for cmd in calls)


@pytest.mark.usefixtures("_cli_argv")
def test_main_pr_mode_reports_a_rejected_title_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: `gh pr edit` is rejected (no write access to the title).

    MOCK SETUP: the edit exits 1, the comment post exits 0.
    EXPECTED BEHAVIOR: the comment is still posted — it carries the
    title fence, so the human can fix the field by hand — but the run
    exits non-zero, because the prefill no longer matches the message.
    """

    def _fake_run(cmd: list[str], **_kw: object) -> FakeProc:
        """Reject the title edit, accept everything else.

        Args:
            cmd: Command list under inspection.
            **_kw: Additional keyword arguments (unused).

        Returns:
            A FakeProc whose returncode depends on the command.
        """
        if cmd[:3] == ["gh", "pr", "edit"]:
            return FakeProc(returncode=1, stderr="403")
        return FakeProc()

    monkeypatch.setattr(mod, "gh_api", _fake_api_with_title("feat: an older title"))
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    sys.argv.extend(
        [
            "--pr",
            "61",
            "--title",
            VALID_TITLE,
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 1


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
