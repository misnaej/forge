"""Tests for ``forge.agent_profile`` — where the agents' time goes."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from forge import agent_profile
from forge.agent_profile import (
    LOOP_REPEAT_THRESHOLD,
    MAIN_SESSION_TYPE,
    PRECOMMIT_FIXER_AGENT,
    PRECOMMIT_RUN_CAP,
    AgentRun,
    ToolRow,
    TranscriptStats,
    collect_runs,
    filter_runs,
    parse_transcript,
    render_json,
    render_report,
    runs_from_ledger,
    runs_from_transcripts,
    tool_rows,
    type_rows,
)


if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


BASE_MS = 1_757_412_000_000  # a fixed, round instant — exact date is not load-bearing
BASE_DT = datetime(2026, 9, 9, 9, 0, 0, tzinfo=UTC)


def _ms_to_dt(ms: int) -> datetime:
    """The ``datetime`` a ledger ``ts_ms`` value parses to.

    Args:
        ms: Milliseconds since the Unix epoch.

    Returns:
        The equivalent UTC ``datetime``.
    """
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _event(
    *,
    event: str,
    ts_ms: int,
    session_id: str = "s1",
    agent_id: str | None = None,
    agent_type: str | None = None,
    **kwargs: object,
) -> dict[str, object]:
    """One ``agent_timing.jsonl`` ledger event, shaped like hook payload.

    Args:
        event: Event type (SubagentStart, SubagentStop, PostToolUse).
        ts_ms: Timestamp in milliseconds.
        session_id: Session identifier (default "s1").
        agent_id: Optional agent identifier.
        agent_type: Optional agent type.
        **kwargs: Extra fields (transcript_path, tool_name, tool_use_id,
            duration_ms) passed through to the result dict.

    Returns:
        A dict shaped like the real hook payload.
    """
    return {
        "ts": _ms_to_dt(ts_ms).isoformat(),
        "ts_ms": ts_ms,
        "event": event,
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "transcript_path": kwargs.get("transcript_path"),
        "tool_name": kwargs.get("tool_name"),
        "tool_use_id": kwargs.get("tool_use_id"),
        "duration_ms": kwargs.get("duration_ms"),
    }


def _write_jsonl(path: Path, records: Iterable[dict[str, object]]) -> None:
    """Write one JSON object per line to *path*, creating its parent dir.

    Args:
        path: File to write.
        records: Objects to write, one per line, in order.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _iso(ts: datetime) -> str:
    """A transcript-style ``Z``-suffixed ISO timestamp.

    Args:
        ts: Timestamp to format.

    Returns:
        *ts* as an ISO-8601 string with a ``Z`` suffix.
    """
    return ts.isoformat().replace("+00:00", "Z")


def _tool_use(id_: str, name: str, tool_input: dict[str, object]) -> dict[str, object]:
    """One ``tool_use`` content block.

    Args:
        id_: The tool call's id.
        name: The tool's name.
        tool_input: The tool call's input payload.

    Returns:
        The content block.
    """
    return {"type": "tool_use", "id": id_, "name": name, "input": tool_input}


def _assistant(
    ts: datetime, blocks: list[dict[str, object]], *, output_tokens: int = 0
) -> dict[str, object]:
    """One assistant transcript record carrying *blocks* as its content.

    Args:
        ts: The record's timestamp.
        blocks: Content blocks for the record's message.
        output_tokens: Reported output-token count. Defaults to 0.

    Returns:
        The transcript record.
    """
    return {
        "type": "assistant",
        "timestamp": _iso(ts),
        "message": {"content": blocks, "usage": {"output_tokens": output_tokens}},
    }


def _user_tool_result(
    ts: datetime, tool_use_id: str, *, agent_id: str | None = None
) -> dict[str, object]:
    """One user record carrying a ``tool_result`` block (not a resume).

    Args:
        ts: The record's timestamp.
        tool_use_id: The id of the tool call this result answers.
        agent_id: When set, the id of the subagent this result resolves.

    Returns:
        The transcript record.
    """
    record: dict[str, object] = {
        "type": "user",
        "timestamp": _iso(ts),
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}
            ]
        },
    }
    if agent_id is not None:
        record["toolUseResult"] = {"agentId": agent_id}
    return record


def _user_resume(ts: datetime, text: str = "continue") -> dict[str, object]:
    """One user record carrying a new prompt (string content, no tool_result).

    Args:
        ts: The record's timestamp.
        text: The prompt text. Defaults to "continue".

    Returns:
        The transcript record.
    """
    return {"type": "user", "timestamp": _iso(ts), "message": {"content": text}}


