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


def _make_agents(tmp_path: Path, count: int) -> None:
    """Create *count* agent files plus an excluded ``_TEMPLATE.md``.

    Args:
        tmp_path: Repo root to populate.
        count: Number of real ``agents/*.md`` files to create.
    """
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (agents / f"agent{i}.md").write_text("x", encoding="utf-8")
    (agents / "_TEMPLATE.md").write_text("template", encoding="utf-8")


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


def test_agent_count_skips_without_inputs(tmp_path: Path) -> None:
    """No agents/ directory or no FOUNDATION.md → nothing to check."""
    assert vdc._check_agent_count(tmp_path) == []


def test_agent_count_clean_on_word_match(tmp_path: Path) -> None:
    """A spelled-out count matching the file count passes."""
    _make_agents(tmp_path, 3)
    _write(tmp_path / "FOUNDATION.md", "The three foundation agents are: a, b, c.")
    assert vdc._check_agent_count(tmp_path) == []


def test_agent_count_clean_on_digit_match_excluding_template(tmp_path: Path) -> None:
    """A digit count matching passes, and ``_TEMPLATE.md`` is not counted."""
    _make_agents(tmp_path, 12)
    _write(tmp_path / "FOUNDATION.md", "forge ships 12 foundation agents total.")
    assert vdc._check_agent_count(tmp_path) == []


def test_agent_count_flags_mismatch(tmp_path: Path) -> None:
    """A claim that disagrees with the actual file count is reported."""
    _make_agents(tmp_path, 5)
    _write(tmp_path / "FOUNDATION.md", "the ten foundation agents are listed below")
    findings = vdc._check_agent_count(tmp_path)
    assert len(findings) == 1
    assert "10" in findings[0]
    assert "5" in findings[0]


def test_main_returns_zero_when_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 0 on an empty repo — every check skips, nothing drifts.

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
    """main() exits 1 when a check reports drift.

    MOCK SETUP: a repo whose FOUNDATION claims nine agents but holds two;
    get_repo_root pinned to it and argv patched.
    """
    _make_agents(tmp_path, 2)
    _write(tmp_path / "FOUNDATION.md", "the nine foundation agents are")
    monkeypatch.setattr(vdc, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vdc.sys, "argv", ["verify-forge-doc-consistency"])
    assert vdc.main() == 1
