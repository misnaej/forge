"""Shared schema for the consumer ``.claude/settings.json`` forge block.

Single source of truth for the Claude Code settings shape that two CLIs
write:

- ``install-forge-claude-md`` (:mod:`forge.install_claudemd`) **scaffolds**
  the file when it does not exist yet.
- ``install-forge-claude`` (:mod:`forge.install_claude`) **registers**
  forge's generated guard hooks and **removes** the keys that named the
  retired plugin.

The retired plugin's marketplace key and id live here so the cleanup and
anything still reading that block name the same keys.
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
