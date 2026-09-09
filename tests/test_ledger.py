"""Tests for ``forge.ledger`` — the append-only ``key=value`` ledger shape."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge.ledger import append_ledger_line, parse_ledger


if TYPE_CHECKING:
    from pathlib import Path


LEDGER_LINE_WITH_SPACES_IN_TAIL = (
    "ts=2024-01-01T00:00:00+00:00  label=r1  exit=0  wall=1.5s  "
    "peak_rss=10.0MB  cmd=pytest -q tests/test_a.py\n"
)
LEDGER_LINE_NO_EQUALS = "not a valid ledger line at all\n"
LEDGER_LINE_NO_TAIL_SEPARATOR = (
    "ts=2024-01-01T00:00:00+00:00  label=-  exit=0  wall=1.0s  peak_rss=n/a\n"
)


# ---------------------------------------------------------------------------
# append_ledger_line
# ---------------------------------------------------------------------------


def test_append_ledger_line_writes_ts_first_then_fields_two_space_separated(
    tmp_path: Path,
) -> None:
    """``ts=`` leads the line; the given fields follow, two spaces apart."""
    path = tmp_path / "code_health" / "some_history.log"
    append_ledger_line(path, {"label": "r1", "exit": 0})
    line = path.read_text(encoding="utf-8")
    parts = line.rstrip("\n").split("  ")
    assert parts[0].startswith("ts=")
    assert parts[1:] == ["label=r1", "exit=0"]


def test_append_ledger_line_creates_parent_dir_on_first_use(tmp_path: Path) -> None:
    """The parent directory need not exist yet — it is created on first append."""
    path = tmp_path / "sub" / "dir" / "history.log"
    assert not path.parent.exists()
    append_ledger_line(path, {"label": "r1"})
    assert path.is_file()


def test_append_ledger_line_tail_field_written_last_with_spaces_preserved(
    tmp_path: Path,
) -> None:
    """The ``tail`` field is written last and its embedded spaces survive."""
    path = tmp_path / "history.log"
    append_ledger_line(path, {"label": "r1"}, tail=("cmd", "pytest -q tests/test_a.py"))
    line = path.read_text(encoding="utf-8")
    assert line.rstrip("\n").endswith("cmd=pytest -q tests/test_a.py")
    assert "  label=r1  cmd=" in line


def test_append_ledger_line_second_call_appends_not_overwrites(tmp_path: Path) -> None:
    """A second call adds a second line; the first line is untouched."""
    path = tmp_path / "history.log"
    append_ledger_line(path, {"label": "r1"})
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    append_ledger_line(path, {"label": "r2"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == first_line
    assert "label=r2" in lines[1]


# ---------------------------------------------------------------------------
# parse_ledger
# ---------------------------------------------------------------------------


def test_parse_ledger_tail_key_keeps_embedded_spaces() -> None:
    """The tail field's value keeps its embedded spaces intact."""
    rows = parse_ledger(LEDGER_LINE_WITH_SPACES_IN_TAIL, tail_key="cmd")
    assert rows == [
        {
            "ts": "2024-01-01T00:00:00+00:00",
            "label": "r1",
            "exit": "0",
            "wall": "1.5s",
            "peak_rss": "10.0MB",
            "cmd": "pytest -q tests/test_a.py",
        }
    ]


def test_parse_ledger_line_without_equals_skipped_adjacent_valid_line_parsed() -> None:
    """A line with no ``=`` at all is skipped; a valid neighbor still parses."""
    text = LEDGER_LINE_NO_EQUALS + LEDGER_LINE_WITH_SPACES_IN_TAIL
    rows = parse_ledger(text, tail_key="cmd")
    assert len(rows) == 1
    assert rows[0]["cmd"] == "pytest -q tests/test_a.py"


def test_parse_ledger_empty_text_returns_empty_list() -> None:
    """Empty ledger text parses to an empty list, not an error."""
    assert parse_ledger("") == []


def test_parse_ledger_row_without_tail_key_separator_omits_tail_key() -> None:
    """A row with no ``  <tail_key>=`` separator is still built, minus that key."""
    rows = parse_ledger(LEDGER_LINE_NO_TAIL_SEPARATOR, tail_key="cmd")
    assert len(rows) == 1
    assert "cmd" not in rows[0]
    assert rows[0]["peak_rss"] == "n/a"


def test_parse_ledger_no_tail_key_requested_returns_all_fields_flat() -> None:
    """Without a ``tail_key``, every space-free field parses flat, none special."""
    rows = parse_ledger(LEDGER_LINE_NO_TAIL_SEPARATOR)
    assert rows == [
        {
            "ts": "2024-01-01T00:00:00+00:00",
            "label": "-",
            "exit": "0",
            "wall": "1.0s",
            "peak_rss": "n/a",
        }
    ]


def test_append_then_parse_round_trip_preserves_field_values(tmp_path: Path) -> None:
    """A line written by ``append_ledger_line`` parses back to the same values."""
    path = tmp_path / "history.log"
    append_ledger_line(
        path,
        {"label": "r1", "exit": 0},
        tail=("cmd", "pytest -q tests/test_a.py"),
    )
    rows = parse_ledger(path.read_text(encoding="utf-8"), tail_key="cmd")
    assert len(rows) == 1
    row = rows[0]
    assert row["ts"]
    assert row["label"] == "r1"
    assert row["exit"] == "0"
    assert row["cmd"] == "pytest -q tests/test_a.py"
