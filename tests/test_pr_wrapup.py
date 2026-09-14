"""Unit tests for forge.pr_wrapup — validate/compose/post lifecycle, and the CLI.

# MOCKING STRATEGY: `validate_wrapup`, `refresh_sections` and `post_gates` are
# pure (text/mapping in, text or violations out) and are exercised directly
# with hand-built bodies — no mocking at all. `_collapse` / `post_wrapup` /
# `main` (`validate`/`post` subcommands) patch the names `pr_wrapup` imports
# from `forge.gh_comments` and `forge.pr_squash_comment` directly in its own
# module namespace (``forge.pr_wrapup.<name>``) — the paging/attribution
# contract behind those names is `gh_comments`'s own, covered in
# ``tests/test_gh_comments.py``. `main`'s `post` subcommand additionally
# patches `gh_pr_view` / `fetch_quietly` / `behind_ahead` / `repo_root` (a real
# ``tmp_path`` repo, never the checkout this suite runs from) and
# `continuation_append.main` — all real `gh`/git seams `post` now reaches
# before it ever gets to `post_wrapup`. One `post` test deliberately leaves
# `continuation_append.main` unpatched (a real ``tmp_path`` repo) to pin the
# ``--`` argv-separator contract for a dash-prefixed PR title. The emergency
# waiver (`_is_emergency_post`) is exercised with a real sentinel file
# (`forge.emergency.write_state`), never a patched `read_state`. `main`'s
# `compose` subcommand runs against a real (`init_git_repo`) ``tmp_path`` repo
# too, patching only `repo_root` plus whichever of `gh_pr_view` /
# `wrapup_freshness` / `run_gate_evidence` a given plan mode reaches (delta,
# light-regen respectively); an armed emergency uses a real sentinel file
# (`forge.emergency.write_state`), never a patched `armed_state`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from forge import pr_wrapup as mod
from forge.emergency import EmergencyState, write_state
from forge.pr_plan import WrapupFreshness
from forge.pr_wrapup_compose import slot
from tests.conftest import CapturedCalls, init_git_repo


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
# validate_wrapup — unfilled compose slots reported first
# ---------------------------------------------------------------------------


def test_validate_wrapup_reports_unfilled_slots_before_other_violations() -> None:
    """SCENARIO: a compose draft with an unfilled slot AND a missing section.

    EXPECTED BEHAVIOR: the unfilled-slot violation is listed first — it
    names the concrete cause an author fixes before the vaguer
    missing-section message even matters.
    """
    body = _body([VERIFIED_AT, slot("summary")], exclude_section="CI Status")
    problems = mod.validate_wrapup(body)
    assert problems[0].startswith("unfilled slot `summary`")
    assert any("missing section `## CI Status`" in p for p in problems[1:])


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
# refresh_sections
# ---------------------------------------------------------------------------


def test_refresh_sections_replaces_existing_ci_status_and_issue_management_bodies() -> (
    None
):
    """Both target sections' bodies are replaced; every other section is untouched."""
    body = _body([VERIFIED_AT, "One summary line."])
    body = body.replace(
        "## Code Quality", "## Issue Management\nplaceholder\n\n## Code Quality"
    )
    refreshed = mod.refresh_sections(
        body, ci_status="✅ passed (2 checks)", issue_management="Closes #9"
    )
    assert "## Issue Management\n\nCloses #9\n\n## Code Quality" in refreshed
    assert "## Code Quality\nPASS\n\n## CI Status" in refreshed
    assert "## CI Status\n\n✅ passed (2 checks)\n\n## Recommendation" in refreshed
    assert "placeholder" not in refreshed
    assert "pending" not in refreshed
    assert refreshed.startswith(
        f"{VERIFIED_AT}\nOne summary line.\n\n## Design Check\nPASS — no issues.\n\n"
        "## Security Review\nPASS — no issues.\n\n## Documentation Check\n"
        "PASS — no issues.\n\n"
    )
    assert refreshed.endswith("## Recommendation\nApprove and merge.\n")


