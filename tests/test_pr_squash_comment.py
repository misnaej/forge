"""Unit tests for forge.pr_squash_comment — validators, body builder, CLI.

# MOCKING STRATEGY: three seams, never the network. ``own_login`` is
# patched so the author check that ``list_marker_comments`` performs does
# not consume a listing fake's response (it is ``lru_cache``d, so an
# unpatched call also makes a test's outcome depend on what ran before
# it — the autouse fixture below clears it). ``gh_api`` is patched
# in the module namespace to serve canned GitHub listings (the reads);
# ``subprocess.run`` is patched to capture argv for the writes (``gh pr
# comment``, ``gh api -X DELETE``). Listing fakes dispatch on the
# endpoint path AND the ``--jq`` expression, because ``ensure_last``
# reads the same endpoint twice for different fields. The rule checkers
# (``_check_title`` and friends) and ``validate`` are pure — text in,
# problems out — so ``capsys`` is the only seam the dry-run stderr
# assertions need; nothing there is monkeypatched.
"""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING, TypeAlias, cast

import pytest

from forge import git_utils
from forge import pr_squash_comment as mod
from tests.conftest import FakeProc, page_json


if TYPE_CHECKING:
    from pathlib import Path


# Captured before the autouse fixture replaces it, so the function's own
# tests still exercise the real one.
_REAL_PR_FRAGMENT_ITEMS = mod._pr_fragment_items

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
    # Ours: the listing filters marker comments by author, so a canned
    # comment without one is a stranger's and is left alone.
    "author": "octocat",
}


# ---------------------------------------------------------------------------
# Title validation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _own_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the gh identity and clear its process cache between tests.

    `list_marker_comments` asks who we are before it lists, and the
    answer is cached for the process — so without this a test's result
    depends on whether an earlier test warmed the cache.
    """
    git_utils.own_login.cache_clear()
    monkeypatch.setattr("forge.gh_comments.own_login", lambda: "octocat")


@pytest.fixture(autouse=True)
def _no_repository_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``main`` see no fragment, so it never reads the real repository.

    Coverage tests override this with a lambda returning real items.
    """
    monkeypatch.setattr(mod, "_pr_fragment_items", lambda: ([], ""))


@pytest.mark.parametrize(
    "title",
    [
        "feat: simple subject",
        "fix(#1): scoped with one ref",
        "fix(#1, #2): scoped with two refs",
        "refactor(audit): named scope",
        "docs: lowercase subject is fine",
        "chore(#99): bump",
        "feat!: breaking change, no scope",
        "fix(scope)!: breaking change, scoped",
    ],
)
def test_check_title_accepts_conventional_forms(title: str) -> None:
    """Conventional-commit titles in known shapes report no problems.

    Pins the accepted title shapes FOUNDATION §6 names, including the
    `!` breaking-change marker immediately before the colon (with or
    without a scope).

    Args:
        title: A conventional-commit format title string.
    """
    assert mod._check_title(title) == []


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
        "feat !: space before the breaking marker",
        "!feat: breaking marker before the type",
    ],
)
def test_check_title_rejects_bad_forms(title: str) -> None:
    """Empty, multi-line, or non-conventional titles report a problem.

    Pins which malformed shapes are rejected, including a `!` breaking
    marker in the wrong position (a space before it, or before the type
    instead of before the colon).

    Args:
        title: A malformed title string (empty, multi-line, or non-conventional).
    """
    assert mod._check_title(title) != []


# ---------------------------------------------------------------------------
# Bullet validation
# ---------------------------------------------------------------------------


def test_check_bullets_accepts_min_count() -> None:
    """Exactly MIN_BULLETS reports no problems.

    Pins the inclusive lower bound of the accepted range.
    """
    assert mod._check_bullets(["a", "b", "c"]) == []


def test_check_bullets_accepts_max_count() -> None:
    """Exactly MAX_BULLETS reports no problems.

    Pins the inclusive upper bound of the accepted range.
    """
    assert mod._check_bullets(["a", "b", "c", "d", "e"]) == []


@pytest.mark.parametrize("n", [0, 1, 2, 6, 7])
def test_check_bullets_rejects_out_of_range(n: int) -> None:
    """Counts outside [MIN_BULLETS, MAX_BULLETS] report the count problem.

    Pins the FOUNDATION §6 bound and its message substring.

    Args:
        n: Number of bullets to test (out-of-range value).
    """
    problems = mod._check_bullets([f"bullet {i}" for i in range(n)])
    assert any("requires 3-5" in p for p in problems)


