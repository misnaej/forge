"""Tests for ``forge.agent_harness`` frozen harness profiles.

# MOCKING STRATEGY: none needed — the module is pure data plus pure
# functions. Every test calls the public API directly; no subprocess, no
# filesystem, no mocks.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest

from forge import agent_harness
from forge.agent_harness import (
    CLAUDE_PROFILE,
    UNENFORCED_VIBE_HOOKS,
    VIBE_PROFILE,
    HarnessName,
    emit_block,
    emit_error,
    emit_pass,
    get_all_harness_names,
    get_harness_profile,
    resolve_hooks_base,
    translate_event,
    translate_payload_field,
    translate_tool_matcher,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_registry_lists_exactly_claude_and_vibe() -> None:
    """The registry holds exactly the two supported harnesses, sorted."""
    assert get_all_harness_names() == ["claude", "vibe"]
    assert set(HarnessName) == {HarnessName.CLAUDE, HarnessName.VIBE}


def test_profiles_are_frozen_and_carry_blocking_events() -> None:
    """Profiles are frozen dataclasses; blocking-event sets are pinned."""
    for profile in (CLAUDE_PROFILE, VIBE_PROFILE):
        with pytest.raises(dataclasses.FrozenInstanceError):
            profile.name = "mutated"  # type: ignore[misc]
    # post_agent is excluded: Vibe forbids strict on it and its deny only
    # injects a retry message — it cannot block.
    assert VIBE_PROFILE.blocking_events == frozenset({"pre_tool", "post_tool"})
    assert CLAUDE_PROFILE.blocking_events == frozenset(
        {
            "PreToolUse",
            "SessionStart",
            "PreCompact",
            "SubagentStart",
            "SubagentStop",
            "PostToolUse",
        }
    )
    assert get_harness_profile("vibe") is VIBE_PROFILE
    assert get_harness_profile("claude") is CLAUDE_PROFILE


def test_unknown_harness_name_hard_fails() -> None:
    """An unknown name raises ValueError naming the known harnesses.

    It must never default to ``claude`` — a silent misdetection emits the
    wrong verdict format, which a harness reads as "allow".
    """
    with pytest.raises(ValueError, match="Unknown harness name 'nope'"):
        get_harness_profile("nope")
    with pytest.raises(ValueError, match="claude, vibe"):
        emit_block("x", "nope")
    with pytest.raises(ValueError, match="claude, vibe"):
        emit_pass("nope")
    with pytest.raises(ValueError, match="claude, vibe"):
        emit_error("x", "nope")


def test_claude_verdict_shapes() -> None:
    """Claude: deny = exit 2 with empty stdout, pass = exit 0 silent."""
    assert emit_block("nope", "claude") == (2, "")
    assert emit_pass("claude") == (0, "")
    assert emit_error("boom", "claude") == (2, "boom")


def test_vibe_deny_verdict_is_stdout_json_on_exit_0() -> None:
    r"""Vibe: deny = exit 0 + stdout JSON ``{"decision":"deny",...}``.

    ``ensure_ascii=False`` keeps a unicode reason raw, not ``\\u``-escaped.
    """
    exit_code, stdout = emit_block("nope", "vibe")
    assert exit_code == 0
    assert json.loads(stdout) == {"decision": "deny", "reason": "nope"}
    _exit_code, unicode_stdout = emit_block("héllo 🌍", "vibe")
    assert "héllo 🌍" in unicode_stdout
    assert "\\u" not in unicode_stdout


def test_vibe_pass_verdict_is_silent_exit_0() -> None:
    """Vibe: pass = exit 0 with empty stdout (no JSON)."""
    assert emit_pass("vibe") == (0, "")


def test_vibe_error_verdict_denies_with_internal_error_prefix() -> None:
    """Vibe errors are denies too (fail-closed), prefixed for humans."""
    exit_code, stdout = emit_error("boom", "vibe")
    assert exit_code == 0
    verdict = json.loads(stdout)
    assert verdict["decision"] == "deny"
    assert verdict["reason"] == "Internal error: boom"


def test_translate_event_vibe_maps_and_returns_none_for_gaps() -> None:
    """Vibe events map to Claude events; unmapped events yield None."""
    assert translate_event("pre_tool", "vibe") == "PreToolUse"
    assert translate_event("post_tool", "vibe") == "PostToolUse"
    assert translate_event("post_agent", "vibe") == "SubagentStop"
    assert translate_event("SessionStart", "vibe") is None
    assert translate_event("PreCompact", "vibe") is None
    assert translate_event("SubagentStart", "vibe") is None


def test_translate_event_claude_is_identity() -> None:
    """Claude profile maps every event to itself, even unknown ones."""
    assert translate_event("PreToolUse", "claude") == "PreToolUse"
    assert translate_event("anything_at_all", "claude") == "anything_at_all"


def test_translate_tool_matcher_vibe() -> None:
    """Vibe matchers translate to Claude tool names; unknown -> None."""
    assert translate_tool_matcher("re:bash|process_start", "vibe") == "Bash"
    assert translate_tool_matcher("bash", "vibe") == "Bash"
    assert translate_tool_matcher("edit", "vibe") == "Edit"
    assert translate_tool_matcher("write_file", "vibe") == "Write"
    assert translate_tool_matcher("web_search", "vibe") is None


def test_translate_tool_matcher_claude() -> None:
    """Claude matchers pass through when known, None when not."""
    assert translate_tool_matcher("Bash", "claude") == "Bash"
    assert translate_tool_matcher("Edit", "claude") == "Edit"
    assert translate_tool_matcher("Write", "claude") == "Write"
    assert translate_tool_matcher("bash", "claude") is None


def test_translate_payload_field() -> None:
    """Vibe ``agent_name`` renames to Claude ``agent_type``; rest pass through."""
    assert translate_payload_field("agent_name", "vibe") == "agent_type"
    assert translate_payload_field("cwd", "vibe") == "cwd"
    assert translate_payload_field("agent_type", "claude") == "agent_type"


def test_unenforced_vibe_hooks_pins_exactly_the_unenforced_hooks() -> None:
    """The unenforced list is exactly the hooks with no Vibe equivalent."""
    assert set(UNENFORCED_VIBE_HOOKS) == {
        "require_fixer_verdict.sh",
        "require_helpers_stopped.sh",
        "caveman_mode.sh",
        "load_continuation.sh",
        "keep_continuation_state.sh",
        "log_agent_timing.sh",
    }
    assert all(reason.strip() for reason in UNENFORCED_VIBE_HOOKS.values())
    # block_fixer_recon.sh is enforced via pre_tool (Bash matcher), so it
    # must not appear in the unenforced list.
    assert "block_fixer_recon.sh" not in UNENFORCED_VIBE_HOOKS


def test_resolve_hooks_base_layouts(tmp_path: Path) -> None:
    """``claude-hooks/`` resolution across forge-repo vs consumer layouts."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    # Forge's own checkout: top-level claude-hooks/, none in the plugin dir.
    (tmp_path / "claude-hooks").mkdir()
    assert resolve_hooks_base(tmp_path, plugin_dir) == tmp_path
    # Consumer install: guards live under the plugin dir.
    (tmp_path / "claude-hooks").rmdir()
    (plugin_dir / "claude-hooks").mkdir()
    assert resolve_hooks_base(tmp_path, plugin_dir) == plugin_dir
    # Neither layout present: fall back to the plugin dir.
    (plugin_dir / "claude-hooks").rmdir()
    assert resolve_hooks_base(tmp_path, plugin_dir) == plugin_dir


def test_module_exposes_profiles_not_a_detector() -> None:
    """No ``detect()`` helper exists — harness identity is caller-named only."""
    assert not hasattr(agent_harness, "detect")
    assert not hasattr(agent_harness, "detect_harness")