def test_refresh_sections_inserts_issue_management_before_code_quality() -> None:
    """No existing Issue Management section.

    One is inserted right before Code Quality.
    """
    body = _body([VERIFIED_AT, "One summary line."])
    refreshed = mod.refresh_sections(
        body, ci_status="✅ passed (1 checks)", issue_management="Closes #4"
    )
    assert refreshed.index("## Issue Management") < refreshed.index("## Code Quality")
    assert "## Issue Management\n\nCloses #4\n\n## Code Quality" in refreshed
    assert "## CI Status\n\n✅ passed (1 checks)\n\n## Recommendation" in refreshed
    assert refreshed.startswith(
        f"{VERIFIED_AT}\nOne summary line.\n\n## Design Check\nPASS — no issues.\n\n"
        "## Security Review\nPASS — no issues.\n\n## Documentation Check\n"
        "PASS — no issues.\n\n"
    )
    assert refreshed.endswith("## Recommendation\nApprove and merge.\n")


def test_refresh_sections_ignores_a_heading_shaped_line_inside_an_earlier_fence() -> (
    None
):
    """SCENARIO: an earlier section quotes tool output containing ``## CI Status``.

    EXPECTED BEHAVIOR: `_section_bounds` (via `fenced_line_indexes`) never
    mistakes the fenced heading-shaped line for a real section boundary —
    the true ``## CI Status`` section is replaced, the fenced block
    survives byte-identical, and the result still validates.
    """
    fenced_block = "```\nsome tool output\n## CI Status\nmore output\n```"
    body = _body(
        [VERIFIED_AT, "One summary line."],
        overrides={"Design Check": f"PASS — no issues.\n\n{fenced_block}"},
    )
    refreshed = mod.refresh_sections(
        body, ci_status="✅ passed (3 checks)", issue_management="Closes #5"
    )
    assert fenced_block in refreshed
    assert "## CI Status\n\n✅ passed (3 checks)\n\n## Recommendation" in refreshed
    assert mod.validate_wrapup(refreshed) == []


# ---------------------------------------------------------------------------
# post_gates
# ---------------------------------------------------------------------------


def test_post_gates_head_mismatch_refuses_naming_both_shas() -> None:
    """The wrap-up's verified SHA not prefixing the PR head refuses, naming both."""
    view = {
        "number": 61,
        "headRefOid": "deadbeef1234567890",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
    }
    refusals, notes = mod.post_gates(view, "7ab3e4e", behind=0, emergency=False)
    assert any("7ab3e4e" in r and "deadbeef" in r for r in refusals)
    assert notes == []


def test_post_gates_conflicting_refuses_naming_the_remedy() -> None:
    """A CONFLICTING branch refuses, naming the merge and the resync escape hatch."""
    view = {
        "number": 61,
        "headRefOid": "7ab3e4e999",
        "baseRefName": "main",
        "mergeable": "CONFLICTING",
    }
    refusals, _notes = mod.post_gates(view, "7ab3e4e", behind=0, emergency=False)
    assert any(
        "git merge origin/main" in r and "forge-resync --resolve-conflicts" in r
        for r in refusals
    )


def test_post_gates_behind_base_refuses() -> None:
    """A branch behind its base refuses, naming the count and the merge."""
    view = {
        "number": 61,
        "headRefOid": "7ab3e4e999",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
    }
    refusals, _notes = mod.post_gates(view, "7ab3e4e", behind=3, emergency=False)
    assert any("3 commit(s) behind origin/main" in r for r in refusals)


def test_post_gates_emergency_exempts_the_behind_base_refusal() -> None:
    """An armed emergency may publish while behind base — never while conflicting.

    The exemption is also named as a note (never silent) so a reader sees
    that the behind-base check was skipped on purpose, not overlooked.
    """
    view = {
        "number": 61,
        "headRefOid": "7ab3e4e999",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
    }
    refusals, notes = mod.post_gates(view, "7ab3e4e", behind=3, emergency=True)
    assert refusals == []
    assert any("behind" in n for n in notes)


