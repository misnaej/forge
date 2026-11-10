"""Identity metadata for Mistral Vibe unified session stores.

A Vibe session store (``$VIBE_HOME/logs/session/unified``) holds one
subdirectory per session: root sessions under their id, subagents under
``child-<id>``. Each carries a ``CURRENT`` pointer to its latest generation
whose ``runtime-state.json`` records the session's identity (kind, parent,
cwd) and its ``core_system_instructions`` text — for subagents that text is
the agent profile's own frontmatter, which names the agent and its model
tier. Root sessions additionally keep a ``meta.json``.

Everything here is tolerant: the store format is internal to Vibe and may
change between releases, so any unreadable or unexpected file yields
``None`` plus a logged warning, never an exception.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)


@dataclass(frozen=True)
class VibeSessionMeta:
    """Identity of one Vibe session directory."""

    session_id: str
    kind: str
    parent_session_id: str | None
    cwd: str | None
    agent_name: str | None
    model: str | None


def default_sessions_dir() -> Path:
    """The unified session store of the active Vibe home.

    Returns:
        ``$VIBE_HOME/logs/session/unified`` when ``VIBE_HOME`` is set,
        otherwise ``~/.vibe/logs/session/unified``.
    """
    home = os.environ.get("VIBE_HOME")
    base = Path(home) if home else Path.home() / ".vibe"
    return base / "logs" / "session" / "unified"


def read_session_meta(session_dir: Path) -> VibeSessionMeta | None:
    """Read one session directory's identity.

    Args:
        session_dir: A session directory inside the unified store.

    Returns:
        The session's metadata, or ``None`` when nothing readable identifies
        it (logged as a warning).
    """
    state = _latest_runtime_state(session_dir) or {}
    identity = _mapping(state.get("identity"))
    metadata = _mapping(state.get("session_metadata"))
    if not identity:
        root_meta = _meta_json(session_dir)
        if root_meta is None:
            logger.warning("vibe session %s: no readable identity", session_dir.name)
            return None
        identity = {
            "session_id": root_meta.get("session_id"),
            "kind": "root",
            "parent_session_id": root_meta.get("parent_session_id"),
        }
        metadata = {"environment": root_meta.get("environment")}
    session_id = identity.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        logger.warning("vibe session %s: identity without session_id", session_dir.name)
        return None
    instructions = state.get("core_system_instructions")
    frontmatter = _frontmatter(instructions) if isinstance(instructions, str) else {}
    environment = _mapping(metadata.get("environment"))
    cwd = (
        _str_or_none(metadata.get("cwd"))
        or _str_or_none(metadata.get("origin_cwd"))
        or _str_or_none(environment.get("working_directory"))
    )
    return VibeSessionMeta(
        session_id=session_id,
        kind=str(identity.get("kind") or "root"),
        parent_session_id=_str_or_none(identity.get("parent_session_id")),
        cwd=cwd,
        agent_name=_str_or_none(metadata.get("agent_name")) or frontmatter.get("name"),
        model=frontmatter.get("model") or _str_or_none(metadata.get("active_model")),
    )


def _mapping(value: dict[str, Any] | None) -> dict[str, Any]:
    """Return *value* when it is a dict, else an empty one.

    Args:
        value: The value to narrow.

    Returns:
        *value* itself when it is a dict, otherwise an empty dict.
    """
    return value if isinstance(value, dict) else {}


def _str_or_none(value: str | None) -> str | None:
    """Return *value* when it is a non-empty string, else None.

    Args:
        value: The value to narrow.

    Returns:
        *value* when it is a non-empty string, otherwise None.
    """
    return value if isinstance(value, str) and value else None


def _latest_runtime_state(session_dir: Path) -> dict[str, Any] | None:
    """The ``runtime-state.json`` of the generation ``CURRENT`` points to.

    Args:
        session_dir: A session directory.

    Returns:
        The parsed state, or ``None`` when absent or unreadable.
    """
    try:
        raw = (session_dir / "CURRENT").read_text(encoding="utf-8").strip()
        try:
            current = json.loads(raw)
            generation = (
                current.get("generation") if isinstance(current, dict) else None
            )
        except json.JSONDecodeError:
            generation = raw  # bare generation id
        if not isinstance(generation, str) or not generation:
            return None
        state_path = session_dir / "generations" / generation / "runtime-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def _meta_json(session_dir: Path) -> dict[str, Any] | None:
    """The root session's ``meta.json``, when present and parseable.

    Args:
        session_dir: A session directory.

    Returns:
        The parsed meta object, or None when absent or unreadable.
    """
    try:
        meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def _frontmatter(text: str) -> dict[str, str]:
    """Parse the YAML-ish frontmatter block at the start of *text*.

    Args:
        text: The agent profile's system-instructions text.

    Returns:
        ``key: value`` pairs from the leading ``---`` block; empty when
        there is none.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields
