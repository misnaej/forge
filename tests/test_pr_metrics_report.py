"""Tests for ``forge.pr_metrics_report`` — reading the metrics database back."""

from __future__ import annotations

import json

from forge.agent_profile_tokens import TokenUsage
from forge.pr_metrics_report import (
    PrRow,
    export_payload,
    fold_agent_types,
    render_html,
    render_report,
)


def _row(ordinal: int, tokens: dict[str, TokenUsage], model: str = "m") -> PrRow:
    """A recorded PR as :func:`load_rows` returns it.

    Args:
        ordinal: Its place in its repo.
        tokens: Agent type → usage.
        model: The main-session model.

    Returns:
        The row.
    """
    return PrRow(
        pr_key=f"key{ordinal}",
        repo_id="abcdef12-0000",
        ordinal=ordinal,
        merged_on="2026-09-30",
        tool="claude-code",
        model=model,
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


def test_fold_keeps_public_agent_types_and_sums_the_rest_as_other() -> None:
    """Behavior: only main, shipped forge agents and Claude Code built-ins keep names.

    A ``forge:`` prefix is not enough: a repo's own agent often carries
    its name.
    """
    folded = fold_agent_types(
        {
            "main": _usage(10),
            "forge:design-checker": _usage(5),
            "Explore": _usage(4),
            "forge:acme-reviewer": _usage(3),
            "design-checker-acme": _usage(2),
        }
    )

    assert set(folded) == {"main", "forge:design-checker", "Explore", "other"}
    assert folded["other"].input_tokens == 5


def test_export_is_the_rows_as_stored_with_schema_version() -> None:
    """Behavior: export carries every stored field and the schema version."""
    payload = json.loads(
        json.dumps(export_payload([_row(1, {"main": _usage(10)})], schema=7))
    )

    assert payload["schema"] == 7
    [pr] = payload["prs"]
    assert pr["pr_key"] == "key1"
    assert pr["repo_id"] == "abcdef12-0000"
    assert pr["merged_on"] == "2026-09-30"
    assert pr["tokens"] == {
        "main": {
            "requests": 1,
            "input": 10,
            "cache_write": 0,
            "cache_read": 0,
            "output": 0,
        }
    }


def test_report_labels_prs_by_day_short_repo_id_and_ordinal() -> None:
    """Behavior: a PR shows as day + short repo id + ordinal; new tokens per line."""
    report = render_report([_row(3, {"main": _usage(400, cached=3600)})])

    line = report.splitlines()[1]
    assert line.startswith("2026-09-30 abcdef#3")
    assert "4k" in line  # all tokens
    assert "90%" in line  # cache share
    assert " 10 " in line  # 400 new / 40 changed lines
    assert "fewer than 4 PRs" in report


def test_html_is_one_self_contained_escaped_file() -> None:
    """Behavior: the page fetches nothing and escapes transcript-read text."""
    page = render_html([_row(1, {"main": _usage(9)}, model="<script>x</script>")])

    assert "<svg" in page
    assert "<script" not in page
    for external in ("http://", "https://", "<link", "src="):
        assert external not in page
    assert "&lt;script&gt;" in page
    assert "abcdef#1" in page