def test_post_gates_unknown_mergeability_is_a_note_not_a_refusal() -> None:
    """GitHub not having computed mergeability yet is reported, not refused."""
    view = {
        "number": 61,
        "headRefOid": "7ab3e4e999",
        "baseRefName": "main",
        "mergeable": "UNKNOWN",
    }
    refusals, notes = mod.post_gates(view, "7ab3e4e", behind=0, emergency=False)
    assert refusals == []
    assert any("UNKNOWN" in n for n in notes)


def test_post_gates_behind_none_is_a_note_not_a_refusal() -> None:
    """An unresolvable base comparison is reported, not refused."""
    view = {
        "number": 61,
        "headRefOid": "7ab3e4e999",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
    }
    refusals, notes = mod.post_gates(view, "7ab3e4e", behind=None, emergency=False)
    assert refusals == []
    assert any("skipped" in n for n in notes)


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


def _pr_view(**overrides: object) -> dict[str, object]:
    """Build a ``gh pr view --json ...`` payload, overridable per test.

    Args:
        **overrides: Fields to replace on top of a clean, mergeable default.

    Returns:
        The dict ``gh_pr_view`` would return.
    """
    view: dict[str, object] = {
        "number": 61,
        "headRefOid": "7ab3e4e1234567890abcdef1234567890abcdef",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [
            {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
        "body": "Closes #42",
        "title": "Some PR title",
    }
    view.update(overrides)
    return view


def test_main_post_gate_refusal_exits_three_and_never_posts_or_appends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_wrapup_body: str,
) -> None:
    """SCENARIO: the PR head has moved past the wrap-up's verified SHA.

    MOCK SETUP: `gh_pr_view` reports a head that does not prefix-match the
    wrap-up's `verified-at:` SHA; `post_wrapup` and `continuation_append.main`
    are faked to fail the test if called at all.
    EXPECTED BEHAVIOR: exit 3 (`EXIT_REFUSED`), no post, no continuation append.
    """
    path = tmp_path / "wrapup.md"
    path.write_text(clean_wrapup_body, encoding="utf-8")
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        mod,
        "gh_pr_view",
        lambda *_a, **_kw: _pr_view(headRefOid="deadbeef0000"),
    )
    monkeypatch.setattr(mod, "fetch_quietly", lambda *_a, **_kw: True)
    monkeypatch.setattr(mod, "behind_ahead", lambda *_a, **_kw: (0, 1))

    def _fail(*_a: object, **_kw: object) -> int:
        """Fail the test if run after a gate refusal.

        Args:
            *_a: Positional arguments (unused).
            **_kw: Keyword arguments (unused).

        Raises:
            AssertionError: Always.
        """
        msg = "must not run after a gate refusal"
        raise AssertionError(msg)

    monkeypatch.setattr(mod, "post_wrapup", _fail)
    monkeypatch.setattr(mod.continuation_append, "main", _fail)

    rc = mod.main(["post", "--pr", "61", "--body-file", str(path)])
    assert rc == mod.EXIT_REFUSED


def test_main_post_clean_run_writes_refreshed_body_posts_and_appends_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_wrapup_body: str,
) -> None:
    """SCENARIO: every gate passes.

    MOCK SETUP: `gh_pr_view` reports a matching head, a mergeable branch, and
    a passing rollup; `fetch_quietly`/`behind_ahead` report the branch even
    with base; `post_wrapup` and `continuation_append.main` are faked to
    record their calls.
    EXPECTED BEHAVIOR: the file on disk is rewritten with the refreshed body
    (CI Status and Issue Management updated from the PR view), `post_wrapup`
    is called with that exact refreshed body, and the CONTINUATION record is
    appended naming the PR number and title.
    """
    path = tmp_path / "wrapup.md"
    path.write_text(clean_wrapup_body, encoding="utf-8")
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "gh_pr_view", lambda *_a, **_kw: _pr_view())
    monkeypatch.setattr(mod, "fetch_quietly", lambda *_a, **_kw: True)
    monkeypatch.setattr(mod, "behind_ahead", lambda *_a, **_kw: (0, 1))
    post_calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        mod, "post_wrapup", lambda pr, body: post_calls.append((pr, body)) or 0
    )
    continuation_calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(
        mod.continuation_append,
        "main",
        lambda argv, *, repo_root: continuation_calls.append((argv, repo_root)) or 0,
    )

    rc = mod.main(["post", "--pr", "61", "--body-file", str(path)])

    assert rc == 0
    assert len(post_calls) == 1
    posted_pr, posted_body = post_calls[0]
    assert posted_pr == 61
    assert "Closes #42" in posted_body
    assert "✅ passed (1 checks)" in posted_body
    assert path.read_text(encoding="utf-8") == posted_body
    assert continuation_calls == [(["--pr", "61", "--", "Some PR title"], tmp_path)]


