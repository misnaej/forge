"""Tests for ``forge-agent-profile --tokens`` — per-request token accounting."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from forge import agent_profile
from forge.agent_profile import (
    AgentRun,
    TranscriptStats,
    default_transcripts_dir,
    parse_transcript,
    render_tokens,
    runs_from_transcripts,
    waste_flags,
)
from forge.agent_profile_tokens import (
    FEW_TOOL_CALLS,
    FIXED_SHARE_THRESHOLD,
    LARGE_RESULT_CHARS,
    NO_SKILL,
    SessionTokens,
    TokenUsage,
    parse_session,
)
from forge.git_utils import code_health_dir
from tests.test_agent_profile import (
    BASE_DT,
    _digest_line,
    _iso,
    _tool_use,
    _user_tool_result,
    _write_jsonl,
)


if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


def _at(seconds: int) -> datetime:
    """A timestamp *seconds* after the shared base instant.

    Args:
        seconds: Offset from ``BASE_DT``.

    Returns:
        The instant.
    """
    return BASE_DT + timedelta(seconds=seconds)


def _request(
    seconds: int,
    message_id: str | None,
    *,
    usage: tuple[int, int, int, int] = (0, 0, 0, 0),
    blocks: list[dict[str, object]] | None = None,
    skill: str | None = None,
) -> dict[str, object]:
    """One assistant record (one content block of a request) with usage.

    Args:
        seconds: Offset of the record's timestamp from ``BASE_DT``.
        message_id: The request id, or ``None`` for an id-less record.
        usage: ``(input, cache_creation, cache_read, output)`` tokens.
        blocks: Content blocks; one text block when omitted.
        skill: Value of ``attributionSkill``, when set.

    Returns:
        The transcript record.
    """
    message: dict[str, object] = {
        "content": blocks if blocks is not None else [{"type": "text", "text": "hi"}],
        "usage": {
            "input_tokens": usage[0],
            "cache_creation_input_tokens": usage[1],
            "cache_read_input_tokens": usage[2],
            "output_tokens": usage[3],
        },
    }
    if message_id is not None:
        message["id"] = message_id
    record: dict[str, object] = {
        "type": "assistant",
        "timestamp": _iso(_at(seconds)),
        "message": message,
    }
    if skill is not None:
        record["attributionSkill"] = skill
    return record


def _result(
    seconds: int, tool_use_id: str, content: str | list[dict[str, object]]
) -> dict[str, object]:
    """One user record returning a tool result.

    Args:
        seconds: Offset of the record's timestamp from ``BASE_DT``.
        tool_use_id: The id of the call it answers.
        content: The result's content (string or text blocks).

    Returns:
        The transcript record.
    """
    return {
        "type": "user",
        "timestamp": _iso(_at(seconds)),
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            ]
        },
    }


def _stats_of(tmp_path: Path, records: list[dict[str, object]]) -> TranscriptStats:
    """Parse a tiny transcript written from *records*.

    Args:
        tmp_path: Directory to write it in.
        records: Transcript records, in order.

    Returns:
        Its stats.
    """
    path = tmp_path / "agent-a1.jsonl"
    _write_jsonl(path, records)
    stats = parse_transcript(path)
    assert stats is not None
    return stats


def _tokens_of(stats: TranscriptStats) -> TokenUsage:
    """The token usage a parsed transcript must carry.

    Args:
        stats: Parsed stats.

    Returns:
        Its usage, asserted present.
    """
    assert stats.tokens is not None
    return stats.tokens


# ---------------------------------------------------------------------------
# usage counting
# ---------------------------------------------------------------------------


def test_usage_counted_once_per_message_id(tmp_path: Path) -> None:
    """Behavior: pins once-per-request counting; per-block counting over-counts cost."""
    stats = _stats_of(
        tmp_path,
        [
            _request(0, "m1", usage=(1, 2, 3, 10)),
            _request(1, "m1", usage=(1, 2, 3, 10)),
            _request(2, "m1", usage=(1, 2, 3, 10)),
            _request(3, "m2", usage=(4, 0, 0, 20)),
            _request(4, None, usage=(5, 0, 0, 30)),
        ],
    )

    tokens = _tokens_of(stats)

    assert stats.turns == 3
    assert tokens.requests == 3
    assert tokens.input_tokens == 1 + 4 + 5
    assert tokens.cache_write_tokens == 2
    assert tokens.cache_read_tokens == 3
    assert tokens.output_tokens == stats.output_tokens == 10 + 20 + 30


def test_fixed_tokens_is_first_request_prompt(tmp_path: Path) -> None:
    """Behavior: fixed context is defined as the first request's whole prompt."""
    stats = _stats_of(
        tmp_path,
        [
            _request(0, "m1", usage=(10, 100, 1000, 0)),
            _request(1, "m2", usage=(20, 200, 5000, 0)),
        ],
    )

    assert _tokens_of(stats).fixed_tokens == 10 + 100 + 1000


