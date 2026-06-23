"""Shared schema for the consumer ``.claude/settings.json`` forge block.

Single source of truth for the Claude Code settings shape that two CLIs
depend on from opposite directions:

- ``install-forge-claude-md`` (:mod:`forge.install_claudemd`) **scaffolds**
  the file and **reads** the marketplace ``ref`` from the global
  ``~/.claude/settings.json`` for the plugin-staleness check.
- ``install-forge-claude-settings``
  (:mod:`forge.install_claude_settings`) **writes** the per-repo
  marketplace + ``enabledPlugins`` block.

The marketplace key path (``extraKnownMarketplaces.forge.source.ref``) and
the ``forge@forge`` plugin id live here so the write side and the read side
cannot silently diverge — a rename on one side that skipped the other would
break the staleness hint with no compile-time signal.
"""

from __future__ import annotations

import copy

from forge.git_utils import _FORGE_GITHUB_REPO


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


def marketplace_entry(ref: str) -> dict[str, object]:
    """Return forge's ``extraKnownMarketplaces[forge]`` value for *ref*.

    Args:
        ref: Marketplace ref (branch / tag).

    Returns:
        The ``{"source": {...}}`` value keyed under the forge marketplace.
    """
    return {"source": {"source": "github", "repo": _FORGE_GITHUB_REPO, "ref": ref}}


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