def test_main_post_no_continuation_flag_skips_the_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_wrapup_body: str,
) -> None:
    """``--no-continuation`` still posts but never calls `continuation_append.main`."""
    path = tmp_path / "wrapup.md"
    path.write_text(clean_wrapup_body, encoding="utf-8")
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "gh_pr_view", lambda *_a, **_kw: _pr_view())
    monkeypatch.setattr(mod, "fetch_quietly", lambda *_a, **_kw: True)
    monkeypatch.setattr(mod, "behind_ahead", lambda *_a, **_kw: (0, 1))
    monkeypatch.setattr(mod, "post_wrapup", lambda _pr, _body: 0)

    def _fail(*_a: object, **_kw: object) -> int:
        """Fail the test if the continuation record is appended.

        Args:
            *_a: Positional arguments (unused).
            **_kw: Keyword arguments (unused).

        Raises:
            AssertionError: Always.
        """
        msg = "must not append continuation with --no-continuation"
        raise AssertionError(msg)

    monkeypatch.setattr(mod.continuation_append, "main", _fail)

    rc = mod.main(["post", "--pr", "61", "--body-file", str(path), "--no-continuation"])
    assert rc == 0


def _emergency_body_in_head() -> str:
    """Build a valid wrap-up whose HEAD carries ``wrapup-mode: emergency``.

    Returns:
        A wrap-up text ``_is_emergency_post`` reads the marker from.
    """
    return _body([VERIFIED_AT, "wrapup-mode: emergency", "One summary line."])


def _emergency_body_in_section(*, fenced: bool) -> str:
    """Build a valid wrap-up with the emergency marker inside a section body.

    The marker text sits in a genuine (non-clean) finding — never in the
    head ``_is_emergency_post`` reads — so the body stays a text any
    author could write, fenced-evidence quote included.

    Args:
        fenced: Whether the marker line sits inside a fenced block.

    Returns:
        A wrap-up text carrying the marker outside its head.
    """
    marker = "wrapup-mode: emergency"
    placed = f"```\n{marker}\n```" if fenced else marker
    finding = f"Found: something happened.\n{placed}\nDisposition: nothing further."
    return _body(
        [VERIFIED_AT, "One summary line."], overrides={"Design Check": finding}
    )


