"""Tests for ``forge.slow_tests_report``."""

from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING

from forge import slow_tests_report
from forge.slow_tests_report import (
    Duration,
    format_baseline_delta,
    format_report,
    load_baseline,
    parse_durations,
    save_baseline,
)


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
    # Without --baseline, no regression block should be appended at all.
    assert "Baseline" not in written


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


# ---------------------------------------------------------------------------
# format_baseline_delta
# ---------------------------------------------------------------------------


def test_format_baseline_delta_all_clear_when_nothing_regressed() -> None:
    """Every duration matches its baseline — the all-clear one-liner, no blocks."""
    durations = [Duration(2.0, "call", "tests/test_a.py::test_x")]
    baseline = {"tests/test_a.py::test_x::call": 2.0}
    report = format_baseline_delta(durations, baseline)
    assert "no regressions" in report
    assert "Regressed" not in report
    assert "New slow tests" not in report


def test_format_baseline_delta_regressed_at_exactly_factor_and_above_floor() -> None:
    """A duration at exactly ``factor * base`` and above the floor is regressed."""
    # base=2.0s, current=3.0s → ratio 1.5 == REGRESSION_FACTOR, current above floor.
    durations = [Duration(3.0, "call", "tests/test_a.py::test_x")]
    baseline = {"tests/test_a.py::test_x::call": 2.0}
    report = format_baseline_delta(durations, baseline)
    assert "Regressed (1):" in report
    assert "test_x" in report


def test_format_baseline_delta_below_floor_excluded_even_when_ratio_qualifies() -> None:
    """A ratio-qualifying duration below the floor is excluded — jitter, not signal."""
    # base=0.1s, current=0.2s → ratio 2.0 (qualifies), but current is below the
    # 1.0s floor.
    durations = [Duration(0.2, "call", "tests/test_a.py::test_x")]
    baseline = {"tests/test_a.py::test_x::call": 0.1}
    report = format_baseline_delta(durations, baseline)
    assert "no regressions" in report


def test_format_baseline_delta_new_slow_absent_from_baseline() -> None:
    """A duration above the floor with no baseline entry is reported as new-slow."""
    durations = [Duration(2.0, "call", "tests/test_a.py::test_new")]
    report = format_baseline_delta(durations, {})
    assert "New slow tests (1):" in report
    assert "test_new" in report
    assert "(new)" in report
    assert "Regressed" not in report


def test_format_baseline_delta_combined_regressed_then_new_slow_ordering() -> None:
    """Regressed and new-slow both present — the regressed block renders first."""
    durations = [
        Duration(3.0, "call", "tests/test_a.py::test_regressed"),
        Duration(2.0, "call", "tests/test_a.py::test_new"),
    ]
    baseline = {"tests/test_a.py::test_regressed::call": 2.0}
    report = format_baseline_delta(durations, baseline)
    regressed_index = report.index("Regressed (1):")
    new_slow_index = report.index("New slow tests (1):")
    assert regressed_index < new_slow_index


# ---------------------------------------------------------------------------
# save_baseline
# ---------------------------------------------------------------------------


def test_save_baseline_writes_sorted_flat_json_with_indent_and_trailing_newline(
    tmp_path: Path,
) -> None:
    """Baseline JSON is flat, key-sorted, 2-space indented, trailing newline."""
    durations = [
        Duration(2.0, "call", "tests/test_b.py::test_z"),
        Duration(1.0, "setup", "tests/test_a.py::test_a"),
    ]
    path = tmp_path / "baseline.json"
    save_baseline(durations, path)
    body = path.read_text(encoding="utf-8")
    assert body.endswith("\n")
    assert (
        body
        == json.dumps(
            {
                "tests/test_a.py::test_a::setup": 1.0,
                "tests/test_b.py::test_z::call": 2.0,
            },
            indent=2,
        )
        + "\n"
    )


def test_save_baseline_empty_durations_writes_empty_object(tmp_path: Path) -> None:
    r"""No durations still write a valid, parseable baseline: ``{}\\n``."""
    path = tmp_path / "baseline.json"
    save_baseline([], path)
    assert path.read_text(encoding="utf-8") == "{}\n"


# ---------------------------------------------------------------------------
# load_baseline
# ---------------------------------------------------------------------------


def test_load_baseline_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    """A missing baseline file degrades to ``{}`` — nothing to compare, not an error."""
    assert load_baseline(tmp_path / "absent.json") == {}


def test_load_baseline_existing_file_returns_float_values(tmp_path: Path) -> None:
    """An existing baseline file's values load back in as floats."""
    path = tmp_path / "baseline.json"
    path.write_text('{"tests/test_a.py::test_x::call": 2}', encoding="utf-8")
    loaded = load_baseline(path)
    assert loaded == {"tests/test_a.py::test_x::call": 2.0}
    assert isinstance(loaded["tests/test_a.py::test_x::call"], float)