# ---------------------------------------------------------------------------
# runs_from_ledger — pairing
# ---------------------------------------------------------------------------


def test_runs_from_ledger_pairs_subagent_start_and_stop_by_agent_id() -> None:
    """Start, two PostToolUse events, and Stop fold into one run keyed by agent_id."""
    events = [
        _event(
            event="SubagentStart",
            ts_ms=BASE_MS,
            agent_id="a1",
            agent_type="forge:design-checker",
        ),
        _event(
            event="PostToolUse",
            ts_ms=BASE_MS + 1000,
            agent_id="a1",
            agent_type="forge:design-checker",
            tool_name="Bash",
            tool_use_id="t1",
            duration_ms=500,
        ),
        _event(
            event="PostToolUse",
            ts_ms=BASE_MS + 3000,
            agent_id="a1",
            agent_type="forge:design-checker",
            tool_name="Read",
            tool_use_id="t2",
            duration_ms=2000,
        ),
        _event(
            event="SubagentStop",
            ts_ms=BASE_MS + 4000,
            agent_id="a1",
            agent_type="forge:design-checker",
            transcript_path="agent-a1.jsonl",
        ),
    ]
    runs = runs_from_ledger(events)
    run = runs["a1"]
    assert run.agent_type == "forge:design-checker"
    assert run.started == _ms_to_dt(BASE_MS)
    assert run.ended == _ms_to_dt(BASE_MS + 4000)
    assert run.tool_calls == 2
    assert run.tool_ms == 2500
    assert run.slowest_tool == "Read"
    assert run.slowest_tool_ms == 2000
    assert run.transcript_path == "agent-a1.jsonl"


def test_runs_from_ledger_run_without_stop_uses_last_event_as_end() -> None:
    """A run missing SubagentStop has no ``ended``; wall_s falls back to last_event."""
    events = [
        _event(
            event="SubagentStart",
            ts_ms=BASE_MS,
            agent_id="a1",
            agent_type="forge:design-checker",
        ),
        _event(
            event="PostToolUse",
            ts_ms=BASE_MS + 5000,
            agent_id="a1",
            agent_type="forge:design-checker",
            tool_name="Bash",
            duration_ms=100,
        ),
    ]
    run = runs_from_ledger(events)["a1"]
    assert run.ended is None
    assert run.wall_s == pytest.approx(5.0)


def test_runs_from_ledger_main_session_tools_land_under_main_session_pseudo_id() -> (
    None
):
    """A tool event with no agent_id is main-session activity, keyed by session_id."""
    events = [
        _event(
            event="PostToolUse",
            ts_ms=BASE_MS,
            session_id="s2",
            agent_id=None,
            tool_name="Bash",
            duration_ms=250,
        )
    ]
    runs = runs_from_ledger(events)
    assert set(runs) == {"main:s2"}
    run = runs["main:s2"]
    assert run.agent_type == MAIN_SESSION_TYPE
    assert run.tool_calls == 1
    assert run.tool_ms == 250


def test_runs_from_ledger_agent_type_arrives_only_on_later_event_still_recorded() -> (
    None
):
    """agent_type missing on Start is still picked up from a later event."""
    events = [
        _event(event="SubagentStart", ts_ms=BASE_MS, agent_id="a2", agent_type=None),
        _event(
            event="SubagentStop",
            ts_ms=BASE_MS + 1000,
            agent_id="a2",
            agent_type="forge:test-writer",
        ),
    ]
    run = runs_from_ledger(events)["a2"]
    assert run.agent_type == "forge:test-writer"


def test_apply_event_precommit_full_run_increments_ledger_precommit_runs() -> None:
    """Each ``precommit_full_run`` ledger line (block_fixer_recon.sh) adds one."""
    events = [
        _event(
            event="SubagentStart",
            ts_ms=BASE_MS,
            agent_id="a3",
            agent_type=PRECOMMIT_FIXER_AGENT,
        ),
        _event(
            event="precommit_full_run",
            ts_ms=BASE_MS + 1000,
            agent_id="a3",
            agent_type=PRECOMMIT_FIXER_AGENT,
        ),
        _event(
            event="precommit_full_run",
            ts_ms=BASE_MS + 2000,
            agent_id="a3",
            agent_type=PRECOMMIT_FIXER_AGENT,
        ),
    ]
    run = runs_from_ledger(events)["a3"]
    assert run.ledger_precommit_runs == 2


# ---------------------------------------------------------------------------
# runs_from_ledger — idle gap clipping
# ---------------------------------------------------------------------------


def test_runs_from_ledger_gap_over_idle_threshold_clipped_in_ledger_active_s() -> None:
    """A 400s gap clips to IDLE_GAP_S (300); a 200s gap counts in full."""
    events = [
        _event(
            event="SubagentStart",
            ts_ms=BASE_MS,
            agent_id="a3",
            agent_type="forge:design-checker",
        ),
        _event(
            event="PostToolUse",
            ts_ms=BASE_MS + 400_000,
            agent_id="a3",
            agent_type="forge:design-checker",
            tool_name="Bash",
            duration_ms=10,
        ),
        _event(
            event="PostToolUse",
            ts_ms=BASE_MS + 600_000,
            agent_id="a3",
            agent_type="forge:design-checker",
            tool_name="Bash",
            duration_ms=10,
        ),
    ]
    run = runs_from_ledger(events)["a3"]
    assert run.ledger_active_s == pytest.approx(500.0)  # 300 (clipped) + 200 (full)


def test_agent_run_active_s_prefers_transcript_stats_over_ledger_when_present() -> None:
    """``active_s`` prefers transcript stats over ledger gap sum."""
    with_stats = AgentRun(
        agent_id="a4",
        agent_type="forge:design-checker",
        ledger_active_s=100.0,
        stats=TranscriptStats(active_s=250.0),
    )
    assert with_stats.active_s == pytest.approx(250.0)

    without_stats = AgentRun(
        agent_id="a5", agent_type="forge:design-checker", ledger_active_s=42.0
    )
    assert without_stats.active_s == pytest.approx(42.0)

    zero_stats = AgentRun(
        agent_id="a6",
        agent_type="forge:design-checker",
        ledger_active_s=17.0,
        stats=TranscriptStats(active_s=0.0),
    )
    assert zero_stats.active_s == pytest.approx(17.0)


# ---------------------------------------------------------------------------
# tool_rows
# ---------------------------------------------------------------------------


def test_tool_rows_aggregates_calls_total_and_peak_per_tool_sorted_by_total_ms() -> (
    None
):
    """Per-tool calls/total/peak aggregate correctly, costliest total first."""
    events = [
        _event(event="PostToolUse", ts_ms=BASE_MS, tool_name="Bash", duration_ms=100),
        _event(
            event="PostToolUse", ts_ms=BASE_MS + 1000, tool_name="Bash", duration_ms=300
        ),
        _event(
            event="PostToolUse",
            ts_ms=BASE_MS + 2000,
            tool_name="Read",
            duration_ms=5000,
        ),
    ]
    rows = tool_rows(events)
    assert rows == [
        ToolRow("Read", 1, 5000, 5000),
        ToolRow("Bash", 2, 400, 300),
    ]


def test_tool_rows_skips_events_without_numeric_duration() -> None:
    """Events with no numeric ``duration_ms`` (or no PostToolUse kind) don't count."""
    events = [
        _event(event="PostToolUse", ts_ms=BASE_MS, tool_name="Bash", duration_ms=None),
        _event(event="SubagentStop", ts_ms=BASE_MS + 1000),
        _event(
            event="PostToolUse", ts_ms=BASE_MS + 2000, tool_name="Read", duration_ms=100
        ),
    ]
    rows = tool_rows(events)
    assert len(rows) == 1
    assert rows[0].tool_name == "Read"


# ---------------------------------------------------------------------------
# parse_transcript
# ---------------------------------------------------------------------------


def test_parse_transcript_counts_turns_tool_calls_and_output_tokens(
    tmp_path: Path,
) -> None:
    """Each assistant record is a turn; tool_use blocks and usage tokens accumulate."""
    t = [BASE_DT + timedelta(seconds=i) for i in range(4)]
    path = tmp_path / "agent-a1.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(
                t[0], [_tool_use("t1", "Bash", {"command": "ls"})], output_tokens=10
            ),
            _user_tool_result(t[1], "t1"),
            _assistant(
                t[2], [_tool_use("t2", "Read", {"file_path": "x"})], output_tokens=5
            ),
            _user_tool_result(t[3], "t2"),
        ],
    )
    stats = parse_transcript(path)
    assert stats is not None
    assert stats.turns == 2
    assert stats.tool_calls == 2
    assert stats.output_tokens == 15
    assert stats.started == t[0]
    assert stats.ended == t[3]


