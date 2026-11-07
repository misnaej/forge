"""Tests for ``forge.gen_vibe_hooks`` — plugin.json -> .vibe/hooks.toml.

# MOCKING STRATEGY: no mocks — the ``plugin_repo`` fixture is a real tmp
# repo with a real ``.claude-plugin/plugin.json`` hooks block; generation
# and TOML parsing run for real (tomllib round-trip included).
"""

from __future__ import annotations

import json
import tomllib
from typing import TYPE_CHECKING

import pytest

from forge.gen_vibe_hooks import (
    generate_vibe_hooks,
    verify_existing_hooks,
    write_vibe_hooks,
)


if TYPE_CHECKING:
    from pathlib import Path


_HOOKS_DIR = "${CLAUDE_PLUGIN_ROOT}/claude-hooks"


def _hooks_block() -> dict[str, list]:
    """A representative plugin.json hooks block for the fixture.

    Returns:
        Hooks dict with Bash/Edit/Write PreToolUse groups, SessionStart and
        SubagentStop groups made of unenforced hooks, and a PostToolUse
        Bash group.
    """
    return {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "command": f"{_HOOKS_DIR}/block_demo.sh",
                        "timeout": 5,
                    },
                    {
                        "command": f"{_HOOKS_DIR}/block_vibe_config.sh",
                        "timeout": 5,
                    },
                ],
            },
            {
                "matcher": "Edit",
                "hooks": [
                    {
                        "command": f"{_HOOKS_DIR}/block_protected_files.sh",
                        "timeout": 5,
                    }
                ],
            },
            {
                "matcher": "Write",
                "hooks": [
                    {
                        "command": f"{_HOOKS_DIR}/block_protected_files.sh",
                        "timeout": 5,
                    }
                ],
            },
        ],
        "SessionStart": [
            {
                "hooks": [
                    {
                        "command": f"{_HOOKS_DIR}/caveman_mode.sh",
                        "timeout": 5,
                    }
                ]
            }
        ],
        "SubagentStop": [
            {
                "hooks": [
                    {
                        "command": f"{_HOOKS_DIR}/log_agent_timing.sh",
                        "timeout": 5,
                    }
                ]
            },
            {
                "matcher": "^(forge:)?precommit-fixer$",
                "hooks": [
                    {
                        "command": f"{_HOOKS_DIR}/require_fixer_verdict.sh",
                        "timeout": 60,
                    }
                ],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "command": f"{_HOOKS_DIR}/warn_stale_wrapup.sh",
                        "timeout": 20,
                    }
                ],
            }
        ],
    }


@pytest.fixture
def plugin_repo(tmp_path: Path) -> Path:
    """A tmp repo whose ``.claude-plugin/plugin.json`` carries a hooks block.

    Returns:
        The project root path.
    """
    root = tmp_path / "proj"
    plugin_dir = root / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    plugin_json = {"name": "demo", "version": "0.0.0", "hooks": _hooks_block()}
    (plugin_dir / "plugin.json").write_text(json.dumps(plugin_json), encoding="utf-8")
    return root


def _entries(toml_content: str) -> list[dict]:
    """Parse generated TOML into its ``[[hooks]]`` entry list.

    Args:
        toml_content: The generated hooks.toml text.

    Returns:
        The parsed hook entries.
    """
    return tomllib.loads(toml_content)["hooks"]


def test_strict_true_on_every_tool_hook_entry(plugin_repo: Path) -> None:
    """Invariant 1: every pre_tool/post_tool entry carries strict = true.

    Vibe defaults strict to false, which turns a hook failure into a
    warning — a corrupted install would silently disarm every guard.
    """
    toml_content, _unenforced, _warnings = generate_vibe_hooks(plugin_repo)
    entries = _entries(toml_content)
    tool_entries = [e for e in entries if e.get("type") in ("pre_tool", "post_tool")]
    assert tool_entries, "fixture must produce tool-hook entries"
    assert all(e.get("strict") is True for e in tool_entries)


def test_bash_groups_use_re_bash_process_start_matcher(plugin_repo: Path) -> None:
    """Invariant 2: Bash guards get ``re:bash|process_start``, never bare ``bash``.

    Vibe routes agent-started background commands through ``process.start``;
    a bare ``bash`` matcher would let a backgrounded ``git push --force``
    walk past every guard.
    """
    toml_content, _unenforced, _warnings = generate_vibe_hooks(plugin_repo)
    matches = {e.get("match") for e in _entries(toml_content)}
    assert "re:bash|process_start" in matches
    assert "edit" in matches
    assert "write_file" in matches
    assert "bash" not in matches


