"""Tests for the verify-forge-docstrings CLI public API."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from forge.verify_docstrings import main, verify_file


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


CLEAN_SOURCE = """\
'''Module docstring.'''


def add(a: int, b: int) -> int:
    '''Add two numbers.

    Args:
        a: First number.
        b: Second number.

    Returns:
        The sum.
    '''
    return a + b
"""

UNDOCUMENTED_PARAM_SOURCE = """\
'''Module docstring.'''


def add(a: int, b: int) -> int:
    '''Add two numbers.

    Args:
        a: First number.

    Returns:
        The sum.
    '''
    return a + b
"""

NO_MODULE_DOCSTRING_SOURCE = """\
def foo() -> None:
    '''Do nothing.'''
"""

SYNTAX_ERROR_SOURCE = "def broken(:\n    pass\n"


def test_verify_file_clean_file_returns_no_issues(tmp_path: Path) -> None:
    """A fully documented file yields no issues."""
    target = tmp_path / "clean.py"
    target.write_text(CLEAN_SOURCE)
    assert verify_file(target) == []


def test_verify_file_undocumented_param_is_error(tmp_path: Path) -> None:
    """A signature parameter missing from the docstring is an error issue."""
    target = tmp_path / "badargs.py"
    target.write_text(UNDOCUMENTED_PARAM_SOURCE)
    issues = verify_file(target)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].function == "add"
    assert "not documented: b" in issues[0].description


def test_verify_file_missing_module_docstring_is_warning(tmp_path: Path) -> None:
    """A file with no module docstring yields a warning issue."""
    target = tmp_path / "nomoddoc.py"
    target.write_text(NO_MODULE_DOCSTRING_SOURCE)
    issues = verify_file(target)
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].function == "<module>"
    assert issues[0].description == "Missing module docstring"


def test_verify_file_syntax_error_returns_parse_issue(tmp_path: Path) -> None:
    """An unparseable file yields a single <parse> error issue."""
    target = tmp_path / "syntaxerr.py"
    target.write_text(SYNTAX_ERROR_SOURCE)
    issues = verify_file(target)
    assert len(issues) == 1
    assert issues[0].function == "<parse>"
    assert issues[0].severity == "error"
    assert issues[0].description.startswith("Syntax error")


def test_verify_file_unreadable_file_returns_parse_issue(tmp_path: Path) -> None:
    """A nonexistent path yields a single <parse> error issue."""
    issues = verify_file(tmp_path / "does_not_exist.py")
    assert len(issues) == 1
    assert issues[0].function == "<parse>"
    assert issues[0].severity == "error"
    assert issues[0].description.startswith("Error reading file")


def test_main_returns_zero_for_clean_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit code is 0 when the target file has no error issues."""
    target = tmp_path / "clean.py"
    target.write_text(CLEAN_SOURCE)
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", str(target)])
    assert main() == 0


def test_main_returns_one_when_target_has_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit code is 1 when the target file has error-severity issues."""
    target = tmp_path / "badargs.py"
    target.write_text(UNDOCUMENTED_PARAM_SOURCE)
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", str(target)])
    assert main() == 1


def test_main_logs_error_issues_under_errors_heading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Error issues are logged at ERROR level under an ERRORS heading."""
    target = tmp_path / "badargs.py"
    target.write_text(UNDOCUMENTED_PARAM_SOURCE)
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", str(target)])
    with caplog.at_level(logging.INFO):
        main()
    error_messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.ERROR
    ]
    assert any("ERRORS (1):" in message for message in error_messages)
    assert any("not documented: b" in message for message in error_messages)


def test_main_returns_one_for_nonexistent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exit code is 1 and an error is logged when the target is missing."""
    missing = tmp_path / "does_not_exist.py"
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", str(missing)])
    with caplog.at_level(logging.ERROR):
        result = main()
    assert result == 1
    assert any("does not exist" in record.getMessage() for record in caplog.records)