def test_parse_transcript_repeat_count_resets_on_resume(tmp_path: Path) -> None:
    """5 identical calls, a resume, then 2 more identical calls: max_repeat stays 5."""
    path = tmp_path / "agent-a1.jsonl"
    segment1 = [
        _assistant(
            BASE_DT + timedelta(seconds=i),
            [_tool_use(f"c{i}", "Bash", {"command": "git status"})],
        )
        for i in range(5)
    ]
    resume = [_user_resume(BASE_DT + timedelta(seconds=5))]
    segment2 = [
        _assistant(
            BASE_DT + timedelta(seconds=6 + i),
            [_tool_use(f"d{i}", "Bash", {"command": "git status"})],
        )
        for i in range(2)
    ]
    _write_jsonl(path, [*segment1, *resume, *segment2])
    stats = parse_transcript(path)
    assert stats is not None
    assert stats.max_repeat == 5
    assert stats.repeated_call == "Bash: git status"


def test_parse_transcript_precommit_runs_counts_full_run_not_only_flag(
    tmp_path: Path,
) -> None:
    """A full ``forge-precommit`` run counts; a ``--only`` invocation does not."""
    path = tmp_path / "agent-a1.jsonl"
    records = [
        _assistant(BASE_DT, [_tool_use("c1", "Bash", {"command": "forge-precommit"})]),
        _assistant(
            BASE_DT + timedelta(seconds=1),
            [_tool_use("c2", "Bash", {"command": "forge-precommit --only ruff"})],
        ),
        _assistant(
            BASE_DT + timedelta(seconds=2),
            [_tool_use("c3", "Bash", {"command": "forge-precommit"})],
        ),
    ]
    _write_jsonl(path, records)
    stats = parse_transcript(path)
    assert stats is not None
    assert stats.precommit_runs == 2


