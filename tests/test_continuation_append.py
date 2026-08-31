"""Tests for ``forge.continuation_append``.

Rotation is covered by two classes of test: behavior-class cases drive
``main()`` with real files under ``tmp_path`` (matching the module's own
``sys.argv`` + ``chdir`` invocation style); a few development-class cases
call the private ``_parse_digests`` / ``_condense_into`` / ``_render_digest``
helpers directly where the plan calls for pinning classification/sort logic
independent of file I/O.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from forge import continuation_append


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _days_ago(n: int) -> str:
    """Return the ISO date *n* days before now (UTC).

    Args:
        n: Number of days in the past (``0`` for today).

    Returns:
        ``YYYY-MM-DD`` date string, seeded arithmetically off the real
        clock so tests never hardcode a date that eventually goes stale.
    """
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%d")


def _write_continuation(
    tmp_path: Path, head: str = "", recent_lines: Sequence[str] = ()
) -> Path:
    """Write ``.plan/CONTINUATION.md`` with a given head and recent entries.

    Mirrors the shape ``_ensure_file_and_section`` / ``_rotate`` expect
    (header, then the auto-appended ``## Recent activity`` section) so
    rotation scenarios can be seeded directly, without going through
    ``main()``.

    Args:
        tmp_path: Repo root (``pytest`` ``tmp_path`` fixture).
        head: Text before the ``## Recent activity`` header, verbatim
            (defaults to just the canonical file header).
        recent_lines: ``- YYYY-MM-DD ...`` entries for the recent section.

    Returns:
        Path to the written ``CONTINUATION.md`` file.
    """
    plan_dir = tmp_path / ".plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / "CONTINUATION.md"
    text = head or f"{continuation_append.FILE_HEADER}\n\n"
    if not text.endswith("\n"):
        text += "\n"
    text += f"{continuation_append.RECENT_HEADER}\n\n"
    text += "\n".join(recent_lines)
    if recent_lines:
        text += "\n"
    path.write_text(text)
    return path


def _filler_lines(n: int | None = None) -> list[str]:
    """Return *n* today-dated, unaged commit lines padding the floor.

    ``_rotate`` never rotates the recent section below
    ``MIN_RECENT_ENTRIES`` raw entries, regardless of age — tests that
    exercise real age-based rotation pad the recent section with these
    (never-aged, always-kept) filler entries so the floor is satisfied
    without rescuing the entries under test.

    Args:
        n: Number of filler lines (defaults to
            ``continuation_append.MIN_RECENT_ENTRIES``).

    Returns:
        ``- YYYY-MM-DD ...`` commit lines dated today.
    """
    if n is None:
        n = continuation_append.MIN_RECENT_ENTRIES
    today = _days_ago(0)
    return [f"- {today} f{i:06x} filler commit {i}" for i in range(n)]


def test_creates_file_and_section_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First commit-append creates the file with header + activity section."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["forge-continuation-append", "--commit", "abc1234", "feat: x"]
    )
    assert continuation_append.main() == 0
    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    assert continuation_append.FILE_HEADER in content
    assert continuation_append.RECENT_HEADER in content
    assert "abc1234 feat: x" in content


def test_appends_commit_line_with_iso_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit lines use ``- YYYY-MM-DD HASH SUBJECT`` shape."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["forge-continuation-append", "--commit", "deadbee", "fix: y"]
    )
    continuation_append.main()
    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    # Last line is the append.
    last = content.strip().splitlines()[-1]
    assert last.startswith("- ")
    assert " deadbee fix: y" in last


def test_appends_pr_wrapup_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR wrap-up lines say ``PR #N wrap-up: <subject>``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["forge-continuation-append", "--pr", "33", "chore: cleanup"]
    )
    continuation_append.main()
    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    assert "PR #33 wrap-up: chore: cleanup" in content


def test_appends_merge_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Merge lines say ``HASH PR merged: <subject>``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["forge-continuation-append", "--merge", "feedbac", "feat: thing"]
    )
    continuation_append.main()
    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    assert "feedbac PR merged: feat: thing" in content


def test_section_header_added_when_file_exists_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing file without the activity-section header gets one added once."""
    (tmp_path / ".plan").mkdir()
    (tmp_path / ".plan" / "CONTINUATION.md").write_text(
        "# Continuation Log\n\n## Status\nIdle.\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["forge-continuation-append", "--commit", "abc1234", "x"]
    )
    continuation_append.main()
    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    assert content.count(continuation_append.RECENT_HEADER) == 1
    assert "## Status" in content  # preserved
    assert "abc1234 x" in content


def test_idempotent_on_section_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two appends produce exactly one header and two activity lines."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["forge-continuation-append", "--commit", "aaa1111", "one"]
    )
    continuation_append.main()
    monkeypatch.setattr(
        "sys.argv", ["forge-continuation-append", "--commit", "bbb2222", "two"]
    )
    continuation_append.main()
    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    assert content.count(continuation_append.RECENT_HEADER) == 1
    assert "aaa1111 one" in content
    assert "bbb2222 two" in content


