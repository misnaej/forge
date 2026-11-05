"""Tests for ``forge.pr_metrics_report`` — reading the metrics database back."""

from __future__ import annotations

import json

from forge.agent_profile_tokens import TokenUsage
from forge.pr_metrics_report import (
    PrRow,
    export_payload,
    render_html,
    render_report,
)


def _row(pr: int, title: str, tokens: dict[str, TokenUsage]) -> PrRow:
    """A recorded PR as :func:`load_rows` returns it.

    Args:
        pr: PR number.
        title: PR title.
        tokens: Agent type → usage.

    Returns:
        The row.
    """
    return PrRow(
        pr=pr,
        title=title,
        merged_at="2026-09-30T10:00:00Z",
        files=2,
        additions=30,
        deletions=10,
        commits=3,
        review_rounds=1,
        wrapups_posted=1,
        precommit_runs=2,
        test_runs=1,
        active_s=60.0,
        wall_s=120.0,
        forge_version="1.0.0",
        tokens=tokens,
    )


def _usage(new: int, cached: int = 0) -> TokenUsage:
    """Usage with *new* fresh input tokens and *cached* cache reads.

    Args:
        new: Fresh input tokens.
        cached: Cache-read tokens.

    Returns:
        The usage.
    """
    return TokenUsage(input_tokens=new, cache_read_tokens=cached, requests=1)


def test_export_carries_figures_only_and_folds_unshipped_agent_types() -> None:
    """Behavior: export holds no title, number, branch or local agent name.

    A ``forge:`` prefix is not enough to be named: only shipped agents are,
    every other type is summed under ``other``.
    """
    rows = [
        _row(
            41,
            "Secret project title",
            {
                "main": _usage(10),
                "forge:design-checker": _usage(5),
                "forge:acme-reviewer": _usage(3),
                "design-checker-acme": _usage(2),
            },
        ),
        _row(42, "Another title", {"main": _usage(1)}),
    ]

    text = json.dumps(export_payload(rows))

    for name in ("title", "Secret", "acme", "2026-09", "41", "42"):
        assert name not in text
    payload = json.loads(text)
    assert [p["ordinal"] for p in payload["prs"]] == [1, 2]
    assert set(payload["prs"][0]["tokens"]) == {"main", "forge:design-checker", "other"}
    assert payload["prs"][0]["tokens"]["other"]["input"] == 5
    assert "forge_version" in payload


def test_report_separates_new_tokens_from_cache_reads() -> None:
    """Behavior: per-line cost uses new tokens; cache% shows what was cached."""
    report = render_report([_row(1, "t", {"main": _usage(400, cached=3600)})])

    line = report.splitlines()[1]
    assert "4k" in line  # all tokens
    assert "90%" in line  # cache share
    assert " 10 " in line  # 400 new / 40 changed lines
    assert "fewer than 4 PRs" in report


def test_html_is_one_self_contained_escaped_file() -> None:
    """Behavior: the page fetches nothing and escapes PR titles."""
    page = render_html([_row(1, "<script>alert(1)</script>", {"main": _usage(9)})])

    assert "<svg" in page
    assert "<script" not in page
    for external in ("http://", "https://", "<link", "src="):
        assert external not in page
    assert "&lt;script&gt;" in page
