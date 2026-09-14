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

import pytest

from forge import git_utils
from forge import pr_squash_comment as mod
from tests.conftest import FakeProc, page_json


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
def test_check_title_accepts_conventional_forms(title: str) -> None:
    """Conventional-commit titles in known shapes report no problems.

    Pins the accepted title shapes FOUNDATION §6 names.

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
    ],
)
def test_check_title_rejects_bad_forms(title: str) -> None:
    """Empty, multi-line, or non-conventional titles report a problem.

    Pins which malformed shapes are rejected.

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


def test_check_word_count_accepts_at_cap() -> None:
    """Exactly MAX_WORDS across title + bullets reports no problems (cap inclusive)."""
    title = "feat: title with five words"  # 5 words
    bullets = [" ".join(["word"] * 15)] * 3  # 45 words
    assert mod._check_word_count(title, bullets) == []


def test_check_word_count_over_cap_reports_header_and_breakdown() -> None:
    """MAX_WORDS + 1 reports the header line and a per-part breakdown.

    Pins the observed count, the cut amount, that each bullet's 1-based
    label (``bullet 1``/``bullet 2``/``bullet 3``) appears verbatim
    rather than a 0-based or unlabeled variant, and that the breakdown
    orders title before bullet 1 before bullet 2 before bullet 3 — a
    reversed or scrambled breakdown must fail this test. The exact
    column spacing is still an implementation detail this test does
    not pin.
    """
    # Deliberately avoids the word "title" in the subject text itself,
    # so counting occurrences of the label below isn't thrown off by a
    # coincidental match in the previewed content.
    title = "feat: message with five words here"  # 6 words
    bullets = [" ".join(["word"] * 15)] * 3  # 45 words
    problems = mod._check_word_count(title, bullets)
    text = "\n".join(problems).lower()
    assert "51 words" in text
    assert "(cut 1)" in text
    assert text.count("bullet") >= 3
    assert text.count("title") == 1
    assert "bullet 1" in text
    assert "bullet 2" in text
    assert "bullet 3" in text
    lines = text.splitlines()
    title_idx = next(i for i, line in enumerate(lines) if line.startswith("  title"))
    bullet_1_idx = next(i for i, line in enumerate(lines) if "bullet 1" in line)
    bullet_2_idx = next(i for i, line in enumerate(lines) if "bullet 2" in line)
    bullet_3_idx = next(i for i, line in enumerate(lines) if "bullet 3" in line)
    assert title_idx < bullet_1_idx < bullet_2_idx < bullet_3_idx


def test_check_word_count_truncates_a_long_part_with_ellipsis() -> None:
    """A long part's preview is truncated, never the full text, in the breakdown.

    Pins that the breakdown stays scannable — a bullet long enough to
    matter for the cap must not reproduce itself in full.
    """
    title = "feat: short title"  # 3 words
    long_bullet = "alphabetazetadelta" * 5  # one long word, no spaces
    other_bullets = [" ".join(["word"] * 24)] * 2  # 48 words
    bullets = [long_bullet, *other_bullets]
    problems = mod._check_word_count(title, bullets)
    text = "\n".join(problems)
    assert long_bullet not in text
    assert long_bullet[:30] in text
    assert "…" in text


def test_check_word_count_multiline_title_preview_shows_first_line_only() -> None:
    """A multi-line title's preview stops at its first line.

    Pins that a later line never leaks into the breakdown — the
    preview is one line, matching the rest of the per-part rows.
    """
    title = "feat: firstlinemarker\nsecondlinemarker should not appear in preview"
    bullets = [" ".join(["word"] * 20)] * 3  # 60 words, well over cap with title
    problems = mod._check_word_count(title, bullets)
    text = "\n".join(problems)
    assert "firstlinemarker" in text
    assert "secondlinemarker" not in text


def test_check_word_count_preview_drops_terminal_escape_sequences() -> None:
    """A bullet's raw escape byte is stripped before the breakdown is built.

    Pins that a non-printable control byte (e.g. an ANSI color code) never
    survives into the per-part preview, which is written raw to stderr —
    while the bullet's other, printable words still appear, since the
    fix filters characters rather than blanking the whole part.
    """
    title = "feat: title with five words"  # 5 words
    escape_bullet = "escape \x1b[31m alert message here"  # 5 tokens
    other_bullets = [" ".join(["word"] * 21)] * 2  # 42 words
    bullets = [escape_bullet, *other_bullets]
    problems = mod._check_word_count(title, bullets)
    text = "\n".join(problems)
    assert "\x1b" not in text
    assert "escape" in text
    assert "alert" in text
    assert "message" in text
    assert "here" in text


def test_check_word_count_whitespace_only_bullet_shows_as_empty() -> None:
    """A whitespace-only bullet's preview reads as empty, not blank.

    Pins that the breakdown makes an empty part visible rather than
    rendering an indistinguishable blank line.
    """
    title = " ".join(["word"] * 40)  # 40 words
    bullets = ["   ", " ".join(["word"] * 15)]  # whitespace-only + 15 words = 55 total
    problems = mod._check_word_count(title, bullets)
    text = "\n".join(problems)
    assert "(empty)" in text


def test_check_word_count_zero_bullets_breakdown_covers_only_the_title() -> None:
    """With no bullets, the breakdown names the title and mentions no bullet.

    Pins that the breakdown only ever lists parts that exist — zero
    bullets means zero bullet rows, not empty placeholders.
    """
    title = " ".join(["word"] * 60)  # 60 words, no bullets
    problems = mod._check_word_count(title, [])
    text = "\n".join(problems).lower()
    assert "60 words" in text
    assert "title" in text
    assert "bullet" not in text


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
    calls: list[tuple[int, str]] = []

    def _fake_list(pr_number: int, marker: str) -> list[dict[str, object]]:
        """Record the delegation call and return a canned result.

        Args:
            pr_number: PR number passed through by the caller.
            marker: Marker string passed through by the caller.

        Returns:
            A single canned comment mapping.
        """
        calls.append((pr_number, marker))
        return [OLD_SQUASH_COMMENT]

    monkeypatch.setattr(mod, "list_marker_comments", _fake_list)
    assert mod._list_squash_comments(61) == [OLD_SQUASH_COMMENT]
    assert calls == [(61, mod.SQUASH_MARKER)]


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
    assert "13/50 words" in captured.err
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
    """Three broken rules all reach stderr; the prefix opens each once.

    SCENARIO: title, bullet count, and word count are all broken at
    once — the last one's problem is multi-line (header + breakdown).
    EXPECTED BEHAVIOR: all three problems are visible in one run, and
    the ``forge-pr-squash-comment: `` prefix opens only the FIRST line
    of each problem — the word-count problem's breakdown lines stay
    unprefixed, since carrying the prefix would make a continuation
    line read as a second, unrelated problem.
    """
    long_bullet = " ".join(["word"] * 55)  # alone pushes the total over MAX_WORDS
    sys.argv.extend(
        ["--dry-run", "--title", "not conventional", "--bullet", long_bullet]
    )
    assert mod.main() == 1
    err = capsys.readouterr().err
    prefix = "forge-pr-squash-comment: "
    lines = err.splitlines()
    prefixed_lines = [line for line in lines if line.startswith(prefix)]
    unprefixed_lines = [line for line in lines if line and not line.startswith(prefix)]
    assert len(prefixed_lines) == 3
    assert any("conventional-commit" in line for line in prefixed_lines)
    assert any("requires 3-5" in line for line in prefixed_lines)
    assert any("words" in line for line in prefixed_lines)
    # The word-count problem's breakdown lines are real and unprefixed.
    assert unprefixed_lines


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