def test_check_bullets_reports_one_problem_per_empty_bullet() -> None:
    """Each whitespace-only bullet is its own problem, count problem absent.

    Pins that an in-range count with several empty entries reports one
    problem per empty entry — not one combined problem, and not the
    count problem, since 5 is in range.
    """
    bullets = ["real one", "  ", "\t", "real two", "   "]
    problems = mod._check_bullets(bullets)
    assert len(problems) == 3
    assert not any("requires 3-5" in p for p in problems)


@pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
def test_check_bullets_rejects_embedded_line_break(line_break: str) -> None:
    """A bullet spanning lines is refused, whatever the line-break flavour.

    Behavior test: the body renders inside a fixed code fence, so a line
    break in a bullet could close it early; this is the contract that the
    refusal names the bullet.

    Args:
        line_break: The line-break sequence placed inside the bullet.
    """
    problems = mod._check_bullets([f"x{line_break}y", "b", "c"])
    assert problems == ["bullet 1 must be a single line"]


def test_check_bullets_names_only_the_multiline_bullet() -> None:
    """Behavior test: the problem carries the offending bullet's 1-based index."""
    problems = mod._check_bullets(["ok", "x\ny", "ok"])
    assert problems == ["bullet 2 must be a single line"]


def test_check_bullets_reports_count_and_empty_bullet_problems_together() -> None:
    """An out-of-range count with empty entries reports both kinds together.

    Pins that the count problem and the per-bullet empty problems are
    independent — a caller broken on both learns of both in one call
    rather than only the first rule the checker hits.
    """
    problems = mod._check_bullets(["", "  "])
    text = "\n".join(problems)
    assert "requires 3-5" in text
    assert text.lower().count("empty") >= 2


# ---------------------------------------------------------------------------
# Word count validation
# ---------------------------------------------------------------------------


LONG_PART = "alphabetazetadelta" * 5  # one long word, no spaces


@pytest.mark.parametrize(
    ("text", "present", "absent"),
    [
        pytest.param(
            LONG_PART, [LONG_PART[:30], "…"], [LONG_PART], id="long-text-ellipsis"
        ),
        pytest.param(
            "feat: firstlinemarker\nsecondlinemarker",
            ["firstlinemarker"],
            ["secondlinemarker"],
            id="first-line-only",
        ),
        # Security property: the preview is written raw to stderr.
        pytest.param(
            "escape \x1b[31m alert message here",
            ["escape", "alert", "message", "here"],
            ["\x1b"],
            id="terminal-escapes-stripped",
        ),
        pytest.param("   ", ["(empty)"], [], id="whitespace-only-is-empty"),
    ],
)
def test_preview_shortens_to_a_printable_first_line(
    text: str, present: list[str], absent: list[str]
) -> None:
    """A breakdown preview is one short printable line, or ``(empty)``.

    Args:
        text: The message part to preview.
        present: Substrings the preview must contain.
        absent: Substrings it must not contain.
    """
    preview = mod._preview(text)
    for needle in present:
        assert needle in preview
    for needle in absent:
        assert needle not in preview


@pytest.mark.parametrize("n_bullets", [3, 0])
def test_word_breakdown_lists_title_then_numbered_bullets(n_bullets: int) -> None:
    """One row per existing part: title first, then ``bullet 1..n`` in order.

    Pins 1-based labels, title-before-bullets ordering, and that zero
    bullets means zero bullet rows rather than empty placeholders.

    Args:
        n_bullets: How many bullets the message carries.
    """
    bullets = [" ".join(["word"] * (i + 1)) for i in range(n_bullets)]
    rows = mod._word_breakdown("feat: message", bullets)
    assert len(rows) == 1 + n_bullets
    assert rows[0].startswith("  title")
    for i, row in enumerate(rows[1:], start=1):
        assert row.startswith(f"  bullet {i}")


# ---------------------------------------------------------------------------
# AI attribution validation
# ---------------------------------------------------------------------------


def test_check_attribution_accepts_clean_message() -> None:
    """A message free of attribution patterns reports no problems.

    Thin delegation test: the phrase layer, the path-shaped exemption,
    and the bare-vendor-token backstop are ``gh_comments.validate_no_ai_attribution``'s
    own contract, covered in ``tests/test_gh_comments.py``. This only pins
    that ``_check_attribution`` joins title + bullets and forwards to
    the shared gate.
    """
    assert mod._check_attribution(VALID_TITLE, VALID_BULLETS) == []


def test_check_attribution_reports_known_pattern() -> None:
    """A known attribution phrase in a bullet reports the shared gate's message.

    Pins that the shared gate's ``ValidationError`` is converted into
    a problem string rather than propagating as a raise.
    """
    problems = mod._check_attribution(VALID_TITLE, ["Generated with Claude"])
    assert any("pattern detected" in p for p in problems)


def test_check_attribution_stays_strict_on_bare_product_mention() -> None:
    """A bare product-name mention is still refused in squash text.

    Squash text becomes permanent history, so unlike the wrap-up it gets
    no fenced-vendor-token exemption.
    """
    problems = mod._check_attribution(VALID_TITLE, ["Claude Code keys its cache"])
    assert problems


# ---------------------------------------------------------------------------
# validate() — combined rule ordering
# ---------------------------------------------------------------------------


def test_validate_returns_empty_list_when_every_rule_passes() -> None:
    """A fully valid title + bullets combination reports no problems."""
    assert mod.validate(VALID_TITLE, VALID_BULLETS) == []


def test_validate_reports_every_failing_rule_in_order() -> None:
    """Two broken rules both appear, in rule order (title, then bullets).

    Pins that a run reports every broken rule rather than stopping at
    the first one, in the order the rules are declared — so a human
    reads problems top-to-bottom sensibly.
    """
    problems = mod.validate("not conventional", ["only one"])
    text = "\n".join(problems)
    assert text.index("conventional-commit") < text.index("requires 3-5")


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


def test_list_squash_comments_delegates_to_shared_marker_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_list_squash_comments`` is a thin wrapper over the shared marker listing.

    Paging, filtering, and the "listing failed" contract live in and are
    covered by ``forge.gh_comments.list_marker_comments`` (see
    ``tests/test_gh_comments.py``); this only pins the delegation — the
    right PR number and :data:`mod.SQUASH_MARKER` reach the shared call.
    """
    calls: list[tuple[int, str, bool]] = []

    def _fake_list(
        pr_number: int, marker: str, *, anchored: bool = False
    ) -> list[dict[str, object]]:
        """Record the delegation call and return a canned result.

        Args:
            pr_number: PR number passed through by the caller.
            marker: Marker string passed through by the caller.
            anchored: Anchoring flag passed through by the caller.

        Returns:
            A single canned comment mapping.
        """
        calls.append((pr_number, marker, anchored))
        return [OLD_SQUASH_COMMENT]

    monkeypatch.setattr(mod, "list_marker_comments", _fake_list)
    assert mod._list_squash_comments(61) == [OLD_SQUASH_COMMENT]
    assert calls == [(61, mod.SQUASH_MARKER, True)]


# A wrap-up that quotes the squash marker mid-body, with its own marker last.
WRAPUP_QUOTING_SQUASH = {
    "id": 888,
    "body": (
        "verified-at: abc1234\n"
        f"Note: the squash comment starts with {mod.SQUASH_MARKER} verbatim.\n\n"
        "<!-- forge:pr-wrapup -->\n"
    ),
    "created_at": "2026-01-02T00:00:00Z",
    "author": "octocat",
}


def test_squash_listing_ignores_a_wrapup_quoting_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: a wrap-up embeds the squash marker mid-body.

    MOCK SETUP: the issue-comments listing returns the real squash
    comment plus that wrap-up; ``subprocess.run`` captures writes.
    EXPECTED BEHAVIOR: the listing sees only the squash comment, posting
    deletes only it, and ``ensure_last`` neither re-posts nor deletes
    the wrap-up.
    """
    calls: list[list[str]] = []

    def _fake_api(path: str, *args: str, **_kw: object) -> str | None:
        """Serve both comments, and activity stamps older than the squash.

        Args:
            path: The gh api endpoint path.
            *args: Trailing gh api arguments; the last is the jq filter.
            **_kw: Additional keyword arguments (unused).

        Returns:
            A canned JSON page for the endpoint under test.
        """
        jq = args[-1] if args else ""
        if "issues/61/comments" in path:
            if "body" in jq:
                return page_json(OLD_SQUASH_COMMENT, WRAPUP_QUOTING_SQUASH)
            return page_json("2026-01-01T00:00:00Z")
        return page_json()

    def _fake_run(cmd: list[str], **_kw: object) -> FakeProc:
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(mod, "gh_api", _fake_api)
    monkeypatch.setattr("forge.gh_comments.gh_api", _fake_api)
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)

    listed = mod._list_squash_comments(61)
    assert listed is not None
    assert [c["id"] for c in listed] == [555]

    assert mod.post_squash_comment(61, "<new body>") == 0
    deletes = [c[-1] for c in calls if c[:4] == ["gh", "api", "-X", "DELETE"]]
    assert len(deletes) == 1
    assert deletes[0].endswith("/issues/comments/555")

    calls.clear()
    assert mod.ensure_last(61) == 0
    assert calls == []


def _message_of(total_words: int) -> tuple[str, list[str]]:
    """Build a valid title and 3 bullets holding exactly ``total_words`` words.

    Args:
        total_words: Whole-message word count wanted (at least 6).

    Returns:
        ``(title, bullets)``; the title is 3 words.
    """
    rest = total_words - 3
    first = rest // 3
    sizes = (first, first, rest - 2 * first)
    return "feat: one two", [" ".join(["w"] * n) for n in sizes]


@pytest.mark.parametrize(
    ("total_words", "warns"),
    [(45, False), (46, True), (54, True), (55, True), (200, True)],
)
def test_length_warns_above_the_ceiling_and_never_refuses(
    total_words: int, *, warns: bool
) -> None:
    """Behavior: the ceiling (45) is silent; any length over it warns, none refuses.

    Args:
        total_words: Whole-message word count under test.
        warns: Whether ``word_cap_warning`` should fire.
    """
    title, bullets = _message_of(total_words)
    assert mod._word_total(title, bullets) == total_words
    assert mod.validate(title, bullets) == []
    warning = mod.word_cap_warning(title, bullets)
    if warns:
        assert warning is not None
        assert str(total_words) in warning
    else:
        assert warning is None


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
        return page_json(OLD_SQUASH_COMMENT)

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
    """``--dry-run`` writes the wrapped body to stdout, exit 0.

    Stderr additionally reports the word-count breakdown — a passing
    dry run is the one place a human sees the margin to the cap.
    """
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
    assert captured.out == mod.build_body(VALID_TITLE, VALID_BULLETS)
    # VALID_TITLE is 4 words; VALID_BULLETS are 3 words x 3 bullets = 9. 4 + 9 = 13.
    assert f"13/{mod.MAX_WORDS} words" in captured.err
    # VALID_TITLE's own subject text contains the word "title", so this
    # only pins presence, not an exact count (see the over-cap test for
    # the count-pinned version, on title text chosen to avoid the clash).
    assert "title" in captured.err.lower()
    assert captured.err.lower().count("bullet") >= 3


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
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "conventional-commit" in captured.err
    assert "words" not in captured.err


@pytest.mark.usefixtures("_cli_argv")
def test_main_reports_every_failing_rule_prefixed_on_its_own_stderr_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two broken rules both reach stderr, each on its own prefixed line.

    SCENARIO: title and bullet count are both broken.
    EXPECTED BEHAVIOR: both problems are visible in one run, each
    opened by the ``forge-pr-squash-comment: `` prefix.
    """
    sys.argv.extend(["--dry-run", "--title", "not conventional", "--bullet", "one"])
    assert mod.main() == 1
    err = capsys.readouterr().err
    prefix = "forge-pr-squash-comment: "
    prefixed_lines = [line for line in err.splitlines() if line.startswith(prefix)]
    assert len(prefixed_lines) == 2
    assert any("conventional-commit" in line for line in prefixed_lines)
    assert any("requires 3-5" in line for line in prefixed_lines)


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
    # `sync_pr_title`'s PR-title read still runs through pr_squash_comment's
    # own `gh_api` binding, but the comment listing runs through
    # `gh_comments.list_marker_comments`, which reads `gh_comments`'s own
    # `gh_api` binding — a separate name, patched separately.
    monkeypatch.setattr("forge.gh_comments.gh_api", _fake_api_with_title(VALID_TITLE))
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
    monkeypatch.setattr(
        "forge.gh_comments.gh_api", lambda *_a, **_kw: page_json(OLD_SQUASH_COMMENT)
    )
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
    EXPECTED BEHAVIOR: the title PATCH runs BEFORE the comment is
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
    # See the same comment in test_main_pr_mode_posts_then_deletes_superseded:
    # the comment listing reads `gh_comments`'s own `gh_api` binding.
    monkeypatch.setattr(
        "forge.gh_comments.gh_api", _fake_api_with_title("feat: an older title")
    )
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
    assert calls[0][:4] == ["gh", "api", "-X", "PATCH"]
    assert calls[0][4].endswith("/pulls/61")
    assert calls[0][-1] == f"title={VALID_TITLE}"
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
    assert not any(cmd[:4] == ["gh", "api", "-X", "PATCH"] for cmd in calls)


@pytest.mark.usefixtures("_cli_argv")
def test_main_pr_mode_reports_a_rejected_title_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: the title PATCH is rejected (no write access to the title).

    MOCK SETUP: the PATCH exits 1, the comment post exits 0.
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
        if cmd[:4] == ["gh", "api", "-X", "PATCH"]:
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
                page_json(OLD_SQUASH_COMMENT)
                if "body" in jq
                else page_json("2026-01-01T00:00:00Z")
            )
        if "pulls/61/comments" in path:
            return page_json("2026-02-01T00:00:00Z")
        return page_json()

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
    # `ensure_last`'s comment listing reads `gh_comments`'s own `gh_api`
    # binding (see the same comment above on the posting test).
    monkeypatch.setattr("forge.gh_comments.gh_api", _fake_api)
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
                page_json(OLD_SQUASH_COMMENT)
                if "body" in jq
                else page_json("2026-01-01T00:00:00Z")
            )
        return page_json()

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
    monkeypatch.setattr("forge.gh_comments.gh_api", _fake_api)
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fail)
    sys.argv.extend(["--pr", "61"])
    assert mod.main() == 0


@pytest.mark.usefixtures("_cli_argv")
def test_main_ensure_last_errors_without_an_existing_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR with no forge squash comment cannot be re-ordered — exit 1."""
    monkeypatch.setattr(mod, "gh_api", lambda *_a, **_kw: page_json())
    monkeypatch.setattr("forge.gh_comments.gh_api", lambda *_a, **_kw: page_json())
    sys.argv.extend(["--pr", "61"])
    assert mod.main() == 1


