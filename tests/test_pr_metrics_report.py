"""Tests for ``forge.pr_metrics_report`` — reading the metrics database back."""

from __future__ import annotations

import json
import sqlite3

from forge import pr_metrics
from forge.agent_profile_tokens import TokenUsage
from forge.pr_metrics_charts import Change
from forge.pr_metrics_report import (
    UNKNOWN_VERSION,
    PrRow,
    export_payload,
    load_rows,
    render_html,
    render_report,
    version_changes,
    version_order,
    version_stats,
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


_REPO_A = "aaaaaa11-0000"
_REPO_B = "bbbbbb22-0000"
#: (key, repo, merged_on, version, changed lines, rounds, active_s, new tokens)
_FIXTURE = (
    ("a1", _REPO_A, "2026-09-01", "1.2.0", 100, 1, 600.0, 1000),
    ("a2", _REPO_A, "2026-09-02", "1.2.0", 100, 3, 1200.0, 3000),
    ("a3", _REPO_A, "2026-09-03", "1.2.0", 100, 2, 1800.0, None),
    ("a4", _REPO_A, "2026-09-10", "1.10.0", 50, 0, 300.0, 500),
    ("b1", _REPO_B, "2026-09-05", None, 10, 1, None, 100),
    ("b2", _REPO_B, "2026-09-06", "1.10.0", 20, 1, 60.0, None),
)


def _fixture_rows(version_override: str | None = None) -> list[PrRow]:
    """Load :data:`_FIXTURE` through a real in-memory metrics database.

    Args:
        version_override: Replaces every recorded version when given.

    Returns:
        The rows as :func:`load_rows` reads them.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(pr_metrics._SCHEMA)
    for key, repo, day, version, lines, rounds, active, new in _FIXTURE:
        chosen = version_override or version
        conn.execute(
            "INSERT INTO prs VALUES (?, ?, ?, 'claude-code', 'm', 1, ?, 0, 1, ?, "
            "1, 1, 1, ?, ?, ?)",
            (key, repo, day, lines, rounds, active, active, chosen),
        )
        if new is not None:
            conn.execute(
                "INSERT INTO pr_tokens VALUES (?, 'main', 1, ?, 0, 0, 0)", (key, new)
            )
    return load_rows(conn)


def test_version_stats_are_medians_per_version_and_repo() -> None:
    """Behavior: medians per (version, repo); token medians skip PRs without tokens."""
    stats = version_stats(_fixture_rows())

    a12 = stats[("1.2.0", _REPO_A)]
    assert (a12.prs, a12.with_tokens) == (3, 2)
    assert a12.new_tokens == 2000
    assert a12.per_line == 20  # 10 and 30 tokens/line; a3 has no tokens
    assert a12.active_min == 20
    assert a12.rounds == 2
    assert stats[("1.10.0", _REPO_A)].per_line == 10


def test_missing_tokens_are_gaps_not_zeros() -> None:
    """Behavior: a group with no token data has no token medians and shows '-'."""
    rows = _fixture_rows()
    gap = version_stats(rows)[("1.10.0", _REPO_B)]

    assert gap.with_tokens == 0
    assert gap.new_tokens is None
    assert gap.per_line is None
    page = render_html(rows)
    row = "<td>1.10.0</td><td>bbbbbb</td><td>1</td><td>0</td><td>-</td><td>-</td>"
    assert row in page


def test_unknown_version_bucket_sorts_last_in_release_order() -> None:
    """Behavior: versions sort by semver, NULL version grouped as 'unknown' last."""
    rows = _fixture_rows()

    assert version_order(rows) == ["1.2.0", "1.10.0", UNKNOWN_VERSION]
    assert version_stats(rows)[(UNKNOWN_VERSION, _REPO_B)].new_tokens == 100


def test_version_charts_are_inline_self_contained_and_escaped() -> None:
    """Behavior: one <svg> per chart, nothing fetched, injected text escaped."""
    page = render_html(_fixture_rows(version_override="<script>v</script>"))

    assert page.count("<svg") == 5  # per-PR bars + four version charts
    assert "<script" not in page
    assert "&lt;script&gt;v&lt;/script&gt;" in page
    for external in ("http://", "https://", "<link", "src="):
        assert external not in page


def test_version_changes_mark_each_repo_move_skipping_unknown() -> None:
    """Behavior: a move is a recorded version differing from the repo's previous one."""
    assert version_changes(_fixture_rows()) == [
        Change("2026-09-10", "aaaaaa", "1.2.0", "1.10.0")
    ]
