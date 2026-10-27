"""forge-agent-profile — where the agents' time goes, and what they wrote.

Forge times pre-commit steps (``precommit_timing.log``), tests
(``forge-slow-tests-report``), and wrapped subprocesses
(``forge-telemetry``), but nothing timed the agents themselves. This
reporter answers, per agent type: how long a run takes, how much of
that is active work versus idle waiting, which runs were slowest,
which tool calls cost the most, and whether a run looks stuck —
re-issuing an identical tool call, or ``forge:precommit-fixer``
exceeding the three full ``forge-precommit`` runs its contract allows.

Three inputs, one primary and two best-effort:

- ``code_health/agent_timing.jsonl`` — the ledger the
  ``log_agent_timing`` Claude Code hook appends on every
  ``SubagentStart`` / ``SubagentStop`` / ``PostToolUse`` event, plus one
  ``precommit_full_run`` line per full ``forge-precommit`` the
  ``block_fixer_recon`` hook lets the fixer run (its three-run cap).
  Hook payloads are documented and stable; this is the durable source
  of truth for run boundaries, agent types, tool durations, and fixer
  run counts.
- Claude Code subagent transcripts (``…/subagents/agent-<id>.jsonl``),
  located through the ledger's ``transcript_path`` fields or scanned
  from a ``--transcripts`` directory (which can only backfill what
  Claude Code still keeps). The format is internal to Claude Code and
  may change between releases, so it is parsed tolerantly — a line that
  does not look as expected is skipped, never raised on — and only
  enriches a run with turn counts, repeated-call detection, and
  precommit-run counts. Claude Code deletes transcripts after
  ``cleanupPeriodDays`` (30 by default).
- ``code_health/agent_transcript_digest.jsonl`` — this reporter's own
  per-run digest of what it read from each transcript, one JSON line
  per new or changed run, kept beside the ledger. A live transcript
  always wins; the digest stands in once the transcript is gone, so
  loop and turn signals survive Claude Code's sweep.

It is a reporter (always exits ``0``) whose only writes are its own
ledgers: each run appends one summary line to
``code_health/agent_profile_history.log`` so trends survive after the
per-run output scrolls away, and refreshes the transcript digest;
``--no-history`` skips both writes. ``/perf`` reads it as one of its
surfaces; ``/report-to-forge`` quotes it as evidence.

Usage:

- ``forge-agent-profile`` — report from the workspace ledger.
- ``forge-agent-profile --agent-type forge:precommit-fixer --last 20``
- ``forge-agent-profile --transcripts ~/.claude/projects/<proj>``
- ``forge-agent-profile --json`` — machine-readable runs + summary.
- ``forge-agent-profile --history`` — render the append-only ledger.
- ``forge-agent-profile --edits`` — which uncommitted files a subagent wrote.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from forge.git_utils import (
    code_health_dir,
    configure_cli_logging,
    diff_names_z,
    git_z_names,
    repo_root,
)
from forge.ledger import append_jsonl_record, append_ledger_line, parse_ledger
from forge.text_clip import truncate


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


configure_cli_logging()
logger = logging.getLogger(__name__)

LEDGER_NAME = "agent_timing.jsonl"
HISTORY_NAME = "agent_profile_history.log"
DIGEST_NAME = "agent_transcript_digest.jsonl"

#: A gap between two consecutive records longer than this is idle time
#: (a sleeping monitor, a human away, a laptop lid) — it counts toward
#: wall time but not active time.
IDLE_GAP_S = 300.0
#: Identical tool call (name + input) repeated at least this many times
#: inside one prompt segment marks a run as a loop suspect.
LOOP_REPEAT_THRESHOLD = 4
#: ``agents/precommit-fixer.md`` caps full ``forge-precommit`` runs.
PRECOMMIT_RUN_CAP = 3
PRECOMMIT_FIXER_AGENT = "forge:precommit-fixer"
MAIN_SESSION_TYPE = "(main session)"

_PRECOMMIT_FULL_RUN_RE = re.compile(r"\bforge-precommit\b(?![^\n]*--only)")
# Claude Code's subagent transcript shape: <session>/subagents/agent-<id>.jsonl.
# Every glob, parse and construction goes through the helpers below, so a
# change to that shape is a change in one place.
_SUBAGENT_DIR = "subagents"
_TRANSCRIPT_PREFIX = "agent-"
_TRANSCRIPT_SUFFIX = ".jsonl"


def _subagent_transcript_name(agent_id: str) -> str:
    """Return the transcript file name for *agent_id*.

    Args:
        agent_id: The subagent's id (``*`` builds the glob).

    Returns:
        ``agent-<id>.jsonl``.
    """
    return f"{_TRANSCRIPT_PREFIX}{agent_id}{_TRANSCRIPT_SUFFIX}"


def _agent_id_from_transcript(path: Path) -> str | None:
    """Return the agent id a subagent transcript path names, if it is one.

    Args:
        path: A candidate transcript path.

    Returns:
        The id, or ``None`` when the name or its directory is not the
        subagent transcript shape.
    """
    name = path.name
    if path.parent.name != _SUBAGENT_DIR or not (
        name.startswith(_TRANSCRIPT_PREFIX) and name.endswith(_TRANSCRIPT_SUFFIX)
    ):
        return None
    agent_id = name[len(_TRANSCRIPT_PREFIX) : -len(_TRANSCRIPT_SUFFIX)]
    return agent_id or None


_SUBAGENT_GLOB = f"*/{_SUBAGENT_DIR}/{_subagent_transcript_name('*')}"


def _resolve_subagent_transcript(
    path: str | None, *, session_id: str | None, agent_id: str
) -> Path | None:
    """The transcript belonging to *agent_id*, or ``None`` when none is readable.

    Claude Code records the *parent session's* transcript on a subagent's
    stop event rather than the subagent's own, so a ledger path says where
    the session wrote — never whose turns the file holds. Reading it
    unchecked charges one session's turns, tool calls and repeated commands
    to every agent that ran beneath it, which is why the name is required
    to carry the agent's own id before the file is trusted.

    Both ids arrive from hook payloads and are spliced into a path, so
    each must be a single path component: an absolute *session_id* would
    discard the recorded directory entirely (``Path("/a") / "/etc"`` is
    ``/etc``), and either id could climb out with ``..``.

    Args:
        path: The ``transcript_path`` the ledger recorded.
        session_id: The run's session, naming the subagent directory.
        agent_id: The agent whose transcript is wanted.

    Returns:
        A readable transcript belonging to *agent_id*, or ``None``.
    """
    if not path or not _is_path_component(agent_id):
        return None
    given = Path(path)
    if _agent_id_from_transcript(given) == agent_id:
        return given if given.is_file() else None
    if not _is_path_component(session_id):
        return None
    derived = (
        given.parent / session_id / _SUBAGENT_DIR / _subagent_transcript_name(agent_id)
    )
    return derived if derived.is_file() else None


def _is_path_component(value: str | None) -> TypeGuard[str]:
    """Whether *value* is safe to splice into a path as one segment.

    Args:
        value: The candidate segment.

    Returns:
        ``True`` when *value* names exactly one ordinary path segment.
    """
    return bool(value) and value not in {".", ".."} and not set(value) & {"/", "\\"}


@dataclass
class TranscriptStats:
    """What a subagent transcript adds to a run's ledger record."""

    started: datetime | None = None
    ended: datetime | None = None
    active_s: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    max_repeat: int = 0
    repeated_call: str = ""
    precommit_runs: int = 0
    output_tokens: int = 0


