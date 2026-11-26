"""Tests for ``forge.smart_test.coverage`` — coverage-validated test selection."""

# MOCKING STRATEGY: ``_context_to_test`` and ``_entry_tests`` are pure-logic
# functions tested directly with in-memory dicts — no file I/O, no mocking.
# ``iter_export_files`` and ``tests_covering`` are tested against real JSON
# files (written to ``tmp_path``) for the happy-path, chunk-boundary,
# missing-file, malformed-file, oversized-file, and non-dict-shaped edge
# cases. ``available_memory_bytes`` is environment-dependent, so it is only
# smoke-tested; oversized-export tests monkeypatch it to a fixed value.

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

from forge.smart_test.coverage import (
    ExportUnavailableError,
    _context_to_test,
    _entry_tests,
    _StreamReader,
    available_memory_bytes,
    iter_export_files,
)
from forge.smart_test.coverage import tests_covering as _tests_covering


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# _context_to_test — pure-unit
# ---------------------------------------------------------------------------


def test_context_to_test_nodeid_returns_path() -> None:
    """A pytest node id ``tests/test_x.py::test_fn`` yields ``tests/test_x.py``."""
    assert _context_to_test("tests/test_x.py::test_fn") == "tests/test_x.py"


def test_context_to_test_nested_nodeid() -> None:
    """A node id with multiple ``::`` separators yields only the file path."""
    assert (
        _context_to_test("tests/sub/test_y.py::Class::test_method")
        == "tests/sub/test_y.py"
    )


def test_context_to_test_empty_returns_none() -> None:
    """An empty context string (code run outside any test) returns ``None``."""
    assert _context_to_test("") is None


def test_context_to_test_non_py_returns_none() -> None:
    """A context whose file portion does not end in ``.py`` returns ``None``."""
    assert _context_to_test("tests/test_x.rb::test_fn") is None


def test_context_to_test_bare_py_path() -> None:
    """A context that IS a bare ``.py`` path (no ``::`` separator) returns it."""
    assert _context_to_test("tests/test_x.py") == "tests/test_x.py"


# ---------------------------------------------------------------------------
# _entry_tests — pure logic on one files member
# ---------------------------------------------------------------------------


def test_entry_tests_returns_covering_tests() -> None:
    """The entry's contexts are translated to test-file paths."""
    info = {
        "contexts": {
            "1": ["tests/test_foo.py::test_one"],
            "5": ["tests/test_foo.py::test_two"],
        }
    }
    assert _entry_tests(info) == {"tests/test_foo.py"}


def test_entry_tests_filters_non_py_contexts() -> None:
    """Non-``.py`` context entries are silently dropped."""
    info = {"contexts": {"1": ["setup.cfg::build", "tests/test_pkg.py::test_fn"]}}
    assert _entry_tests(info) == {"tests/test_pkg.py"}


def test_entry_tests_empty_contexts_returns_empty() -> None:
    """A file entry with no contexts contributes nothing to the result."""
    assert _entry_tests({"contexts": {}}) == set()


def test_entry_tests_missing_contexts_key_returns_empty() -> None:
    """An entry with no ``contexts`` key at all returns an empty set."""
    assert _entry_tests({}) == set()


def test_entry_tests_non_list_context_value_is_skipped() -> None:
    """A non-list ``contexts`` value degrades to an empty set, not a crash."""
    assert _entry_tests({"contexts": {"1": 5}}) == set()


# ---------------------------------------------------------------------------
# iter_export_files — streaming parse
# ---------------------------------------------------------------------------

MULTI_FILE_EXPORT: dict[str, object] = {
    "meta": {"version": 2.2, "timestamp": "2026-11-26T00:00:00"},
    "files": {
        "src/foo.py": {
            "contexts": {"1": ["tests/test_foo.py::test_one|run"], "5": [""]},
            "summary": {"num_statements": 12},
        },
        "src/bar.py": {
            "contexts": {
                "2": ["tests/test_bar.py::test_a|run", "tests/test_bar.py::test_b|run"]
            },
            "summary": {"num_statements": 345678},
        },
        "tests/test_foo.py": {"contexts": {"1": ["tests/test_foo.py::test_self|run"]}},
    },
    "totals": {"covered_lines": 123456789, "percent_covered": 87.654321},
}


