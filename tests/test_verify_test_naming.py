"""Tests for forge.verify_test_naming.

Justification: this CLI catches test
conventions that ruff cannot — error-test naming patterns, helper
function placement (conftest vs in-file), parametrize ID style,
descriptive fixture names, module-level constant casing, duplicate
filename detection. These rules are semantic, not syntactic, so they
live here rather than in ruff config.

Each test below either:
- Confirms the rule fires on a deliberately-bad fixture, or
- Confirms ruff would NOT have caught the same violation (proving
  non-redundancy).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import verify_test_naming


if TYPE_CHECKING:
    from pathlib import Path


def _verify(tmp_path: Path, name: str, source: str) -> list[verify_test_naming.Issue]:
    """Write *source* as *name* under tmp_path and run verify_file.

    Args:
        tmp_path: Pytest tmp dir.
        name: Filename (e.g. ``"test_widget.py"``).
        source: File contents.

    Returns:
        List of issues reported by ``verify_file``.
    """
    path = tmp_path / name
    path.write_text(source)
    return verify_test_naming.verify_file(path)


def test_rule3_singular_raise_in_test_name_flagged(tmp_path: Path) -> None:
    """A test named ``test_X_raise`` → flagged (must be ``_raises``).

    Ruff has no equivalent — this is a test-naming convention.
    """
    src = """
def test_compute_negative_input_raise():
    pass
"""
    issues = _verify(tmp_path, "test_widget.py", src)
    assert any("_raises" in i.description for i in issues)


def test_rule4_helper_in_test_file_needs_underscore_prefix(tmp_path: Path) -> None:
    """Non-`_` helper next to tests → flagged (move to conftest or rename).

    Ruff has no equivalent — pytest convention, not syntax.
    """
    src = """
def build_widget():
    return object()

def test_widget_size():
    assert build_widget() is not None
"""
    issues = _verify(tmp_path, "test_widget.py", src)
    assert any(i.function == "build_widget" and "_" in i.description for i in issues)


def test_rule4_underscore_prefixed_helper_is_fine(tmp_path: Path) -> None:
    """Same helper with `_` prefix produces no Rule 4 issue."""
    src = """
def _build_widget():
    return object()

def test_widget_size():
    assert _build_widget() is not None
"""
    issues = _verify(tmp_path, "test_widget.py", src)
    assert not any(i.function == "_build_widget" for i in issues)


def test_rule7_generic_fixture_name_is_flagged(tmp_path: Path) -> None:
    """A fixture named ``data`` → flagged (must be descriptive).

    Ruff has no equivalent — semantic naming check.
    """
    src = """
import pytest

@pytest.fixture
def data():
    return {"a": 1}

def test_x(data):
    assert data["a"] == 1
"""
    issues = _verify(tmp_path, "test_widget.py", src)
    assert any(i.function == "data" for i in issues)


def test_rule8_parametrize_ids_must_be_snake_case(tmp_path: Path) -> None:
    """Parametrize IDs in CamelCase or with spaces → flagged.

    Ruff has no equivalent — the IDs are string literals inside a decorator.
    """
    src = """
import pytest

@pytest.mark.parametrize(
    "x",
    [1, 2],
    ids=["HappyPath", "sad path"],
)
def test_widget(x):
    assert x in {1, 2}
"""
    issues = _verify(tmp_path, "test_widget.py", src)
    assert len([i for i in issues if "parametrize" in i.description.lower()]) >= 1


def test_rule9_module_level_snake_case_constant_is_flagged(tmp_path: Path) -> None:
    """A module-level literal in snake_case → flagged (should be UPPERCASE).

    Ruff has no equivalent — N816 only catches mixed-case at module scope,
    not snake_case-vs-UPPER for constants.
    """
    src = """
default_size = 42

def test_widget():
    assert default_size == 42
"""
    issues = _verify(tmp_path, "test_widget.py", src)
    assert any(i.function == "default_size" for i in issues)


def test_rule9_uppercase_constant_passes(tmp_path: Path) -> None:
    """A properly UPPERCASE constant does not trigger Rule 9."""
    src = """
DEFAULT_SIZE = 42

def test_widget():
    assert DEFAULT_SIZE == 42
"""
    issues = _verify(tmp_path, "test_widget.py", src)
    assert not any(i.function == "DEFAULT_SIZE" for i in issues)


def test_rule5_duplicate_filenames_in_same_dir_flagged(tmp_path: Path) -> None:
    """Two test files with the same normalized name in one dir → flagged.

    Ruff has no equivalent — cross-file check.
    Implementation only flags duplicates within the same directory
    (different dirs are commonly intentional).
    """
    # Implementation normalizes via: strip 'test_' → lowercase → drop '_'.
    # So `test_my_widget.py` and `test_mywidget.py` collide on `mywidget`.
    a = tmp_path / "test_my_widget.py"
    b = tmp_path / "test_mywidget.py"
    a.write_text("def test_x():\n    assert True\n")
    b.write_text("def test_y():\n    assert True\n")
    issues = verify_test_naming._check_duplicate_file_names([a, b])
    assert any("duplicate" in i.description.lower() for i in issues)


def test_conftest_helper_without_underscore_is_allowed(tmp_path: Path) -> None:
    """Conftest helpers do not need a `_` prefix (Rule 4 exception)."""
    src = """
def build_widget():
    return object()
"""
    issues = _verify(tmp_path, "conftest.py", src)
    assert not any(i.function == "build_widget" for i in issues)
