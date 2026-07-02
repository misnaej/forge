"""Tests for the verify-forge-docstrings CLI public API.

# MOCKING STRATEGY (for the scope-filter tests added for issue #83):
# ``main()`` obtains its file lists via ``get_tracked_files`` / ``get_modified_files``
# (both patched at ``forge.verify_docstrings.*``), scopes them via
# ``resolve_tool_roots`` (patched to return controlled roots), and drops
# repo-wide excludes via ``load_config`` (patched to return a ForgeConfig with
# a controlled ``exclude`` list). ``verify_file`` is patched to capture which
# absolute paths it is called with and to return ``[]`` (no issues). Real files
# are created under ``tmp_path`` for the survivors so they pass the
# ``full_path.exists()`` guard inside ``main()``. ``monkeypatch.chdir(tmp_path)``
# ensures ``repo_root = Path.cwd()`` resolves to ``tmp_path`` for the entire
# execution of ``main()``.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from forge.config import ForgeConfig
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


def test_scope_all_uses_tracked_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--scope all` selects files via get_tracked_files, not the diff."""
    used: list[str] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "forge.verify_docstrings.get_tracked_files",
        lambda: used.append("all") or [],
    )
    monkeypatch.setattr(
        "forge.verify_docstrings.get_modified_files",
        lambda: used.append("diff") or [],
    )
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", "--scope", "all"])
    assert main() == 0
    assert used == ["all"]


def test_scope_diff_uses_modified_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--scope diff` selects files via get_modified_files."""
    used: list[str] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "forge.verify_docstrings.get_tracked_files",
        lambda: used.append("all") or [],
    )
    monkeypatch.setattr(
        "forge.verify_docstrings.get_modified_files",
        lambda: used.append("diff") or [],
    )
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", "--scope", "diff"])
    assert main() == 0
    assert used == ["diff"]


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


# ---------------------------------------------------------------------------
# scope-all / scope-diff filtering (issue #83 — root + exclude pipeline)
# ---------------------------------------------------------------------------


def test_scope_all_excludes_files_outside_declared_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope=all restricts files to declared source/test roots only.

    SCENARIO: tracked files include one file inside src/ and one outside any
        declared root (outside/).
    MOCK SETUP: get_tracked_files returns both; resolve_tool_roots returns
        ["src"]; load_config returns an empty exclude list. Only src/a.py
        exists on disk so it can pass the full_path.exists() guard.
    EXPECTED BEHAVIOR: verify_file is called for src/a.py and is NOT called
        for outside/x.py, which is dropped by filter_under_roots.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "outside").mkdir()
    src_file = tmp_path / "src" / "a.py"
    src_file.write_text("# stub")
    (tmp_path / "outside" / "x.py").write_text("# stub")

    called_with: list[Path] = []

    def _fake_verify(filepath: Path) -> list:
        called_with.append(filepath)
        return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "forge.verify_docstrings.get_tracked_files",
        lambda: ["src/a.py", "outside/x.py"],
    )
    monkeypatch.setattr(
        "forge.verify_docstrings.resolve_tool_roots", lambda *_a, **_kw: ["src"]
    )
    monkeypatch.setattr(
        "forge.verify_docstrings.load_config", lambda _: ForgeConfig(exclude=[])
    )
    monkeypatch.setattr("forge.verify_docstrings.verify_file", _fake_verify)
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", "--scope", "all"])

    assert main() == 0
    called_names = {p.name for p in called_with}
    assert "a.py" in called_names
    assert "x.py" not in called_names


def test_scope_all_respects_exclude_glob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope=all drops files matching [tool.forge].exclude globs.

    SCENARIO: two files both live under src/ (inside roots) but one matches
        the repo-wide exclude glob ``*.gen.py``.
    MOCK SETUP: get_tracked_files returns both files; resolve_tool_roots
        returns ["src"]; load_config exclude=["*.gen.py"]. BOTH files exist
        on disk so the main() exists() guard cannot mask a missing filter —
        only filter_excluded may drop src/gen.py.
    EXPECTED BEHAVIOR: verify_file is called for src/a.py only; src/mod.gen.py
        is dropped by filter_excluded.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("# stub")
    (tmp_path / "src" / "mod.gen.py").write_text("# stub")

    called_with: list[Path] = []

    def _fake_verify(filepath: Path) -> list:
        called_with.append(filepath)
        return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "forge.verify_docstrings.get_tracked_files",
        lambda: ["src/a.py", "src/mod.gen.py"],
    )
    monkeypatch.setattr(
        "forge.verify_docstrings.resolve_tool_roots", lambda *_a, **_kw: ["src"]
    )
    monkeypatch.setattr(
        "forge.verify_docstrings.load_config",
        lambda _: ForgeConfig(exclude=["*.gen.py"]),
    )
    monkeypatch.setattr("forge.verify_docstrings.verify_file", _fake_verify)
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", "--scope", "all"])

    assert main() == 0
    called_names = {p.name for p in called_with}
    assert "a.py" in called_names
    assert "mod.gen.py" not in called_names


def test_scope_diff_respects_exclude_glob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope=diff drops files matching [tool.forge].exclude globs.

    SCENARIO: two modified files, one matches the exclude glob ``*.gen.py``.
    MOCK SETUP: get_modified_files returns both files; load_config
        exclude=["*.gen.py"]. BOTH files exist on disk so the main() exists()
        guard cannot mask a missing filter — only filter_excluded may drop
        src/auto.gen.py.
    EXPECTED BEHAVIOR: verify_file is called for src/b.py only; src/auto.gen.py
        is dropped by filter_excluded.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("# stub")
    (tmp_path / "src" / "auto.gen.py").write_text("# stub")

    called_with: list[Path] = []

    def _fake_verify(filepath: Path) -> list:
        called_with.append(filepath)
        return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "forge.verify_docstrings.get_modified_files",
        lambda: ["src/b.py", "src/auto.gen.py"],
    )
    monkeypatch.setattr(
        "forge.verify_docstrings.load_config",
        lambda _: ForgeConfig(exclude=["*.gen.py"]),
    )
    monkeypatch.setattr("forge.verify_docstrings.verify_file", _fake_verify)
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", "--scope", "diff"])

    assert main() == 0
    called_names = {p.name for p in called_with}
    assert "b.py" in called_names
    assert "auto.gen.py" not in called_names


def test_scope_all_empty_after_root_filter_returns_zero_and_no_verify_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope=all with all files outside roots yields exit 0 and no verify calls.

    SCENARIO: tracked files exist but all live outside the declared root.
    MOCK SETUP: get_tracked_files returns a file under outside/ that exists on
        disk (so the main() exists() guard cannot mask a missing filter);
        resolve_tool_roots returns ["src"] so filter_under_roots drops it.
    EXPECTED BEHAVIOR: py_files is empty, verify_file is never called, main()
        returns 0.
    """
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "x.py").write_text("# stub")

    called_with: list[Path] = []

    def _fake_verify(filepath: Path) -> list:
        called_with.append(filepath)
        return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "forge.verify_docstrings.get_tracked_files", lambda: ["outside/x.py"]
    )
    monkeypatch.setattr(
        "forge.verify_docstrings.resolve_tool_roots", lambda *_a, **_kw: ["src"]
    )
    monkeypatch.setattr(
        "forge.verify_docstrings.load_config", lambda _: ForgeConfig(exclude=[])
    )
    monkeypatch.setattr("forge.verify_docstrings.verify_file", _fake_verify)
    monkeypatch.setattr(sys, "argv", ["verify-forge-docstrings", "--scope", "all"])

    assert main() == 0
    assert called_with == []