def test_mutually_exclusive_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One of --commit / --pr / --merge must be supplied."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "subject"])
    with pytest.raises(SystemExit):
        continuation_append.main()


# --- Rotation: behavior-class (main() + real files) -----------------------


def test_rotate_ages_out_entry_past_max_recent_age_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry older than the default 2-day window rotates to the archive."""
    old_date = _days_ago(3)
    _write_continuation(
        tmp_path,
        recent_lines=[f"- {old_date} 1234567 old commit", *_filler_lines()],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    recent_section = content.split(continuation_append.RECENT_HEADER, 1)[1]
    assert "1234567" not in recent_section
    archive = (tmp_path / ".plan" / "CONTINUATION-archive.md").read_text()
    assert f"- {old_date} 1234567 old commit" in archive


def test_rotate_overflows_past_max_recent_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oldest entries beyond the default 50-entry cap rotate out."""
    today = _days_ago(0)
    lines = [f"- {today} {i:07x} commit {i}" for i in range(55)]
    _write_continuation(tmp_path, recent_lines=lines)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    recent_section = content.split(continuation_append.RECENT_HEADER, 1)[1]
    kept = [ln for ln in recent_section.splitlines() if ln.startswith("- ")]
    assert len(kept) == continuation_append.DEFAULT_MAX_RECENT_ENTRIES
    assert kept == lines[5:]
    archive = (tmp_path / ".plan" / "CONTINUATION-archive.md").read_text()
    for line in lines[:5]:
        assert line in archive


