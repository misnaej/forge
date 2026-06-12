"""Tests for ``forge.slow_tests_report``."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from forge import slow_tests_report
from forge.slow_tests_report import Duration, format_report, parse_durations


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


SINGLE_SECTION = """\
============================= test session starts ==============================
collected 3 items

tests/test_a.py ...                                                      [100%]

============================= slowest 25 durations =============================
2.50s call     tests/test_a.py::test_slow
1.20s setup    tests/test_a.py::test_fixture
0.80s call     tests/test_a.py::test_mid
============================== 3 passed in 4.60s ===============================
"""

MULTI_SECTION = """\
============================= slowest 25 durations =============================
1.00s call     tests/test_a.py::test_x
3.00s call     tests/test_b.py::test_y
============================== 2 passed in 4.10s ===============================
============================= slowest 25 durations =============================
5.00s call     tests/test_c.py::test_z
2.00s call     tests/test_a.py::test_x
============================== 2 passed in 7.20s ===============================
"""


def test_parse_single_section_sorted_desc() -> None:
    """A single durations section is parsed and ranked slowest first."""
    durations = parse_durations(SINGLE_SECTION)
    assert durations == [
        Duration(2.50, "call", "tests/test_a.py::test_slow"),
        Duration(1.20, "setup", "tests/test_a.py::test_fixture"),
        Duration(0.80, "call", "tests/test_a.py::test_mid"),
    ]


def test_parse_merges_sections_keeping_worst() -> None:
    """Entries from every section merge; duplicates keep the max time."""
    durations = parse_durations(MULTI_SECTION)
    nodeids = [(d.nodeid, d.seconds) for d in durations]
    # test_x appears in both sections (1.0s and 2.0s) — keep 2.0s, once.
    assert ("tests/test_a.py::test_x", 2.00) in nodeids
    assert sum(n == "tests/test_a.py::test_x" for n, _ in nodeids) == 1
    # Global ranking across sections.
    assert durations[0] == Duration(5.00, "call", "tests/test_c.py::test_z")


def test_parse_handles_bare_durations_header() -> None:
    """`pytest --durations=0` emits 'slowest durations' (no count) — still parsed."""
    bare = (
        "===================== slowest durations ======================\n"
        "1.50s call     tests/test_a.py::test_z\n"
        "===================== 1 passed in 1.6s =======================\n"
    )
    assert parse_durations(bare) == [Duration(1.50, "call", "tests/test_a.py::test_z")]


def test_parse_ignores_durations_lines_outside_a_section() -> None:
    """A duration-shaped line with no preceding header is not captured."""
    stray = "0.99s call tests/test_a.py::test_orphan\n"
    assert parse_durations(stray) == []


def test_parse_empty_when_no_section() -> None:
    """Output without a durations section yields no entries."""
    assert parse_durations("1 passed in 0.01s\n") == []


def test_format_report_empty() -> None:
    """The no-data report names the missing flag, not a blank line."""
    assert "no timing data" in format_report([], 25)


def test_format_report_respects_top() -> None:
    """Only the top-N rows render, and the header reports N of total."""
    durations = parse_durations(SINGLE_SECTION)
    report = format_report(durations, top=2)
    assert "top 2 of 3" in report
    assert "test_mid" not in report  # third-slowest dropped by top=2


def test_main_reads_file_and_writes_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main`` parses ``--log`` and persists the report to ``--out``."""
    log = tmp_path / "pytest.log"
    log.write_text(SINGLE_SECTION, encoding="utf-8")
    out = tmp_path / "code_health" / "slow_tests.log"
    monkeypatch.setattr(
        "sys.argv",
        ["forge-slow-tests-report", "--log", str(log), "--out", str(out), "--top", "5"],
    )
    assert slow_tests_report.main() == 0
    written = out.read_text(encoding="utf-8")
    assert "test_slow" in written
    assert "Slowest tests" in written


def test_main_missing_log_is_graceful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing log is reported as no-data and still exits 0."""
    monkeypatch.setattr(
        "sys.argv", ["forge-slow-tests-report", "--log", str(tmp_path / "absent.log")]
    )
    assert slow_tests_report.main() == 0


def test_main_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--log -`` reads the pytest output from stdin."""
    monkeypatch.setattr("sys.stdin", io.StringIO(SINGLE_SECTION))
    monkeypatch.setattr("sys.argv", ["forge-slow-tests-report", "--log", "-"])
    assert slow_tests_report.main() == 0