@dataclass
class AgentRun:
    """One agent invocation, assembled from the ledger and its transcript."""

    agent_id: str
    agent_type: str
    session_id: str | None = None
    started: datetime | None = None
    ended: datetime | None = None
    transcript_path: str | None = None
    description: str | None = None
    tool_calls: int = 0
    tool_ms: int = 0
    slowest_tool: str = ""
    slowest_tool_ms: int = 0
    ledger_active_s: float = 0.0
    ledger_precommit_runs: int = 0
    stats: TranscriptStats | None = None
    last_event: datetime | None = field(default=None, repr=False)

    @property
    def wall_s(self) -> float:
        """Seconds from first to last known record, or ``0`` when unbounded."""
        start = self.started or (self.stats.started if self.stats else None)
        end = (
            self.ended or self.last_event or (self.stats.ended if self.stats else None)
        )
        if start is None or end is None:
            return 0.0
        return max((end - start).total_seconds(), 0.0)

    @property
    def active_s(self) -> float:
        """Wall time with every gap longer than :data:`IDLE_GAP_S` clipped."""
        if self.stats is not None and self.stats.active_s:
            return self.stats.active_s
        return self.ledger_active_s

    @property
    def loop_suspect(self) -> bool:
        """True when one identical tool call recurs past the threshold."""
        return self.stats is not None and self.stats.max_repeat >= LOOP_REPEAT_THRESHOLD

    @property
    def precommit_runs(self) -> int:
        """Full ``forge-precommit`` runs: the ledger's count, or the transcript's.

        The ``precommit_full_run`` ledger events (written by the
        ``block_fixer_recon`` hook as it enforces the cap) are the primary
        source; the transcript regex covers history recorded before that
        hook existed. The larger count wins so neither source can hide a
        breach the other saw.
        """
        from_transcript = self.stats.precommit_runs if self.stats is not None else 0
        return max(self.ledger_precommit_runs, from_transcript)

    @property
    def cap_breach(self) -> bool:
        """True for a precommit-fixer run past its full-run cap."""
        return (
            self.agent_type == PRECOMMIT_FIXER_AGENT
            and self.precommit_runs > PRECOMMIT_RUN_CAP
        )


@dataclass(frozen=True)
class TypeRow:
    """Per-agent-type aggregate for the report table."""

    agent_type: str
    runs: int
    mean_s: float
    p50_s: float
    max_s: float
    wall_s: float
    active_s: float


@dataclass(frozen=True)
class ToolRow:
    """Per-tool aggregate from ``PostToolUse`` durations."""

    tool_name: str
    calls: int
    total_ms: int
    max_ms: int


# --- parsing ---------------------------------------------------------------


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects in *path*, skipping lines that are not one.

    Args:
        path: File to read, one JSON object per line.

    Yields:
        Each line's parsed object, in file order.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


MUTATING_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})


@dataclass(frozen=True)
class EditReceipt:
    """Which working-tree files a subagent wrote, rather than the caller.

    An edit is indistinguishable from the caller's own once it is in the
    tree, so the caller commits it as theirs. This answers that from the
    hook ledger instead of from an agent's self-report.

    ``known`` is the load-bearing field. An absent or empty ledger means
    the evidence is missing, NOT that no subagent wrote anything — a
    fresh clone, ``FORGE_NO_AGENT_TIMING=1`` and a machine without
    ``jq`` all produce one, and reading that as "clean" is the
    absence-of-evidence error this receipt exists to prevent.

    Attributes:
        known: Whether the ledger could be read at all.
        reason: Why it could not, when ``known`` is false.
        by_file: Repo-relative path → the agent types that wrote it, or
            ``None`` exactly when ``known`` is false — so an unknown
            receipt cannot be read as an empty, clean one.
        pathless: Subagent writes seen with no recorded path. Rows
            written by a hook older than path recording all look like
            this, so a zero ``by_file`` beside a non-zero count means
            "cannot tell", not "nothing happened".
    """

    known: bool
    reason: str = ""
    by_file: dict[str, set[str]] | None = None
    pathless: int = 0

    def __post_init__(self) -> None:
        """Refuse a receipt whose ``by_file`` disagrees with ``known``.

        Raises:
            ValueError: When ``by_file`` is ``None`` for a known receipt,
                or set for an unknown one.
        """
        if self.known != (self.by_file is not None):
            msg = "EditReceipt.by_file must be None exactly when known is false"
            raise ValueError(msg)