@pytest.mark.parametrize(
    ("body", "sentinel_pr", "expect_posted"),
    [
        pytest.param(
            _emergency_body_in_section(fenced=False),
            None,
            False,
            id="marker-in-a-section-body-never-counts",
        ),
        pytest.param(
            _emergency_body_in_section(fenced=True),
            None,
            False,
            id="marker-inside-a-fenced-section-never-counts",
        ),
        pytest.param(
            _emergency_body_in_section(fenced=False),
            61,
            False,
            id="marker-in-a-section-body-with-a-matching-sentinel-still-refuses",
        ),
        pytest.param(
            _emergency_body_in_head(), None, False, id="head-marker-with-no-sentinel"
        ),
        pytest.param(
            _emergency_body_in_head(),
            999,
            False,
            id="head-marker-with-a-sentinel-for-a-different-pr",
        ),
        pytest.param(
            _emergency_body_in_head(),
            61,
            True,
            id="head-marker-with-a-sentinel-for-this-pr",
        ),
    ],
)
def test_main_post_emergency_waiver_needs_head_marker_and_matching_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    sentinel_pr: int | None,
    *,
    expect_posted: bool,
) -> None:
    """SCENARIO: a branch 3 commits behind base claims the emergency waiver.

    MOCK SETUP: `gh_pr_view` reports a matching head and a MERGEABLE
    branch; `behind_ahead` reports 3 commits behind; when *sentinel_pr* is
    given, a REAL sentinel file (`forge.emergency.write_state`) records it
    — never a patched `read_state`. `post_wrapup` / `continuation_append.main`
    are faked to record whether they ran. `tmp_path` needs no git init on
    the success path — `_branch_messages` degrades softly, per the
    existing clean-run test.
    EXPECTED BEHAVIOR: the behind-base refusal is waived ONLY when the
    marker sits in the wrap-up's head (never a section body, fenced or
    not) AND a sentinel structurally records this exact PR — every other
    combination refuses with exit 3 and never posts.

    Args:
        body: The wrap-up text under test.
        sentinel_pr: PR number a real sentinel records, or `None` for no
            sentinel at all.
        expect_posted: Whether this combination should succeed and post.
    """
    path = tmp_path / "wrapup.md"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "gh_pr_view", lambda *_a, **_kw: _pr_view())
    monkeypatch.setattr(mod, "fetch_quietly", lambda *_a, **_kw: True)
    monkeypatch.setattr(mod, "behind_ahead", lambda *_a, **_kw: (3, 0))
    if sentinel_pr is not None:
        write_state(
            tmp_path,
            EmergencyState(
                ledger_issue=777,
                reason="prod is down",
                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                spent=True,
                pr_number=sentinel_pr,
            ),
        )
    post_calls: list[int] = []
    continuation_calls: list[list[str]] = []
    monkeypatch.setattr(
        mod, "post_wrapup", lambda pr, _body: post_calls.append(pr) or 0
    )
    monkeypatch.setattr(
        mod.continuation_append,
        "main",
        lambda argv, **_kwargs: continuation_calls.append(argv) or 0,
    )

    rc = mod.main(["post", "--pr", "61", "--body-file", str(path)])

    if expect_posted:
        assert rc == 0
        assert post_calls == [61]
        assert continuation_calls
    else:
        assert rc == mod.EXIT_REFUSED
        assert post_calls == []
        assert continuation_calls == []


def test_main_post_title_starting_with_dash_reaches_the_real_continuation_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_wrapup_body: str,
) -> None:
    """SCENARIO: a PR titled like a CLI flag (``--wip``, no space).

    A space-free dash-prefixed title is deliberate: argparse treats any
    arg containing a space as positional on its own (confirmed: even
    ``"--rotate things"`` parses fine with no ``--`` separator at all), so
    only a space-free title like ``--wip`` actually exercises the ``--``
    argv-separator contract this test pins.

    MOCK SETUP: only `gh_pr_view` / `fetch_quietly` / `behind_ahead` /
    `post_wrapup` are faked — `continuation_append.main` runs for REAL
    against `tmp_path`, the exact seam the ``--`` argv separator protects.
    EXPECTED BEHAVIOR: no `SystemExit` from argparse mis-parsing the title
    as a flag; `.plan/CONTINUATION.md` gains a line naming the PR and the
    literal, dash-prefixed title.
    """
    path = tmp_path / "wrapup.md"
    path.write_text(clean_wrapup_body, encoding="utf-8")
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "gh_pr_view", lambda *_a, **_kw: _pr_view(title="--wip"))
    monkeypatch.setattr(mod, "fetch_quietly", lambda *_a, **_kw: True)
    monkeypatch.setattr(mod, "behind_ahead", lambda *_a, **_kw: (0, 1))
    monkeypatch.setattr(mod, "post_wrapup", lambda _pr, _body: 0)

    rc = mod.main(["post", "--pr", "61", "--body-file", str(path)])

    assert rc == 0
    continuation = (tmp_path / ".plan" / "CONTINUATION.md").read_text(encoding="utf-8")
    assert "PR #61 wrap-up: --wip" in continuation


