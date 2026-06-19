"""Tests for the shared ``.claude/settings.json`` forge-block schema."""

from __future__ import annotations

from forge import claude_settings_schema as schema


def test_scaffold_is_a_fresh_copy() -> None:
    """scaffold() returns independent copies so callers can mutate freely."""
    first = schema.scaffold()
    first["hooks"]["PreToolUse"].append("mutated")  # type: ignore[index]
    assert schema.scaffold() == {"hooks": {"PreToolUse": [], "PostToolUse": []}}


def test_marketplace_entry_shape() -> None:
    """The entry nests source/repo/ref under a single ``source`` key."""
    entry = schema.marketplace_entry("dev")
    assert entry["source"]["ref"] == "dev"  # type: ignore[index]
    assert entry["source"]["source"] == "github"  # type: ignore[index]


def test_write_read_round_trip() -> None:
    """read_marketplace_ref recovers the ref marketplace_entry wrote.

    This couples the write side (install-forge-claude-settings) to the read
    side (install-forge-claude-md's channel detection): a divergence in the
    key path breaks this round-trip.
    """
    settings = {
        "extraKnownMarketplaces": {
            schema.MARKETPLACE_KEY: schema.marketplace_entry("main")
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
