"""Agent-run stats from Mistral Vibe unified session journals.

Vibe journals (``<session>/journal/*.jsonl``) are append-only recovery
records, one JSON object per line: ``action_result`` records carry each
LLM completion's parts (tool calls) and usage, ``core_input`` records carry
the wall-clock timestamp, and ``projection_delta`` records carry the
session's cumulative token usage. This module folds them into one
:class:`VibeRun` per subagent session, which
:mod:`forge.agent_profile` merges into its report.

Parsing is tolerant like the Claude transcript reader: a malformed line or
a missing field is skipped, never raised on — Vibe's store format is
internal and may change between releases.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge.agent_profile_tokens import TokenUsage
from forge.git_utils import github_slug
from forge.vibe_metadata import default_sessions_dir, read_session_meta


if TYPE_CHECKING:
    from collections.abc import Iterator


logger = logging.getLogger(__name__)

#: Mirrors ``agent_profile.IDLE_GAP_S``: a gap between two journal records
#: longer than this is idle wall time (a sleeping monitor, a human away),
#: not active work.
IDLE_GAP_S = 300.0

#: Key prefix for Vibe-derived runs, so they never collide with ledger or
#: Claude transcript agent ids in the report or the digest.
VIBE_RUN_PREFIX = "vibe:"


@dataclass
class VibeRun:
    """One Vibe subagent session as profiler input."""

    session_id: str
    agent_type: str
    started: datetime | None = None
    ended: datetime | None = None
    active_s: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    max_repeat: int = 0
    repeated_call: str = ""
    tokens: TokenUsage = field(default_factory=TokenUsage)


def runs_from_vibe_sessions(
    root: Path, sessions_dir: Path | None = None
) -> dict[str, VibeRun]:
    """Every Vibe subagent session that ran in *root*'s checkout.

    A session belongs to the repo when its recorded cwd is *root* or shares
    its GitHub ``origin`` slug — the same rule ``repo_transcript_dirs``
    applies to Claude project directories, so parallel clones each get
    their own sessions. Root (main) sessions are skipped: the ledger
    already covers main-session tool activity.

    Args:
        root: The checkout asking.
        sessions_dir: The unified store to scan; the active Vibe home's
            when omitted.

    Returns:
        Runs keyed by ``vibe:<session_id>``.
    """
    sessions_dir = sessions_dir if sessions_dir is not None else default_sessions_dir()
    if not sessions_dir.is_dir():
        return {}
    slug = github_slug(root)
    runs: dict[str, VibeRun] = {}
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        meta = read_session_meta(session_dir)
        if meta is None or meta.kind != "subagent" or not meta.cwd:
            continue
        cwd = Path(meta.cwd)
        same_repo = cwd.resolve() == root.resolve() or (
            slug is not None and github_slug(cwd) == slug
        )
        if not same_repo:
            continue
        run = parse_vibe_journal(session_dir)
        if run is None:
            continue
        run.session_id = meta.session_id
        run.agent_type = meta.agent_name or meta.session_id
        runs[f"{VIBE_RUN_PREFIX}{meta.session_id}"] = run
    return runs


def parse_vibe_journal(session_dir: Path) -> VibeRun | None:
    """Fold one session's journal records into a run.

    Args:
        session_dir: A session directory holding ``journal/*.jsonl``.

    Returns:
        The run, or ``None`` when no record carries a timestamp.
    """
    run = VibeRun(session_id=session_dir.name, agent_type=session_dir.name)
    scan = _JournalScan()
    for journal in sorted((session_dir / "journal").glob("*.jsonl")):
        for record in _iter_jsonl(journal):
            scan.record(run, record)
    scan.finish(run)
    return run if run.started is not None else None


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects in *path*, skipping lines that are not one.

    Args:
        path: File to read, one JSON object per line.

    Yields:
        Each line's parsed object, in file order.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


class _JournalScan:
    """Running state of one :func:`parse_vibe_journal` pass."""

    def __init__(self) -> None:
        """Start an empty scan."""
        self._previous: datetime | None = None
        self._seen_actions: set[str] = set()
        self._signatures: Counter[str] = Counter()
        self._labels: dict[str, str] = {}
        self._last_usage: dict[str, int] = {}

    def record(self, run: VibeRun, record: dict[str, Any]) -> None:
        """Fold one journal record into *run*.

        Args:
            run: The run being built; updated in place.
            record: One journal record.
        """
        ts = _record_ts(record)
        if ts is not None:
            run.started = run.started or ts
            run.ended = ts
            if self._previous is not None:
                gap = (ts - self._previous).total_seconds()
                if gap <= IDLE_GAP_S:
                    run.active_s += max(gap, 0.0)
            self._previous = ts
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        kind = record.get("type")
        if kind == "action_result":
            self._action_result(run, record, payload)
        elif kind == "projection_delta":
            self._projection(payload)

    def finish(self, run: VibeRun) -> None:
        """Apply the fallback cumulative usage when no per-request usage was seen.

        Args:
            run: The run being built; updated in place.
        """
        if run.tokens.requests == 0 and self._last_usage:
            run.tokens = TokenUsage(
                input_tokens=self._last_usage.get("inputTokens", 0),
                cache_read_tokens=self._last_usage.get("cachedInputTokens", 0),
                output_tokens=self._last_usage.get("outputTokens", 0),
                requests=1,
            )

    def _action_result(
        self, run: VibeRun, record: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Count one completion's turn, tool calls, repeats and usage.

        Args:
            run: The run being built; updated in place.
            record: The full journal record.
            payload: Its ``payload`` member.
        """
        action_id = payload.get("action_id") or record.get("action_id")
        if isinstance(action_id, str):
            if action_id in self._seen_actions:
                return
            self._seen_actions.add(action_id)
        result = payload.get("result")
        completion = _mapping(_mapping(result).get("result"))
        parts = completion.get("parts")
        if not isinstance(parts, list):
            parts = []
        run.turns += 1
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "tool_call":
                continue
            run.tool_calls += 1
            signature = f"{part.get('name')}:{part.get('arguments_json')}"
            self._signatures[signature] += 1
            self._labels.setdefault(signature, str(part.get("name") or "?"))
            if self._signatures[signature] > run.max_repeat:
                run.max_repeat = self._signatures[signature]
                run.repeated_call = self._labels[signature]
        usage = _mapping(completion.get("usage"))
        if usage:
            run.tokens.input_tokens += int(usage.get("input_tokens") or 0)
            run.tokens.cache_read_tokens += int(usage.get("cached_input_tokens") or 0)
            run.tokens.cache_write_tokens += int(usage.get("cache_write_tokens") or 0)
            run.tokens.output_tokens += int(usage.get("output_tokens") or 0)
            run.tokens.requests += 1

    def _projection(self, payload: dict[str, Any]) -> None:
        """Keep the latest cumulative session token usage as a fallback.

        Args:
            payload: A projection_delta record's ``payload`` member.
        """
        delta = payload.get("delta")
        if not isinstance(delta, list):
            return
        for entry in delta:
            if not isinstance(entry, dict) or entry.get("op") != "set_envelope":
                continue
            session = _mapping(_mapping(entry.get("state")).get("session"))
            usage = _mapping(session.get("tokenUsage"))
            if usage:
                self._last_usage = {
                    key: int(value)
                    for key, value in usage.items()
                    if isinstance(value, int | float)
                }


def _mapping(value: dict[str, Any] | None) -> dict[str, Any]:
    """Return *value* when it is a dict, else an empty one.

    Args:
        value: The value to narrow.

    Returns:
        *value* itself when it is a dict, otherwise an empty dict.
    """
    return value if isinstance(value, dict) else {}


def _record_ts(record: dict[str, Any]) -> datetime | None:
    """One journal record's wall-clock time.

    The timestamp's nesting inside the record has moved between Vibe
    releases (top level, ``payload.determinism``, ``payload.input.determinism``),
    so the first ``time_unix_ms`` found anywhere in the record wins, with the
    record-level ``ts_ms`` as fallback.

    Args:
        record: The full journal record.

    Returns:
        The record's timestamp, or ``None`` when it carries none.
    """
    found = _find_ms(record, "time_unix_ms")
    if found is None and isinstance(record.get("ts_ms"), int | float):
        found = record["ts_ms"]
    if isinstance(found, int | float) and found > 0:
        return datetime.fromtimestamp(found / 1000, tz=UTC)
    return None


def _find_ms(value: dict[str, Any] | None, key: str) -> int | float | None:
    """Depth-first search for the first numeric *key* in a nested structure.

    Args:
        value: The structure to search.
        key: The key to find.

    Returns:
        The first numeric value found under *key*, or ``None``.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key and isinstance(v, int | float):
                return v
            found = _find_ms(v, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_ms(item, key)
            if found is not None:
                return found
    return None