def test_main_post_refuses_when_the_refreshed_body_fails_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_wrapup_body: str,
) -> None:
    """SCENARIO: `refresh_sections` corrupts an otherwise-valid wrap-up.

    MOCK SETUP: `mod.refresh_sections` is patched to drop the ``## CI
    Status`` section entirely — a refresh-time regression, not an
    authoring mistake; `post_wrapup` is faked to fail the test if called.
    EXPECTED BEHAVIOR: exit 2 (a body defect, distinct from the gate's
    exit 3), no post, and the file on disk is untouched — the write only
    happens after the refreshed body validates.
    """
    path = tmp_path / "wrapup.md"
    path.write_text(clean_wrapup_body, encoding="utf-8")
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "gh_pr_view", lambda *_a, **_kw: _pr_view())
    monkeypatch.setattr(mod, "fetch_quietly", lambda *_a, **_kw: True)
    monkeypatch.setattr(mod, "behind_ahead", lambda *_a, **_kw: (0, 1))
    monkeypatch.setattr(
        mod, "refresh_sections", lambda text, **_kw: text.replace("## CI Status", "")
    )

    def _fail(*_args: object, **_kwargs: object) -> int:
        """Fail the test if `post_wrapup` runs for an invalid refreshed body.

        Args:
            *_args: Unused arguments.
            **_kwargs: Unused keyword arguments.
        """
        msg = "must not post an invalid refreshed wrap-up"
        raise AssertionError(msg)

    monkeypatch.setattr(mod, "post_wrapup", _fail)

    rc = mod.main(["post", "--pr", "61", "--body-file", str(path)])

    assert rc == 2
    assert path.read_text(encoding="utf-8") == clean_wrapup_body


# ---------------------------------------------------------------------------
# main — compose
# ---------------------------------------------------------------------------


def test_main_compose_writes_the_wrapup_file_and_prints_the_slot_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``compose`` in a real (git-initialized) tmp repo, plan supplied via ``--plan``.

    MOCK SETUP: only `repo_root` is patched to the tmp repo — everything
    else (`added_paths`, `run_git`, `resolve_steps`, ...) runs for real
    against the empty tmp repo, degrading harmlessly (no diff, no
    `code_health/` logs yet).
    EXPECTED BEHAVIOR: `code_health/pr_wrapup.md` is written with exactly
    the one remaining `summary` slot, and the CLI reports that count.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"mode": "light-code", "reporters": [], "reasons": ["small diff"]})
    )

    rc = mod.main(["compose", "--base", "HEAD", "--plan", str(plan_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "1 slot(s) to fill: summary" in out
    written = (tmp_path / "code_health" / "pr_wrapup.md").read_text(encoding="utf-8")
    assert "wrapup-mode: light" in written
    assert "small diff" in written


def test_main_compose_reports_a_compose_error_as_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A full-mode plan with no reporter report files given refuses.

    Missing evidence causes exit 2.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "mode": "full",
                "reporters": [
                    "design-checker",
                    "security-checker",
                    "docs-types-checker",
                ],
                "reasons": [],
            }
        )
    )

    rc = mod.main(["compose", "--base", "HEAD", "--plan", str(plan_path)])

    assert rc == 2
    assert "missing report for required reporter" in capsys.readouterr().out