def _write_export(tmp_path: Path, text: str) -> Path:
    """Write *text* as ``cov.json`` under *tmp_path* and return the path.

    Args:
        tmp_path: Path to write the coverage file.
        text: JSON-formatted coverage data to write.
    """
    cov = tmp_path / "cov.json"
    cov.write_text(text, encoding="utf-8")
    return cov


def test_iter_export_files_matches_json_loads(tmp_path: Path) -> None:
    """Streaming yields exactly the ``files`` entries a whole-document parse has."""
    cov = _write_export(tmp_path, json.dumps(MULTI_FILE_EXPORT))
    assert list(iter_export_files(cov)) == list(MULTI_FILE_EXPORT["files"].items())  # type: ignore[union-attr]


@pytest.mark.parametrize("chunk_size", [1, 3, 7, 64, 1000])
def test_iter_export_files_tiny_chunks_match(tmp_path: Path, chunk_size: int) -> None:
    """Chunk boundaries never corrupt a value — numbers split across reads included.

    The export carries floats and big ints outside ``files``; a number is
    the only JSON token that can be a valid prefix of a longer one, so tiny
    chunks are what exercise the accept-only-at-delimiter rule.

    Args:
        tmp_path: Pytest's per-test directory.
        chunk_size: Read-buffer size to force frequent refills.
    """
    cov = _write_export(tmp_path, json.dumps(MULTI_FILE_EXPORT))
    got = list(iter_export_files(cov, chunk_size=chunk_size))
    assert got == list(MULTI_FILE_EXPORT["files"].items())  # type: ignore[union-attr]


def test_iter_export_files_empty_object_yields_nothing(tmp_path: Path) -> None:
    """An empty top-level object streams zero entries without error."""
    cov = _write_export(tmp_path, "{}")
    assert list(iter_export_files(cov)) == []


def test_iter_export_files_buffer_stays_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read buffer is compacted as entries are consumed, not grown to the file.

    A complete value is accepted when a delimiter follows it, so the
    buffer only ever holds the current value plus one chunk. Without that
    rule (e.g. ``:`` missing from the delimiter set, so a key string is
    never accepted until EOF) the buffer refills to the whole file and
    peak memory scales with the export again — this test pins the bound.
    """
    export = {
        "files": {
            f"src/m{i}.py": {
                "contexts": {str(n): ["t.py::test_x|run"] for n in range(50)}
            }
            for i in range(500)
        }
    }
    text = json.dumps(export)
    cov = _write_export(tmp_path, text)
    peak = {"chars": 0}
    orig_fill = _StreamReader._fill

    def spy_fill(self: _StreamReader) -> bool:
        result = orig_fill(self)
        peak["chars"] = max(peak["chars"], len(self._buf))
        return result

    monkeypatch.setattr(_StreamReader, "_fill", spy_fill)
    entries = list(iter_export_files(cov, chunk_size=1024))
    assert len(entries) == 500
    assert peak["chars"] < 8 * 1024
    assert peak["chars"] < len(text) // 4


def test_iter_export_files_missing_raises_export_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A path that does not exist raises ``ExportUnavailableError`` with a warning."""
    with caplog.at_level(logging.WARNING), pytest.raises(ExportUnavailableError):
        list(iter_export_files(tmp_path / "nonexistent.json"))
    assert "not found" in caplog.text


def test_iter_export_files_truncated_json_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A half-written export raises ``ExportUnavailableError`` with a parse warning."""
    text = json.dumps(MULTI_FILE_EXPORT)
    cov = _write_export(tmp_path, text[: len(text) // 2])
    with caplog.at_level(logging.WARNING), pytest.raises(ExportUnavailableError):
        list(iter_export_files(cov))
    assert "could not parse" in caplog.text


def test_iter_export_files_non_dict_top_level_raises(tmp_path: Path) -> None:
    """Valid JSON that is not an object (a list) is rejected as unusable."""
    cov = _write_export(tmp_path, "[1, 2]")
    with pytest.raises(ExportUnavailableError):
        list(iter_export_files(cov))


def test_iter_export_files_files_member_not_an_object_raises(tmp_path: Path) -> None:
    """A ``files`` member that is not an object is rejected as malformed."""
    cov = _write_export(tmp_path, json.dumps({"files": None}))
    with pytest.raises(ExportUnavailableError):
        list(iter_export_files(cov))


def test_iter_export_files_oversized_export_declines_with_named_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An export too large for available memory is declined, naming both figures.

    The pre-flight check compares file size times ``PARSE_RSS_FACTOR``
    against available memory; the warning must name the export size and
    the available memory so the skip is diagnosable instead of a silent
    signal death.
    """
    monkeypatch.setattr("forge.smart_test.coverage.available_memory_bytes", lambda: 8)
    cov = _write_export(tmp_path, json.dumps(MULTI_FILE_EXPORT))
    with (
        pytest.raises(ExportUnavailableError, match="too large"),
        caplog.at_level(logging.WARNING),
    ):
        list(iter_export_files(cov))
    assert "memory is available" in caplog.text
    assert "skipping the coverage-backed step" in caplog.text


