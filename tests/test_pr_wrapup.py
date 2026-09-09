"""Unit tests for forge.pr_wrapup — the validator, collapse/post lifecycle, and CLI.

# MOCKING STRATEGY: `validate_wrapup` is pure (text in, violations out) and
# is exercised directly with hand-built bodies — no mocking at all.
# `_collapse` / `post_wrapup` / `main` patch the names `pr_wrapup` imports
# from `forge.gh_comments` and `forge.pr_squash_comment` directly in its own
# module namespace (``forge.pr_wrapup.<name>``) — the paging/attribution
# contract behind those names is `gh_comments`'s own, covered in
# ``tests/test_gh_comments.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from forge import pr_wrapup as mod
from tests.conftest import CapturedCalls


if TYPE_CHECKING:
    from pathlib import Path


VERIFIED_AT = "verified-at: 7ab3e4e   (PR #56, branch fix/foo)"

# One clean, one-line body per required section — the happy-path baseline
# every head-contract test layers on top of.
_CLEAN_SECTION_BODIES: dict[str, str] = {
    "Design Check": "PASS — no issues.",
    "Security Review": "PASS — no issues.",
    "Documentation Check": "PASS — no issues.",
    "Code Quality": "PASS",
    "CI Status": "PASS",
    "Recommendation": "Approve and merge.",
}


def _sections_text(
    *, exclude: str | None = None, overrides: dict[str, str] | None = None
) -> str:
    """Render the required sections, one clean line each unless overridden.

    Args:
        exclude: A section title to omit entirely, or ``None`` for all six.
        overrides: Per-title body text replacing the clean default.

    Returns:
        The ``## `` section text, ready to append after the head.
    """
    overrides = overrides or {}
    parts = [
        f"## {title}\n{overrides.get(title, line)}\n"
        for title, line in _CLEAN_SECTION_BODIES.items()
        if title != exclude
    ]
    return "\n".join(parts)


def _body(
    head_lines: list[str],
    *,
    exclude_section: str | None = None,
    overrides: dict[str, str] | None = None,
) -> str:
    """Assemble a wrap-up body from *head_lines* plus the required sections.

    Args:
        head_lines: Lines before the first ``## `` section.
        exclude_section: A required section title to drop, or ``None``.
        overrides: Per-title section body overrides, or ``None``.

    Returns:
        The full wrap-up text.
    """
    return (
        "\n".join(head_lines)
        + "\n\n"
        + _sections_text(exclude=exclude_section, overrides=overrides)
    )


# ---------------------------------------------------------------------------
# Composite fixtures — realistic bodies reused across several assertions
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_wrapup_body() -> str:
    """A realistic, fully report-by-exception wrap-up: valid()."""
    return (
        f"{VERIFIED_AT}\n"
        "wrapup-mode: full\n"
        "This PR adds the forge-pr-wrapup CLI: it validates a wrap-up body "
        "against the report-by-exception rule, posts it as the PR's newest "
        "comment, collapses every earlier marker-carrying wrap-up into a "
        "details block, and re-posts the squash-merge comment so it stays "
        "last.\n"
        "prior-art-searched: yes\n"
        "- searched for existing wrap-up validators; forge-pr-squash-comment "
        "was the closest prior art, so its comment-lifecycle plumbing (list, "
        "post, delete, patch) was extracted into a shared gh_comments module "
        "both CLIs now import\n"
        "\n" + _sections_text()
    )


@pytest.fixture
def realistic_findings_wrapup_body() -> str:
    """A wrap-up carrying one real (non-clean) finding: valid()."""
    return (
        f"{VERIFIED_AT}\n"
        "wrapup-mode: full\n"
        "This PR adds the forge-pr-wrapup CLI, sharing its GitHub comment "
        "plumbing with forge-pr-squash-comment through a new gh_comments "
        "module.\n"
        "prior-art-searched: yes\n"
        "- forge-pr-squash-comment's listing/post/delete helpers were the "
        "closest prior art and are now shared, not duplicated\n"
        "\n"
        + _sections_text(
            overrides={
                "Design Check": (
                    "Found: the wrap-up and squash-comment CLIs duplicated "
                    "paginated-listing and attribution-check logic. Disposition: "
                    "extracted both into `forge.gh_comments`, imported by each "
                    "CLI; both existing test suites re-pointed at the shared "
                    "module."
                )
            }
        )
    )


@pytest.fixture
def overlong_narrating_wrapup_body() -> str:
    """A narrated, header-less wrap-up: many distinguishable violations."""
    return (
        "This wrap-up forgot its header entirely, so nothing here can be "
        "trusted as a verification record.\n"
        "Here is a second sentence narrating what happened during the "
        "review, at some length, for no good reason at all.\n"
        "And a third line piling on more narration that the "
        "report-by-exception rule exists specifically to forbid in a "
        "wrap-up.\n"
        "\n"
        "## Design Check\n"
        "PASS — reviewed every changed file line by line and found nothing "
        "worth flagging, though it took a while to be sure of that.\n"
        "Also double-checked the design against the FOUNDATION principles "
        "document just to be thorough about it.\n"
        "\n"
        "## Security Review\n"
        "PASS — no secrets, no injection points, nothing suspicious in the "
        "diff after a careful manual read-through of every hunk.\n"
        "Ran a mental threat model over the new subprocess calls as well, "
        "just in case.\n"
        "\n"
        "## Documentation Check\n"
        "PASS — docstrings look fine, Args and Returns match the "
        "signatures, nothing else worth mentioning here today.\n"
        "Spent extra time confirming every Google-style section was present "
        "and accurate.\n"
        "\n"
        "## Code Quality\n"
        "PASS — ruff is clean, tests pass, nothing else to say.\n"
        "Also re-ran the whole suite a second time for good measure.\n"
        "\n"
        "## CI Status\n"
        "PASS — every job green on the latest push.\n"
        "Refreshed the status page twice to be sure nothing flaked.\n"
        "\n"
        "## Recommendation\n"
        "Approve and merge whenever convenient for the reviewer.\n"
        "No further changes are required before this can land.\n"
    )


# ---------------------------------------------------------------------------
# validate_wrapup — header contract
# ---------------------------------------------------------------------------


def test_validate_wrapup_accepts_a_clean_report_by_exception_body(
    clean_wrapup_body: str,
) -> None:
    """A fully clean, one-line-per-section wrap-up has no violations."""
    assert mod.validate_wrapup(clean_wrapup_body) == []


def test_validate_wrapup_accepts_a_body_with_one_real_finding(
    realistic_findings_wrapup_body: str,
) -> None:
    """One non-clean section earns its extra word-budget allowance."""
    assert mod.validate_wrapup(realistic_findings_wrapup_body) == []


def test_validate_wrapup_reports_many_violations_for_a_narrated_body(
    overlong_narrating_wrapup_body: str,
) -> None:
    """A header-less, narrated, over-budget body fails on several distinct rules."""
    problems = mod.validate_wrapup(overlong_narrating_wrapup_body)
    assert any("verified-at" in p for p in problems)
    assert any("summary line" in p for p in problems)
    assert any("is clean (PASS) but runs" in p for p in problems)
    assert any("is a status line" in p for p in problems)
    assert any("words over a budget" in p for p in problems)
    assert len(problems) >= 5


def test_validate_wrapup_requires_verified_at_first_line() -> None:
    """A body whose first line is not the verified-at header is refused."""
    body = _body(["not a header", "One summary line."])
    assert any("verified-at" in p for p in mod.validate_wrapup(body))


def test_validate_wrapup_rejects_zero_summary_lines() -> None:
    """A header with no summary sentence at all is refused."""
    problems = mod.validate_wrapup(_body([VERIFIED_AT]))
    assert any("found 0" in p for p in problems)


def test_validate_wrapup_rejects_two_summary_lines() -> None:
    """More than one summary sentence is narration, and is refused."""
    body = _body([VERIFIED_AT, "First summary sentence.", "Second summary sentence."])
    assert any("found 2" in p for p in mod.validate_wrapup(body))


def test_validate_wrapup_does_not_count_title_mode_comment_or_prior_art_lines() -> None:
    """Non-metadata lines don't count toward the one-summary-line budget.

    A `# ` title, `wrapup-mode:`, an HTML comment, and prior-art evidence
    do not count toward the one-summary-line budget.
    """
    body = _body(
        [
            VERIFIED_AT,
            "# PR Wrap-up",
            "wrapup-mode: full",
            "<!-- internal note -->",
            "The one real summary line.",
            "prior-art-searched: yes",
            "- evidence line one",
            "- evidence line two",
        ]
    )
    assert mod.validate_wrapup(body) == []


# ---------------------------------------------------------------------------
# validate_wrapup — required sections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", mod.REQUIRED_SECTIONS)
def test_validate_wrapup_rejects_a_missing_required_section(missing: str) -> None:
    """Each required section's absence is reported by name.

    Args:
        missing: The required section title to omit from the body.
    """
    body = _body([VERIFIED_AT, "One summary line."], exclude_section=missing)
    problems = mod.validate_wrapup(body)
    assert any(f"missing section `## {missing}`" in p for p in problems)


def test_validate_wrapup_refuses_a_multiline_clean_section() -> None:
    """A clean (non-status) section spanning more than one line is narration."""
    body = _body(
        [VERIFIED_AT, "One summary line."],
        overrides={
            "Design Check": "PASS — no issues.\nAlso double-checked everything twice."
        },
    )
    problems = mod.validate_wrapup(body)
    assert any("is clean (PASS) but runs 2 lines" in p for p in problems)


def test_validate_wrapup_accepts_a_multiline_findings_section() -> None:
    """Multiline findings sections are permitted under the extra budget.

    A non-clean (findings) section may run multiple lines — that is what
    the extra word-budget allowance is for.
    """
    body = _body(
        [VERIFIED_AT, "One summary line."],
        overrides={
            "Design Check": (
                "Found: an unused import lingered in pr_wrapup.py. Disposition: "
                "removed before commit; no behavior change."
            )
        },
    )
    assert mod.validate_wrapup(body) == []


def test_validate_wrapup_refuses_a_multiline_status_section() -> None:
    """A status section is one line whatever it says — findings or not."""
    body = _body(
        [VERIFIED_AT, "One summary line."],
        overrides={"Code Quality": "PASS\nAlso re-ran the suite twice."},
    )
    problems = mod.validate_wrapup(body)
    assert any("`## Code Quality` is a status line" in p for p in problems)


# ---------------------------------------------------------------------------
# validate_wrapup — word budget
# ---------------------------------------------------------------------------


def test_validate_wrapup_refuses_an_over_budget_clean_body() -> None:
    """A verbose but line-count-clean body can still bust the flat 120-word cap."""
    status_long = (
        "PASS — reviewed the change carefully end to end, re-read every "
        "touched file twice, and found absolutely nothing at all worth "
        "calling out or flagging here today."
    )
    body = _body(
        [
            VERIFIED_AT,
            (
                "This wrap-up exists purely to validate the shared word-budget "
                "contract with a longer than usual but still single-line summary "
                "sentence for the test, padded out a little further so the total "
                "comfortably clears the default budget."
            ),
        ],
        overrides={
            "Design Check": status_long,
            "Security Review": status_long,
            "Documentation Check": status_long,
            "Recommendation": (
                "Approve and merge whenever the reviewer is ready to do so, "
                "no blockers remain."
            ),
        },
    )
    problems = mod.validate_wrapup(body)
    assert any(f"words over a budget of {mod.WORD_BUDGET}" in p for p in problems)


def test_validate_wrapup_accepts_the_same_body_once_one_section_is_a_real_finding() -> (
    None
):
    """Real findings earn back the per-finding allowance.

    Converting one padded clean section into a real finding earns the
    per-finding allowance back — same rough length, no other change.
    """
    status_long = (
        "PASS — reviewed the change carefully end to end, re-read every "
        "touched file twice, and found absolutely nothing at all worth "
        "calling out or flagging here today."
    )
    finding = (
        "Found: the Design Check reviewer had to trace one shared helper "
        "through two call sites before confirming the refactor preserved "
        "behavior end to end across both callers. Disposition: no change "
        "needed; documented here since it took real investigation rather "
        "than a glance."
    )
    body = _body(
        [
            VERIFIED_AT,
            (
                "This wrap-up exists purely to validate the shared word-budget "
                "contract with a longer than usual but still single-line summary "
                "sentence for the test, padded out a little further so the total "
                "comfortably clears the default budget."
            ),
        ],
        overrides={
            "Design Check": finding,
            "Security Review": status_long,
            "Documentation Check": status_long,
            "Recommendation": (
                "Approve and merge whenever the reviewer is ready to do so, "
                "no blockers remain."
            ),
        },
    )
    assert mod.validate_wrapup(body) == []


def test_validate_wrapup_excludes_a_large_fenced_block_from_the_word_count() -> None:
    """A fenced block (quoted tool output) never counts toward the budget."""
    fenced = (
        "```\n"
        + "\n".join(["this fenced noise line has several words in it"] * 30)
        + "\n```"
    )
    body = _body(
        [VERIFIED_AT, "One clean summary line describing the change."],
        overrides={"Design Check": f"PASS — no issues.\n\n{fenced}"},
    )
    assert mod.validate_wrapup(body) == []


# ---------------------------------------------------------------------------
# validate_wrapup — attribution wiring
# ---------------------------------------------------------------------------


def test_validate_wrapup_wires_the_shared_attribution_gate() -> None:
    """An AI-attribution phrase anywhere in the body is rejected.

    The phrase list, the path-shaped exemption, and the bare-vendor-token
    backstop are ``gh_comments.validate_no_ai_attribution``'s own contract,
    covered in ``tests/test_gh_comments.py``; this only pins that
    ``validate_wrapup`` calls into it.
    """
    body = _body(
        [VERIFIED_AT, "One summary line."],
        overrides={"Recommendation": "Approve and merge — Generated with Claude."},
    )
    problems = mod.validate_wrapup(body)
    assert any("pattern detected" in p for p in problems)


# ---------------------------------------------------------------------------
# _collapse
# ---------------------------------------------------------------------------


def test_collapse_patches_one_marker_comment_into_a_details_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: one earlier wrap-up is still visible when a new one posts.

    MOCK SETUP: ``patch_comment`` is faked to record its call.
    EXPECTED BEHAVIOR: it is called once with a ``<details>`` wrapper naming
    both SHAs, the original body kept verbatim inside; returns 1.
    """
    captured = CapturedCalls()

    def _fake_patch(comment_id: int, body: str) -> bool:
        """Record the patch call as ``[id, body]`` and report success.

        Args:
            comment_id: Comment id under edit.
            body: The new body being written.

        Returns:
            ``True`` (the edit is accepted).
        """
        captured.calls.append([str(comment_id), body])
        return True

    monkeypatch.setattr(mod, "patch_comment", _fake_patch)
    old_body = "verified-at: 1234567 (PR #1)\nOld wrap-up body text."
    collapsed = mod._collapse([{"id": 42, "body": old_body}], "89abcde")
    assert collapsed == 1
    assert len(captured.calls) == 1
    comment_id, new_body = captured.calls[0]
    assert comment_id == "42"
    assert (
        "<details><summary>Superseded by verified-at 89abcde — this wrap-up "
        "verified 1234567</summary>" in new_body
    )
    assert old_body in new_body