def subagent_edits(
    root: Path,
    *,
    since: datetime | None = None,
    session_id: str | None = None,
    paths: Iterable[str] | None = None,
) -> EditReceipt:
    """Read the hook ledger for files a subagent wrote.

    Rows with no ``agent_type`` are the main session's own work and are
    excluded — the question is what someone else changed. Only
    tool-based writes carry a path, so anything an agent writes through
    a shell command is invisible here; the result is partial evidence by
    construction and callers must not present it as an audit.

    Args:
        root: Repository root.
        since: Ignore rows older than this instant. Wall-clock based, so
            it assumes whatever produced the cutoff and whatever stamped
            the ledger agree; under backward skew a genuinely later write
            compares as earlier and is dropped, which is why the receipt
            does not scope by time.
        session_id: Restrict to one session. The ledger is shared across
            sessions and worktrees in a clone, so without this a
            parallel run's edits read as this one's.
        paths: Restrict to these repo-relative paths, e.g. the files
            about to be committed.

    Returns:
        The receipt. Check ``known`` before reading ``by_file``.
    """
    ledger = code_health_dir(root) / LEDGER_NAME
    if not ledger.is_file():
        return EditReceipt(known=False, reason=f"no ledger at {ledger}")
    wanted = set(paths) if paths is not None else None
    by_file: dict[str, set[str]] = defaultdict(set)
    seen_any = False
    pathless = 0
    for event in _iter_jsonl(ledger):
        seen_any = True
        agent = (event.get("agent_type") or "").strip()
        if not agent or event.get("tool_name") not in MUTATING_TOOLS:
            continue
        if session_id and event.get("session_id") != session_id:
            continue
        when = _parse_ts(event.get("ts"))
        if since is not None and when is not None and when < since:
            continue
        raw = event.get("file_path")
        if not isinstance(raw, str) or not raw:
            # A write whose path the hook never recorded. Counted, not
            # dropped: a hook older than path recording makes EVERY row
            # look like this, and silently returning an empty result
            # would report "no subagent edits" for a tree full of them.
            pathless += 1
            continue
        rel = _relative_to_root(raw, root)
        # Match on the path as recorded; only the displayed label is
        # sanitized. Matching the label dropped any name the label
        # changes (" lead space.py" strips to "lead space.py").
        if wanted is not None and rel not in wanted:
            continue
        by_file[_safe_label(rel)].add(agent)
    if not seen_any:
        return EditReceipt(known=False, reason="ledger is empty")
    if not by_file and pathless:
        return EditReceipt(
            known=False,
            reason=(
                f"{pathless} subagent write(s) recorded without a file path — "
                "the running hook predates path recording; run "
                "`forge-plugin-sync`, then restart the Claude Code session"
            ),
            pathless=pathless,
        )
    return EditReceipt(known=True, by_file=dict(by_file), pathless=pathless)


PATH_LABEL_CAP = 200


def _safe_label(raw: str) -> str:
    """Render an untrusted path as one bounded, single-line label.

    The value is whatever the calling tool declared as its target — a
    string an agent chooses, not a verified filesystem fact — and it is
    reproduced verbatim into a hand-back and from there into a published
    PR comment. So the agent this feature exists to catch is also the one
    supplying the text: newlines could forge extra receipt lines, control
    characters could corrupt a terminal, and prose could address whoever
    reads the wrap-up. Collapse and cap it (§8, generated text is
    behavior).

    Args:
        raw: Path string as recorded.

    Returns:
        A single-line label of at most :data:`PATH_LABEL_CAP` + 1
        characters (the cap plus a one-character ellipsis when cut).
    """
    flat = "".join(ch if ch.isprintable() else " " for ch in raw).strip()
    return truncate(flat, PATH_LABEL_CAP) or "<empty>"