def test_main_compose_dash_prefixed_base_refuses_before_touching_repo_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `--base` value shaped like a flag is refused before any git call.

    Mirrors `pr_plan.main`'s and `git_utils.fetch_quietly`'s own dash-prefix
    guard: `_cmd_compose` checks `args.base` before ever calling
    `repo_root()`, so `repo_root` is faked to fail the test if it runs.
    """

    def _fail() -> object:
        """Fail the test if `repo_root` (and thus any git call) runs.

        Raises:
            AssertionError: Always.
        """
        msg = "must not resolve repo_root for a dash-prefixed --base"
        raise AssertionError(msg)

    monkeypatch.setattr(mod, "repo_root", _fail)

    rc = mod.main(["compose", "--base=-evil"])

    assert rc == 2
    assert "invalid --base" in capsys.readouterr().out


def test_main_compose_armed_emergency_skips_reporters_and_sets_wrapup_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: a real armed `forge-emergency` sentinel exists in the repo.

    MOCK SETUP: none — `forge.emergency.write_state` writes a real,
    currently-armed sentinel (not spent, expiry an hour out); `_gather_inputs`
    reads it through the real `armed_state`, never a patched one.
    EXPECTED BEHAVIOR: the written wrap-up carries `wrapup-mode: emergency`
    and every reporter section reads `SKIPPED (emergency: ledger #<N>)`.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    write_state(
        tmp_path,
        EmergencyState(
            ledger_issue=777,
            reason="prod is down",
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        ),
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"mode": "full", "reporters": [], "reasons": []}))

    rc = mod.main(["compose", "--base", "HEAD", "--plan", str(plan_path)])

    assert rc == 0
    written = (tmp_path / "code_health" / "pr_wrapup.md").read_text(encoding="utf-8")
    assert "wrapup-mode: emergency" in written
    assert written.count("SKIPPED (emergency: ledger #777)") == 3


def test_main_compose_delta_mode_renders_prior_sha_rollup_and_checked_issue_management(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: `compose --pr N` with a delta-mode plan (reporters unchanged).

    MOCK SETUP: `mod.gh_pr_view` returns a PR view carrying a closing
    keyword and a passing rollup; `mod.wrapup_freshness` returns the prior
    wrap-up's `verified-at:` SHA — the two real `gh` seams `_gather_inputs`
    reaches in delta mode.
    EXPECTED BEHAVIOR: every reporter section renders compose's mechanical
    delta line naming the prior SHA, CI Status carries the rollup summary,
    and Issue Management carries no "the PR body was not searched" suffix
    — the PR body was read (`pr_body_checked=True`).
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        mod,
        "gh_pr_view",
        lambda *_a, **_kw: {
            "body": "Closes #99",
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
        },
    )
    monkeypatch.setattr(
        mod,
        "wrapup_freshness",
        lambda _pr: WrapupFreshness(fresh=True, latest_verified_at="abc1234"),
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"mode": "delta", "reporters": [], "reasons": []}))

    rc = mod.main(["compose", "--base", "HEAD", "--pr", "61", "--plan", str(plan_path)])

    assert rc == 0
    written = (tmp_path / "code_health" / "pr_wrapup.md").read_text(encoding="utf-8")
    assert written.count("PASS — unchanged since abc1234 (delta)") == 3
    assert "✅ passed (1 checks)" in written
    assert "Closes #99" in written
    assert "the PR body was not searched" not in written


def test_main_compose_pr_view_unavailable_reports_unknown_ci_and_unsearched_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: `compose --pr N` when `gh` cannot read the PR at all.

    MOCK SETUP: `mod.gh_pr_view` returns `None` (gh failure, missing auth,
    or an unknown PR) — the light-code plan needs no reporter reports, so
    this isolates `_ci_status` / `render_issue_management`'s degrade path.
    EXPECTED BEHAVIOR: CI Status reads `unknown — could not read PR #61`
    (never the "pending" wording reserved for a PR that does not exist
    yet), and Issue Management carries the "the PR body was not searched"
    suffix — the closing-keyword search fell back to commit messages alone.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "gh_pr_view", lambda *_a, **_kw: None)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"mode": "light-code", "reporters": [], "reasons": ["small diff"]})
    )

    rc = mod.main(["compose", "--base", "HEAD", "--pr", "61", "--plan", str(plan_path)])

    assert rc == 0
    written = (tmp_path / "code_health" / "pr_wrapup.md").read_text(encoding="utf-8")
    assert "unknown — could not read PR #61" in written
    assert "the PR body was not searched" in written


def test_main_compose_pr_view_without_rollup_reports_no_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: `compose --pr N` reads a PR whose checks haven't started yet.

    MOCK SETUP: `mod.gh_pr_view` returns a readable view carrying no
    `statusCheckRollup` key at all — isolates `_ci_status`'s "no checks
    reported" fallback from its `view is None` branch, covered by the
    sibling test above.
    EXPECTED BEHAVIOR: CI Status reads `no checks reported` — distinct
    from both the "pending" wording (no PR yet) and the "unknown" wording
    (PR unreadable): here the PR IS readable, it just has no rollup.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "gh_pr_view", lambda *_a, **_kw: {"body": "Closes #7"})
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"mode": "light-code", "reporters": [], "reasons": ["small diff"]})
    )

    rc = mod.main(["compose", "--base", "HEAD", "--pr", "61", "--plan", str(plan_path)])

    assert rc == 0
    written = (tmp_path / "code_health" / "pr_wrapup.md").read_text(encoding="utf-8")
    assert "no checks reported" in written


@pytest.mark.parametrize("passed", [False, True])
def test_main_compose_light_regen_gates_or_fences_the_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    passed: bool,
) -> None:
    """SCENARIO: light-regen mode runs the provenance gates.

    MOCK SETUP: `mod.run_gate_evidence` is patched to return a fixed
    `(passed, block)` pair — the real gate subprocess is `run_gate_evidence`'s
    own seam (`tests/test_git_utils.py`), not `_gather_inputs`'s.
    EXPECTED BEHAVIOR: a failing gate refuses composing (exit 2, naming the
    refusal); a passing gate carries the evidence fence into the
    Documentation Check section.

    Args:
        passed: Whether the mocked gate evidence check passes.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    block = "## Provenance gates\n\nProvenance gates pass.\n\n```\nstep output\n```\n"
    monkeypatch.setattr(mod, "run_gate_evidence", lambda *_a, **_kw: (passed, block))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"mode": "light-regen", "reporters": [], "reasons": []})
    )

    rc = mod.main(["compose", "--base", "HEAD", "--plan", str(plan_path)])

    if not passed:
        assert rc == 2
        assert "light-regen provenance gates failed" in capsys.readouterr().out
    else:
        assert rc == 0
        written = (tmp_path / "code_health" / "pr_wrapup.md").read_text(
            encoding="utf-8"
        )
        start = written.index("## Documentation Check")
        end = written.index("## Issue Management")
        doc_section = written[start:end]
        assert "```\nstep output\n```" in doc_section


