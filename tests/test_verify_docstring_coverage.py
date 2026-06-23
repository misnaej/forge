"""Tests for ``forge.verify_docstring_coverage``."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from forge import verify_docstring_coverage


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_WELL_DOCUMENTED = textwrap.dedent(
    '''
    """Module-level docstring."""


    def well_documented() -> int:
        """Return seven."""
        return 7


    class Foo:
        """Class docstring."""

        def method(self) -> int:
            """Return zero."""
            return 0
    '''
).lstrip()


_PARTIALLY_DOCUMENTED = textwrap.dedent(
    '''
    """Module-level docstring."""


    def documented() -> int:
        """Return seven."""
        return 7


    def undocumented() -> int:
        return 0


    def also_undocumented() -> int:
        return -1
    '''
).lstrip()


def _write_pyproject(root: Path, *, fail_under: float, badge: bool = False) -> None:
    """Write a minimal ``pyproject.toml`` configuring the coverage gate.

    Args:
        root: Repository root to write into.
        fail_under: Coverage threshold for ``[tool.interrogate]``.
        badge: When True, opt into badge generation via
            ``[tool.forge.docstring_coverage].badge = true``.
    """
    badge_section = "[tool.forge.docstring_coverage]\nbadge = true\n\n" if badge else ""
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project]
            name = "demo"
            version = "0.0.0"

            {badge_section}[tool.interrogate]
            fail-under = {fail_under}
            """
        ).lstrip()
    )


def _write_src(root: Path, body: str, name: str = "demo.py") -> None:
    """Write a Python module under ``src/`` to drive the coverage check.

    Args:
        root: Repository root.
        body: Module body (already-dedented Python source).
        name: Module filename, default ``demo.py``.
    """
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / name).write_text(body)


