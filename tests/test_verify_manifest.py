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


def test_fail_on_injected_key_via_escaped_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version value smuggling an injected key via escaped quotes → exit 1.

    A textual rewrite that only escapes quotes inside the version string
    produces a payload that is still *valid* JSON (CWE-116) — the outer
    parse succeeds, but the "version" value itself carries a second key.
    The schema check must reject this on shape (non-semver string), not
    rely on the JSON parser to catch it.
    """
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    payload = '{"version": "1.2.3\\", \\"pwned\\": \\"x"}'
    (plugin_dir / "plugin.json").write_text(payload)
    # Confirm the payload really is valid JSON — the attack only works
    # because the textual rewrite output still parses.
    json.loads(payload)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 1
    log = (tmp_path / "code_health" / "manifest_json.log").read_text()
    assert "plugin.json" in log


def test_fail_on_v_prefixed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``v``-prefixed version (not bare semver) → exit 1."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps({"version": "v1.2.3"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 1
    log = (tmp_path / "code_health" / "manifest_json.log").read_text()
    assert "plugin.json" in log


def test_fail_on_non_string_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A numeric version value → exit 1 with a "must be a string" message."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps({"version": 123}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 1
    log = (tmp_path / "code_health" / "manifest_json.log").read_text()
    assert "must be a string" in log


def test_pass_when_version_key_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``version`` key in plugin.json → exit 0 (the field is optional)."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps({"name": "x"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 0


def test_pass_on_marketplace_json_non_semver_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-semver version in marketplace.json → exit 0 (scope pin).

    The schema check is scoped to ``plugin.json`` only — other manifests
    keep plain-parse behavior regardless of their "version" shape.
    """
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "marketplace.json").write_text(
        json.dumps({"version": "not-a-semver"})
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 0


def test_version_error_returns_none_on_valid_semver(tmp_path: Path) -> None:
    """The helper returns None for a well-formed plugin.json version."""
    p = tmp_path / "plugin.json"
    p.write_text(json.dumps({"version": "1.2.3"}))
    assert verify_manifest._version_error(p) is None


def test_version_error_returns_message_on_malformed_semver(tmp_path: Path) -> None:
    """The helper returns a message for a version like "1.2" (too short)."""
    p = tmp_path / "plugin.json"
    p.write_text(json.dumps({"version": "1.2"}))
    err = verify_manifest._version_error(p)
    assert err is not None
    assert "1.2" in err


def test_version_error_returns_none_on_malformed_json(tmp_path: Path) -> None:
    """The helper returns None when the file fails to parse (defensive guard).

    Direct-call-only case: :func:`main` never reaches ``_version_error`` for
    a manifest that fails ``_parse_json_error`` first (short-circuit "or"),
    so this guard is unreachable through the CLI — only a direct call
    exercises it.
    """
    p = tmp_path / "plugin.json"
    p.write_text("{not valid")
    assert verify_manifest._version_error(p) is None