def _relative_to_root(raw: str, root: Path) -> str:
    """Return *raw* relative to *root*, marked when it is not under it.

    A path that will not relativise is either outside the repository or
    unresolvable. Both fall back to the raw string, so mark them: a
    reviewer skimming the receipt should be able to tell an ordinary
    repo file from one an agent wrote somewhere it had no business
    writing.

    Args:
        raw: Path as the hook recorded it, usually absolute.
        root: Repository root.

    Returns:
        The repo-relative form, else the label prefixed with ``!``.
    """
    try:
        return str(Path(raw).resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return f"!{_safe_label(raw)}"


def render_edit_receipt(receipt: EditReceipt) -> str:
    """Render the receipt as the line an agent hands back.

    Args:
        receipt: The receipt to render.

    Returns:
        One line when the evidence is missing, one when nothing
        qualifies, else a header and a line per file.
    """
    by_file = receipt.by_file
    if by_file is None:
        return (
            f"subagent edits: UNKNOWN ({receipt.reason}) — "
            "absent evidence is not evidence of absence; "
            "check by hand before committing."
        )
    if not by_file:
        return "subagent edits: none recorded (tool-based writes only)."
    head = "subagent edits (tool-based writes only — shell writes are invisible):"
    if receipt.pathless:
        head = (
            f"subagent edits — INCOMPLETE, {receipt.pathless} further write(s) "
            "have no recorded path:"
        )
    lines = [head]
    # Fenced: this line is reproduced into a markdown PR comment, and the
    # path is agent-supplied. A code span renders it literally instead of
    # letting it style, link, or pose as structure around it.
    lines.extend(
        f"  `{path}` — {', '.join(sorted(agents))}"
        for path, agents in sorted(by_file.items())
    )
    return "\n".join(lines)


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` suffix accepted); ``None`` if not one.

    Args:
        value: The raw field to parse.

    Returns:
        The timezone-aware ``datetime``, or ``None`` when *value* isn't a
        parseable ISO-8601 string.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _event_ts(event: dict[str, Any]) -> datetime | None:
    """A ledger event's timestamp, preferring the millisecond field.

    Args:
        event: One ledger event.

    Returns:
        The event's timestamp, or ``None`` when neither field parses.
    """
    ms = event.get("ts_ms")
    if isinstance(ms, int | float):
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    return _parse_ts(event.get("ts"))


def _clipped_gap(previous: datetime | None, current: datetime) -> float:
    """Seconds between two records, clipped at :data:`IDLE_GAP_S`.

    Args:
        previous: The prior record's timestamp, or ``None`` for the first.
        current: The current record's timestamp.

    Returns:
        The clipped, non-negative gap in seconds.
    """
    if previous is None:
        return 0.0
    return min(max((current - previous).total_seconds(), 0.0), IDLE_GAP_S)


def _run_key(event: dict[str, Any]) -> tuple[str, str]:
    """``(agent_id, agent_type)`` for an event; main-session tools get a pseudo id.

    Args:
        event: One ledger event.

    Returns:
        The run's ``(agent_id, agent_type)`` key.
    """
    agent_id = event.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        agent_type = event.get("agent_type")
        return agent_id, agent_type if isinstance(agent_type, str) else ""
    return f"main:{event.get('session_id') or '?'}", MAIN_SESSION_TYPE


def _apply_event(runs: dict[str, AgentRun], event: dict[str, Any]) -> None:
    """Fold one ledger event into *runs*.

    Args:
        runs: Runs accumulated so far, keyed by ``agent_id``; updated in place.
        event: The ledger event to fold in.
    """
    ts = _event_ts(event)
    if ts is None:
        return
    agent_id, agent_type = _run_key(event)
    run = runs.setdefault(agent_id, AgentRun(agent_id=agent_id, agent_type=agent_type))
    if agent_type and not run.agent_type:
        run.agent_type = agent_type
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id and not run.session_id:
        run.session_id = session_id
    run.ledger_active_s += _clipped_gap(run.last_event, ts)
    run.last_event = ts
    if run.started is None:
        run.started = ts
    kind = event.get("event")
    if kind == "SubagentStop":
        run.ended = ts
        path = event.get("transcript_path")
        if isinstance(path, str) and path:
            run.transcript_path = path
    elif kind == "PostToolUse":
        _apply_tool_event(run, event)
    elif kind == "precommit_full_run":
        # Written by block_fixer_recon.sh as it allows a full run; the
        # SubagentStart that carries agent_type always precedes it, so
        # the run's type is already known here.
        run.ledger_precommit_runs += 1


def _apply_tool_event(run: AgentRun, event: dict[str, Any]) -> None:
    """Accumulate one ``PostToolUse`` event's duration into *run*.

    Args:
        run: The run to update in place.
        event: The ``PostToolUse`` ledger event.
    """
    run.tool_calls += 1
    duration = event.get("duration_ms")
    if not isinstance(duration, int | float):
        return
    ms = int(duration)
    run.tool_ms += ms
    if ms > run.slowest_tool_ms:
        run.slowest_tool_ms = ms
        run.slowest_tool = str(event.get("tool_name") or "?")


def runs_from_ledger(events: Iterable[dict[str, Any]]) -> dict[str, AgentRun]:
    """Pair ledger events into runs keyed by agent id.

    A run without a ``SubagentStop`` (still running, or the session died)
    has no ``ended``; its last event stands in as the end — a lower bound
    beats a dropped run.

    Args:
        events: Ledger events in file order.

    Returns:
        Runs keyed by ``agent_id`` (main-session tool activity under a
        ``main:<session_id>`` pseudo id).
    """
    runs: dict[str, AgentRun] = {}
    for event in events:
        _apply_event(runs, event)
    return runs


def tool_rows(events: Iterable[dict[str, Any]]) -> list[ToolRow]:
    """Aggregate ``PostToolUse`` durations per tool name, costliest first.

    Args:
        events: Ledger events.

    Returns:
        One row per tool name seen with a duration, sorted by total time.
    """
    calls: Counter[str] = Counter()
    total: Counter[str] = Counter()
    peak: dict[str, int] = defaultdict(int)
    for event in events:
        if event.get("event") != "PostToolUse":
            continue
        duration = event.get("duration_ms")
        if not isinstance(duration, int | float):
            continue
        name = str(event.get("tool_name") or "?")
        calls[name] += 1
        total[name] += int(duration)
        peak[name] = max(peak[name], int(duration))
    return sorted(
        (ToolRow(name, calls[name], total[name], peak[name]) for name in calls),
        key=lambda row: -row.total_ms,
    )


def _content_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The content blocks of a transcript record's message, if it has any.

    Args:
        record: One transcript record.

    Returns:
        The record's message content blocks, or an empty list when absent.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _is_resume(record: dict[str, Any]) -> bool:
    """True for a user record carrying a new prompt rather than tool results.

    Args:
        record: One transcript record.

    Returns:
        ``True`` when *record* is a user prompt, not a tool result.
    """
    if record.get("type") != "user":
        return False
    blocks = _content_blocks(record)
    return not any(block.get("type") == "tool_result" for block in blocks)


def _call_signature(block: dict[str, Any]) -> str:
    """Identity of a tool call: its name plus its canonicalised input.

    Args:
        block: A ``tool_use`` content block.

    Returns:
        A string identifying repeats of the same call.
    """
    payload = json.dumps(block.get("input"), sort_keys=True, default=str)
    return f"{block.get('name')}:{payload}"


def _describe_call(block: dict[str, Any]) -> str:
    """Human label for a tool call, trimmed for a report line.

    Args:
        block: A ``tool_use`` content block.

    Returns:
        The tool name plus its command/file-path detail, truncated to 72 chars.
    """
    tool_input = block.get("input")
    detail = ""
    if isinstance(tool_input, dict):
        detail = str(tool_input.get("command") or tool_input.get("file_path") or "")
    return _call_label(f"{block.get('name')}: {detail}".strip(": "))


CALL_LABEL_CAP = 72


def _call_label(text: str) -> str:
    """Flatten *text* to printable characters and cap it at the label width.

    Applied both when a label is built from a transcript and when one is
    read back from the digest: the digest is a writable file, so a label
    loaded from it is as untrusted as the command it came from, and a
    line break or a long run of text would otherwise reach the report.

    Args:
        text: The raw label.

    Returns:
        *text* with every non-printable character turned into a space,
        cut to :data:`CALL_LABEL_CAP` characters.
    """
    flat = "".join(ch if ch.isprintable() else " " for ch in text)
    return flat[:CALL_LABEL_CAP]


def parse_transcript(path: Path) -> TranscriptStats | None:
    """Extract timing and loop signals from one subagent transcript.

    Repeated-call and precommit-run counts are taken per **prompt
    segment** — a new user prompt resets them — because an agent
    resumed with a follow-up message legitimately re-runs ``git
    status``; only repetition inside a single prompt suggests a loop.

    Args:
        path: The ``agent-<id>.jsonl`` transcript.

    Returns:
        The stats, or ``None`` when the file holds no timestamped record.
    """
    stats = TranscriptStats()
    previous: datetime | None = None
    signatures: Counter[str] = Counter()
    labels: dict[str, str] = {}
    precommit_in_segment = 0
    for record in _iter_jsonl(path):
        ts = _parse_ts(record.get("timestamp"))
        if ts is None:
            continue
        stats.started = stats.started or ts
        stats.ended = ts
        stats.active_s += _clipped_gap(previous, ts)
        previous = ts
        if _is_resume(record):
            stats.precommit_runs = max(stats.precommit_runs, precommit_in_segment)
            precommit_in_segment = 0
            signatures.clear()
            continue
        if record.get("type") != "assistant":
            continue
        stats.turns += 1
        stats.output_tokens += _output_tokens(record)
        for block in _content_blocks(record):
            if block.get("type") != "tool_use":
                continue
            stats.tool_calls += 1
            signature = _call_signature(block)
            signatures[signature] += 1
            labels.setdefault(signature, _describe_call(block))
            if signatures[signature] > stats.max_repeat:
                stats.max_repeat = signatures[signature]
                stats.repeated_call = labels[signature]
            precommit_in_segment += _is_full_precommit_run(block)
    stats.precommit_runs = max(stats.precommit_runs, precommit_in_segment)
    return stats if stats.started is not None else None


def _output_tokens(record: dict[str, Any]) -> int:
    """Output tokens reported on an assistant record, ``0`` when absent.

    Args:
        record: One assistant transcript record.

    Returns:
        The reported output-token count, or ``0`` when missing.
    """
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    tokens = usage.get("output_tokens") if isinstance(usage, dict) else 0
    return tokens if isinstance(tokens, int) else 0


def _is_full_precommit_run(block: dict[str, Any]) -> int:
    """``1`` when a ``Bash`` tool call runs ``forge-precommit`` without ``--only``.

    Args:
        block: A ``tool_use`` content block.

    Returns:
        ``1`` for a full ``forge-precommit`` run, ``0`` otherwise.
    """
    tool_input = block.get("input")
    if block.get("name") != "Bash" or not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command")
    return int(
        isinstance(command, str) and bool(_PRECOMMIT_FULL_RUN_RE.search(command))
    )


# --- discovery -------------------------------------------------------------


def _agent_types_from_sessions(root: Path) -> dict[str, tuple[str, str | None]]:
    """Map subagent ids to ``(agent_type, description)`` from main-session transcripts.

    The ``Agent`` tool's result record names the spawned ``agentId``; its
    ``tool_use`` block names the ``subagent_type``. Pairing the two gives
    the type for transcripts that predate the hook ledger.

    Args:
        root: A Claude Code project directory holding main-session transcripts.

    Returns:
        Agent id to ``(agent_type, description)``, for ids found this way.
    """
    pending: dict[str, tuple[str, str | None]] = {}
    found: dict[str, tuple[str, str | None]] = {}
    for session in root.glob("*.jsonl"):
        for record in _iter_jsonl(session):
            for block in _content_blocks(record):
                if block.get("type") == "tool_use" and block.get("name") in {
                    "Agent",
                    "Task",
                }:
                    tool_input = block.get("input") or {}
                    pending[str(block.get("id"))] = (
                        str(tool_input.get("subagent_type") or "general-purpose"),
                        tool_input.get("description"),
                    )
            result = record.get("toolUseResult")
            if not isinstance(result, dict):
                continue
            agent_id = result.get("agentId")
            for block in _content_blocks(record):
                use_id = str(block.get("tool_use_id"))
                if (
                    block.get("type") == "tool_result"
                    and use_id in pending
                    and agent_id
                ):
                    found[str(agent_id)] = pending[use_id]
    return found


def runs_from_transcripts(root: Path) -> dict[str, AgentRun]:
    """Build runs from every subagent transcript under *root*.

    Args:
        root: A Claude Code project directory
            (``~/.claude/projects/<encoded-cwd>``).

    Returns:
        Runs keyed by agent id; the type is ``"?"`` when no main-session
        transcript names it.
    """
    types = _agent_types_from_sessions(root)
    runs: dict[str, AgentRun] = {}
    for path in root.glob(_SUBAGENT_GLOB):
        agent_id = _agent_id_from_transcript(path)
        if agent_id is None:
            continue
        stats = parse_transcript(path)
        if stats is None:
            continue
        agent_type, description = types.get(agent_id, ("?", None))
        runs[agent_id] = AgentRun(
            agent_id=agent_id,
            agent_type=agent_type,
            transcript_path=str(path),
            description=description,
            stats=stats,
        )
    return runs


# --- transcript digest -----------------------------------------------------

_DIGEST_COUNTS = (
    "turns",
    "tool_calls",
    "max_repeat",
    "precommit_runs",
    "output_tokens",
)


def _digest_record(run: AgentRun, stats: TranscriptStats) -> dict[str, object]:
    """One digest line for *run*: its ids, its transcript stats, and when.

    Args:
        run: The run the stats belong to.
        stats: The stats parsed from the run's live transcript.

    Returns:
        A JSON-serializable record (datetimes as ISO strings).
    """
    record: dict[str, object] = {"agent_id": run.agent_id, "session_id": run.session_id}
    record.update(asdict(stats))
    record["started"] = _iso(stats.started)
    record["ended"] = _iso(stats.ended)
    record["digested_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return record


def _is_count(value: object) -> bool:
    """Whether *value* is a JSON integer (``bool`` excluded).

    Args:
        value: A decoded JSON value.

    Returns:
        ``True`` for an ``int`` that is not a ``bool``.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _stats_from_digest(record: dict[str, Any]) -> TranscriptStats | None:
    """Rebuild the stats one digest line carries, ``None`` when it is damaged.

    A line with a missing, mistyped or non-finite field is rejected whole
    rather than filled with defaults: a digest stands in for a transcript
    nobody can re-read, so a half-trusted line would report numbers no
    source holds. ``json`` accepts ``NaN`` and ``Infinity``, hence the
    finiteness check; the label is re-flattened because the file is
    writable by anyone with the checkout.

    Args:
        record: One decoded digest line.

    Returns:
        The stats, or ``None`` when any field is absent, the wrong type,
        or not a finite number.
    """
    active_s = record.get("active_s")
    started = _parse_ts(record.get("started"))
    if (
        started is None
        or not all(_is_count(record.get(name)) for name in _DIGEST_COUNTS)
        or not isinstance(active_s, int | float)
        or isinstance(active_s, bool)
        or not math.isfinite(active_s)
        or not isinstance(record.get("repeated_call"), str)
    ):
        return None
    return TranscriptStats(
        started=started,
        ended=_parse_ts(record.get("ended")),
        active_s=float(active_s),
        repeated_call=_call_label(record["repeated_call"]),
        **{name: record[name] for name in _DIGEST_COUNTS},
    )


def _load_digest(path: Path) -> dict[str, TranscriptStats]:
    """Read the transcript digest: the last good line per agent id wins.

    Args:
        path: The digest file (may not exist yet).

    Returns:
        Agent id to its most recently digested stats; damaged lines skipped.
    """
    digest: dict[str, TranscriptStats] = {}
    for record in _iter_jsonl(path):
        agent_id = record.get("agent_id")
        stats = _stats_from_digest(record)
        if isinstance(agent_id, str) and agent_id and stats is not None:
            digest[agent_id] = stats
    return digest


def _digest_is_behind(stats: TranscriptStats, known: TranscriptStats | None) -> bool:
    """Whether *stats* says more than the digest already holds for its run.

    A resumed agent's transcript keeps growing, so a later end or more
    turns is a change worth a new line; anything else would only repeat
    the last one on every report.

    Args:
        stats: Stats just parsed from a live transcript.
        known: The digest's stats for the same agent id, if any.

    Returns:
        ``True`` when the run is new to the digest or has grown since.
    """
    if known is None or stats.turns > known.turns:
        return True
    if stats.ended is None:
        return False
    return known.ended is None or stats.ended > known.ended


def _record_digest(
    path: Path, runs: Iterable[AgentRun], digest: dict[str, TranscriptStats]
) -> None:
    """Append a digest line for every run whose stats are new or have grown.

    A write failure is logged, never raised: the digest is enrichment
    and the reporter must still exit ``0``.

    Args:
        path: The digest file.
        runs: Runs with their final stats.
        digest: What the digest held before this report.
    """
    try:
        for run in runs:
            stats = run.stats
            if stats is not None and _digest_is_behind(stats, digest.get(run.agent_id)):
                append_jsonl_record(path, _digest_record(run, stats))
    except OSError as exc:
        logger.warning("could not update the transcript digest at %s: %s", path, exc)


def collect_runs(
    ledger_path: Path, transcripts_root: Path | None, *, record_digest: bool = True
) -> list[AgentRun]:
    """Assemble every known run: ledger first, transcripts as enrichment or backfill.

    A live transcript is always preferred — a resumed agent's keeps
    growing — and the digest beside the ledger
    (:data:`DIGEST_NAME`) fills in only the runs whose transcript no
    longer resolves, so their loop and turn signals outlive Claude
    Code's transcript sweep. A run with neither keeps ``stats`` at
    ``None``: no borrowed numbers.

    Args:
        ledger_path: The hook ledger (may not exist yet).
        transcripts_root: Optional project directory to scan for
            transcripts the ledger does not name.
        record_digest: Append changed transcript stats to the digest.

    Returns:
        Runs ordered by start time, oldest first.
    """
    runs = runs_from_ledger(_iter_jsonl(ledger_path)) if ledger_path.is_file() else {}
    for run in runs.values():
        transcript = _resolve_subagent_transcript(
            run.transcript_path, session_id=run.session_id, agent_id=run.agent_id
        )
        if transcript is not None:
            run.stats = parse_transcript(transcript)
    if transcripts_root is not None:
        for agent_id, scanned in runs_from_transcripts(transcripts_root).items():
            known = runs.get(agent_id)
            if known is None:
                runs[agent_id] = scanned
            elif known.stats is None:
                # A ledger run whose transcript the ledger misnamed keeps its
                # timings; only the transcript half was missing.
                known.stats = scanned.stats
                known.transcript_path = scanned.transcript_path
    digest_path = ledger_path.parent / DIGEST_NAME
    digest = _load_digest(digest_path)
    for run in runs.values():
        if run.stats is None:
            run.stats = digest.get(run.agent_id)
    if record_digest:
        _record_digest(digest_path, runs.values(), digest)
    return sorted(runs.values(), key=_start_of)


def _start_of(run: AgentRun) -> datetime:
    """A run's best-known start, for ordering (unknown sorts first).

    Args:
        run: The run to order.

    Returns:
        The run's start time, falling back to its transcript's or the
        minimum ``datetime`` when neither is known.
    """
    transcript_start = run.stats.started if run.stats else None
    return run.started or transcript_start or datetime.min.replace(tzinfo=UTC)


# --- selection + summary ---------------------------------------------------


def filter_runs(
    runs: list[AgentRun],
    *,
    agent_type: str | None = None,
    since: datetime | None = None,
    last: int | None = None,
) -> list[AgentRun]:
    """Narrow *runs* by type, start time, and recency.

    Args:
        runs: Runs, oldest first.
        agent_type: Keep only this agent type.
        since: Keep runs starting at or after this instant.
        last: Keep only the most recent *n* runs (after the other filters).

    Returns:
        The surviving runs, still oldest first.
    """
    kept = runs
    if agent_type:
        kept = [r for r in kept if r.agent_type == agent_type]
    if since is not None:
        kept = [r for r in kept if _start_of(r) >= since or _start_of(r).year == 1]
    if last is not None:
        kept = kept[-last:]
    return kept


def type_rows(runs: Iterable[AgentRun]) -> list[TypeRow]:
    """Aggregate wall time per agent type, costliest total first.

    Args:
        runs: Runs to aggregate.

    Returns:
        One row per agent type.
    """
    by_type: dict[str, list[AgentRun]] = defaultdict(list)
    for run in runs:
        by_type[run.agent_type or "?"].append(run)
    rows = []
    for agent_type, group in by_type.items():
        walls = sorted(r.wall_s for r in group)
        rows.append(
            TypeRow(
                agent_type=agent_type,
                runs=len(group),
                mean_s=statistics.fmean(walls),
                p50_s=statistics.median(walls),
                max_s=walls[-1],
                wall_s=sum(walls),
                active_s=sum(r.active_s for r in group),
            )
        )
    return sorted(rows, key=lambda row: -row.wall_s)


# --- rendering -------------------------------------------------------------


_MINUTE_S = 60
_HOUR_S = 3600


def _fmt_s(seconds: float) -> str:
    """Compact duration: ``42s``, ``3m10s``, ``2h05m``.

    Args:
        seconds: Duration in seconds.

    Returns:
        The formatted duration.
    """
    total = round(seconds)
    if total < _MINUTE_S:
        return f"{total}s"
    if total < _HOUR_S:
        return f"{total // _MINUTE_S}m{total % _MINUTE_S:02d}s"
    return f"{total // _HOUR_S}h{(total % _HOUR_S) // _MINUTE_S:02d}m"


def _run_label(run: AgentRun) -> str:
    """One report line's identity for a run: type + description or id.

    Args:
        run: The run to label.

    Returns:
        The label, truncated to 80 characters.
    """
    tail = run.description or run.agent_id
    return f"{run.agent_type or '?'}  {tail}"[:80]


def render_report(runs: list[AgentRun], tools: list[ToolRow], *, top: int) -> str:
    """Render the human report.

    Args:
        runs: Runs to report (already filtered).
        tools: Per-tool aggregates from the ledger.
        top: How many slowest runs to list.

    Returns:
        The report text.
    """
    if not runs:
        return (
            "No agent runs found — the log_agent_timing hook writes "
            "code_health/agent_timing.jsonl as agents run."
        )
    lines = [f"Agent profile — {len(runs)} run(s)", ""]
    lines += _render_type_table(runs)
    lines += _render_slowest(runs, top)
    lines += _render_tools(tools)
    lines += _render_suspects(runs)
    return "\n".join(lines)


def _render_type_table(runs: list[AgentRun]) -> list[str]:
    """Per-type table plus the wall/active totals line.

    Args:
        runs: Runs to summarize.

    Returns:
        The table's report lines, including a trailing blank line.
    """
    header = (
        f"{'agent type':<34} {'runs':>5} {'mean':>7} {'p50':>7} "
        f"{'max':>7} {'wall':>8} {'active':>8}"
    )
    lines = ["Per agent type (sorted by total wall time):", header]
    lines.extend(
        f"{row.agent_type[:34]:<34} {row.runs:>5} {_fmt_s(row.mean_s):>7} "
        f"{_fmt_s(row.p50_s):>7} {_fmt_s(row.max_s):>7} {_fmt_s(row.wall_s):>8} "
        f"{_fmt_s(row.active_s):>8}"
        for row in type_rows(runs)
    )
    wall = sum(r.wall_s for r in runs)
    active = sum(r.active_s for r in runs)
    lines.append(
        f"total wall {_fmt_s(wall)}, active {_fmt_s(active)} "
        f"(gaps over {int(IDLE_GAP_S)}s count as idle)"
    )
    return [*lines, ""]


def _render_slowest(runs: list[AgentRun], top: int) -> list[str]:
    """The *top* slowest runs by wall time.

    Args:
        runs: Runs to consider.
        top: How many of the slowest runs to list.

    Returns:
        The section's report lines, including a trailing blank line.
    """
    lines = [f"Slowest {min(top, len(runs))} run(s):"]
    for run in sorted(runs, key=lambda r: -r.wall_s)[:top]:
        turns = run.stats.turns if run.stats else 0
        tools = run.tool_calls or (run.stats.tool_calls if run.stats else 0)
        tool_note = ""
        if run.slowest_tool:
            tool_note = (
                f", slowest tool {run.slowest_tool} "
                f"{_fmt_s(run.slowest_tool_ms / 1000)}"
            )
        lines.append(
            f"  {_fmt_s(run.wall_s):>7} wall {_fmt_s(run.active_s):>7} active  "
            f"turns={turns:<3} tools={tools:<3} {_run_label(run)}{tool_note}"
        )
    return [*lines, ""]


def _render_tools(tools: list[ToolRow]) -> list[str]:
    """Per-tool cost table (ledger ``PostToolUse`` durations only).

    Args:
        tools: Per-tool aggregates, costliest first.

    Returns:
        The table's report lines, or an empty list when *tools* is empty.
    """
    if not tools:
        return []
    lines = ["Tool time (from PostToolUse durations):"]
    lines.extend(
        f"  {row.tool_name:<16} calls={row.calls:<5} "
        f"total={_fmt_s(row.total_ms / 1000):>7} "
        f"max={_fmt_s(row.max_ms / 1000):>7}"
        for row in tools[:10]
    )
    return [*lines, ""]


def _render_suspects(runs: list[AgentRun]) -> list[str]:
    """Loop suspects and precommit-fixer cap breaches.

    Args:
        runs: Runs to scan for loop suspects and cap breaches.

    Returns:
        The section's report lines.
    """
    loops = [(r, r.stats) for r in runs if r.loop_suspect and r.stats is not None]
    lines = [
        (
            f"Loop suspects (identical call >={LOOP_REPEAT_THRESHOLD}x in one "
            f"prompt): {len(loops)}"
        )
    ]
    lines += [
        f"  {stats.max_repeat:>2}x {stats.repeated_call}  [{_run_label(run)}]"
        for run, stats in loops
    ]
    breaches = [r for r in runs if r.cap_breach]
    lines.append(
        f"{PRECOMMIT_FIXER_AGENT} runs past the {PRECOMMIT_RUN_CAP}-run cap: "
        f"{len(breaches)}"
    )
    lines += [
        f"  {run.precommit_runs} full forge-precommit runs  [{_run_label(run)}]"
        for run in breaches
    ]
    return lines


def _iso(value: object) -> str | None:
    """ISO-8601 text for a datetime, ``None`` for anything else.

    Args:
        value: Any object; checked if it is a datetime instance.

    Returns:
        ISO-8601 formatted string if value is a datetime, otherwise None.
    """
    return value.isoformat() if isinstance(value, datetime) else None


def _run_json(run: AgentRun) -> dict[str, Any]:
    """JSON-safe view of a run (datetimes as ISO strings, properties included).

    Args:
        run: The run to serialize.

    Returns:
        The run's fields as a JSON-serializable dict.
    """
    data: dict[str, Any] = {
        key: value for key, value in asdict(run).items() if key != "last_event"
    }
    data["started"] = _iso(run.started)
    data["ended"] = _iso(run.ended)
    if run.stats is not None:
        stats: dict[str, Any] = dict(data["stats"])
        stats["started"] = _iso(run.stats.started)
        stats["ended"] = _iso(run.stats.ended)
        data["stats"] = stats
    data.update(
        wall_s=round(run.wall_s, 1),
        active_s=round(run.active_s, 1),
        loop_suspect=run.loop_suspect,
        cap_breach=run.cap_breach,
    )
    return data


def render_json(runs: list[AgentRun], tools: list[ToolRow]) -> str:
    """Machine-readable report: runs, per-type rows, per-tool rows.

    Args:
        runs: Runs to report.
        tools: Per-tool aggregates.

    Returns:
        A JSON document.
    """
    return json.dumps(
        {
            "runs": [_run_json(r) for r in runs],
            "types": [asdict(row) for row in type_rows(runs)],
            "tools": [asdict(row) for row in tools],
        },
        indent=2,
    )


# --- history ---------------------------------------------------------------


def append_history(root: Path, runs: list[AgentRun], label: str) -> None:
    """Append one summary line for this report to the profile ledger.

    Args:
        root: Repository root.
        runs: The reported runs.
        label: Run label (``-`` when unlabeled).
    """
    append_ledger_line(
        code_health_dir(root) / HISTORY_NAME,
        {
            "label": label or "-",
            "runs": len(runs),
            "types": len({r.agent_type for r in runs}),
            "wall_s": f"{sum(r.wall_s for r in runs):.0f}",
            "active_s": f"{sum(r.active_s for r in runs):.0f}",
            "loop_suspects": sum(r.loop_suspect for r in runs),
            "cap_breaches": sum(r.cap_breach for r in runs),
        },
    )


def _render_history(root: Path) -> int:
    """Print the append-only profile ledger as a trend table.

    Args:
        root: Repository root.

    Returns:
        Process exit status, always ``0``.
    """
    path = code_health_dir(root) / HISTORY_NAME
    if not path.is_file():
        logger.info(
            "No agent-profile history at %s — run forge-agent-profile first.", path
        )
        return 0
    rows = parse_ledger(path.read_text(encoding="utf-8"))
    header = (
        f"{'ts':<20} {'label':<12} {'runs':>5} {'wall_s':>8} {'active_s':>9} "
        f"{'loops':>6} {'caps':>5}"
    )
    lines = [f"Agent-profile history ({len(rows)} report(s)):", header]
    lines += [
        f"{r.get('ts', '?'):<20} {r.get('label', '-'):<12} {r.get('runs', '?'):>5} "
        f"{r.get('wall_s', '?'):>8} {r.get('active_s', '?'):>9} "
        f"{r.get('loop_suspects', '?'):>6} {r.get('cap_breaches', '?'):>5}"
        for r in rows
    ]
    logger.info("%s", "\n".join(lines))
    return 0


# --- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface."""
    parser = argparse.ArgumentParser(
        prog="forge-agent-profile",
        description=(
            "Report where agent and subagent time goes, and which files a "
            "subagent wrote (writes only its own code_health/ ledgers, "
            "always exits 0)."
        ),
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        help=f"Hook ledger (default: {LEDGER_NAME} in the log directory — "
        "<repo>/code_health/, or $FORGE_CODE_HEALTH_DIR when set).",
    )
    parser.add_argument(
        "--transcripts",
        type=Path,
        help="Transcripts directory (~/.claude/projects/<encoded-cwd>) to scan "
        "for runs the ledger does not name; it backfills only what Claude "
        "Code still keeps.",
    )
    parser.add_argument(
        "--agent-type",
        help="Only runs of this agent type (e.g. forge:precommit-fixer).",
    )
    parser.add_argument(
        "--since", help="Only runs starting at or after this ISO-8601 instant."
    )
    parser.add_argument("--last", type=int, help="Only the N most recent runs.")
    parser.add_argument(
        "--top", type=int, default=10, help="Slowest runs to list (default 10)."
    )
    parser.add_argument("--label", default="", help="Label for the history line.")
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of the text report."
    )
    parser.add_argument(
        "--history", action="store_true", help="Render the append-only history ledger."
    )
    parser.add_argument(
        "--edits",
        action="store_true",
        help="Report which uncommitted files a subagent wrote, not the caller.",
    )
    parser.add_argument(
        "--session",
        help="Restrict --edits to one session id (the ledger is shared per clone).",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not append to the history ledger or refresh the transcript digest.",
    )
    return parser


def _render_edits(root: Path, *, session_id: str | None = None) -> int:
    """Print the subagent-edit receipt for the uncommitted change set.

    Scoped by *file*, never by time: anything already committed no
    longer differs from ``HEAD``, so it drops out on its own. Time
    scoping is deliberately avoided — a row for a file that stayed dirty
    across an earlier commit, ordinary under the staged-subset commit
    recipe, would be excluded and the file reported clean, which is the
    failure this receipt exists to prevent. Attribution therefore errs
    toward over-reporting: an old row for a file dirty again today may
    over-attribute, which a reader can see and correct, where
    under-attribution is silent.

    Args:
        root: Repository root.
        session_id: Restrict to one session id.

    Returns:
        ``0`` always — a reporter never gates.
    """
    # NUL-separated listings: whitespace splitting cut "my file.py" in two
    # and git quoting hid non-ASCII names from the receipt.
    changed = diff_names_z(root, "HEAD") + git_z_names(
        root, "ls-files", "-z", "--others", "--exclude-standard"
    )
    logger.info(
        "%s",
        render_edit_receipt(
            subagent_edits(root, session_id=session_id, paths=set(changed))
        ),
    )
    return 0


def main() -> int:
    """Entry point for ``forge-agent-profile``.

    Returns:
        ``0`` always — a reporter never gates; ``2`` only for unusable
        arguments.
    """
    args = _build_parser().parse_args()
    root = repo_root()
    if args.edits:
        return _render_edits(root, session_id=args.session)
    if args.history:
        return _render_history(root)
    since = _parse_ts(args.since) if args.since else None
    if args.since and since is None:
        logger.error("--since must be an ISO-8601 timestamp, got %r", args.since)
        return 2
    ledger_path = args.ledger or code_health_dir(root) / LEDGER_NAME
    events = list(_iter_jsonl(ledger_path)) if ledger_path.is_file() else []
    runs = filter_runs(
        collect_runs(ledger_path, args.transcripts, record_digest=not args.no_history),
        agent_type=args.agent_type,
        since=since,
        last=args.last,
    )
    tools = tool_rows(events)
    output = (
        render_json(runs, tools)
        if args.json
        else render_report(runs, tools, top=args.top)
    )
    logger.info("%s", output)
    if not args.no_history:
        append_history(root, runs, args.label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