def test_pass_when_coverage_meets_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coverage at or above ``fail-under`` returns exit 0 + writes log."""
    _write_pyproject(tmp_path, fail_under=90.0)
    _write_src(tmp_path, _WELL_DOCUMENTED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-docstring-coverage"])
    assert verify_docstring_coverage.main() == 0
    log = (tmp_path / "code_health" / "docstring_coverage.log").read_text()
    assert ">= fail-under" in log


def test_fail_when_coverage_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coverage below ``fail-under`` returns exit 1 + logs the gap."""
    _write_pyproject(tmp_path, fail_under=90.0)
    _write_src(tmp_path, _PARTIALLY_DOCUMENTED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-docstring-coverage"])
    assert verify_docstring_coverage.main() == 1
    log = (tmp_path / "code_health" / "docstring_coverage.log").read_text()
    assert "< fail-under" in log


def test_skip_when_no_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``pyproject.toml`` → skip (consumer has not opted in)."""
    _write_src(tmp_path, _WELL_DOCUMENTED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-docstring-coverage"])
    assert verify_docstring_coverage.main() == 0
    log = (tmp_path / "code_health" / "docstring_coverage.log").read_text()
    assert "skipped" in log


def test_skip_when_no_configured_paths_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pyproject.toml`` present but no configured scan root exists → skip."""
    _write_pyproject(tmp_path, fail_under=90.0)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-docstring-coverage"])
    assert verify_docstring_coverage.main() == 0
    log = (tmp_path / "code_health" / "docstring_coverage.log").read_text()
    assert "skipped" in log


def test_badge_written_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``badge = true`` writes ``.badges/DocstringCoverage.svg``."""
    _write_pyproject(tmp_path, fail_under=90.0, badge=True)
    _write_src(tmp_path, _WELL_DOCUMENTED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-docstring-coverage"])
    assert verify_docstring_coverage.main() == 0
    badge = tmp_path / ".badges" / "DocstringCoverage.svg"
    assert badge.is_file()
    assert badge.read_text().lstrip().startswith(
        "<?xml"
    ) or badge.read_text().lstrip().startswith("<svg")


def test_badge_omitted_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``badge = true`` the ``.badges/`` directory stays untouched."""
    _write_pyproject(tmp_path, fail_under=90.0)
    _write_src(tmp_path, _WELL_DOCUMENTED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-docstring-coverage"])
    assert verify_docstring_coverage.main() == 0
    assert not (tmp_path / ".badges").exists()


def test_missing_list_format_for_precommit_fixer_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MISSING: <path>:<line>:<name>`` lines emit per undocumented symbol.

    This format is the dispatch contract with ``forge:precommit-fixer``.
    Breaking it silently breaks the agent's ability to find the symbols
    needing docstrings.
    """
    _write_pyproject(tmp_path, fail_under=50.0)
    _write_src(tmp_path, _PARTIALLY_DOCUMENTED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-docstring-coverage"])
    verify_docstring_coverage.main()
    log = (tmp_path / "code_health" / "docstring_coverage.log").read_text()
    assert "## Missing docstrings (" in log
    assert "MISSING:" in log
    assert "undocumented" in log
    assert "also_undocumented" in log


def test_default_fail_under_matches_foundation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``fail-under`` in ``[tool.interrogate]`` → default 90 (FOUNDATION §8)."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\n\n[tool.interrogate]\n'
    )
    _write_src(tmp_path, _WELL_DOCUMENTED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-docstring-coverage"])
    assert verify_docstring_coverage.main() == 0
    log = (tmp_path / "code_health" / "docstring_coverage.log").read_text()
    assert "fail-under 90" in log


def _write_forge_toml(tmp_path: Path, body: str) -> None:
    """Write a ``[tool.forge]`` block to tmp_path's pyproject.toml.

    Args:
        tmp_path: Repo root to write into.
        body: TOML lines placed under ``[tool.forge]`` (may include subtables).
    """
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.forge]\n{body}\n", encoding="utf-8"
    )


def test_scan_paths_defaults_to_src_and_tests(tmp_path: Path) -> None:
    """No config → smart-detected ``src`` / ``tests`` roots (source + tests)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    result = verify_docstring_coverage._scan_paths(tmp_path)
    assert result == [
        str((tmp_path / "src").resolve()),
        str((tmp_path / "tests").resolve()),
    ]


def test_scan_paths_defaults_to_repo_layout(tmp_path: Path) -> None:
    """No per-tool paths → ``[tool.forge].source_dirs + test_dirs``."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "t").mkdir()
    _write_forge_toml(tmp_path, 'source_dirs = ["lib"]\ntest_dirs = ["t"]')
    result = verify_docstring_coverage._scan_paths(tmp_path)
    assert result == [
        str((tmp_path / "lib").resolve()),
        str((tmp_path / "t").resolve()),
    ]


def test_scan_paths_per_tool_override_wins(tmp_path: Path) -> None:
    """``[tool.forge.docstring_coverage].paths`` overrides the repo layout."""
    (tmp_path / "src").mkdir()
    (tmp_path / "only").mkdir()
    _write_forge_toml(
        tmp_path,
        'source_dirs = ["src"]\n[tool.forge.docstring_coverage]\npaths = ["only"]',
    )
    result = verify_docstring_coverage._scan_paths(tmp_path)
    assert result == [str((tmp_path / "only").resolve())]


def test_scan_paths_honors_configured_paths(tmp_path: Path) -> None:
    """``[tool.forge.docstring_coverage].paths`` overrides the default roots."""
    (tmp_path / "projects").mkdir()
    (tmp_path / "src").mkdir()
    _write_forge_toml(
        tmp_path, '[tool.forge.docstring_coverage]\npaths = ["projects", "src"]'
    )
    result = verify_docstring_coverage._scan_paths(tmp_path)
    assert result == [
        str((tmp_path / "projects").resolve()),
        str((tmp_path / "src").resolve()),
    ]


def test_scan_paths_rejects_traversal_outside_repo(tmp_path: Path) -> None:
    """A ``..`` path escaping the repo is dropped (path-traversal guard)."""
    (tmp_path / "src").mkdir()
    (tmp_path.parent / "secret").mkdir(exist_ok=True)
    _write_forge_toml(
        tmp_path,
        '[tool.forge.docstring_coverage]\npaths = ["../secret", "src"]',
    )
    result = verify_docstring_coverage._scan_paths(tmp_path)
    assert result == [str((tmp_path / "src").resolve())]


def test_scan_paths_rejects_absolute_path_outside_repo(tmp_path: Path) -> None:
    """An absolute path outside the repo is dropped (path-traversal guard)."""
    (tmp_path / "src").mkdir()
    _write_forge_toml(
        tmp_path, '[tool.forge.docstring_coverage]\npaths = ["/etc", "src"]'
    )
    result = verify_docstring_coverage._scan_paths(tmp_path)
    assert result == [str((tmp_path / "src").resolve())]


def test_scan_paths_empty_when_none_exist(tmp_path: Path) -> None:
    """Configured roots that don't exist → empty list (caller skips cleanly)."""
    _write_forge_toml(tmp_path, '[tool.forge.docstring_coverage]\npaths = ["nope"]')
    assert verify_docstring_coverage._scan_paths(tmp_path) == []
