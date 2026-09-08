"""Tests for ``forge.slow_tests_report``."""

from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING

import pytest

from forge import slow_tests_report
from forge.slow_tests_report import (
    Duration,
    _source_roots,
    durations_truncated,
    format_baseline_delta,
    format_coverage_ranking,
    format_report,
    load_baseline,
    nodeid_base,
    parse_durations,
    save_baseline,
    seconds_by_base,
    unique_statements,
)


if TYPE_CHECKING:
    from pathlib import Path


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


def test_main_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``--log -`` parses the pytest output piped in on stdin.

    The exit code alone cannot tell the two outcomes apart: a broken
    stdin branch falls through to the missing-log path, which also
    reports no data and also exits 0. The parsed node id in the report
    is what distinguishes reading from silently reading nothing.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(SINGLE_SECTION))
    monkeypatch.setattr("sys.argv", ["forge-slow-tests-report", "--log", "-"])
    with caplog.at_level(logging.INFO):
        assert slow_tests_report.main() == 0
    assert "test_slow" in caplog.text


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
    """A duration above the floor with no baseline entry is reported as new-slow.

    Pins the real-but-empty-baseline half of the ``None``/``{}`` split: a
    genuinely empty ``{}`` baseline (already loaded, not the ``None`` a
    missing/malformed file produces) legitimately reports every
    above-floor duration as new-slow — see
    ``test_main_bare_baseline_uses_default_path_and_degrades_when_absent``
    for the sibling ``None`` case, where the whole comparison is skipped.
    """
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


def test_load_baseline_missing_file_returns_none(tmp_path: Path) -> None:
    """A missing baseline file is ``None`` — no baseline configured, not empty."""
    assert load_baseline(tmp_path / "absent.json") is None


def test_load_baseline_empty_object_file_returns_empty_dict(tmp_path: Path) -> None:
    """A committed baseline that is literally ``{}`` loads as a real, empty baseline.

    Nothing else distinguishes "no baseline file" from "an intentionally
    empty one" at the loader — this is the only test that puts a literal
    ``{}`` on disk and checks it comes back as ``{}``, not ``None``.
    """
    path = tmp_path / "baseline.json"
    path.write_text("{}", encoding="utf-8")
    loaded = load_baseline(path)
    assert loaded == {}
    assert loaded is not None


def test_load_baseline_existing_file_returns_float_values(tmp_path: Path) -> None:
    """An existing baseline file's values load back in as floats."""
    path = tmp_path / "baseline.json"
    path.write_text('{"tests/test_a.py::test_x::call": 2}', encoding="utf-8")
    loaded = load_baseline(path)
    assert loaded == {"tests/test_a.py::test_x::call": 2.0}
    assert isinstance(loaded["tests/test_a.py::test_x::call"], float)