def test_parse_transcript_tolerates_junk_lines_interspersed_with_valid_records(
    tmp_path: Path,
) -> None:
    """Non-JSON lines between valid records are skipped, not fatal."""
    path = tmp_path / "agent-a1.jsonl"
    lines = [
        "not json at all",
        json.dumps(
            _assistant(
                BASE_DT, [_tool_use("c1", "Bash", {"command": "ls"})], output_tokens=1
            )
        ),
        "{truncated json",
        json.dumps(_user_tool_result(BASE_DT + timedelta(seconds=1), "c1")),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stats = parse_transcript(path)
    assert stats is not None
    assert stats.turns == 1
    assert stats.tool_calls == 1


def test_parse_transcript_all_junk_returns_none(tmp_path: Path) -> None:
    """A transcript with no timestamped record at all returns ``None``."""
    path = tmp_path / "agent-a1.jsonl"
    path.write_text("not json at all\n{also not json\n", encoding="utf-8")
    assert parse_transcript(path) is None


# ---------------------------------------------------------------------------
# runs_from_transcripts / collect_runs
# ---------------------------------------------------------------------------


def test_agent_type_linked_via_tool_result_agent_id(tmp_path: Path) -> None:
    """Main session Agent tool_use pairs subagent via tool_result agent_id."""
    root = tmp_path
    session = root / "session1.jsonl"
    _write_jsonl(
        session,
        [
            _assistant(
                BASE_DT,
                [
                    _tool_use(
                        "toolu_agent1",
                        "Agent",
                        {
                            "subagent_type": "forge:design-checker",
                            "description": "Review the diff",
                        },
                    )
                ],
            ),
            _user_tool_result(
                BASE_DT + timedelta(seconds=1), "toolu_agent1", agent_id="a1"
            ),
        ],
    )
    sub_path = root / "proj" / "subagents" / "agent-a1.jsonl"
    _write_jsonl(
        sub_path,
        [
            _assistant(
                BASE_DT + timedelta(seconds=2),
                [_tool_use("t1", "Bash", {"command": "ls"})],
            )
        ],
    )

    runs = runs_from_transcripts(root)

    assert "a1" in runs
    run = runs["a1"]
    assert run.agent_type == "forge:design-checker"
    assert run.description == "Review the diff"
    assert run.transcript_path == str(sub_path)
    assert run.stats is not None


def test_runs_from_transcripts_unlinked_transcript_gets_unknown_type_placeholder(
    tmp_path: Path,
) -> None:
    """A subagent transcript with no main-session linkage gets the ``"?"`` type."""
    root = tmp_path / "root2"
    sub_path = root / "proj" / "subagents" / "agent-a9.jsonl"
    _write_jsonl(
        sub_path, [_assistant(BASE_DT, [_tool_use("t1", "Bash", {"command": "ls"})])]
    )

    runs = runs_from_transcripts(root)

    assert runs["a9"].agent_type == "?"
    assert runs["a9"].description is None


def test_collect_runs_ledger_run_enriched_by_its_named_transcript(
    tmp_path: Path,
) -> None:
    """A ledger run whose transcript_path resolves gets its stats filled in."""
    ledger_path = tmp_path / "agent_timing.jsonl"
    transcript_path = tmp_path / "transcript_a1.jsonl"
    _write_jsonl(
        transcript_path,
        [
            _assistant(
                BASE_DT, [_tool_use("t1", "Bash", {"command": "ls"})], output_tokens=3
            )
        ],
    )
    _write_jsonl(
        ledger_path,
        [
            _event(
                event="SubagentStart",
                ts_ms=BASE_MS,
                agent_id="a1",
                agent_type="forge:design-checker",
            ),
            _event(
                event="SubagentStop",
                ts_ms=BASE_MS + 1000,
                agent_id="a1",
                agent_type="forge:design-checker",
                transcript_path=str(transcript_path),
            ),
        ],
    )

    runs = collect_runs(ledger_path, None)

    run = next(r for r in runs if r.agent_id == "a1")
    assert run.stats is not None
    assert run.stats.tool_calls == 1
    assert run.stats.output_tokens == 3


def test_collect_runs_transcript_only_run_backfills_when_absent_from_ledger(
    tmp_path: Path,
) -> None:
    """A subagent transcript the ledger never names still appears in the result."""
    ledger_path = tmp_path / "agent_timing.jsonl"
    _write_jsonl(
        ledger_path,
        [
            _event(
                event="SubagentStart",
                ts_ms=BASE_MS,
                agent_id="a1",
                agent_type="forge:design-checker",
            ),
            _event(
                event="SubagentStop",
                ts_ms=BASE_MS + 1000,
                agent_id="a1",
                agent_type="forge:design-checker",
            ),
        ],
    )
    transcripts_root = tmp_path / "project"
    sub_path = transcripts_root / "proj" / "subagents" / "agent-a2.jsonl"
    _write_jsonl(
        sub_path, [_assistant(BASE_DT, [_tool_use("t1", "Bash", {"command": "ls"})])]
    )

    runs = collect_runs(ledger_path, transcripts_root)

    assert {r.agent_id for r in runs} == {"a1", "a2"}


def test_collect_runs_orders_by_start_time_oldest_first_unknown_start_sorts_first(
    tmp_path: Path,
) -> None:
    """Runs sort oldest-first; unknown start sorts earliest."""
    ledger_path = tmp_path / "agent_timing.jsonl"
    _write_jsonl(
        ledger_path,
        [
            _event(
                event="SubagentStart",
                ts_ms=BASE_MS + 5000,
                agent_id="a1",
                agent_type="forge:design-checker",
            ),
            _event(
                event="SubagentStart",
                ts_ms=BASE_MS,
                agent_id="a2",
                agent_type="forge:test-writer",
            ),
        ],
    )

    runs = collect_runs(ledger_path, None)

    assert [r.agent_id for r in runs] == ["a2", "a1"]

    # A run neither the ledger nor a transcript ever timestamps cannot arise from
    # real input (both always carry a timestamp) — verify the tie-break `_start_of`
    # uses for such a run directly: it sorts before every known-start run.
    unknown = AgentRun(agent_id="unknown", agent_type="?")
    assert [
        r.agent_id for r in sorted([*runs, unknown], key=agent_profile._start_of)
    ] == [
        "unknown",
        "a2",
        "a1",
    ]


# ---------------------------------------------------------------------------
# filter_runs
# ---------------------------------------------------------------------------


def _t(seconds: int) -> datetime:
    """A fixed base instant plus *seconds* — for building ordered runs.

    Args:
        seconds: Offset from the base instant.

    Returns:
        The offset ``datetime``.
    """
    return BASE_DT + timedelta(seconds=seconds)


def test_filter_runs_by_agent_type_since_and_last_combined() -> None:
    """agent_type, since, and last narrow the list together, in that order."""
    runs = [
        AgentRun(agent_id="1", agent_type="A", started=_t(0)),
        AgentRun(agent_id="2", agent_type="B", started=_t(1)),
        AgentRun(agent_id="3", agent_type="A", started=_t(2)),
        AgentRun(agent_id="4", agent_type="A", started=_t(3)),
    ]
    filtered = filter_runs(runs, agent_type="A", since=_t(1), last=1)
    assert [r.agent_id for r in filtered] == ["4"]


def test_filter_runs_since_keeps_runs_with_unknown_start_time() -> None:
    """A run with no known start survives a ``since`` filter that would drop it."""
    runs = [
        AgentRun(agent_id="unknown", agent_type="A"),
        AgentRun(agent_id="known", agent_type="A", started=_t(5)),
    ]
    filtered = filter_runs(runs, since=_t(10))
    assert [r.agent_id for r in filtered] == ["unknown"]


# ---------------------------------------------------------------------------
# type_rows / _fmt_s
# ---------------------------------------------------------------------------


def test_type_rows_aggregates_mean_p50_max_wall_active_per_type_sorted_by_wall() -> (
    None
):
    """Per-type mean/p50/max/wall/active aggregate, sorted by total wall time."""
    runs = [
        AgentRun(
            agent_id="1",
            agent_type="A",
            started=_t(0),
            ended=_t(10),
            ledger_active_s=8.0,
        ),
        AgentRun(
            agent_id="2",
            agent_type="A",
            started=_t(0),
            ended=_t(30),
            ledger_active_s=25.0,
        ),
        AgentRun(
            agent_id="3",
            agent_type="B",
            started=_t(0),
            ended=_t(5),
            ledger_active_s=5.0,
        ),
    ]
    rows = type_rows(runs)
    assert rows[0].agent_type == "A"
    assert rows[0].runs == 2
    assert rows[0].mean_s == pytest.approx(20.0)
    assert rows[0].p50_s == pytest.approx(20.0)
    assert rows[0].max_s == pytest.approx(30.0)
    assert rows[0].wall_s == pytest.approx(40.0)
    assert rows[0].active_s == pytest.approx(33.0)
    assert rows[1].agent_type == "B"
    assert rows[1].runs == 1


@pytest.mark.development
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(42, "42s"), (190, "3m10s"), (7500, "2h05m")],
)
def test_fmt_s_formats_seconds_minutes_and_hours_boundaries(
    seconds: int, expected: str
) -> None:
    """Development: pins ``_fmt_s`` formatting during implementation.

    Args:
        seconds: Duration to format, in seconds.
        expected: The expected compact-duration string.
    """
    assert agent_profile._fmt_s(seconds) == expected


