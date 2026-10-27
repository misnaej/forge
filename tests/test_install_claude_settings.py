"""Tests for ``forge.install_claude_settings``.

# MOCKING STRATEGY: the helpers run against a real tmp_path repo; ``main``
# tests pin get_repo_root to tmp_path and patch sys.argv so argparse does
# not consume pytest's arguments.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from forge import install_claude_settings as ics


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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


def test_merge_turns_shared_forge_off_and_preserves_the_rest() -> None:
    """Merge drops forge's marketplace, disables forge@forge, keeps the rest."""
    existing: dict[str, object] = {
        "permissions": {"allow": ["x"]},
        "extraKnownMarketplaces": {
            "other": {"source": "y"},
            "forge": {"source": {"source": "github", "ref": "main"}},
        },
        "enabledPlugins": {"other@m": True, "forge@forge": True},
    }
    merged = ics._merge(existing)
    assert merged["permissions"] == {"allow": ["x"]}
    assert merged["extraKnownMarketplaces"] == {"other": {"source": "y"}}
    assert merged["enabledPlugins"] == {"other@m": True, "forge@forge": False}


def test_merge_drops_marketplaces_key_left_empty() -> None:
    """A marketplaces map holding only forge's entry is removed whole."""
    merged = ics._merge({"extraKnownMarketplaces": {"forge": {}}})
    assert "extraKnownMarketplaces" not in merged


def test_is_current_only_when_marketplace_gone_and_plugin_disabled() -> None:
    """`_is_current` needs both: no forge marketplace and forge@forge false."""
    assert ics._is_current(ics._merge({})) is True
    assert ics._is_current({"enabledPlugins": {"forge@forge": True}}) is False
    assert ics._is_current({}) is False
    assert (
        ics._is_current(
            {
                "extraKnownMarketplaces": {"forge": {}},
                "enabledPlugins": {"forge@forge": False},
            }
        )
        is False
    )


def test_main_writes_block_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """main() creates .claude/settings.json with the shared plugin off.

    MOCK SETUP: get_repo_root pinned to tmp_path; argv is the bare
    invocation.
    EXPECTED BEHAVIOR: the write points at ``forge-plugin-sync``, which
    installs this checkout's own forge.
    """
    monkeypatch.setattr(ics, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(ics.sys, "argv", ["install-forge-claude-settings"])
    caplog.set_level(logging.INFO)
    assert ics.main() == 0
    assert "forge-plugin-sync" in caplog.text
    written = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert written["enabledPlugins"]["forge@forge"] is False
    assert "extraKnownMarketplaces" not in written
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
