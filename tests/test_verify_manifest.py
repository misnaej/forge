"""Tests for ``forge.verify_manifest``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from forge import verify_manifest


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_skipped_when_no_plugin_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .claude-plugin/ dir → exit 0 and log says (skipped)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 0
    log = (tmp_path / "code_health" / "manifest_json.log").read_text()
    assert "skipped" in log


def test_pass_on_valid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All .claude-plugin/*.json files parse → exit 0, log says OK."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps({"name": "x"}))
    (plugin_dir / "marketplace.json").write_text(json.dumps({"name": "x"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 0
    assert "OK" in (tmp_path / "code_health" / "manifest_json.log").read_text()


def test_fail_on_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed manifest → exit 1, log contains the filename."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text("{not valid")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 1
    log = (tmp_path / "code_health" / "manifest_json.log").read_text()
    assert "plugin.json" in log


def test_parse_json_error_returns_none_on_valid(tmp_path: Path) -> None:
    """The helper returns None when JSON parses cleanly."""
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"a": 1}))
    assert verify_manifest._parse_json_error(p) is None


def test_parse_json_error_returns_message_on_invalid(tmp_path: Path) -> None:
    """The helper returns a ``filename: error`` string when JSON fails."""
    p = tmp_path / "p.json"
    p.write_text("{nope")
    err = verify_manifest._parse_json_error(p)
    assert err is not None
    assert err.startswith("p.json:")