@pytest.mark.parametrize(
    ("usage", "fixed_cost", "fixed_share"),
    [
        pytest.param(
            TokenUsage(
                input_tokens=100,
                cache_write_tokens=200,
                cache_read_tokens=700,
                fixed_tokens=100,
                requests=4,
            ),
            400,
            0.4,
            id="normal",
        ),
        pytest.param(
            TokenUsage(input_tokens=1000, fixed_tokens=600, requests=2),
            1000,
            1.0,
            id="fixed-exceeds-prompt-clamps",
        ),
        pytest.param(TokenUsage(fixed_tokens=50, requests=3), 0, 0.0, id="zero-prompt"),
    ],
)
def test_token_usage_properties(
    usage: TokenUsage, fixed_cost: int, fixed_share: float
) -> None:
    """Behavior: fixed cost stays within the prompt; empty prompt never divides.

    Args:
        usage: Token usage under test.
        fixed_cost: Expected fixed cost, clamped to the prompt size.
        fixed_share: Expected fixed share of the prompt.
    """
    assert usage.fixed_cost == fixed_cost
    assert usage.fixed_share == pytest.approx(fixed_share)


# ---------------------------------------------------------------------------
# main-session parsing
# ---------------------------------------------------------------------------


def test_parse_session_splits_tokens_by_attribution_skill() -> None:
    """Behavior: main-session cost goes to the active skill, else to "(no skill)"."""
    session = parse_session(
        [
            _request(0, "m1", usage=(10, 0, 0, 0), skill="perf"),
            _request(1, "m2", usage=(20, 0, 0, 0), skill="perf"),
            _request(2, "m3", usage=(7, 0, 0, 0)),
        ]
    )

    assert set(session.by_skill) == {"perf", NO_SKILL}
    assert session.by_skill["perf"].input_tokens == 30
    assert session.by_skill[NO_SKILL].input_tokens == 7
    assert session.usage.input_tokens == 37


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_parse_session_records_delegation_brief_and_handback(tool: str) -> None:
    """Behavior: delegation line rests on brief size, caller skill, longest handback.

    Args:
        tool: Delegation tool name whose tool_use block carries the brief.
    """
    brief = "b" * 50
    frame = '<agent-message from="abc-1">'
    short = frame + "short"
    longest = frame + "r" * 200
    session = parse_session(
        [
            _request(
                0,
                "m1",
                blocks=[_tool_use("t1", tool, {"prompt": brief})],
                skill="perf",
            ),
            {"type": "user", "timestamp": _iso(_at(1)), "message": {"content": short}},
            {
                "type": "user",
                "timestamp": _iso(_at(2)),
                "message": {"content": [{"type": "text", "text": longest}]},
            },
            {"type": "user", "timestamp": _iso(_at(3)), "message": {"content": short}},
        ]
    )

    assert session.skill_of_call == {"t1": "perf"}
    assert session.brief_chars == {"t1": 50}
    assert session.handback_chars == {"abc-1": len(longest)}


# ---------------------------------------------------------------------------
# transcript signals
# ---------------------------------------------------------------------------


def test_parse_transcript_largest_tool_result_and_label(tmp_path: Path) -> None:
    """Behavior: waste flag names the largest result and the call that produced it."""
    stats = _stats_of(
        tmp_path,
        [
            _request(
                0,
                "m1",
                blocks=[
                    _tool_use("c1", "Read", {"file_path": "/a/small.py"}),
                    _tool_use("c2", "Bash", {"command": "cat huge.log"}),
                ],
            ),
            _result(1, "c1", [{"type": "text", "text": "x" * 30}]),
            _result(2, "c2", "y" * 500),
        ],
    )

    assert stats.largest_result_chars == 500
    assert stats.largest_result == "Bash: cat huge.log"


