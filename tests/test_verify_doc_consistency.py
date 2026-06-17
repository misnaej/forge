"""Tests for ``forge.verify_doc_consistency``.

# MOCKING STRATEGY: each test builds a throwaway repo tree under tmp_path
# (pyproject, docs/cli-reference.md, agents/*.md, FOUNDATION.md) and runs
# the real check functions against it. ``main`` tests pin get_repo_root to
# tmp_path and patch sys.argv so argparse does not see pytest's argv.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import verify_doc_consistency as vdc


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write(path: Path, text: str) -> None:
    """Write *text* to *path*, creating parent directories as needed.

    Args:
        path: Destination file path.
        text: Contents to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_cli_coverage_skips_without_inputs(tmp_path: Path) -> None:
    """No pyproject or no cli-reference → nothing to check."""
    assert vdc._check_cli_coverage(tmp_path) == []


def test_cli_coverage_clean_when_all_documented(tmp_path: Path) -> None:
    """Every `[project.scripts]` name present in the reference → no findings."""
    _write(tmp_path / "pyproject.toml", '[project.scripts]\nfoo-cli = "x:main"\n')
    _write(tmp_path / "docs" / "cli-reference.md", "# CLIs\n- foo-cli does things\n")
    assert vdc._check_cli_coverage(tmp_path) == []


def test_cli_coverage_flags_missing(tmp_path: Path) -> None:
    """A script absent from the reference doc is reported by name."""
    _write(
        tmp_path / "pyproject.toml",
        '[project.scripts]\nfoo-cli = "x:main"\nbar-cli = "y:main"\n',
    )
    _write(tmp_path / "docs" / "cli-reference.md", "# CLIs\n- foo-cli only\n")
    findings = vdc._check_cli_coverage(tmp_path)
    assert len(findings) == 1
    assert "bar-cli" in findings[0]


def test_cli_coverage_malformed_pyproject_skips(tmp_path: Path) -> None:
    """A malformed pyproject yields no findings rather than raising."""
    _write(tmp_path / "pyproject.toml", "this is = = not [[[ toml")
    _write(tmp_path / "docs" / "cli-reference.md", "# CLIs\n")
    assert vdc._check_cli_coverage(tmp_path) == []


def test_main_returns_zero_when_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 0 on an empty repo — the check skips, nothing drifts.

    MOCK SETUP: get_repo_root pinned to an empty tmp_path; argv patched so
    argparse does not consume pytest's arguments.
    """
    monkeypatch.setattr(vdc, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vdc.sys, "argv", ["verify-forge-doc-consistency"])
    assert vdc.main() == 0


def test_main_returns_one_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 1 when CLI coverage drifts.

    MOCK SETUP: a repo with a [project.scripts] CLI absent from the
    reference doc; get_repo_root pinned to it and argv patched.
    """
    _write(tmp_path / "pyproject.toml", '[project.scripts]\nundocumented = "x:main"\n')
    _write(tmp_path / "docs" / "cli-reference.md", "# CLIs\n")
    monkeypatch.setattr(vdc, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vdc.sys, "argv", ["verify-forge-doc-consistency"])
    assert vdc.main() == 1