def test_iter_export_files_deeply_nested_json_degrades_instead_of_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """JSON nested past the interpreter's recursion limit degrades, never raises.

    ``raw_decode`` blows its recursion limit on deep nesting and raises
    ``RecursionError`` — a ``RuntimeError``, not a ``ValueError``, so it
    needs its own ``except`` arm in the loader. Both consumers promise to
    warn and continue rather than raise on any malformed export.
    """
    deep = _write_export(tmp_path, "[" * 100_000 + "]" * 100_000)
    with caplog.at_level(logging.WARNING), pytest.raises(ExportUnavailableError):
        list(iter_export_files(deep))
    assert "could not parse" in caplog.text


def test_available_memory_bytes_is_none_or_positive() -> None:
    """The helper returns ``None`` when nothing is measurable, else a positive int."""
    available = available_memory_bytes()
    assert available is None or available > 0


# ---------------------------------------------------------------------------
# tests_covering — file I/O + dispatch
# ---------------------------------------------------------------------------


def test_tests_covering_json_dispatch(tmp_path: Path) -> None:
    """A ``.json`` file is streamed, parsed, and covering tests returned."""
    data = {
        "files": {"src/pkg.py": {"contexts": {"1": ["tests/test_pkg.py::test_fn"]}}}
    }
    cov = _write_export(tmp_path, json.dumps(data))
    result = _tests_covering(cov, {"src/pkg.py"})
    assert "tests/test_pkg.py" in result


def test_tests_covering_ignores_unchanged_files(tmp_path: Path) -> None:
    """File entries not in the ``changed`` set are skipped during the stream."""
    data = {
        "files": {
            "src/unchanged.py": {
                "contexts": {"1": ["tests/test_unchanged.py::test_x"]}
            },
            "src/changed.py": {"contexts": {"1": ["tests/test_changed.py::test_y"]}},
        }
    }
    cov = _write_export(tmp_path, json.dumps(data))
    assert _tests_covering(cov, {"src/changed.py"}) == {"tests/test_changed.py"}


def test_tests_covering_missing_file_returns_empty(tmp_path: Path) -> None:
    """A path that does not exist on disk returns an empty set without raising."""
    result = _tests_covering(tmp_path / "nonexistent.json", {"src/foo.py"})
    assert result == set()


def test_tests_covering_malformed_json_returns_empty(tmp_path: Path) -> None:
    """A file that cannot be parsed as JSON returns an empty set without raising."""
    bad = _write_export(tmp_path, "not valid json{{{")
    assert _tests_covering(bad, {"src/foo.py"}) == set()


def test_tests_covering_non_dict_json_returns_empty(tmp_path: Path) -> None:
    """Valid JSON that parses to a list, not an object, returns an empty set.

    A ``coverage json`` export is always an object; a list-shaped file is
    the wrong artifact entirely (not a coverage export at all), and the
    streaming loader must reject it the same as a malformed one.
    """
    cov = _write_export(tmp_path, json.dumps([1, 2]))
    assert _tests_covering(cov, {"src/foo.py"}) == set()


def test_tests_covering_oversized_export_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An export too large for available memory degrades to an empty set."""
    monkeypatch.setattr("forge.smart_test.coverage.available_memory_bytes", lambda: 8)
    cov = _write_export(tmp_path, json.dumps(MULTI_FILE_EXPORT))
    assert _tests_covering(cov, {"src/foo.py"}) == set()


def test_tests_covering_deeply_nested_json_degrades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Deeply nested JSON degrades to an empty set with a warning, never raises."""
    deep = _write_export(tmp_path, "[" * 100_000 + "]" * 100_000)
    with caplog.at_level(logging.WARNING):
        assert _tests_covering(deep, {"src/foo.py"}) == set()
    assert "could not parse" in caplog.text
