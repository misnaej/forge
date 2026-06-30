"""Tests for ``forge.smart_test.coverage`` — coverage-validated test selection."""

# MOCKING STRATEGY: ``_context_to_test`` and ``_from_json`` are pure-logic or
# read-only functions tested directly with no mocking.  ``tests_covering`` is
# tested against real JSON files (written to ``tmp_path``) for the .json
# dispatch path and a missing-file edge case.  For the SQLite / coverage-lib
# absent path, ``sys.modules["coverage"]`` is set to ``None`` via monkeypatch
# so ``from coverage import CoverageData`` inside ``_from_sqlite`` raises
# ``ImportError`` without requiring the library to be absent in the environment.

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from forge.smart_test.coverage import _context_to_test, _from_json
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
# _from_json — reads a JSON coverage export
# ---------------------------------------------------------------------------


def test_from_json_returns_covering_tests(tmp_path: Path) -> None:
    """Changed file's contexts are translated to test-file paths."""
    data = {
        "files": {
            "src/foo.py": {
                "contexts": {
                    "1": ["tests/test_foo.py::test_one"],
                    "5": ["tests/test_foo.py::test_two"],
                }
            }
        }
    }
    cov = tmp_path / "cov.json"
    cov.write_text(json.dumps(data), encoding="utf-8")
    result = _from_json(cov, {"src/foo.py"})
    assert result == {"tests/test_foo.py"}


def test_from_json_ignores_unchanged_files(tmp_path: Path) -> None:
    """File entries not in the ``changed`` set are silently skipped."""
    data = {
        "files": {
            "src/unchanged.py": {"contexts": {"1": ["tests/test_unchanged.py::test_x"]}}
        }
    }
    cov = tmp_path / "cov.json"
    cov.write_text(json.dumps(data), encoding="utf-8")
    result = _from_json(cov, {"src/other.py"})
    assert result == set()


def test_from_json_filters_non_py_contexts(tmp_path: Path) -> None:
    """Non-``.py`` context entries are silently dropped."""
    data = {
        "files": {
            "src/pkg.py": {
                "contexts": {"1": ["setup.cfg::build", "tests/test_pkg.py::test_fn"]}
            }
        }
    }
    cov = tmp_path / "cov.json"
    cov.write_text(json.dumps(data), encoding="utf-8")
    result = _from_json(cov, {"src/pkg.py"})
    assert result == {"tests/test_pkg.py"}


def test_from_json_empty_contexts_returns_empty(tmp_path: Path) -> None:
    """A file entry with no contexts contributes nothing to the result."""
    data = {"files": {"src/pkg.py": {"contexts": {}}}}
    cov = tmp_path / "cov.json"
    cov.write_text(json.dumps(data), encoding="utf-8")
    result = _from_json(cov, {"src/pkg.py"})
    assert result == set()


def test_from_json_missing_files_key_returns_empty(tmp_path: Path) -> None:
    """A JSON export with no ``files`` key returns an empty set."""
    cov = tmp_path / "cov.json"
    cov.write_text(json.dumps({}), encoding="utf-8")
    result = _from_json(cov, {"src/foo.py"})
    assert result == set()


# ---------------------------------------------------------------------------
# tests_covering — dispatch layer
# ---------------------------------------------------------------------------


def test_tests_covering_json_dispatch(tmp_path: Path) -> None:
    """A ``.json`` path routes to ``_from_json`` and returns covering tests."""
    data = {
        "files": {"src/pkg.py": {"contexts": {"1": ["tests/test_pkg.py::test_fn"]}}}
    }
    cov = tmp_path / "cov.json"
    cov.write_text(json.dumps(data), encoding="utf-8")
    result = _tests_covering(cov, {"src/pkg.py"}, tmp_path)
    assert "tests/test_pkg.py" in result


def test_tests_covering_missing_file_returns_empty(tmp_path: Path) -> None:
    """A path that does not exist on disk returns an empty set without raising."""
    missing = tmp_path / "nonexistent.json"
    result = _tests_covering(missing, {"src/foo.py"}, tmp_path)
    assert result == set()


@pytest.mark.parametrize("suffix", [".coverage", ".db"])
def test_tests_covering_sqlite_coverage_lib_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    """When the ``coverage`` library is absent, a non-JSON path returns empty set.

    SCENARIO: ``--coverage-db`` points at a SQLite-style DB; the ``coverage``
        Python library is not installed.
    MOCK SETUP: ``sys.modules["coverage"]`` is set to ``None`` so
        ``from coverage import CoverageData`` inside ``_from_sqlite`` raises
        ``ImportError``; the real file is a dummy byte sequence.
    EXPECTED BEHAVIOR: ``tests_covering`` returns ``set()`` and does not raise.

    Args:
        suffix: File extension for the fake DB path (``".coverage"`` or ``".db"``).
    """
    db = tmp_path / f"data{suffix}"
    db.write_bytes(b"SQLite format 3\x00fake")
    # Setting the entry to None causes ``from coverage import …`` to raise
    # ImportError, simulating the library being absent.
    monkeypatch.setitem(sys.modules, "coverage", None)  # type: ignore[arg-type]

    result = _tests_covering(db, {"src/foo.py"}, tmp_path)
    assert result == set()