def test_rotate_pins_aged_entry_named_in_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An aged entry referencing a PR named in ``## In progress`` is kept."""
    head = (
        f"{continuation_append.FILE_HEADER}\n\n"
        "## In progress\n- **PR #33** open, awaiting merge.\n\n"
    )
    old_date = _days_ago(3)
    _write_continuation(
        tmp_path,
        head=head,
        recent_lines=[f"- {old_date} PR #33 wrap-up: shipped the thing"],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    assert "PR #33 wrap-up: shipped the thing" in content
    assert not (tmp_path / ".plan" / "CONTINUATION-archive.md").exists()


def test_rotate_digest_line_has_one_entry_per_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The condensed digest line counts each kind and collects PR numbers."""
    date = _days_ago(3)
    _write_continuation(
        tmp_path,
        recent_lines=[
            f"- {date} 1234567 fix bug",
            f"- {date} PR #5 wrap-up: ship feature",
            f"- {date} 89abcde PR merged: release",
            f"- {date} did some random other note",
            *_filler_lines(),
        ],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    expected = f"- {date} — 1 commit(s), 1 wrap-up(s), 1 merge(s), 1 other, PRs #5"
    assert expected in content


def test_rotate_digest_merges_across_two_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second rotation on the same date sums counts and unions PR sets."""
    date = _days_ago(3)
    _write_continuation(
        tmp_path,
        recent_lines=[f"- {date} 1111111 first commit", *_filler_lines()],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    path = tmp_path / ".plan" / "CONTINUATION.md"
    continuation_append._append_line(path, f"- {date} 2222222 second commit")
    continuation_append._append_line(path, f"- {date} PR #7 wrap-up: second wrapup")
    assert continuation_append.main() == 0

    content = path.read_text()
    expected = f"- {date} — 2 commit(s), 1 wrap-up(s), 0 merge(s), 0 other, PRs #7"
    assert expected in content


def test_archive_header_written_once_and_appended_across_rotations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rotations write the archive header once and keep both entries."""
    date1 = _days_ago(3)
    _write_continuation(
        tmp_path,
        recent_lines=[f"- {date1} 1111111 first entry", *_filler_lines()],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    archive_path = tmp_path / ".plan" / "CONTINUATION-archive.md"
    first_archive = archive_path.read_text()
    assert first_archive.count(continuation_append.ARCHIVE_HEADER) == 1
    assert "1111111" in first_archive

    date2 = _days_ago(3)
    continuation_append._append_line(
        tmp_path / ".plan" / "CONTINUATION.md", f"- {date2} 2222222 second entry"
    )
    assert continuation_append.main() == 0

    second_archive = archive_path.read_text()
    assert second_archive.count(continuation_append.ARCHIVE_HEADER) == 1
    assert "1111111" in second_archive
    assert "2222222" in second_archive


def test_rotate_is_idempotent_on_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second --rotate with no new stale entries leaves the file untouched."""
    date = _days_ago(3)
    _write_continuation(
        tmp_path,
        recent_lines=[f"- {date} 1234567 solo entry", *_filler_lines()],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0
    content_after_first = (tmp_path / ".plan" / "CONTINUATION.md").read_text()

    assert continuation_append.main() == 0
    content_after_second = (tmp_path / ".plan" / "CONTINUATION.md").read_text()

    assert content_after_second == content_after_first


def test_rotate_classifies_unrecognized_content_as_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line whose content matches none of the known kinds counts as other."""
    date = _days_ago(3)
    _write_continuation(
        tmp_path,
        recent_lines=[
            f"- {date} abcdef too short a hash to be a commit",
            *_filler_lines(),
        ],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    expected = f"- {date} — 0 commit(s), 0 wrap-up(s), 0 merge(s), 1 other"
    assert expected in content


def test_rotate_without_subject_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--rotate needs no subject and succeeds even on a fresh repo."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0


def test_commit_without_subject_raises_system_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--commit without a subject is an argument error (argparse exits)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["forge-continuation-append", "--commit", "abc1234"]
    )
    with pytest.raises(SystemExit):
        continuation_append.main()


def test_pyproject_continuation_config_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real [tool.forge.continuation] table overrides both defaults."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.continuation]\nmax_recent_entries = 2\nmax_recent_age_days = 0\n"
    )
    today = _days_ago(0)
    yesterday = _days_ago(1)
    lines = [
        f"- {yesterday} aaaaaaa old commit",
        f"- {today} bbbbbbb today commit 1",
        f"- {today} ccccccc today commit 2",
        f"- {today} ddddddd today commit 3",
    ]
    _write_continuation(tmp_path, recent_lines=lines)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    recent_section = content.split(continuation_append.RECENT_HEADER, 1)[1]
    assert "aaaaaaa" not in recent_section
    assert "bbbbbbb" not in recent_section
    assert "ccccccc" in recent_section
    assert "ddddddd" in recent_section
    archive = (tmp_path / ".plan" / "CONTINUATION-archive.md").read_text()
    assert "aaaaaaa" in archive
    assert "bbbbbbb" in archive


@pytest.mark.parametrize("bad_key", ["max_recent_entries", "max_recent_age_days"])
def test_non_int_config_value_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_key: str
) -> None:
    """A non-int [tool.forge.continuation] value falls back to its default.

    Args:
        bad_key: The config key given a non-int value, parametrized over
            both continuation-config keys so each falls back independently.
    """
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.forge.continuation]\n{bad_key} = "not-an-int"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])

    if bad_key == "max_recent_entries":
        today = _days_ago(0)
        lines = [f"- {today} {i:07x} commit {i}" for i in range(51)]
        _write_continuation(tmp_path, recent_lines=lines)
        assert continuation_append.main() == 0
        content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
        recent_section = content.split(continuation_append.RECENT_HEADER, 1)[1]
        kept = [ln for ln in recent_section.splitlines() if ln.startswith("- ")]
        assert len(kept) == continuation_append.DEFAULT_MAX_RECENT_ENTRIES
    else:
        stale = _days_ago(3)
        fresh = _days_ago(1)
        _write_continuation(
            tmp_path,
            recent_lines=[
                f"- {stale} 1111111 stale commit",
                f"- {fresh} 2222222 fresh commit",
                *_filler_lines(),
            ],
        )
        assert continuation_append.main() == 0
        content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
        recent_section = content.split(continuation_append.RECENT_HEADER, 1)[1]
        assert "1111111" not in recent_section
        assert "2222222" in recent_section


def test_rotate_preserves_structured_head_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rotation never touches the Status / In progress / Next sections."""
    head = (
        f"{continuation_append.FILE_HEADER}\n\n"
        "## Status\nWorking on issue #412.\n\n"
        "## In progress\n- Doing thing A\n- Doing thing B\n\n"
        "## Next potential work\n1. Thing C\n\n"
    )
    old_date = _days_ago(3)
    _write_continuation(
        tmp_path,
        head=head,
        recent_lines=[f"- {old_date} 1234567 aged commit", *_filler_lines()],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    assert content.startswith(head)


def test_rotate_floor_keeps_ten_newest_entries_when_all_aged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor keeps the newest MIN_RECENT_ENTRIES raw even when everything aged."""
    aged_date = _days_ago(3)
    lines = [f"- {aged_date} {i:07x} commit {i}" for i in range(20)]
    _write_continuation(tmp_path, recent_lines=lines)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    recent_section = content.split(continuation_append.RECENT_HEADER, 1)[1]
    kept = [ln for ln in recent_section.splitlines() if ln.startswith("- ")]
    assert kept == lines[10:]
    assert len(kept) == continuation_append.MIN_RECENT_ENTRIES

    archive = (tmp_path / ".plan" / "CONTINUATION-archive.md").read_text()
    for line in lines[:10]:
        assert line in archive
    for line in lines[10:]:
        assert line not in archive


def test_rotate_cap_evicts_oldest_unpinned_entries_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count cap evicts the oldest UNPINNED entries; a pinned one survives."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.continuation]\nmax_recent_entries = 5\n"
    )
    head = (
        f"{continuation_append.FILE_HEADER}\n\n"
        "## In progress\n- **PR #77** open, awaiting merge.\n\n"
    )
    today = _days_ago(0)
    pinned_line = f"- {today} PR #77 wrap-up: shipped partial"
    unpinned_lines = [f"- {today} {i:07x} commit {i}" for i in range(1, 7)]
    recent_lines = [pinned_line, *unpinned_lines]
    _write_continuation(tmp_path, head=head, recent_lines=recent_lines)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    assert continuation_append.main() == 0

    content = (tmp_path / ".plan" / "CONTINUATION.md").read_text()
    recent_section = content.split(continuation_append.RECENT_HEADER, 1)[1]
    kept = [ln for ln in recent_section.splitlines() if ln.startswith("- ")]
    assert kept == [pinned_line, *unpinned_lines[2:]]

    archive = (tmp_path / ".plan" / "CONTINUATION-archive.md").read_text()
    assert unpinned_lines[0] in archive
    assert unpinned_lines[1] in archive


def test_rotate_wip_advisory_logged_when_pinned_exceed_half_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A WIP advisory logs when pinned kept entries exceed half the entry cap."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.continuation]\nmax_recent_entries = 4\n"
    )
    head = (
        f"{continuation_append.FILE_HEADER}\n\n"
        "## In progress\n- **PR #99** open, awaiting merge.\n\n"
    )
    today = _days_ago(0)
    recent_lines = [
        f"- {today} PR #99 wrap-up: partial one",
        f"- {today} PR #99 wrap-up: partial two",
        f"- {today} PR #99 wrap-up: partial three",
        f"- {today} 1234567 unrelated commit",
    ]
    _write_continuation(tmp_path, head=head, recent_lines=recent_lines)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-continuation-append", "--rotate"])
    caplog.set_level(logging.INFO, logger="forge.continuation_append")

    assert continuation_append.main() == 0

    assert any(
        record.name == "forge.continuation_append"
        and "advisory:" in record.getMessage()
        for record in caplog.records
    )


# --- Rotation: development-class (direct helper calls) --------------------


def test_parse_digests_drops_garbage_lines() -> None:
    """Development-class: pins ``_parse_digests``'s malformed-line skip.

    Plan-justified: the drop behavior has no separate main()-level
    observable beyond the (already-covered) rendered digest, so it is
    checked directly against the parser's return value.
    """
    digest_lines = [
        "- 2026-08-01 — 3 commit(s), 1 wrap-up(s), 0 merge(s), 0 other, PRs #2 #9",
        "not a digest line at all",
        "- 2026-08-02 totally malformed",
    ]
    acc = continuation_append._parse_digests(digest_lines)
    assert acc == {"2026-08-01": (3, 1, 0, 0, {"#2", "#9"})}


def test_condense_into_classifies_all_kinds_and_pr_set() -> None:
    """Development-class: pins ``_condense_into``'s per-kind classification.

    Plan-justified: exercises the classifier's kind/PR-set bookkeeping in
    isolation, independent of the digest string rendering covered by the
    behavior-class digest-format case.
    """
    overflow = [
        "- 2026-08-01 1234567 fix bug",
        "- 2026-08-01 PR #5 wrap-up: ship",
        "- 2026-08-01 89abcde PR merged: release",
        "- 2026-08-01 something else entirely",
        "not a valid entry line",
    ]
    acc = continuation_append._condense_into({}, overflow)
    assert acc == {"2026-08-01": (1, 1, 1, 1, {"#5"})}


def test_render_digest_sorts_dates_and_prs_numerically() -> None:
    """Development-class: pins ``_render_digest``'s sort order.

    Plan-justified: proves PR numbers sort numerically (#2 #9 #33), not
    lexicographically (which would order #33 before #9) — a distinction
    only visible by calling the renderer directly with a crafted
    accumulator.
    """
    acc: dict[str, list[object]] = {
        "2026-08-05": [1, 0, 0, 0, set()],
        "2026-08-01": [2, 1, 0, 0, {"#33", "#9", "#2"}],
        "2026-08-03": [0, 0, 1, 0, set()],
    }
    lines = continuation_append._render_digest(acc)
    assert lines == [
        "- 2026-08-01 — 2 commit(s), 1 wrap-up(s), 0 merge(s), 0 other, PRs #2 #9 #33",
        "- 2026-08-03 — 0 commit(s), 0 wrap-up(s), 1 merge(s), 0 other",
        "- 2026-08-05 — 1 commit(s), 0 wrap-up(s), 0 merge(s), 0 other",
    ]
