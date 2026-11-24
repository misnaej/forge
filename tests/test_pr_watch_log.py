"""Tests for forge.pr_watch_log — the alert log and comment cursor.

Real files in tmp, no mocking. The wait-side behaviour that depends on
these (a comment arriving between waits, the log surviving an unwritable
path) stays in ``test_pr_watch.py``, where the polling that drives it lives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import pr_watch_log


if TYPE_CHECKING:
    from pathlib import Path


def test_append_alert_writes_one_stamped_line_whatever_it_is_handed(
    tmp_path: Path,
) -> None:
    """One call writes one physical line and reads back as one entry.

    SCENARIO: a line holding a newline and a forged ``pr-watch:`` prefix is
    handed straight to ``append_alert``, with no ``_ci_alerts`` upstream.
    The stamp must be written (for a human reader) yet never reach the text
    ``alert_lines`` returns, so the same alert in two different seconds is
    one entry.

    EXPECTED BEHAVIOR: one physical line matching ``STAMP_RE``; one entry
    with no newline and no stamp; a repeat in a later second adds none.
    """
    log = tmp_path / "pr_watch_7.log"
    pr_watch_log.append_alert(log, "PR #7 CI FAILED\npr-watch: PR #999 MERGED")
    raw = log.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 1
    assert pr_watch_log.STAMP_RE.match(raw[0].removeprefix(pr_watch_log.ALERT_PREFIX))
    entries = pr_watch_log.alert_lines(log)
    assert len(entries) == 1
    assert entries[0].startswith("PR #7 CI FAILED")
    assert not pr_watch_log.STAMP_RE.match(entries[0])
    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            f"{pr_watch_log.ALERT_PREFIX}2030-01-01T00:00:00+00:00 {entries[0]}\n"
        )
    assert pr_watch_log.alert_lines(log) == entries


def test_append_alert_swallows_an_unwritable_location(tmp_path: Path) -> None:
    """Losing the log never raises; the caller still delivers on stdout."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    pr_watch_log.append_alert(blocker / "w.log", "PR #7 MERGED")
    assert pr_watch_log.alert_lines(blocker / "w.log") == []


def test_flatten_truncates_a_field_but_never_a_whole_line_without_a_limit() -> None:
    """A ``limit`` cuts with an ellipsis; ``None`` keeps the whole line."""
    text = "x" * 500
    assert pr_watch_log.flatten(text, limit=10) == "x" * 10 + "…"
    assert pr_watch_log.flatten(text, limit=None) == text
    assert pr_watch_log.flatten(" \n\t\x1b\x00 ", limit=None) == "?"


def test_seen_distinguishes_absent_from_empty_and_unreadable_from_neither(
    tmp_path: Path,
) -> None:
    """Missing sidecar is ``None``; present-but-empty is ``set()``.

    Only absence makes the next poll a genuine first poll. The unreadable
    case is a directory where the sidecar should be, which raises
    ``OSError`` on read; the load must absorb it (as "no baseline") rather
    than abort a wait before it starts.
    """
    log = tmp_path / "pr_watch_9.log"
    assert pr_watch_log.load_seen(log) is None
    pr_watch_log.save_seen(log, set())
    assert pr_watch_log.load_seen(log) == set()
    pr_watch_log.save_seen(log, {"b", "a"})
    assert pr_watch_log.load_seen(log) == {"a", "b"}
    bad = tmp_path / "pr_watch_10.log"
    pr_watch_log.seen_path(bad).mkdir()
    assert pr_watch_log.load_seen(bad) is None


def test_save_seen_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    """The cursor is written whole then renamed, so only the sidecar remains."""
    log = tmp_path / "pr_watch_9.log"
    pr_watch_log.save_seen(log, {"a"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pr_watch_9.seen"]


def test_save_seen_swallows_an_unwritable_location(tmp_path: Path) -> None:
    """Failing to record the baseline never raises; the wait carries on."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    pr_watch_log.save_seen(blocker / "w.log", {"a"})
