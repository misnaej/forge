"""Static integrity tests for forge's own plugin/marketplace manifests + layout."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = REPO_ROOT / ".claude-plugin"


def test_plugin_json_present_and_well_formed() -> None:
    """`.claude-plugin/plugin.json` exists and parses, with required fields."""
    path = MANIFEST_DIR / "plugin.json"
    assert path.is_file()
    data = json.loads(path.read_text())
    for field in ("name", "version", "description", "author", "license"):
        assert field in data, f"plugin.json missing {field}"
    assert data["name"] == "forge"
    assert data["license"] == "MIT"


def test_plugin_json_inline_hooks() -> None:
    """plugin.json declares hooks inline (required by Claude Code 2.1.x)."""
    data = json.loads((MANIFEST_DIR / "plugin.json").read_text())
    assert "hooks" in data, (
        "plugin.json should declare hooks inline, not via hooks.json"
    )
    assert "PreToolUse" in data["hooks"]


def test_marketplace_json_present_and_well_formed() -> None:
    """`.claude-plugin/marketplace.json` exists and parses."""
    path = MANIFEST_DIR / "marketplace.json"
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["name"] == "forge"
    assert "plugins" in data
    assert len(data["plugins"]) >= 1


def test_marketplace_plugin_source_format() -> None:
    """marketplace.json plugin entry uses the supported `"./"` source format."""
    data = json.loads((MANIFEST_DIR / "marketplace.json").read_text())
    forge_plugin = next(p for p in data["plugins"] if p["name"] == "forge")
    assert forge_plugin["source"] == "./"


def test_no_top_level_hooks_json() -> None:
    """hooks.json at repo root is deprecated; hooks must live in plugin.json."""
    assert not (REPO_ROOT / "hooks.json").exists(), (
        "hooks.json was removed; hooks now declared inline in plugin.json"
    )


def test_expected_plugin_dirs_present() -> None:
    """Plugin ships agents/, skills/, claude-hooks/ with content."""
    for sub in ("agents", "skills", "claude-hooks"):
        d = REPO_ROOT / sub
        assert d.is_dir(), f"{sub}/ missing"
        assert any(d.iterdir()), f"{sub}/ is empty"


def test_pyproject_entry_points_declared() -> None:
    """pyproject.toml declares all forge CLI entry points."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    for cli in (
        "forge-precommit",
        "fix-forge-ruff",
        "verify-forge-docstrings",
        "install-forge-labels",
        "forge-doctor",
    ):
        assert cli in pyproject, f"pyproject missing entry point for {cli}"


def test_license_file_exists() -> None:
    """MIT LICENSE file is present."""
    license_file = REPO_ROOT / "LICENSE"
    assert license_file.is_file()
    contents = license_file.read_text()
    assert "MIT License" in contents
    assert "Jean Simonnet" in contents