@pytest.mark.usefixtures("_cli_argv")
@pytest.mark.parametrize(
    ("total_words", "warns"),
    [(45, False), (52, True), (62, True)],
)
def test_main_dry_run_warns_on_stderr_only_over_the_ceiling(
    capsys: pytest.CaptureFixture[str], *, total_words: int, warns: bool
) -> None:
    """Behavior: dry run exits 0 with the body on stdout; warns only past 45 words.

    Far past the ceiling (62) still exits 0 — length never refuses.

    Args:
        total_words: Whole-message word count under test.
        warns: Whether stderr should carry the ``warning:`` line.
    """
    title, bullets = _message_of(total_words)
    sys.argv.extend(
        [
            "--dry-run",
            "--title",
            title,
            *(item for b in bullets for item in ("--bullet", b)),
        ]
    )
    assert mod.main() == 0
    captured = capsys.readouterr()
    assert captured.out == mod.build_body(title, bullets)
    assert f"{total_words}/{mod.MAX_WORDS} words" in captured.err
    assert ("forge-pr-squash-comment: warning: " in captured.err) is warns
    if warns:
        assert f"is {total_words} words" in captured.err


# ---------------------------------------------------------------------------
# Coverage claims
# ---------------------------------------------------------------------------

FRAGMENT_ITEMS = ["first delivered thing", "second delivered thing", "third one"]


@pytest.mark.parametrize(
    ("bullet", "claimed", "text"),
    [
        pytest.param("[1] text", {1}, "text", id="single"),
        pytest.param("[1,4-6] text", {1, 4, 5, 6}, "text", id="list-and-range"),
        pytest.param("[ 1 , 4-6 ] text", {1, 4, 5, 6}, "text", id="spaced"),
        pytest.param("plain text", set(), "plain text", id="no-claim"),
        pytest.param("[abc] text", set(), "[abc] text", id="malformed-stays-text"),
        pytest.param("[1]", {1}, "", id="claim-only"),
        pytest.param("[1][2] text", {1, 2}, "text", id="adjacent-groups"),
        pytest.param("[1] [3] more", {1, 3}, "more", id="spaced-groups"),
        pytest.param("[1-99999999999] x", set(), "x", id="oversized-range"),
        pytest.param("[1-102] x", set(), "x", id="range-just-over-the-cap"),
        pytest.param("[1-102,2] x", {2}, "x", id="oversized-piece-spares-others"),
        pytest.param("[1-101] x", set(range(1, 102)), "x", id="range-at-the-cap"),
        pytest.param("[\u00b2] z", set(), "[\u00b2] z", id="non-ascii-digit"),
        pytest.param("[" + "9" * 5000 + "] z", set(), "z", id="huge-number"),
    ],
)
def test_parse_coverage_forms(bullet: str, claimed: set[int], text: str) -> None:
    """Behavior: every claim shape yields its index set and the stripped text.

    Args:
        bullet: Raw ``--bullet`` value.
        claimed: Indices expected to be claimed.
        text: Bullet text expected to remain.
    """
    assert mod.parse_coverage(bullet) == (frozenset(claimed), text)