def test_collapse_skips_an_already_collapsed_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body already starting with `<details>` is left alone — no double wrap."""

    def _fail_patch(comment_id: int, body: str) -> bool:
        """Fail the test if an already-collapsed comment is patched again.

        Args:
            comment_id: Unused.
            body: Unused.

        Raises:
            AssertionError: Always.
        """
        msg = "already-collapsed comment must not be patched again"
        raise AssertionError(msg)

    monkeypatch.setattr(mod, "patch_comment", _fail_patch)
    existing = [
        {"id": 42, "body": "<details><summary>old</summary>\nbody\n</details>\n"}
    ]
    assert mod._collapse(existing, "89abcde") == 0


def test_collapse_uses_a_placeholder_sha_when_none_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comment with no `verified-at:` line names `?` as the old SHA."""
    captured = CapturedCalls()
    monkeypatch.setattr(
        mod,
        "patch_comment",
        lambda comment_id, body: captured.calls.append([str(comment_id), body]) or True,
    )
    mod._collapse([{"id": 42, "body": "no header in this comment at all"}], "89abcde")
    assert "this wrap-up verified ?</summary>" in captured.calls[0][1]


def test_collapse_does_not_count_a_failed_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected edit is not counted as collapsed — it stays visible, uncollapsed."""
    monkeypatch.setattr(mod, "patch_comment", lambda *_a, **_kw: False)
    existing = [{"id": 42, "body": "verified-at: 1234567\nold body"}]
    assert mod._collapse(existing, "89abcde") == 0


# ---------------------------------------------------------------------------
# post_wrapup
# ---------------------------------------------------------------------------


def test_post_wrapup_runs_list_then_post_then_collapse_then_ensure_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: a normal post with one earlier wrap-up to collapse.

    MOCK SETUP: every collaborator is faked to append its own call marker
    to one shared ``CapturedCalls`` list.
    EXPECTED BEHAVIOR: listing runs before the post (so the new comment is
    never mistaken for one it supersedes), the collapse after, and
    ``ensure_last`` last (keeps the squash comment newest).
    """
    captured = CapturedCalls()

    def _fake_list(pr_number: int, marker: str) -> list[dict[str, object]]:
        """Record the listing call and return one earlier wrap-up.

        Args:
            pr_number: PR number passed through by the caller.
            marker: Marker string passed through by the caller.

        Returns:
            One canned earlier wrap-up comment.
        """
        captured.calls.append(["list", str(pr_number), marker])
        return [{"id": 1, "body": "verified-at: 1234567\nold"}]

    def _fake_post(pr_number: int, body: str) -> int:
        """Record the post call and report success.

        Args:
            pr_number: PR number passed through by the caller.
            body: Stamped body passed through by the caller (unused).

        Returns:
            ``0``.
        """
        captured.calls.append(["post", str(pr_number)])
        return 0

    def _fake_patch(comment_id: int, body: str) -> bool:
        """Record the collapse call and report success.

        Args:
            comment_id: Comment id passed through by the caller.
            body: New body passed through by the caller (unused).

        Returns:
            ``True``.
        """
        captured.calls.append(["patch", str(comment_id)])
        return True

    def _fake_ensure_last(pr_number: int) -> int:
        """Record the ensure_last call and report success.

        Args:
            pr_number: PR number passed through by the caller.

        Returns:
            ``0``.
        """
        captured.calls.append(["ensure_last", str(pr_number)])
        return 0

    monkeypatch.setattr(mod, "list_marker_comments", _fake_list)
    monkeypatch.setattr(mod, "post_new_comment", _fake_post)
    monkeypatch.setattr(mod, "patch_comment", _fake_patch)
    monkeypatch.setattr(mod, "ensure_last", _fake_ensure_last)

    rc = mod.post_wrapup(61, "verified-at: 89abcde\nnew wrap-up body")
    assert rc == 0
    assert captured.calls == [
        ["list", "61", mod.WRAPUP_MARKER],
        ["post", "61"],
        ["patch", "1"],
        ["ensure_last", "61"],
    ]


