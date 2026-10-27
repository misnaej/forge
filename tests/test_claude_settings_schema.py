"""Tests for the shared ``.claude/settings.json`` forge-block schema."""

from __future__ import annotations

from forge import claude_settings_schema as schema


def test_scaffold_is_a_fresh_copy() -> None:
    """scaffold() returns independent copies so callers can mutate freely."""
    first = schema.scaffold()
    first["hooks"]["PreToolUse"].append("mutated")  # type: ignore[index]
    assert schema.scaffold() == {"hooks": {"PreToolUse": [], "PostToolUse": []}}