def test_check_coverage_names_every_unclaimed_item() -> None:
    """Behavior: each unclaimed item appears verbatim, claimed ones do not."""
    items = ["alpha item", "beta item", "gamma item", "delta item"]
    problems = mod._check_coverage([frozenset({2})], items)
    assert len(problems) == 1
    for item in (items[0], items[2], items[3]):
        assert item in problems[0]
    assert items[1] not in problems[0]


def test_check_coverage_passes_when_all_claimed_with_shared_claims() -> None:
    """Behavior: one index claimed by two bullets is allowed."""
    claims = [frozenset({1, 2}), frozenset({2, 3})]
    assert mod._check_coverage(claims, FRAGMENT_ITEMS) == []


def test_check_coverage_reports_out_of_range_claim() -> None:
    """Behavior: index 0 (1-based rule) and one past the end are both reported."""
    claims = [frozenset({0, 1, 2, 3, 4})]
    problems = mod._check_coverage(claims, FRAGMENT_ITEMS)
    assert len(problems) == 1
    assert "[0, 4]" in problems[0]


def test_check_coverage_skips_empty_items() -> None:
    """Behavior: a repo with no fragment items has nothing to cover."""
    assert mod._check_coverage([frozenset()], []) == []


def test_validate_skips_coverage_when_claims_is_none() -> None:
    """Behavior: programmatic callers passing no claims skip the rule."""
    assert mod.validate(VALID_TITLE, VALID_BULLETS, fragment_items=FRAGMENT_ITEMS) == []


def test_validate_refuses_empty_claims_against_fragment_items() -> None:
    """Behavior: claims all empty with items present still refuses.

    This is the CLI enforcement: ``main`` always passes a list, so bullets
    with no claims cannot dodge the check the way ``claims=None`` does.
    """
    claims = [frozenset()] * len(VALID_BULLETS)
    problems = mod.validate(
        VALID_TITLE, VALID_BULLETS, claims=claims, fragment_items=FRAGMENT_ITEMS
    )
    assert len(problems) == 1
    assert "covered by no squash bullet" in problems[0]


def _use_fragment_items(
    monkeypatch: pytest.MonkeyPatch, items: list[str], note: str = ""
) -> None:
    """Make ``main`` see *items* as the PR fragment's bullets.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        items: Fragment bullets to serve.
        note: Skip note to serve alongside them.
    """
    monkeypatch.setattr(mod, "_pr_fragment_items", lambda: (items, note))


def _bullet_args(bullets: list[str]) -> list[str]:
    """Interleave ``--bullet`` flags with *bullets*.

    Args:
        bullets: Raw bullet values.

    Returns:
        A flat argv fragment.
    """
    return [item for b in bullets for item in ("--bullet", b)]


@pytest.mark.usefixtures("_cli_argv")
def test_main_refuses_naming_unclaimed_fragment_bullets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: a message leaving a fragment bullet unclaimed exits 1, naming it."""
    _use_fragment_items(monkeypatch, FRAGMENT_ITEMS)
    bullets = ["[1] bullet alpha", "[2] bullet beta", "bullet gamma"]
    sys.argv.extend(["--dry-run", "--title", VALID_TITLE, *_bullet_args(bullets)])
    assert mod.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "forge-pr-squash-comment: 1 changelog bullet(s)" in captured.err
    assert FRAGMENT_ITEMS[2] in captured.err


@pytest.mark.usefixtures("_cli_argv")
def test_main_dry_run_never_posts_the_coverage_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: the claim is stripped before rendering and before counting.

    The body becomes the permanent ``main`` commit, so a leaked ``[1``
    would corrupt it. Words are counted from the stripped text too.
    """
    _use_fragment_items(monkeypatch, FRAGMENT_ITEMS)
    raw = [
        "[1] bullet alpha description",
        "[2] bullet beta description",
        "[3] bullet gamma description",
    ]
    sys.argv.extend(["--dry-run", "--title", VALID_TITLE, *_bullet_args(raw)])
    assert mod.main() == 0
    captured = capsys.readouterr()
    assert captured.out == mod.build_body(VALID_TITLE, VALID_BULLETS)
    assert "[1" not in captured.out
    assert f"13/{mod.MAX_WORDS} words" in captured.err
    breakdown = [ln for ln in captured.err.splitlines() if ln.startswith("  bullet")]
    assert all("[" not in ln for ln in breakdown)