def test_unmapped_matcher_raises_value_error(tmp_path: Path) -> None:
    """Invariant 3: an unmapped non-agent matcher is an error, never a skip."""
    root = tmp_path / "proj"
    plugin_dir = root / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    hooks = {
        "PreToolUse": [
            {
                "matcher": "WebSearch",
                "hooks": [{"command": f"{_HOOKS_DIR}/block_demo.sh"}],
            }
        ]
    }
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "0.0.0", "hooks": hooks}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unmapped Claude matcher 'WebSearch'"):
        generate_vibe_hooks(root)


def test_unenforced_hooks_reported_never_silently_dropped(plugin_repo: Path) -> None:
    """Invariant 4: hooks with no Vibe equivalent are reported, not dropped."""
    toml_content, unenforced, warnings = generate_vibe_hooks(plugin_repo)
    assert "SessionStart: caveman_mode.sh" in unenforced
    assert "SubagentStop: log_agent_timing.sh" in unenforced
    assert (
        "SubagentStop[^(forge:)?precommit-fixer$]: require_fixer_verdict.sh"
        in unenforced
    )
    assert any("Unenforced hooks" in w for w in warnings)
    # SubagentStop hooks are unenforced, but the shipped Vibe-only extra
    # hooks (post_agent replacements) are merged in as direct entries.
    post_agent = [e for e in _entries(toml_content) if e.get("event") == "post_agent"]
    assert {e["name"] for e in post_agent} == {
        "vibe-session-start",
        "vibe-continuation-state",
    }


def test_every_entry_routes_through_forge_hook_dispatch_with_roster(
    plugin_repo: Path,
) -> None:
    """Every generated command calls forge-hook-dispatch with --roster.

    The dispatcher is the live fail-closed path; the roster sidecar is the
    single runtime source of guard commands (plugin.json is never read at
    runtime).
    """
    toml_content, _unenforced, _warnings = generate_vibe_hooks(plugin_repo)
    entries = _entries(toml_content)
    assert entries
    for entry in entries:
        command = entry["command"]
        if entry.get("event") == "post_agent":
            # post_agent cannot block (strict forbidden); Vibe-only extras
            # run their script directly, no dispatcher routing.
            assert not command.startswith("forge-hook-dispatch")
            assert entry.get("strict") in (None, False)
            continue
        assert command.startswith("forge-hook-dispatch --harness vibe ")
        assert f"--event {entry['event']} " in command
        assert "--matcher " in command
        assert "--roster " in command
        assert command.rstrip().endswith('.vibe/forge-roster.json"')


def test_written_hooks_toml_round_trips_through_tomllib(plugin_repo: Path) -> None:
    """The written .vibe/hooks.toml parses back to the generated entries."""
    toml_content, _unenforced, _warnings = generate_vibe_hooks(plugin_repo)
    assert write_vibe_hooks(plugin_repo) == 0
    written = plugin_repo / ".vibe" / "hooks.toml"
    assert written.is_file()
    with written.open("rb") as f:
        parsed = tomllib.load(f)
    assert parsed["hooks"] == _entries(toml_content)


def test_verify_existing_hooks_enforces_strict(plugin_repo: Path) -> None:
    """verify_existing_hooks fails a blocking entry lacking strict=true."""
    vibe_dir = plugin_repo / ".vibe"
    vibe_dir.mkdir(parents=True)
    lax = (
        '[[hooks]]\nname = "pre_tool:re:bash|process_start"\ntype = "pre_tool"\n'
        'event = "pre_tool"\nmatch = "re:bash|process_start"\n'
        'command = "forge-hook-dispatch --harness vibe"\ntimeout = 12.0\n'
    )
    (vibe_dir / "hooks.toml").write_text(lax, encoding="utf-8")
    assert verify_existing_hooks(plugin_repo) == 1
    strict = lax + "strict = true\n"
    (vibe_dir / "hooks.toml").write_text(strict, encoding="utf-8")
    assert verify_existing_hooks(plugin_repo) == 0
    # No hooks.toml at all is not a violation (nothing installed yet).
    (vibe_dir / "hooks.toml").unlink()
    assert verify_existing_hooks(plugin_repo) == 0


def test_check_mode_writes_nothing(plugin_repo: Path) -> None:
    """--check reports success without creating .vibe/hooks.toml."""
    assert write_vibe_hooks(plugin_repo, check_mode=True) == 0
    assert not (plugin_repo / ".vibe").exists()