def test_repeated_read_records_reread_chars(tmp_path: Path) -> None:
    """Behavior: only second-and-later reads of one file count as re-read cost."""
    stats = _stats_of(
        tmp_path,
        [
            _request(
                0, "m1", blocks=[_tool_use("c1", "Read", {"file_path": "/a/x.py"})]
            ),
            _result(1, "c1", "a" * 50),
            _request(
                2, "m2", blocks=[_tool_use("c2", "Read", {"file_path": "/a/x.py"})]
            ),
            _result(3, "c2", "b" * 120),
            _request(
                4, "m3", blocks=[_tool_use("c3", "Read", {"file_path": "/a/y.py"})]
            ),
            _result(5, "c3", "c" * 900),
        ],
    )

    assert stats.repeated_reads == {"/a/x.py": 120}


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------


def test_digest_line_without_token_fields_loads_tokens_none() -> None:
    """Behavior: a pre-token digest line loads as unknown cost, never zero."""
    stats = agent_profile._stats_from_digest(_digest_line("a1"))

    assert stats is not None
    assert stats.tokens is None
    assert stats.largest_result_chars == 0
    assert stats.repeated_reads == {}


_USAGE_FIELDS = {
    "input_tokens": 1,
    "cache_write_tokens": 2,
    "cache_read_tokens": 3,
    "output_tokens": 4,
    "fixed_tokens": 5,
    "requests": 6,
}


def test_digest_line_round_trips_token_fields() -> None:
    """Behavior: a digest line with token fields restores them for swept transcripts."""
    stats = agent_profile._stats_from_digest(
        _digest_line(
            "a1",
            tokens=_USAGE_FIELDS,
            largest_result_chars=77,
            largest_result="Bash: big",
            repeated_reads={"/a/x.py": 9},
        )
    )

    assert stats is not None
    assert stats.tokens == TokenUsage(**_USAGE_FIELDS)
    assert stats.largest_result_chars == 77
    assert stats.largest_result == "Bash: big"
    assert stats.repeated_reads == {"/a/x.py": 9}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"tokens": "lots"}, id="tokens-string"),
        pytest.param({"tokens": {"input_tokens": 1}}, id="tokens-missing-fields"),
        pytest.param({"largest_result_chars": "big"}, id="chars-wrong-type"),
        pytest.param({"repeated_reads": ["/a"]}, id="reads-wrong-type"),
        pytest.param({"repeated_reads": {"/a": "x"}}, id="reads-value-wrong-type"),
        pytest.param(
            {"tokens": {**_USAGE_FIELDS, "input_tokens": -1}},
            id="tokens-negative-count",
        ),
        pytest.param({"largest_result_chars": -1}, id="chars-negative"),
        pytest.param({"repeated_reads": {"/a": -1}}, id="reads-negative"),
        pytest.param({"largest_result_chars": True}, id="chars-bool"),
    ],
)
def test_damaged_tokens_field_rejects_line(overrides: dict[str, object]) -> None:
    """Behavior: a present-but-damaged token field rejects the whole digest line.

    Args:
        overrides: Digest-line fields replacing the valid defaults.
    """
    assert agent_profile._stats_from_digest(_digest_line("a1", **overrides)) is None


_ENDED = _at(30)


def _known(*, tokens: TokenUsage | None, turns: int = 2) -> TranscriptStats:
    """Stats with a fixed end, as a digest line or a fresh parse would hold.

    Args:
        tokens: The usage, or ``None`` for unknown.
        turns: Turn count.

    Returns:
        The stats.
    """
    return TranscriptStats(started=BASE_DT, ended=_ENDED, turns=turns, tokens=tokens)


