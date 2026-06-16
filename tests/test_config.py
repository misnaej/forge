"""Tests for forge.config."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge.config import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_DEV_BRANCH,
    ForgeConfig,
    load_config,
    read_pyproject_raw,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_default_is_single_track() -> None:
    """Default ForgeConfig has base == dev so dual_track is False.

    Back-compat guarantee for consumer repos without ``[tool.forge]``.
    """
    cfg = ForgeConfig()
    assert cfg.base_branch == DEFAULT_BASE_BRANCH
    assert cfg.dev_branch == DEFAULT_DEV_BRANCH
    assert cfg.dual_track is False


def test_dual_track_flag_flips_when_branches_differ() -> None:
    """dual_track flips True the moment dev_branch differs from base_branch."""
    cfg = ForgeConfig(base_branch="main", dev_branch="dev")
    assert cfg.dual_track is True


def test_load_config_missing_pyproject_returns_defaults(tmp_path: Path) -> None:
    """No ``pyproject.toml`` → single-track defaults."""
    cfg = load_config(tmp_path)
    assert cfg == ForgeConfig()


def test_load_config_pyproject_without_tool_forge(tmp_path: Path) -> None:
    """``pyproject.toml`` exists but lacks ``[tool.forge]`` → defaults."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "foo"\nversion = "0.1.0"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg == ForgeConfig()


def test_load_config_reads_dual_track_block(tmp_path: Path) -> None:
    """``[tool.forge]`` with explicit branch names is parsed correctly."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\ndev_branch = "dev"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.base_branch == "main"
    assert cfg.dev_branch == "dev"
    assert cfg.dual_track is True


def test_load_config_custom_branch_names(tmp_path: Path) -> None:
    """Custom branch names (e.g. ``master`` / ``next``) round-trip."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "master"\ndev_branch = "next"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.base_branch == "master"
    assert cfg.dev_branch == "next"
    assert cfg.dual_track is True


def test_load_config_malformed_toml_falls_back_to_defaults(tmp_path: Path) -> None:
    """A broken ``pyproject.toml`` degrades to defaults — never raises.

    Config reads happen in hot paths (hooks, agents, pre-commit) so a
    parse failure must not block the workflow.
    """
    (tmp_path / "pyproject.toml").write_text("not [ valid toml @@@")
    cfg = load_config(tmp_path)
    assert cfg == ForgeConfig()


def test_load_config_partial_block_uses_defaults(tmp_path: Path) -> None:
    """``[tool.forge]`` with only one of the two keys → other defaults."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\ndev_branch = "trunk"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.base_branch == DEFAULT_BASE_BRANCH
    assert cfg.dev_branch == "trunk"


def test_read_pyproject_raw_returns_full_dict(tmp_path: Path) -> None:
    """The shared raw reader returns the whole parsed TOML tree."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\n\n[tool.interrogate]\nfail-under = 90\n'
    )
    data = read_pyproject_raw(tmp_path)
    assert data["tool"]["forge"]["base_branch"] == "main"
    assert data["tool"]["interrogate"]["fail-under"] == 90


def test_read_pyproject_raw_empty_on_missing_or_malformed(tmp_path: Path) -> None:
    """Missing file and malformed TOML both degrade to ``{}`` (never raise)."""
    assert read_pyproject_raw(tmp_path) == {}
    (tmp_path / "pyproject.toml").write_text("not [ valid toml @@@")
    assert read_pyproject_raw(tmp_path) == {}
