"""Names forge's plugin ships, read from the packaged roster.

``data/plugin-roster.toml`` ships with the pip package, so a consumer repo
resolves the same names without a forge checkout. A forge-repo test
regenerates it from ``agents/*.md``, ``skills/*/SKILL.md`` and
``claude-hooks/*.sh`` and diffs, so it cannot go stale.

One reader, because the two that existed before disagreed: the agent-doc
verifier let a roster of the wrong shape escape as an exception and kept
empty names, while the harness generator swallowed more failures but
iterated a scalar ``names`` as characters and offered each as a name to
rewrite. Callers: the agent-doc verifier, the Vibe installer and
generators, and ``forge-metrics``. Leaf module — stdlib only.
"""

from __future__ import annotations

import tomllib
from importlib import resources
from typing import Final


ROSTER_RESOURCE: Final = "data/plugin-roster.toml"

# The kinds the roster carries. Agents and skills share the plugin's
# ``forge:`` namespace; hooks are addressed by path and never namespaced.
_NAMESPACED_KINDS: Final = ("agents", "skills")


def roster_names(kind: str) -> frozenset[str]:
    """Names listed under one ``[kind]`` table of the packaged roster.

    Every read or parse failure yields an empty set rather than raising.
    This reader runs inside a commit gate, an installer and a metrics
    report, so a packaged file that is missing on an older install, or
    malformed, degrades the caller instead of blocking the commit. A non-string or
    empty entry is not a name and is dropped.

    Args:
        kind: ``"agents"``, ``"skills"`` or ``"hooks"``.

    Returns:
        The names under *kind*, without any namespace prefix; empty when
        the roster is absent, unreadable, or shaped unexpectedly.
    """
    # ValueError covers TOMLDecodeError and UnicodeDecodeError both. The
    # shape is checked rather than caught: an `except TypeError` here would
    # also swallow a wrong call signature, turning a programming error into
    # a silently empty roster.
    try:
        text = (
            resources.files("forge")
            .joinpath(ROSTER_RESOURCE)
            .read_text(encoding="utf-8")
        )
        table = tomllib.loads(text)
    except (OSError, ValueError, ModuleNotFoundError):
        return frozenset()
    # A TOML document is always a table, so `table` needs no check; its
    # members do, because a hand-edit can put a scalar under either key.
    kind_table = table.get(kind)
    names = kind_table.get("names") if isinstance(kind_table, dict) else None
    if not isinstance(names, list):
        return frozenset()
    return frozenset(n for n in names if isinstance(n, str) and n)


def shipped_plugin_names() -> frozenset[str]:
    """Every name the plugin addresses under its own namespace.

    Returns:
        The union of the agent and skill names. Hooks are excluded: they
        are referenced by path, so no generator rewrites them.
    """
    return frozenset().union(*(roster_names(kind) for kind in _NAMESPACED_KINDS))


def shipped_agent_names() -> frozenset[str]:
    """Agent names forge's plugin ships, without the namespace prefix.

    Returns:
        The names under the roster's ``agents`` table.
    """
    return roster_names("agents")