# ---------------------------------------------------------------------------
# loop_suspect / cap_breach
# ---------------------------------------------------------------------------


def test_cap_breach_true_only_for_precommit_fixer_agent_type_past_cap() -> None:
    """Only ``forge:precommit-fixer`` runs strictly over PRECOMMIT_RUN_CAP breach."""
    over_cap = AgentRun(
        agent_id="1",
        agent_type=PRECOMMIT_FIXER_AGENT,
        stats=TranscriptStats(precommit_runs=PRECOMMIT_RUN_CAP + 1),
    )
    at_cap = AgentRun(
        agent_id="2",
        agent_type=PRECOMMIT_FIXER_AGENT,
        stats=TranscriptStats(precommit_runs=PRECOMMIT_RUN_CAP),
    )
    other_type_over_cap = AgentRun(
        agent_id="3",
        agent_type="forge:design-checker",
        stats=TranscriptStats(precommit_runs=PRECOMMIT_RUN_CAP + 5),
    )
    assert over_cap.cap_breach is True
    assert at_cap.cap_breach is False
    assert other_type_over_cap.cap_breach is False


def test_precommit_runs_property_takes_max_of_ledger_and_transcript() -> None:
    """``precommit_runs`` is the larger of the ledger count and the transcript's.

    Neither source can hide a breach the other saw: the ledger is
    primary (written by ``block_fixer_recon.sh`` as it enforces the
    cap), the transcript regex covers history recorded before that
    hook existed.
    """
    ledger_wins = AgentRun(
        agent_id="1",
        agent_type=PRECOMMIT_FIXER_AGENT,
        ledger_precommit_runs=6,
        stats=TranscriptStats(precommit_runs=2),
    )
    transcript_wins = AgentRun(
        agent_id="2",
        agent_type=PRECOMMIT_FIXER_AGENT,
        ledger_precommit_runs=2,
        stats=TranscriptStats(precommit_runs=5),
    )
    assert ledger_wins.precommit_runs == 6
    assert transcript_wins.precommit_runs == 5


def test_cap_breach_true_from_ledger_count_alone_no_transcript() -> None:
    """A ledger-only run (no transcript at all) still breaches from its own count."""
    run = AgentRun(
        agent_id="1",
        agent_type=PRECOMMIT_FIXER_AGENT,
        ledger_precommit_runs=PRECOMMIT_RUN_CAP + 1,
        stats=None,
    )
    assert run.cap_breach is True