def test_load_baseline_malformed_json_degrades_to_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupted or wrong-shaped baseline degrades to ``None`` — never raises.

    Pins the always-exit-0 contract this reporter promises under CI's
    ``if: always()``: a bad merge or hand-edit to the committed baseline
    must warn, not crash the run that is meant to report on it. ``None``,
    not ``{}`` — a malformed file is "no usable baseline", never a real
    empty one, so callers must not read every duration back as new-slow.
    """
    path = tmp_path / "baseline.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_baseline(path) is None
    assert "malformed" in caplog.text

    # Valid JSON but the wrong shape (a list has no .items()) hits the same
    # degrade-to-None path via the AttributeError branch of the except clause.
    caplog.clear()
    path.write_text("[1, 2]", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_baseline(path) is None
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

    A missing baseline must skip the comparison outright. Loading it as
    an empty mapping instead would render every above-floor duration as
    ``(new)`` — a wall of new-slow entries in a repo that deliberately
    has no baseline, and no all-clear line anywhere in the report.
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
    assert "no usable baseline at" in written
    assert "comparison skipped" in written
    assert "New slow tests" not in written
    assert "(new)" not in written


def test_main_baseline_scans_beyond_top_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--baseline`` compares every parsed duration, not the ``--top``-truncated slice.

    ``--top 1`` keeps only ``test_slow`` (2.50s) in the printed table, but
    ``test_fixture`` (1.20s, second-slowest) regressed against its 0.50s
    baseline — above both the factor and the floor. If ``main``'s
    ``--baseline`` branch passed :func:`format_baseline_delta` the
    truncated ``durations[:top]`` instead of the full list,
    ``test_fixture`` would never reach the comparison and this
    regression would go unreported.
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


# ---------------------------------------------------------------------------
# nodeid_base
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nodeid", "expected"),
    [
        ("t.py::test_x|run", "t.py::test_x"),
        # A parametrize id can itself contain a "[" — the bracket split
        # must take the FIRST one, or this would collapse to "t.py::test_x[a".
        ("t.py::test_x[a[0]-b]|run", "t.py::test_x"),
        ("t.py::test_x", "t.py::test_x"),
    ],
)
def test_nodeid_base_strips_phase_and_first_bracket_only(
    nodeid: str, expected: str
) -> None:
    """Phase suffix and parametrize bracket are stripped, bracket split first-``[``.

    Args:
        nodeid: A pytest node id or coverage context string to collapse.
        expected: The expected ``path::test_function`` base.
    """
    assert nodeid_base(nodeid) == expected


# ---------------------------------------------------------------------------
# seconds_by_base
# ---------------------------------------------------------------------------


def test_seconds_by_base_sums_variants_and_phases() -> None:
    """Setup+call of one variant plus call of another SUM into one base key.

    Deliberately unlike :func:`parse_durations`, which keeps the max
    across duplicate ``(nodeid, phase)`` entries — the cost a test
    function makes the suite pay is every phase and every variant added
    up, not the single worst one.
    """
    durations = [
        Duration(1.0, "setup", "t.py::test_x[a]"),
        Duration(2.0, "call", "t.py::test_x[a]"),
        Duration(3.0, "call", "t.py::test_x[b]"),
    ]
    assert seconds_by_base(durations) == {"t.py::test_x": 6.0}


# ---------------------------------------------------------------------------
# unique_statements
# ---------------------------------------------------------------------------

# One line per scenario, keyed to a distinct file so each assertion below
# is exact and self-contained: a line's owner(s) is the coverage context
# list; contexts carry the pytest-cov `<nodeid>|<phase>` shape.
COVERAGE_MIXED_OWNERSHIP_EXPORT: dict[str, object] = {
    "files": {
        "src/pkg/m.py": {
            "contexts": {
                "1": ["tests/test_m.py::test_solo|run"],
                "2": [
                    "tests/test_m.py::test_shared_a|run",
                    "tests/test_m.py::test_shared_b|run",
                ],
                "3": [
                    "tests/test_m.py::test_phases|setup",
                    "tests/test_m.py::test_phases|run",
                ],
                "4": [
                    "tests/test_m.py::test_p[x]|run",
                    "tests/test_m.py::test_p[y]|run",
                ],
                "5": [""],
                "6": ["", "tests/test_m.py::test_import_mixed|run"],
            }
        },
        "/abs/repo/src/pkg/n.py": {
            "contexts": {"1": ["tests/test_m.py::test_absolute_path|run"]}
        },
        "tests/test_m.py": {
            "contexts": {"1": ["tests/test_m.py::test_only_in_tests_file|run"]}
        },
    }
}


def test_unique_statements_line_owned_by_one_test_counts_once() -> None:
    """A line whose only context is one test's counts once for that test."""
    unique, _ = unique_statements(COVERAGE_MIXED_OWNERSHIP_EXPORT, ["src"])
    assert unique["tests/test_m.py::test_solo"] == 1


def test_unique_statements_shared_line_excluded_from_unique_but_seen() -> None:
    """A line two tests both cover counts for neither, but both are seen."""
    unique, seen = unique_statements(COVERAGE_MIXED_OWNERSHIP_EXPORT, ["src"])
    assert "tests/test_m.py::test_shared_a" not in unique
    assert "tests/test_m.py::test_shared_b" not in unique
    assert "tests/test_m.py::test_shared_a" in seen
    assert "tests/test_m.py::test_shared_b" in seen


def test_unique_statements_setup_and_run_phases_collapse_to_one_owner() -> None:
    """A line context-tagged for setup AND run of one test is still unique to it."""
    unique, _ = unique_statements(COVERAGE_MIXED_OWNERSHIP_EXPORT, ["src"])
    assert unique["tests/test_m.py::test_phases"] == 1


def test_unique_statements_parametrized_variants_collapse_to_base() -> None:
    """Two parametrized variants of one test collapse to a single owner.

    Variants must collapse before uniqueness is computed. Left as
    distinct owners, ``test_p[x]`` and ``test_p[y]`` make every line
    they share read as covered-by-two — so a parametrized test scores
    ~0 unique statements and the ranking advises deleting all of it.
    """
    unique, _ = unique_statements(COVERAGE_MIXED_OWNERSHIP_EXPORT, ["src"])
    assert unique["tests/test_m.py::test_p"] == 1


def test_unique_statements_all_empty_context_contributes_nothing() -> None:
    """A line whose only context is the empty (import-time) string adds no owner.

    Asserted as an exact ``seen`` set: if line "5" (``[""]``) contributed a
    phantom entry (e.g. the empty string itself, unfiltered), this set
    would contain one more member than the eight real test owners below.
    """
    _, seen = unique_statements(COVERAGE_MIXED_OWNERSHIP_EXPORT, ["src"])
    assert seen == {
        "tests/test_m.py::test_solo",
        "tests/test_m.py::test_shared_a",
        "tests/test_m.py::test_shared_b",
        "tests/test_m.py::test_phases",
        "tests/test_m.py::test_p",
        "tests/test_m.py::test_import_mixed",
        "tests/test_m.py::test_absolute_path",
        "tests/test_m.py::test_only_in_tests_file",
    }


def test_unique_statements_empty_context_mixed_with_real_attributes_to_test() -> None:
    """An empty context alongside a real one still attributes the line to the test."""
    unique, seen = unique_statements(COVERAGE_MIXED_OWNERSHIP_EXPORT, ["src"])
    assert unique["tests/test_m.py::test_import_mixed"] == 1
    assert "tests/test_m.py::test_import_mixed" in seen


def test_unique_statements_absolute_path_matches_source_root() -> None:
    """An absolute file path is still matched against a repo-relative source root.

    Coverage emits absolute paths unless ``relative_files=true`` is set —
    scoping must not silently drop every entry for a repo without it.
    """
    unique, _ = unique_statements(COVERAGE_MIXED_OWNERSHIP_EXPORT, ["src"])
    assert unique["tests/test_m.py::test_absolute_path"] == 1


def test_unique_statements_scoped_out_test_still_recorded_in_seen() -> None:
    """A test outside source_roots is excluded from unique, but kept in seen.

    ``seen`` is deliberately gathered unscoped: a test entirely outside
    the source roots renders "0 uniq" downstream, a real verdict — not
    "(no coverage data)", which would misreport a scoping choice as a
    data gap.
    """
    unique, seen = unique_statements(COVERAGE_MIXED_OWNERSHIP_EXPORT, ["src"])
    assert "tests/test_m.py::test_only_in_tests_file" not in unique
    assert "tests/test_m.py::test_only_in_tests_file" in seen


def test_unique_statements_empty_roots_counts_everything() -> None:
    """An empty ``source_roots`` list scopes to nothing excluded — everything counts."""
    unique, _ = unique_statements(COVERAGE_MIXED_OWNERSHIP_EXPORT, [])
    assert unique["tests/test_m.py::test_only_in_tests_file"] == 1


def test_unique_statements_non_list_context_value_degrades_instead_of_raising() -> None:
    """A ``contexts`` value that is not a list is skipped, not iterated.

    This reporter runs under CI's ``if: always()``, so a malformed export
    must degrade to an empty verdict rather than raise past a caller that
    promises never to fail the run.
    """
    export_with_int_where_context_list_expected = {
        "files": {"src/a.py": {"contexts": {"1": 5}}}
    }
    assert unique_statements(export_with_int_where_context_list_expected, ["src"]) == (
        {},
        set(),
    )


def test_unique_statements_non_string_context_entry_filtered_not_whole_line() -> None:
    """A non-string context entry is dropped; a valid entry beside it still counts.

    ``nodeid_base`` expects a string to split on. One junk entry (e.g. an
    int a malformed export produced) must not take the rest of the same
    line's real attribution down with it.
    """
    export_with_junk_entry_beside_real_context = {
        "files": {"src/a.py": {"contexts": {"1": [123, "t.py::test_x|run"]}}}
    }
    unique, seen = unique_statements(
        export_with_junk_entry_beside_real_context, ["src"]
    )
    assert unique["t.py::test_x"] == 1
    assert "t.py::test_x" in seen


# ---------------------------------------------------------------------------
# format_coverage_ranking
# ---------------------------------------------------------------------------

# seen = {test_zero, test_dummy_other, test_high_ratio, test_low_ratio,
# test_instant, test_quick}; unique = {test_high_ratio: 3, test_low_ratio: 1,
# test_instant: 1, test_quick: 2} — test_zero and test_dummy_other share
# line "1" so neither is unique, giving test_zero a real (not data-gap)
# 0-uniq row. test_instant and test_quick both fall below the too-fast
# floor but carry different unique counts, so the too-fast tier has an
# order of its own to pin.
COVERAGE_RANKING_EXPORT: dict[str, object] = {
    "files": {
        "src/pkg/m.py": {
            "contexts": {
                "1": [
                    "tests/test_m.py::test_zero|run",
                    "tests/test_m.py::test_dummy_other|run",
                ],
                "2": ["tests/test_m.py::test_high_ratio|run"],
                "3": ["tests/test_m.py::test_high_ratio|run"],
                "4": ["tests/test_m.py::test_high_ratio|run"],
                "5": ["tests/test_m.py::test_low_ratio|run"],
                "6": ["tests/test_m.py::test_instant|run"],
                "7": ["tests/test_m.py::test_quick|run"],
                "8": ["tests/test_m.py::test_quick|run"],
            }
        }
    }
}

RANKING_DURATIONS = [
    Duration(2.0, "call", "tests/test_m.py::test_zero"),  # 0 uniq / 2.0s -> ratio 0.0
    Duration(1.0, "call", "tests/test_m.py::test_high_ratio"),  # 3 uniq / 1.0s -> 3.0
    Duration(4.0, "call", "tests/test_m.py::test_low_ratio"),  # 1 uniq / 4.0s -> 0.25
    Duration(1.0, "call", "tests/test_m.py::test_absent"),  # not in the export at all
    Duration(0.0, "call", "tests/test_m.py::test_instant"),  # 1 uniq, below floor
    Duration(0.0, "call", "tests/test_m.py::test_quick"),  # 2 uniq, below floor
]


def test_format_coverage_ranking_tiers_ratio_then_too_fast_then_no_data() -> None:
    """Rows sort by tier, not by a single competing number.

    A real ratio outranks every "too fast to rank" row, which in turn
    outranks a "no coverage data" row — and within the too-fast tier,
    rows order by their own ascending unique count rather than falling
    back to dict-insertion order.
    """
    report = format_coverage_ranking(
        RANKING_DURATIONS, COVERAGE_RANKING_EXPORT, ["src"], top=10, truncated=False
    )
    rows = report.splitlines()
    idx = {
        name: next(i for i, row in enumerate(rows) if name in row)
        for name in (
            "test_zero",
            "test_low_ratio",
            "test_high_ratio",
            "test_instant",
            "test_quick",
            "test_absent",
        )
    }
    # (a) every real-ratio row precedes both degrade tiers.
    assert idx["test_zero"] < idx["test_low_ratio"] < idx["test_high_ratio"]
    assert idx["test_high_ratio"] < idx["test_instant"]
    assert idx["test_high_ratio"] < idx["test_quick"]
    # (b) too-fast rows are ordered by their own ascending unique count.
    assert idx["test_instant"] < idx["test_quick"]
    # (c) the no-coverage-data row is last, after both too-fast rows.
    assert idx["test_absent"] > idx["test_instant"]
    assert idx["test_absent"] > idx["test_quick"]
    assert "0 uniq" in rows[idx["test_zero"]]
    assert "(too fast to rank)" in rows[idx["test_instant"]]
    assert "(too fast to rank)" in rows[idx["test_quick"]]
    assert "(no coverage data)" in rows[idx["test_absent"]]


def test_format_coverage_ranking_respects_top_and_reports_of_total() -> None:
    """``top=2`` keeps only the two lowest-worth rows and reports "2 of 6"."""
    report = format_coverage_ranking(
        RANKING_DURATIONS, COVERAGE_RANKING_EXPORT, ["src"], top=2, truncated=False
    )
    assert "top 2 of 6" in report
    assert "test_zero" in report
    assert "test_low_ratio" in report
    assert "test_high_ratio" not in report
    assert "test_absent" not in report
    assert "test_instant" not in report
    assert "test_quick" not in report


def test_format_coverage_ranking_truncated_note_present_when_true() -> None:
    """``truncated=True`` appends the lower-bound-seconds NOTE line."""
    report = format_coverage_ranking(
        RANKING_DURATIONS, COVERAGE_RANKING_EXPORT, ["src"], top=10, truncated=True
    )
    assert "NOTE" in report
    assert "lower bounds" in report


def test_format_coverage_ranking_truncated_note_absent_when_false() -> None:
    """``truncated=False`` omits the NOTE line entirely."""
    report = format_coverage_ranking(
        RANKING_DURATIONS, COVERAGE_RANKING_EXPORT, ["src"], top=10, truncated=False
    )
    assert "NOTE" not in report


@pytest.mark.parametrize(
    "data",
    [
        {"files": {"src/pkg/m.py": {}}},
        {"files": {"src/pkg/m.py": {"contexts": {"1": [""], "2": [""]}}}},
    ],
    ids=["no-contexts-key", "contexts-all-empty"],
)
def test_format_coverage_ranking_skip_when_no_per_test_contexts(
    data: dict[str, object],
) -> None:
    """No usable per-test contexts anywhere in the export → the skip line, zero rows.

    Args:
        data: A coverage export with no per-test context data, in either
            of the two shapes that produce it (key absent, or present but
            every context list is the empty-string import-time marker).
    """
    report = format_coverage_ranking([], data, [], top=25, truncated=False)
    assert "no per-test contexts" in report
    assert len(report.splitlines()) == 1


def test_format_coverage_ranking_no_timing_data_when_seen_but_no_durations() -> None:
    """A non-empty export with no durations to rank reports "no timing data"."""
    data: dict[str, object] = {
        "files": {"src/pkg/m.py": {"contexts": {"1": ["t.py::test_x|run"]}}}
    }
    report = format_coverage_ranking([], data, [], top=25, truncated=False)
    assert "no timing data to rank" in report


# ---------------------------------------------------------------------------
# durations_truncated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            (
                "==== slowest durations ====\n"
                "(6 durations < 0.005s hidden.  Use -vv to show these durations.)"
            ),
            True,
        ),
        (
            (
                "==== slowest 2 durations ====\n"
                "1.00s call tests/test_a.py::test_one\n"
                "0.50s call tests/test_a.py::test_two\n"
                "==== 2 passed in 1.50s ===="
            ),
            True,
        ),
        (
            (
                "==== slowest 2 durations ====\n"
                "1.00s call tests/test_a.py::test_one\n"
                "==== 1 passed in 1.00s ===="
            ),
            False,
        ),
        ("==== slowest durations ====", False),
        (
            (
                f"==== slowest {'9' * 5000} durations ====\n"
                "1.00s call tests/test_a.py::test_one"
            ),
            False,
        ),
        (
            (
                "==== slowest 2 durations ====\n"
                "1.00s call tests/test_a.py::test_one\n"
                "0.50s call tests/test_a.py::test_two"
            ),
            True,
        ),
        (
            (
                "==== slowest 1 durations ====\n"
                "1.00s call tests/test_a.py::test_one\n"
                "==== slowest 5 durations ====\n"
                "0.50s call tests/test_a.py::test_two"
            ),
            True,
        ),
    ],
    ids=[
        "hidden-trailer",
        "numbered-section-filled-to-limit",
        "numbered-section-under-limit",
        "bare-header-no-trailer",
        "digit-run-too-long-to-be-a-real-header",
        "end-of-text-filled-section-no-separator",
        "back-to-back-headers-no-separator-between",
    ],
)
def test_durations_truncated(text: str, *, expected: bool) -> None:
    """Truncation is claimed only on evidence, never on a header's mere presence.

    A "durations hidden" trailer, or a numbered section that printed a
    full N entry rows, are both proof pytest may have cut more. A
    numbered section under its own limit, or the unnumbered
    ``--durations=0`` header with no trailer, prove nothing was cut. A
    digit run longer than any real ``--durations=N`` is not a header at
    all — the function degrades to ``False`` rather than raising on a
    junk line. A filled section is judged at whatever boundary closes
    it — a separator, the next ``slowest N durations`` header, or the
    end of the log — never only at a separator.

    Args:
        text: The raw pytest durations section text to check.
        expected: Whether the text indicates a truncated list.
    """
    assert durations_truncated(text) is expected


