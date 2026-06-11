"""Unit tests for forge.pr_squash_comment — validators, body builder, CLI."""

from __future__ import annotations

import sys

import pytest

from forge import pr_squash_comment as mod


VALID_TITLE = "feat(#42): example squash title"
VALID_BULLETS = [
    "bullet alpha description",
    "bullet beta description",
    "bullet gamma description",
]


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
    """Exactly MAX_WORDS passes (cap is inclusive)."""
    title = "feat: title with five words"  # 5 words
    bullets = [
        " ".join(["word"] * 15),
        " ".join(["word"] * 15),
        " ".join(["word"] * 15),
    ]
    # total = 5 + 15 + 15 + 15 = 50
    mod._validate_word_count(title, bullets)


def test_validate_word_count_rejects_above_cap() -> None:
    """MAX_WORDS + 1 words raises."""
    title = "feat: title with five words here"  # 6 words
    bullets = [
        " ".join(["word"] * 15),
        " ".join(["word"] * 15),
        " ".join(["word"] * 15),
    ]
    # total = 6 + 45 = 51
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
        "🤖 AI-generated",
        "Built with Anthropic",
        "Assisted by AI on this PR",
    ],
)
def test_validate_no_ai_attribution_rejects_known_patterns(blob: str) -> None:
    """Any AI-attribution pattern in title or bullets raises.

    Args:
        blob: A string containing a known AI-attribution pattern.
    """
    with pytest.raises(mod.ValidationError):
        mod._validate_no_ai_attribution(VALID_TITLE, [*VALID_BULLETS, blob])


def test_validate_no_ai_attribution_accepts_clean_message() -> None:
    """A message free of attribution patterns passes."""
    mod._validate_no_ai_attribution(VALID_TITLE, VALID_BULLETS)


# ---------------------------------------------------------------------------
# Body builder
# ---------------------------------------------------------------------------


def test_build_body_wraps_in_literal_triple_backtick_fence() -> None:
    """The body contains a real ``` fence — not escaped backticks."""
    body = mod.build_body(VALID_TITLE, VALID_BULLETS)
    assert "```" in body
    assert r"\`\`\`" not in body


def test_build_body_includes_title_and_every_bullet() -> None:
    """Title and each bullet appear in the rendered body."""
    body = mod.build_body(VALID_TITLE, VALID_BULLETS)
    assert VALID_TITLE in body
    for b in VALID_BULLETS:
        assert f"- {b}" in body


def test_build_body_has_copy_verbatim_cue() -> None:
    """The 'copy verbatim' header is included so the user can act on it."""
    body = mod.build_body(VALID_TITLE, VALID_BULLETS)
    assert "copy verbatim" in body.lower()


def test_build_body_fence_appears_exactly_twice() -> None:
    """One opening fence, one closing fence — no extras (no inner fence)."""
    body = mod.build_body(VALID_TITLE, VALID_BULLETS)
    assert body.count("```") == 2


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
        [
            "--dry-run",
            "--title",
            VALID_TITLE,
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 0
    captured = capsys.readouterr()
    assert "```" in captured.out
    assert VALID_TITLE in captured.out


@pytest.mark.usefixtures("_cli_argv")
def test_main_validation_failure_returns_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bad title fails validation; main returns 1 and stderr names rule."""
    sys.argv.extend(
        [
            "--dry-run",
            "--title",
            "not conventional",
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 1
    captured = capsys.readouterr()
    assert "conventional-commit" in captured.err


@pytest.mark.usefixtures("_cli_argv")
def test_main_pr_mode_calls_gh_pr_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --dry-run, main shells out to ``gh pr comment``."""
    calls: list[list[str]] = []

    class _Proc:
        """Minimal stub for ``subprocess.CompletedProcess``."""

        returncode = 0

    def _fake_run(cmd: list[str], **_kw: object) -> _Proc:
        """Capture cmd; return a zero-exit stub.

        Args:
            cmd: Command list to capture.
            **_kw: Additional keyword arguments (unused).

        Returns:
            A stub _Proc with returncode 0.
        """
        calls.append(cmd)
        return _Proc()

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


@pytest.mark.usefixtures("_cli_argv")
def test_main_patch_mode_calls_gh_api_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--patch`` shells out to ``gh api -X PATCH`` for the comment id."""
    calls: list[list[str]] = []

    class _Proc:
        """Minimal stub returning JSON for repo detection + ok for patch."""

        def __init__(self, stdout: str = "", returncode: int = 0) -> None:
            """Record outputs for the simulated invocation.

            Args:
                stdout: Captured stdout for the stub call.
                returncode: Exit code to expose.
            """
            self.stdout = stdout
            self.returncode = returncode

    def _fake_run(cmd: list[str], **_kw: object) -> _Proc:
        """Return a repo slug on the lookup call; ok on the patch call.

        Args:
            cmd: Command list to capture and conditionally process.
            **_kw: Additional keyword arguments (unused).

        Returns:
            A _Proc with JSON output for repo view calls, else default ok response.
        """
        calls.append(cmd)
        if cmd[:3] == ["gh", "repo", "view"]:
            return _Proc(stdout='{"nameWithOwner":"x/y"}\n')
        return _Proc()

    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    sys.argv.extend(
        [
            "--patch",
            "999",
            "--title",
            VALID_TITLE,
            *(item for b in VALID_BULLETS for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 0
    patch_call = calls[-1]
    assert patch_call[0:2] == ["gh", "api"]
    assert "-X" in patch_call
    assert "PATCH" in patch_call
    assert "repos/x/y/issues/comments/999" in patch_call