def test_load_baseline_malformed_json_degrades_to_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupted or wrong-shaped baseline degrades to ``{}`` — never raises.

    Pins the always-exit-0 contract this reporter promises under CI's
    ``if: always()``: a bad merge or hand-edit to the committed baseline
    must warn, not crash the run that is meant to report on it.
    """
    path = tmp_path / "baseline.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_baseline(path) == {}
    assert "malformed" in caplog.text

    # Valid JSON but the wrong shape (a list has no .items()) hits the same
    # degrade-to-empty path via the AttributeError branch of the except clause.
    caplog.clear()
    path.write_text("[1, 2]", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_baseline(path) == {}
    assert "malformed" in caplog.text


# ---------------------------------------------------------------------------
# main — baseline flags
# ---------------------------------------------------------------------------


def test_main_log_and_baseline_appends_delta_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--log`` plus ``--baseline <path>`` appends regression block to ``--out``."""
    log = tmp_path / "pytest.log"
    log.write_text(SINGLE_SECTION, encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"
    # test_slow's baseline is far lower than its 2.50s current time — regresses.
    baseline_path.write_text(
        json.dumps({"tests/test_a.py::test_slow::call": 1.0}), encoding="utf-8"
    )
    out = tmp_path / "code_health" / "slow_tests.log"
    monkeypatch.setattr(
        "sys.argv",
        [
            "forge-slow-tests-report",
            "--log",
            str(log),
            "--baseline",
            str(baseline_path),
            "--out",
            str(out),
        ],
    )
    assert slow_tests_report.main() == 0
    written = out.read_text(encoding="utf-8")
    assert "Baseline comparison" in written
    assert "Regressed (1):" in written
    assert "test_slow" in written


def test_main_bare_baseline_uses_default_path_and_degrades_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare ``--baseline`` resolves :data:`DEFAULT_BASELINE` relative to cwd.

    ``monkeypatch.chdir`` pins the cwd to an isolated ``tmp_path`` — the
    real repo root has its own committed baseline, so without the chdir
    this test would silently read production data instead of exercising
    the "no baseline present" path.
    """
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "pytest.log"
    log.write_text(SINGLE_SECTION, encoding="utf-8")
    assert not (tmp_path / slow_tests_report.DEFAULT_BASELINE).exists()
    out = tmp_path / "code_health" / "slow_tests.log"
    monkeypatch.setattr(
        "sys.argv",
        ["forge-slow-tests-report", "--log", str(log), "--baseline", "--out", str(out)],
    )
    assert slow_tests_report.main() == 0
    written = out.read_text(encoding="utf-8")
    # Every duration is unseen (empty baseline) — reported as new-slow, not
    # an error, proving load_baseline's missing-file path degraded cleanly.
    assert "Baseline comparison" in written
    assert "New slow tests" in written


def test_main_baseline_scans_beyond_top_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--baseline`` compares every parsed duration, not the ``--top``-truncated slice.

    ``--top 1`` keeps only ``test_slow`` (2.50s) in the printed table, but
    ``test_fixture`` (1.20s, second-slowest) regressed against its 0.50s
    baseline — above both the factor and the floor. If
    :func:`format_baseline_delta` were called with the truncated
    ``durations[:top]`` instead of the full list
    (src/forge/slow_tests_report.py:327-331), ``test_fixture`` would never
    reach the comparison and this regression would go unreported.
    """
    log = tmp_path / "pytest.log"
    log.write_text(SINGLE_SECTION, encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"tests/test_a.py::test_fixture::setup": 0.50}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "forge-slow-tests-report",
            "--log",
            str(log),
            "--top",
            "1",
            "--baseline",
            str(baseline_path),
        ],
    )
    with caplog.at_level(logging.INFO):
        assert slow_tests_report.main() == 0
    # The printed table is truncated to top 1 — test_fixture dropped there.
    assert "top 1 of 3" in caplog.text
    # But the Baseline block still scanned the full list and caught it.
    assert "Regressed (1):" in caplog.text
    assert "test_fixture" in caplog.text


def test_main_update_baseline_writes_file_that_round_trips_through_load_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--update-baseline`` writes a baseline that :func:`load_baseline` reads back."""
    log = tmp_path / "pytest.log"
    log.write_text(SINGLE_SECTION, encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "forge-slow-tests-report",
            "--log",
            str(log),
            "--update-baseline",
            str(baseline_path),
        ],
    )
    assert slow_tests_report.main() == 0
    loaded = load_baseline(baseline_path)
    assert loaded == {
        "tests/test_a.py::test_slow::call": 2.50,
        "tests/test_a.py::test_fixture::setup": 1.20,
        "tests/test_a.py::test_mid::call": 0.80,
    }
