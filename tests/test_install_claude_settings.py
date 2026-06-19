"""Tests for ``forge.install_claude_settings``.

# MOCKING STRATEGY: the helpers run against a real tmp_path repo; ``main``
# tests pin get_repo_root to tmp_path and patch sys.argv so argparse does
# not consume pytest's arguments.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from forge import install_claude_settings as ics


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write_pin(repo_root: Path, ref: str) -> None:
    """Write a pyproject.toml carrying a forge-scripts pin at *ref*.

    Args:
        repo_root: Directory to write ``pyproject.toml`` into.
        ref: The git ref the forge-scripts pin points at.
    """
    (repo_root / "pyproject.toml").write_text(
        "[project.optional-dependencies]\n"
        f'dev = ["forge-scripts @ git+https://github.com/misnaej/forge.git@{ref}"]\n',
        encoding="utf-8",
    )


def test_resolve_ref_flag_wins(tmp_path: Path) -> None:
    """An explicit --ref beats both the pip pin and the default."""
    _write_pin(tmp_path, "dev")
    assert ics._resolve_ref(tmp_path, "v9.9.9") == "v9.9.9"


def test_resolve_ref_auto_matches_pip_pin(tmp_path: Path) -> None:
    """With no --ref, the marketplace ref tracks the forge-scripts pip pin."""
    _write_pin(tmp_path, "dev")
    assert ics._resolve_ref(tmp_path, None) == "dev"


def test_resolve_ref_defaults_to_main(tmp_path: Path) -> None:
    """No --ref and no pin → 'main'."""
    assert ics._resolve_ref(tmp_path, None) == "main"


def test_load_settings_absent_seeds_scaffold(tmp_path: Path) -> None:
    """A missing settings file seeds the empty-hook scaffold (not bare {}).

    Seeding the scaffold means a standalone fresh-repo run preserves the
    hook arrays ``install-forge-claude-md`` would otherwise write, instead
    of producing a settings file with no ``hooks`` key that later blocks
    that scaffold (the file then already exists).
    """
    loaded = ics._load_settings(tmp_path / ".claude" / "settings.json")
    assert loaded == {"hooks": {"PreToolUse": [], "PostToolUse": []}}


def test_load_settings_malformed_is_none(tmp_path: Path) -> None:
    """A present-but-unparseable settings file loads as None (do-not-overwrite)."""
    path = tmp_path / "settings.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert ics._load_settings(path) is None


def test_merge_preserves_existing_keys() -> None:
    """Merge keeps other keys, marketplaces, and enabledPlugins entries."""
    existing: dict[str, object] = {
        "permissions": {"allow": ["x"]},
        "extraKnownMarketplaces": {"other": {"source": "y"}},
        "enabledPlugins": {"other@m": True},
    }
    merged = ics._merge(existing, "dev")
    assert merged["permissions"] == {"allow": ["x"]}
    markets = merged["extraKnownMarketplaces"]
    plugins = merged["enabledPlugins"]
    assert isinstance(markets, dict)
    assert isinstance(plugins, dict)
    assert markets["other"] == {"source": "y"}
    assert plugins["other@m"] is True
    assert plugins["forge@forge"] is True
    assert markets["forge"]["source"]["ref"] == "dev"  # type: ignore[index]


def test_is_current_true_when_block_present_and_ref_matches() -> None:
    """`_is_current` is True only when the marketplace ref AND enable match."""
    settings = ics._merge({}, "main")
    assert ics._is_current(settings, "main") is True
    assert ics._is_current(settings, "dev") is False  # ref drift
    assert ics._is_current({}, "main") is False


def test_main_writes_block_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() creates .claude/settings.json enabling the plugin.

    MOCK SETUP: get_repo_root pinned to tmp_path (no pyproject → ref main);
    argv is the bare invocation.
    """
    monkeypatch.setattr(ics, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(ics.sys, "argv", ["install-forge-claude-settings"])
    assert ics.main() == 0
    written = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert written["enabledPlugins"]["forge@forge"] is True
    assert written["extraKnownMarketplaces"]["forge"]["source"]["ref"] == "main"
    # Fresh-repo write seeds the empty-hook scaffold too, so a later
    # install-forge-claude-md run does not find a hooks-less file it skips.
    assert written["hooks"] == {"PreToolUse": [], "PostToolUse": []}


def test_main_idempotent_when_already_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second run with the block already present is a no-op success."""
    monkeypatch.setattr(ics, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(ics.sys, "argv", ["install-forge-claude-settings"])
    assert ics.main() == 0
    assert ics.main() == 0


def test_main_check_fails_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--check returns 1 when the block is missing, and writes nothing."""
    monkeypatch.setattr(ics, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(ics.sys, "argv", ["install-forge-claude-settings", "--check"])
    assert ics.main() == 1
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_main_refuses_to_overwrite_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed settings.json is reported and left untouched (exit 1)."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{ broken", encoding="utf-8")
    monkeypatch.setattr(ics, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(ics.sys, "argv", ["install-forge-claude-settings"])
    assert ics.main() == 1
    assert (claude / "settings.json").read_text() == "{ broken"
