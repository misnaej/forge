"""Shared schema for the consumer ``.claude/settings.json`` forge block.

Single source of truth for the Claude Code settings shape that two CLIs
depend on from opposite directions:

- ``install-forge-claude-md`` (:mod:`forge.install_claudemd`) **scaffolds**
  the file and **reads** the marketplace ``ref`` from the global
  ``~/.claude/settings.json`` for the plugin-staleness check.
- ``install-forge-claude-settings``
  (:mod:`forge.install_claude_settings`) **removes** the shared ``forge``
  marketplace and turns ``forge@forge`` off, so only the checkout's own
  ``forge-plugin-sync`` install loads.

The marketplace key path (``extraKnownMarketplaces.forge.source.ref``) and
the ``forge@forge`` plugin id live here so the side that removes the block
and the side that reads it cannot silently diverge — a rename on one side
that skipped the other would go unnoticed with no compile-time signal.
"""

from __future__ import annotations

import copy


MARKETPLACE_KEY = "forge"
PLUGIN_KEY = "forge@forge"

# Minimal settings written when no .claude/settings.json exists yet: empty
# hook arrays consumers fill with their own ${CLAUDE_PROJECT_DIR}-rooted
# registrations. Both CLIs seed an absent file from this so neither clobbers
# the other's contribution regardless of which runs first.
_SCAFFOLD: dict[str, object] = {"hooks": {"PreToolUse": [], "PostToolUse": []}}


def scaffold() -> dict[str, object]:
    """Return a fresh deep copy of the empty-file settings scaffold.

    Returns:
        A new ``{"hooks": {"PreToolUse": [], "PostToolUse": []}}`` dict;
        deep-copied so callers may mutate it without touching the module
        constant.
    """
    return copy.deepcopy(_SCAFFOLD)


def read_marketplace_ref(settings: dict[str, object]) -> str | None:
    """Return ``extraKnownMarketplaces.forge.source.ref`` from *settings*.

    Walks the chain via per-step ``isinstance(node, dict)`` guards rather
    than chained ``.get`` calls, so a null or wrong-typed intermediate value
    does not raise.

    Args:
        settings: A parsed settings object.

    Returns:
        The configured ref string, or ``None`` when absent / malformed.
    """
    node: object = settings
    for key in ("extraKnownMarketplaces", MARKETPLACE_KEY, "source", "ref"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None