def test_main_compose_code_quality_overrides_unstamped_environment_step_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: an unstamped environment-step log sits next to a clean PASS row.

    MOCK SETUP: none — `_code_quality` reads the real `code_health/` files
    and calls the real `precommit.freshness_verdicts` for the tmp repo,
    where `env_sync` is a default-on step with `StepDef.checks_files=False`.
    EXPECTED BEHAVIOR: `freshness_verdicts` overrides `env_sync`'s freshness
    verdict to `n/a` before `_code_quality` renders, so an unstamped
    `env_sync.log` never turns a passing row into a "not verified at this
    tree" warning — an environment step judges the machine, not the tree,
    so its own log is never expected to carry a tree stamp.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    health = tmp_path / "code_health"
    health.mkdir()
    (health / "precommit_timing.log").write_text(
        "# produced-at: tree=unknown head=abc1234 2026-01-01T00:00:00+00:00\n"
        "forge-precommit per-step timing (newest run overwrites)\n\n"
        f"{'env_sync':<28} {1.0:>7.1f}s  PASS\n\n"
        f"{'total':<28} {1.0:>7.1f}s\n",
        encoding="utf-8",
    )
    (health / "env_sync.log").write_text("env_sync: ok\n", encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"mode": "light-code", "reporters": [], "reasons": ["small diff"]})
    )

    rc = mod.main(["compose", "--base", "HEAD", "--plan", str(plan_path)])

    assert rc == 0
    written = (tmp_path / "code_health" / "pr_wrapup.md").read_text(encoding="utf-8")
    assert "not verified at this tree: env_sync" not in written
