"""Tests for ``forge.smart_test.coverage`` — coverage-validated test selection."""

# MOCKING STRATEGY: ``_context_to_test`` and ``_from_json`` are pure-logic
# functions tested directly with in-memory dicts — no file I/O, no mocking.
# ``tests_covering`` is tested against real JSON files (written to ``tmp_path``)
# for the happy-path, missing-file, and malformed-file edge cases.

from __future__ import annotations

import json
from typing import TYPE_CHECKING

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
# _from_json — pure logic on a parsed dict
# ---------------------------------------------------------------------------


def test_from_json_returns_covering_tests() -> None:
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
    result = _from_json(data, {"src/foo.py"})
    assert result == {"tests/test_foo.py"}


def test_from_json_ignores_unchanged_files() -> None:
    """File entries not in the ``changed`` set are silently skipped."""
    data = {
        "files": {
            "src/unchanged.py": {"contexts": {"1": ["tests/test_unchanged.py::test_x"]}}
        }
    }
    result = _from_json(data, {"src/other.py"})
    assert result == set()


def test_from_json_filters_non_py_contexts() -> None:
    """Non-``.py`` context entries are silently dropped."""
    data = {
        "files": {
            "src/pkg.py": {
                "contexts": {"1": ["setup.cfg::build", "tests/test_pkg.py::test_fn"]}
            }
        }
    }
    result = _from_json(data, {"src/pkg.py"})
    assert result == {"tests/test_pkg.py"}


def test_from_json_empty_contexts_returns_empty() -> None:
    """A file entry with no contexts contributes nothing to the result."""
    data = {"files": {"src/pkg.py": {"contexts": {}}}}
    result = _from_json(data, {"src/pkg.py"})
    assert result == set()


def test_from_json_missing_files_key_returns_empty() -> None:
    """A JSON export with no ``files`` key returns an empty set."""
    result = _from_json({}, {"src/foo.py"})
    assert result == set()


# ---------------------------------------------------------------------------
# tests_covering — file I/O + dispatch
# ---------------------------------------------------------------------------


def test_tests_covering_json_dispatch(tmp_path: Path) -> None:
    """A ``.json`` file is read, parsed, and covering tests returned."""
    data = {
        "files": {"src/pkg.py": {"contexts": {"1": ["tests/test_pkg.py::test_fn"]}}}
    }
    cov = tmp_path / "cov.json"
    cov.write_text(json.dumps(data), encoding="utf-8")
    result = _tests_covering(cov, {"src/pkg.py"})
    assert "tests/test_pkg.py" in result


def test_tests_covering_missing_file_returns_empty(tmp_path: Path) -> None:
    """A path that does not exist on disk returns an empty set without raising."""
    missing = tmp_path / "nonexistent.json"
    result = _tests_covering(missing, {"src/foo.py"})
    assert result == set()


def test_tests_covering_malformed_json_returns_empty(tmp_path: Path) -> None:
    """A file that cannot be parsed as JSON returns an empty set without raising."""
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json{{{", encoding="utf-8")
    result = _tests_covering(bad, {"src/foo.py"})
    assert result == set()
