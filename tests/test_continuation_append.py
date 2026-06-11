"""Tests for ``forge.continuation_append``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge import continuation_append


if TYPE_CHECKING:
    from pathlib import Path


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