def test_loop_suspect_true_at_threshold_false_one_below() -> None:
    """max_repeat at LOOP_REPEAT_THRESHOLD is a suspect; one below is not."""
    at_threshold = AgentRun(
        agent_id="1",
        agent_type="x",
        stats=TranscriptStats(max_repeat=LOOP_REPEAT_THRESHOLD),
    )
    below_threshold = AgentRun(
        agent_id="2",
        agent_type="x",
        stats=TranscriptStats(max_repeat=LOOP_REPEAT_THRESHOLD - 1),
    )
    assert at_threshold.loop_suspect is True
    assert below_threshold.loop_suspect is False


# ---------------------------------------------------------------------------
# render_report / render_json
# ---------------------------------------------------------------------------


def test_render_report_empty_runs_returns_hook_install_hint() -> None:
    """No runs at all reports the hint to let the hook start writing the ledger."""
    report = render_report([], [], top=5)
    assert "log_agent_timing" in report
    assert "agent_timing.jsonl" in report


def test_render_report_includes_loop_suspects_and_gated_cap_breaches_section() -> None:
    """A loop-suspect run and a cap-breaching precommit-fixer run both surface."""
    loop_run = AgentRun(
        agent_id="loop1",
        agent_type="forge:design-checker",
        started=BASE_DT,
        ended=BASE_DT + timedelta(seconds=30),
        description="loop desc",
        stats=TranscriptStats(
            started=BASE_DT,
            ended=BASE_DT + timedelta(seconds=30),
            active_s=30.0,
            turns=3,
            tool_calls=6,
            max_repeat=5,
            repeated_call="Bash: git status",
        ),
    )
    fixer_run = AgentRun(
        agent_id="fix1",
        agent_type=PRECOMMIT_FIXER_AGENT,
        started=BASE_DT,
        ended=BASE_DT + timedelta(seconds=60),
        description="fixer desc",
        stats=TranscriptStats(
            started=BASE_DT,
            ended=BASE_DT + timedelta(seconds=60),
            active_s=60.0,
            precommit_runs=PRECOMMIT_RUN_CAP + 1,
        ),
    )
    report = render_report([loop_run, fixer_run], [], top=5)
    assert (
        f"Loop suspects (identical call >={LOOP_REPEAT_THRESHOLD}x in one prompt): 1"
        in report
    )
    assert "5x Bash: git status" in report
    assert (
        f"{PRECOMMIT_FIXER_AGENT} runs past the {PRECOMMIT_RUN_CAP}-run cap: 1"
        in report
    )
    assert f"{PRECOMMIT_RUN_CAP + 1} full forge-precommit runs" in report


def test_render_json_shape_has_runs_types_tools_top_level_keys_with_iso_datetimes() -> (
    None
):
    """The JSON document has exactly the three top-level sections, ISO timestamps."""
    run = AgentRun(
        agent_id="1",
        agent_type="A",
        started=BASE_DT,
        ended=BASE_DT + timedelta(seconds=10),
    )
    tools = [ToolRow("Bash", 1, 100, 100)]
    doc = json.loads(render_json([run], tools))
    assert set(doc) == {"runs", "types", "tools"}
    assert doc["runs"][0]["started"] == BASE_DT.isoformat()
    assert doc["runs"][0]["ended"] == (BASE_DT + timedelta(seconds=10)).isoformat()
    assert doc["types"][0]["agent_type"] == "A"
    assert doc["tools"][0]["tool_name"] == "Bash"


def test_render_json_with_stats_serialises_nested_datetimes_and_flags() -> None:
    """A run's nested ``stats`` datetimes serialise to ISO; flags land top-level."""
    run = AgentRun(
        agent_id="1",
        agent_type=PRECOMMIT_FIXER_AGENT,
        stats=TranscriptStats(
            started=BASE_DT,
            ended=BASE_DT + timedelta(seconds=10),
            max_repeat=LOOP_REPEAT_THRESHOLD,
            precommit_runs=PRECOMMIT_RUN_CAP + 1,
        ),
    )
    doc = json.loads(render_json([run], []))
    run_doc = doc["runs"][0]
    assert run_doc["stats"]["started"] == BASE_DT.isoformat()
    assert run_doc["stats"]["ended"] == (BASE_DT + timedelta(seconds=10)).isoformat()
    assert run_doc["loop_suspect"] is True
    assert run_doc["cap_breach"] is True
    assert "last_event" not in run_doc