@pytest.mark.usefixtures("_cli_argv")
def test_main_pr_mode_never_posts_the_coverage_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavior: the body handed to ``gh pr comment`` carries no claim."""
    bodies: list[str] = []

    def _fake_run(cmd: list[str], **kw: object) -> FakeProc:
        if cmd[:3] == ["gh", "pr", "comment"]:
            bodies.append(str(kw["input"]))
        return FakeProc()

    _use_fragment_items(monkeypatch, FRAGMENT_ITEMS)
    monkeypatch.setattr(mod, "gh_api", _fake_api_with_title(VALID_TITLE))
    monkeypatch.setattr("forge.gh_comments.gh_api", _fake_api_with_title(VALID_TITLE))
    monkeypatch.setattr("forge.pr_squash_comment.subprocess.run", _fake_run)
    raw = [
        "[1] bullet alpha description",
        "[2] bullet beta description",
        "[3] bullet gamma description",
    ]
    sys.argv.extend(["--pr", "61", "--title", VALID_TITLE, *_bullet_args(raw)])
    assert mod.main() == 0
    assert bodies == [mod.build_body(VALID_TITLE, VALID_BULLETS)]
    assert "[1" not in bodies[0]


@pytest.mark.usefixtures("_cli_argv")
def test_main_dry_run_prints_numbered_fragment_bullets_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: dry run shows the numbering a claim refers to; a post does not."""
    _use_fragment_items(monkeypatch, FRAGMENT_ITEMS)
    raw = [
        "[1] bullet alpha description",
        "[2] bullet beta description",
        "[3] bullet gamma description",
    ]
    sys.argv.extend(["--dry-run", "--title", VALID_TITLE, *_bullet_args(raw)])
    assert mod.main() == 0
    err = capsys.readouterr().err
    assert "changelog bullets to cover:" in err
    for i, item in enumerate(FRAGMENT_ITEMS, start=1):
        assert f"  {i}. {item}" in err


@pytest.mark.usefixtures("_cli_argv")
def test_main_dry_run_with_no_claims_still_prints_the_numbering_and_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: the call an author makes to learn the numbering shows it.

    A dry run with no claims fails the coverage rule; the listing must
    come out before that refusal, or the one caller who needs it never
    sees it.
    """
    _use_fragment_items(monkeypatch, FRAGMENT_ITEMS)
    raw = ["bullet alpha description", "bullet beta description", "bullet gamma"]
    sys.argv.extend(["--dry-run", "--title", VALID_TITLE, *_bullet_args(raw)])
    assert mod.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("changelog bullets to cover:") == 1
    for i, item in enumerate(FRAGMENT_ITEMS, start=1):
        assert f"  {i}. {item}" in captured.err
    assert "covered by no squash bullet" in captured.err


@pytest.mark.usefixtures("_cli_argv")
def test_main_dry_run_prints_the_numbering_once_when_it_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: moving the listing ahead of validation did not duplicate it."""
    _use_fragment_items(monkeypatch, FRAGMENT_ITEMS)
    raw = ["[1] bullet alpha", "[2] bullet beta", "[3] bullet gamma"]
    sys.argv.extend(["--dry-run", "--title", VALID_TITLE, *_bullet_args(raw)])
    assert mod.main() == 0
    assert capsys.readouterr().err.count("changelog bullets to cover:") == 1