# ---------------------------------------------------------------------------
# main — --coverage-json
# ---------------------------------------------------------------------------

COVERAGE_JSON_LOG = """\
============================= slowest 25 durations =============================
2.00s call     tests/test_a.py::test_slow
============================== 1 passed in 2.10s ===============================
"""

COVERAGE_JSON_EXPORT: dict[str, object] = {
    "files": {
        "src/pkg/m.py": {
            "contexts": {"1": ["tests/test_a.py::test_slow|run"]},
        }
    }
}


def test_main_coverage_json_happy_path_ranks_by_worth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--coverage-json`` appends a ranking block built from the real export.

    Fakes the source roots rather than depending on this repo's own
    ``[tool.forge].source_dirs`` — a lambda, not ``unittest.mock.Mock``,
    since only the return value matters here.
    """
    monkeypatch.setattr(
        "forge.slow_tests_report.resolve_tool_roots", lambda *_a, **_k: ["src"]
    )
    log = tmp_path / "pytest.log"
    log.write_text(COVERAGE_JSON_LOG, encoding="utf-8")
    coverage_json = tmp_path / "coverage.json"
    coverage_json.write_text(json.dumps(COVERAGE_JSON_EXPORT), encoding="utf-8")
    out = tmp_path / "slow_tests.log"
    monkeypatch.setattr(
        "sys.argv",
        [
            "forge-slow-tests-report",
            "--log",
            str(log),
            "--coverage-json",
            str(coverage_json),
            "--out",
            str(out),
        ],
    )
    assert slow_tests_report.main() == 0
    written = out.read_text(encoding="utf-8")
    assert "Coverage worth" in written
    assert "uniq/s" in written
    assert "Baseline" not in written


def test_main_coverage_json_missing_export_degrades_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--coverage-json`` path that does not exist degrades to a skip line."""
    log = tmp_path / "pytest.log"
    log.write_text(COVERAGE_JSON_LOG, encoding="utf-8")
    missing = tmp_path / "absent-coverage.json"
    out = tmp_path / "slow_tests.log"
    monkeypatch.setattr(
        "sys.argv",
        [
            "forge-slow-tests-report",
            "--log",
            str(log),
            "--coverage-json",
            str(missing),
            "--out",
            str(out),
        ],
    )
    assert slow_tests_report.main() == 0
    written = out.read_text(encoding="utf-8")
    assert "no usable coverage export" in written
    assert "ranking skipped" in written


# ---------------------------------------------------------------------------
# _source_roots
# ---------------------------------------------------------------------------


def _exit_as_if_no_git_repo() -> None:
    """Fake ``repo_root()``'s behavior outside a git repo."""
    raise SystemExit(1)


def test_source_roots_degrades_to_empty_list_when_repo_root_exits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``repo_root()`` that exits (no git repo) degrades to ``[]``, not a crash.

    ``repo_root()`` calls ``sys.exit(1)`` outside a git repo; this
    reporter's always-exit-0 contract must hold on the ``--coverage-json``
    path too, so the ``SystemExit`` is caught and reported as a warning
    rather than propagated.
    """
    monkeypatch.setattr("forge.slow_tests_report.repo_root", _exit_as_if_no_git_repo)
    with caplog.at_level(logging.WARNING):
        assert _source_roots() == []
    assert "not a git repo" in caplog.text