def test_render_report_with_tool_rows_renders_tool_time_section() -> None:
    """Passing tool rows renders the "Tool time" section with per-tool stats."""
    run = AgentRun(
        agent_id="1",
        agent_type="A",
        started=BASE_DT,
        ended=BASE_DT + timedelta(seconds=5),
    )
    tools = [ToolRow("Bash", 3, 4500, 3000)]
    report = render_report([run], tools, top=5)
    assert "Tool time" in report
    assert any("Bash" in line and "calls=3" in line for line in report.splitlines())


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_append_history_writes_label_runs_types_wall_active_loop_and_cap_fields(
    tmp_path: Path,
) -> None:
    """One appended line carries the label plus every aggregate field."""
    loop_run = AgentRun(
        agent_id="loop1",
        agent_type="forge:design-checker",
        started=BASE_DT,
        ended=BASE_DT + timedelta(seconds=10),
        stats=TranscriptStats(max_repeat=LOOP_REPEAT_THRESHOLD),
    )
    fixer_run = AgentRun(
        agent_id="fix1",
        agent_type=PRECOMMIT_FIXER_AGENT,
        started=BASE_DT,
        ended=BASE_DT + timedelta(seconds=20),
        stats=TranscriptStats(precommit_runs=PRECOMMIT_RUN_CAP + 1),
    )
    agent_profile.append_history(tmp_path, [loop_run, fixer_run], "nightly")
    line = (tmp_path / "code_health" / "agent_profile_history.log").read_text(
        encoding="utf-8"
    )
    assert "label=nightly" in line
    assert "runs=2" in line
    assert "types=2" in line
    assert "wall_s=" in line
    assert "active_s=" in line
    assert "loop_suspects=1" in line
    assert "cap_breaches=1" in line


def test_render_history_missing_file_logs_hint_returns_0(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No agent-profile history yet — returns 0 and logs the "run it first" hint."""
    with caplog.at_level(logging.INFO, logger="forge.agent_profile"):
        code = agent_profile._render_history(tmp_path)
    assert code == 0
    assert "No agent-profile history" in caplog.text


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_since_bad_value_exits_2_and_logs_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Unparsable ``--since`` exits 2 with targeted error, never traceback."""
    monkeypatch.setattr(agent_profile, "repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-agent-profile", "--since", "not-a-date"])
    with caplog.at_level(logging.ERROR, logger="forge.agent_profile"):
        code = agent_profile.main()
    assert code == 2
    assert "--since must be an ISO-8601 timestamp" in caplog.text


def test_main_json_flag_writes_json_report_and_still_appends_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``--json`` emits the machine-readable report and history still gets a line."""
    ledger_path = tmp_path / "code_health" / "agent_timing.jsonl"
    _write_jsonl(
        ledger_path,
        [
            _event(
                event="SubagentStart",
                ts_ms=BASE_MS,
                agent_id="a1",
                agent_type="forge:design-checker",
            ),
            _event(
                event="SubagentStop",
                ts_ms=BASE_MS + 1000,
                agent_id="a1",
                agent_type="forge:design-checker",
            ),
        ],
    )
    monkeypatch.setattr(agent_profile, "repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-agent-profile", "--json", "--label", "x"])
    with caplog.at_level(logging.INFO, logger="forge.agent_profile"):
        code = agent_profile.main()
    assert code == 0
    doc = json.loads(caplog.records[-1].getMessage())
    assert len(doc["runs"]) == 1
    assert (tmp_path / "code_health" / "agent_profile_history.log").is_file()


def test_main_no_history_flag_skips_history_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-history`` reports normally but never touches the history ledger."""
    monkeypatch.setattr(agent_profile, "repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-agent-profile", "--no-history"])
    assert agent_profile.main() == 0
    assert not (tmp_path / "code_health" / "agent_profile_history.log").exists()


def test_main_history_flag_renders_ledger_without_reading_agent_timing_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``--history`` renders the profile ledger and never opens agent_timing.jsonl."""
    monkeypatch.setattr(agent_profile, "repo_root", lambda: tmp_path)
    agent_profile.append_history(tmp_path, [], "prior")
    call_count = {"n": 0}

    def _spy(path: Path) -> list[dict[str, object]]:
        call_count["n"] += 1
        return []

    monkeypatch.setattr(agent_profile, "_iter_jsonl", _spy)
    monkeypatch.setattr("sys.argv", ["forge-agent-profile", "--history"])
    with caplog.at_level(logging.INFO, logger="forge.agent_profile"):
        code = agent_profile.main()
    assert code == 0
    assert "Agent-profile history" in caplog.text
    assert call_count["n"] == 0