@pytest.mark.parametrize(
    ("known_tokens", "new_tokens", "behind"),
    [
        pytest.param(None, TokenUsage(requests=1), True, id="tokens-newly-available"),
        pytest.param(None, None, False, id="still-unknown"),
        pytest.param(
            TokenUsage(requests=1), TokenUsage(requests=1), False, id="unchanged"
        ),
        pytest.param(TokenUsage(requests=1), None, False, id="transcript-lost-tokens"),
    ],
)
def test_known_run_with_tokens_none_is_behind_once_transcript_yields_tokens(
    known_tokens: TokenUsage | None, new_tokens: TokenUsage | None, *, behind: bool
) -> None:
    """Behavior: an old digest line is re-recorded once, only once tokens appear.

    Args:
        known_tokens: Token usage recorded in the existing digest line.
        new_tokens: Token usage a fresh transcript parse yields.
        behind: Expected verdict on whether the digest line is behind.
    """
    result = agent_profile._digest_is_behind(
        _known(tokens=new_tokens), _known(tokens=known_tokens)
    )

    assert result is behind


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        pytest.param("my repo", "my-repo", id="space"),
        pytest.param("a.b_c", "a-b-c", id="dot-and-underscore"),
        pytest.param("x--y", "x--y", id="dashes-kept"),
    ],
)
def test_default_transcripts_dir_encodes_non_alphanumerics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, encoded: str
) -> None:
    """Behavior: the directory name is Claude Code's encoding of the absolute path.

    Args:
        name: Repository directory name containing non-alphanumerics.
        encoded: Expected encoded suffix of the transcripts directory name.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    prefix = re.sub(r"[^A-Za-z0-9]", "-", str(tmp_path.resolve()))
    expected = home / ".claude" / "projects" / f"{prefix}-{encoded}"
    expected.mkdir(parents=True)

    assert default_transcripts_dir(tmp_path / name) == expected


def test_default_transcripts_dir_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavior: no Claude Code on the machine (CI) means no transcripts, no error."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert default_transcripts_dir(tmp_path / "repo") is None


def test_meta_agent_type_beats_session_pairing(tmp_path: Path) -> None:
    """Behavior: sidecar names the type; session pairing and sidecar fill the rest."""
    root = tmp_path
    _write_jsonl(
        root / "s1.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": _iso(_at(0)),
                "message": {
                    "content": [
                        _tool_use(
                            "t1",
                            "Agent",
                            {
                                "subagent_type": "forge:paired",
                                "description": "from session",
                            },
                        )
                    ]
                },
            },
            _user_tool_result(_at(1), "t1", agent_id="a1"),
        ],
    )
    for agent_id in ("a1", "a2"):
        _write_jsonl(
            root / "sess9" / "subagents" / f"agent-{agent_id}.jsonl",
            [_request(0, f"m-{agent_id}", usage=(1, 0, 0, 0))],
        )
    (root / "sess9" / "subagents" / "agent-a1.meta.json").write_text(
        json.dumps({"agentType": "forge:meta", "description": "ignored"}),
        encoding="utf-8",
    )
    (root / "sess9" / "subagents" / "agent-a2.meta.json").write_text(
        json.dumps({"description": "from sidecar"}), encoding="utf-8"
    )

    runs = runs_from_transcripts(root)

    assert runs["a1"].agent_type == "forge:meta"
    assert runs["a1"].description == "from session"
    assert runs["a1"].session_id == "sess9"
    assert runs["a2"].agent_type == "?"
    assert runs["a2"].description == "from sidecar"


# ---------------------------------------------------------------------------
# waste flags
# ---------------------------------------------------------------------------

_PROMPT = 1000
_AT_THRESHOLD = int(
    FIXED_SHARE_THRESHOLD * _PROMPT / 2
)  # two requests -> exactly the share


@pytest.mark.parametrize(
    ("tool_calls", "fixed", "largest", "reads", "expected"),
    [
        pytest.param(1, 1, 0, {}, ["single-call"], id="single-call"),
        pytest.param(
            FEW_TOOL_CALLS,
            _AT_THRESHOLD,
            0,
            {},
            ["fixed-context"],
            id="fixed-at-threshold",
        ),
        pytest.param(
            FEW_TOOL_CALLS, _AT_THRESHOLD - 1, 0, {}, [], id="fixed-below-threshold"
        ),
        pytest.param(
            FEW_TOOL_CALLS + 1, _AT_THRESHOLD, 0, {}, [], id="fixed-but-many-calls"
        ),
        pytest.param(
            FEW_TOOL_CALLS + 1,
            1,
            LARGE_RESULT_CHARS,
            {},
            ["large-result"],
            id="large-result",
        ),
        pytest.param(
            FEW_TOOL_CALLS + 1,
            1,
            LARGE_RESULT_CHARS - 1,
            {},
            [],
            id="result-below-limit",
        ),
        pytest.param(
            FEW_TOOL_CALLS + 1,
            1,
            0,
            {"/a/x.py": 400},
            ["repeated-read"],
            id="repeated-read",
        ),
    ],
)
def test_waste_flags(
    tool_calls: int,
    fixed: int,
    largest: int,
    reads: dict[str, int],
    expected: list[str],
) -> None:
    """Behavior: each waste flag fires exactly at its documented threshold.

    Args:
        tool_calls: Tool-call count recorded for the run.
        fixed: Fixed prompt tokens recorded for the run.
        largest: Largest tool result size in characters.
        reads: Per-path repeated-read counts.
        expected: Waste flag kinds expected to fire, in order.
    """
    usage = TokenUsage(input_tokens=_PROMPT, fixed_tokens=fixed, requests=2)
    stats = TranscriptStats(
        tool_calls=tool_calls,
        tokens=usage,
        largest_result_chars=largest,
        largest_result="Bash: cat",
        repeated_reads=reads,
    )
    run = AgentRun(agent_id="a1", agent_type="forge:x", stats=stats)

    assert [flag.kind for flag in waste_flags(run)] == expected


def test_waste_flags_empty_without_token_data() -> None:
    """Behavior: a run with unknown cost is never flagged on guesses."""
    stats = TranscriptStats(
        tool_calls=0,
        largest_result_chars=LARGE_RESULT_CHARS,
        repeated_reads={"/a": 400},
    )

    assert waste_flags(AgentRun("a1", "forge:x", stats=stats)) == []
    assert waste_flags(AgentRun("a2", "forge:x", stats=None)) == []


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_render_tokens_sections_unknown_run_count_and_delegation_line() -> None:
    """Behavior: report names every table, the left-out runs, the delegation cost."""
    usage = TokenUsage(
        input_tokens=2000, fixed_tokens=500, requests=4, output_tokens=40
    )
    known = AgentRun(
        "a1",
        "forge:x",
        stats=TranscriptStats(tool_calls=9, tokens=usage),
        caller_skill="perf",
    )
    unknown = AgentRun("a2", "forge:y", stats=None)
    session = SessionTokens(
        by_skill={"perf": TokenUsage(input_tokens=900, requests=2)},
        brief_chars={"t1": 40},
        handback_chars={"a1": 80},
    )

    report = render_tokens([known, unknown], {"s1": session})

    assert "Subagents, per agent type:" in report
    assert "forge:x" in report
    assert "Subagents, per skill that launched them:" in report
    assert "Main session, per active skill:" in report
    assert "1 run(s) without token data left out." in report
    assert "Delegations: 1 — briefs 40 chars sent, reports 80 chars returned" in report
    assert "Waste flags: 0" in report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_tokens_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Behavior: --tokens finds this checkout's transcripts, joins them, logs total."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    project = (
        home
        / ".claude"
        / "projects"
        / re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))
    )
    _write_jsonl(
        project / "s1.jsonl",
        [
            _request(
                0,
                "m1",
                usage=(50, 0, 0, 0),
                blocks=[
                    _tool_use(
                        "t1", "Agent", {"prompt": "do it", "subagent_type": "forge:x"}
                    )
                ],
                skill="perf",
            ),
            _user_tool_result(_at(1), "t1", agent_id="a1"),
        ],
    )
    sub = project / "s1" / "subagents"
    _write_jsonl(
        sub / "agent-a1.jsonl",
        [
            _request(0, "m1", usage=(100, 0, 0, 5)),
            _request(1, "m2", usage=(100, 0, 100, 5)),
        ],
    )
    (sub / "agent-a1.meta.json").write_text(
        json.dumps({"toolUseId": "t1"}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agent_profile, "repo_root", lambda: repo)
    monkeypatch.setattr("sys.argv", ["forge-agent-profile", "--tokens", "--label", "t"])

    with caplog.at_level(logging.INFO, logger="forge.agent_profile"):
        code = agent_profile.main()

    assert code == 0
    assert "Subagents, per agent type:" in caplog.text
    assert "Subagents, per skill that launched them:" in caplog.text
    assert "Main session, per active skill:" in caplog.text
    history = (code_health_dir(repo) / agent_profile.HISTORY_NAME).read_text()
    assert re.search(r"\btokens=\d+", history)
    assert "tokens=0" not in history


def test_default_report_does_not_read_default_transcripts_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavior: without --tokens the report keeps reading the ledger alone."""

    def _explode(_root: Path) -> Path | None:
        msg = "default report must not look for transcripts"
        raise AssertionError(msg)

    monkeypatch.setattr(agent_profile, "default_transcripts_dir", _explode)
    monkeypatch.setattr(agent_profile, "repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-agent-profile"])

    assert agent_profile.main() == 0
