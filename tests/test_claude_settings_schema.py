"""Tests for the shared ``.claude/settings.json`` forge-block schema."""

from __future__ import annotations

from forge import claude_settings_schema as schema


def test_scaffold_is_a_fresh_copy() -> None:
    """scaffold() returns independent copies so callers can mutate freely."""
    first = schema.scaffold()
    first["hooks"]["PreToolUse"].append("mutated")  # type: ignore[index]
    assert schema.scaffold() == {"hooks": {"PreToolUse": [], "PostToolUse": []}}


def test_read_marketplace_ref_reads_the_source_ref() -> None:
    """read_marketplace_ref finds the ref under the forge marketplace key."""
    settings = {
        "extraKnownMarketplaces": {
            schema.MARKETPLACE_KEY: {"source": {"source": "github", "ref": "main"}}
        }
    }
    assert schema.read_marketplace_ref(settings) == "main"


def test_read_marketplace_ref_missing_or_malformed() -> None:
    """A missing chain or a null mid-chain yields None, never an error."""
    assert schema.read_marketplace_ref({}) is None
    assert schema.read_marketplace_ref({"extraKnownMarketplaces": None}) is None
    assert (
        schema.read_marketplace_ref(
            {"extraKnownMarketplaces": {schema.MARKETPLACE_KEY: {"source": {}}}}
        )
        is None
    )