def test_post_wrapup_returns_early_on_post_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed post never runs the collapse or the ensure-last re-post."""

    def _fail(*_a: object, **_kw: object) -> None:
        """Fail the test if called after a failed post.

        Args:
            *_a: Positional arguments (unused).
            **_kw: Keyword arguments (unused).

        Raises:
            AssertionError: Always.
        """
        msg = "must not run after a failed post"
        raise AssertionError(msg)

    monkeypatch.setattr(mod, "list_marker_comments", lambda *_a, **_kw: [])
    monkeypatch.setattr(mod, "post_new_comment", lambda *_a, **_kw: 3)
    monkeypatch.setattr(mod, "patch_comment", _fail)
    monkeypatch.setattr(mod, "ensure_last", _fail)
    assert mod.post_wrapup(61, "verified-at: 89abcde\nbody") == 3


def test_post_wrapup_still_posts_when_listing_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed listing only warns — the new wrap-up is posted regardless."""
    posted: list[str] = []
    monkeypatch.setattr(mod, "list_marker_comments", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        mod, "post_new_comment", lambda _pr, body: posted.append(body) or 0
    )
    monkeypatch.setattr(mod, "ensure_last", lambda _pr: 0)
    with caplog.at_level(logging.WARNING):
        rc = mod.post_wrapup(61, "verified-at: 89abcde\nbody")
    assert rc == 0
    assert posted
    assert "could not list earlier wrap-ups" in caplog.text


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_validate_prints_each_violation_and_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every violation is printed with the `pr-wrapup:` prefix; exit 2."""
    path = tmp_path / "wrapup.md"
    path.write_text("not a header\n", encoding="utf-8")
    assert mod.main(["validate", str(path)]) == 2
    out = capsys.readouterr().out
    assert "pr-wrapup: " in out
    assert "verified-at" in out


def test_main_validate_reports_a_clean_body_as_valid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    clean_wrapup_body: str,
) -> None:
    """A clean body prints the `is valid.` confirmation and exits 0."""
    path = tmp_path / "wrapup.md"
    path.write_text(clean_wrapup_body, encoding="utf-8")
    assert mod.main(["validate", str(path)]) == 0
    assert f"{path} is valid." in capsys.readouterr().out


def test_main_reports_an_unreadable_file_as_exit_two(
    tmp_path: Path,
) -> None:
    """A missing body file is a clean exit 2, not an unhandled exception."""
    missing = tmp_path / "does-not-exist.md"
    assert mod.main(["validate", str(missing)]) == 2


def test_main_post_with_violations_never_calls_post_wrapup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation runs before posting — an invalid body never reaches gh."""

    def _fail(*_a: object, **_kw: object) -> int:
        """Fail the test if `post_wrapup` runs for an invalid body.

        Args:
            *_a: Positional arguments (unused).
            **_kw: Keyword arguments (unused).

        Raises:
            AssertionError: Always.
        """
        msg = "must not post an invalid wrap-up"
        raise AssertionError(msg)

    path = tmp_path / "wrapup.md"
    path.write_text("not a header\n", encoding="utf-8")
    monkeypatch.setattr(mod, "post_wrapup", _fail)
    assert mod.main(["post", "--pr", "61", "--body-file", str(path)]) == 2


def test_main_post_delegates_to_post_wrapup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_wrapup_body: str,
) -> None:
    """A clean body's ``post`` delegates to `post_wrapup` and returns its rc."""
    calls: list[tuple[int, str]] = []

    def _fake_post_wrapup(pr_number: int, text: str) -> int:
        """Record the delegation call and return a distinguishing rc.

        Args:
            pr_number: PR number passed through by ``main``.
            text: Validated wrap-up text passed through by ``main``.

        Returns:
            ``5`` — a value ``main`` cannot have produced itself.
        """
        calls.append((pr_number, text))
        return 5

    path = tmp_path / "wrapup.md"
    path.write_text(clean_wrapup_body, encoding="utf-8")
    monkeypatch.setattr(mod, "post_wrapup", _fake_post_wrapup)
    assert mod.main(["post", "--pr", "61", "--body-file", str(path)]) == 5
    assert calls == [(61, clean_wrapup_body)]
