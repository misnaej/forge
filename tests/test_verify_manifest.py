"""Tests for ``forge.verify_manifest``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from forge import verify_manifest
from forge.git_utils import code_health_dir


if TYPE_CHECKING:
    from pathlib import Path


def test_skipped_when_no_plugin_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .claude-plugin/ dir → exit 0 and log says (skipped)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 0
    log = (code_health_dir(tmp_path) / "manifest_json.log").read_text()
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
    assert "OK" in (code_health_dir(tmp_path) / "manifest_json.log").read_text()


def test_fail_on_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed manifest → exit 1, log contains the filename."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text("{not valid")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 1
    log = (code_health_dir(tmp_path) / "manifest_json.log").read_text()
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
    log = (code_health_dir(tmp_path) / "manifest_json.log").read_text()
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
    log = (code_health_dir(tmp_path) / "manifest_json.log").read_text()
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
    log = (code_health_dir(tmp_path) / "manifest_json.log").read_text()
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


_HOOK = {"command": "x.sh"}


@pytest.fixture
def manifest_with_repeated_matchers(tmp_path: Path) -> Path:
    """A plugin.json repeating ``Bash`` (PreToolUse) and no-matcher (SubagentStop).

    Returns:
        Path of the written plugin.json.
    """
    p = tmp_path / "plugin.json"
    hooks = {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [_HOOK]},
            {"matcher": "Bash", "hooks": [_HOOK]},
        ],
        "SubagentStop": [{"hooks": [_HOOK]}, {"hooks": [_HOOK]}],
    }
    p.write_text(json.dumps({"hooks": hooks}))
    return p


def test_duplicate_matcher_error_names_every_repeated_pair(
    manifest_with_repeated_matchers: Path,
) -> None:
    """Every repeated event/matcher pair is named, absent matcher included."""
    err = verify_manifest._duplicate_matcher_error(manifest_with_repeated_matchers)
    assert err is not None
    assert "PreToolUse/Bash" in err
    assert "SubagentStop/(no matcher)" in err


@pytest.mark.parametrize(
    "hooks",
    [
        {"PreToolUse": [{"matcher": "Bash"}, {"matcher": "Edit"}]},
        {
            "PreToolUse": [{"matcher": "Bash"}],
            "PostToolUse": [{"matcher": "Bash"}],
        },
        None,
        ["not", "a", "dict"],
        {"PreToolUse": "not a list"},
        {"PreToolUse": ["not a dict", "not a dict"]},
        {"PreToolUse": [{"matcher": 5}, {"matcher": 5}]},
    ],
    ids=[
        "distinct-matchers",
        "same-matcher-different-events",
        "hooks-absent",
        "hooks-not-a-dict",
        "event-not-a-list",
        "group-not-a-dict",
        "non-str-matcher",
    ],
)
def test_duplicate_matcher_error_returns_none_when_nothing_repeats(
    tmp_path: Path, hooks: object
) -> None:
    """Non-repeating or malformed-shape hooks yield None, never a false positive.

    Args:
        hooks: Parametrized ``hooks`` value written to the manifest;
            ``None`` omits the key.
    """
    p = tmp_path / "plugin.json"
    manifest = {} if hooks is None else {"hooks": hooks}
    p.write_text(json.dumps(manifest))
    assert verify_manifest._duplicate_matcher_error(p) is None


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (
            "marketplace.json",
            json.dumps(
                {"hooks": {"Stop": [{"matcher": "A"}, {"matcher": "A"}]}},
            ),
        ),
        ("plugin.json", "{not valid"),
    ],
    ids=["non-plugin-file", "unparseable-plugin-json"],
)
def test_duplicate_matcher_error_ignores_non_plugin_files_and_unparseable(
    tmp_path: Path, name: str, content: str
) -> None:
    """Only a parseable plugin.json is checked; anything else returns None.

    Direct-call-only for the unparseable case: :func:`main` short-circuits on
    ``_parse_json_error`` first.

    Args:
        name: Parametrized file name the manifest content is written to.
        content: Parametrized file content; may be valid or unparseable JSON.
    """
    p = tmp_path / name
    p.write_text(content)
    assert verify_manifest._duplicate_matcher_error(p) is None


def test_fail_on_duplicate_matcher(
    manifest_with_repeated_matchers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated matcher in plugin.json → exit 1, log names the event."""
    root = manifest_with_repeated_matchers.parent
    plugin_dir = root / ".claude-plugin"
    plugin_dir.mkdir()
    manifest_with_repeated_matchers.rename(plugin_dir / "plugin.json")
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.argv", ["verify-forge-manifest"])
    assert verify_manifest.main() == 1
    log = (code_health_dir(root) / "manifest_json.log").read_text()
    assert "SubagentStop" in log