@pytest.mark.usefixtures("_cli_argv")
def test_main_never_posts_claim_residue_from_stacked_groups(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: `[1][2]` and `[1] [3]` leave no bracket in the posted body.

    The body becomes the permanent ``main`` commit, so a surviving second
    claim group would corrupt it.
    """
    _use_fragment_items(monkeypatch, FRAGMENT_ITEMS)
    raw = [
        "[1][2] bullet alpha description",
        "[3] [1] bullet beta description",
        "bullet gamma description",
    ]
    sys.argv.extend(["--dry-run", "--title", VALID_TITLE, *_bullet_args(raw)])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "[" not in out
    assert "bullet alpha description" in out
    assert "bullet beta description" in out


@pytest.mark.usefixtures("_cli_argv")
def test_main_pr_mode_omits_numbered_fragment_bullets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behavior: the numbered listing is a dry-run aid, absent without --dry-run."""
    _use_fragment_items(monkeypatch, FRAGMENT_ITEMS)
    monkeypatch.setattr(mod, "gh_api", _fake_api_with_title(VALID_TITLE))
    monkeypatch.setattr("forge.gh_comments.gh_api", _fake_api_with_title(VALID_TITLE))
    monkeypatch.setattr(
        "forge.pr_squash_comment.subprocess.run", lambda *_a, **_kw: FakeProc()
    )
    raw = [
        "[1] bullet alpha description",
        "[2] bullet beta description",
        "[3] bullet gamma description",
    ]
    sys.argv.extend(["--pr", "61", "--title", VALID_TITLE, *_bullet_args(raw)])
    assert mod.main() == 0
    assert "changelog bullets to cover" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _pr_fragment_items — which fragment belongs to the PR
# ---------------------------------------------------------------------------


class _FakeFragment:
    """Stand-in for a parsed fragment: only ``body`` is read."""

    def __init__(self, body: str) -> None:
        """Hold *body*.

        Args:
            body: The fragment body text.
        """
        self.body = body


#: One `_pr_fragment_items` scenario, as each parametrized row carries it.
_FragmentSetup: TypeAlias = "dict[str, object]"


def _wire_fragment_collaborators(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    setup: _FragmentSetup,
) -> None:
    """Replace ``_pr_fragment_items``' collaborators with fakes.

    The scenario arrives as one mapping rather than six parameters
    because each parametrized row already carries it as one: spreading
    it over a signature only to re-assemble it at the call site buys
    nothing and needs a type-ignore to pass a dict through.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        tmp_path: Stand-in repository root.
        setup: The scenario — ``root_is_none`` (make ``repo_root`` report
            no repository), ``fragments_mode`` (what ``is_fragments_mode``
            answers), ``added`` (paths ``branch_added_fragments``
            reports), ``fragment`` (what ``validate_fragment`` parses,
            ``None`` for unreadable) and ``errors``.
    """
    root_is_none = bool(setup.get("root_is_none", False))
    fragments_mode = bool(setup.get("fragments_mode", True))
    added = cast("list[str]", setup.get("added", []))
    fragment = cast("_FakeFragment | None", setup.get("fragment"))
    errors = cast("list[str]", setup.get("errors", []))

    monkeypatch.setattr(mod, "repo_root", lambda: None if root_is_none else tmp_path)
    monkeypatch.setattr(mod, "is_fragments_mode", lambda _root: fragments_mode)
    monkeypatch.setattr(
        mod, "load_config", lambda _root: types.SimpleNamespace(base_branch="main")
    )
    monkeypatch.setattr(mod, "resolve_pr_base_ref", lambda _root, _base: "origin/main")
    # The real one takes base_ref keyword-only; the double accepts and
    # ignores it, so the production call site stays exercised as written.
    monkeypatch.setattr(
        mod, "branch_added_fragments", lambda _root, **_kwargs: list(added)
    )
    monkeypatch.setattr(mod, "validate_fragment", lambda _path: (fragment, errors))


@pytest.mark.parametrize(
    ("setup", "items", "note_part"),
    [
        pytest.param(
            {"root_is_none": True, "added": [], "fragment": None, "errors": []},
            [],
            "not in a repository",
            id="not-a-repo",
        ),
        pytest.param(
            {"fragments_mode": False, "added": [], "fragment": None, "errors": []},
            [],
            None,
            id="fragments-mode-off-no-note",
        ),
        pytest.param(
            {"added": [], "fragment": None, "errors": []},
            [],
            "adds no changelog fragment",
            id="no-fragment-added",
        ),
        pytest.param(
            {
                "added": ["changelog.d/b.fixed.md", "changelog.d/a.added.md"],
                "fragment": None,
                "errors": [],
            },
            [],
            "adds 2 changelog fragments "
            "(changelog.d/a.added.md, changelog.d/b.fixed.md)",
            id="several-fragments",
        ),
        pytest.param(
            {
                "added": ["changelog.d/a.added.md"],
                "fragment": None,
                "errors": ["a.added.md: empty entry body"],
            },
            [],
            "could not be read (a.added.md: empty entry body)",
            id="unreadable-fragment",
        ),
        pytest.param(
            {
                "added": ["changelog.d/a.added.md"],
                "fragment": _FakeFragment("- one\n  wrapped\n- two\n"),
                "errors": [],
            },
            ["one wrapped", "two"],
            None,
            id="happy-path",
        ),
    ],
)
def test_pr_fragment_items_skip_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    setup: dict[str, object],
    items: list[str],
    note_part: str | None,
) -> None:
    """Behavior: every skip path returns no items and says why; happy path returns them.

    Fragments mode off is the odd one out: no items and no note, since a
    repo without fragments has nothing to explain.

    Args:
        setup: Keyword arguments for the collaborator fakes.
        items: Expected fragment items.
        note_part: Substring expected in the note, or ``None`` for no note.
    """
    _wire_fragment_collaborators(monkeypatch, tmp_path, setup)
    got_items, note = _REAL_PR_FRAGMENT_ITEMS()
    assert got_items == items
    if note_part is None:
        assert note is None
    else:
        assert note is not None
        assert note_part in note
