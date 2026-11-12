"""Behavior tests for vibe_sessions journal parsing and repo matching.

Covers timestamp DFS, action dedupe, token accumulation + projection
fallback, idle-gap clipping, malformed lines, and cwd/slug repo matching.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge.agent_profile_tokens import NO_SKILL
from forge.vibe_sessions import (
    main_sessions_from_vibe,
    parse_vibe_journal,
    parse_vibe_main_journal,
    runs_from_vibe_sessions,
)


def _ts(ms: int) -> datetime:
    """Epoch milliseconds to a UTC datetime (tests compute, never hardcode).

    Args:
        ms: Epoch milliseconds.

    Returns:
        The corresponding UTC datetime.
    """
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _action_result(
    action_id: str, ms: int, *, parts: list | None = None, usage: dict | None = None
) -> dict:
    """One action_result journal record in Vibe's real nesting.

    Args:
        action_id: The completion's action id.
        ms: Epoch-millisecond timestamp.
        parts: The completion's content parts (tool calls).
        usage: The completion's token usage, when present.

    Returns:
        The journal record as a dict.
    """
    completion: dict = {"parts": parts or []}
    if usage is not None:
        completion["usage"] = usage
    return {
        "type": "action_result",
        "payload": {"action_id": action_id, "result": {"result": completion}},
        "time_unix_ms": ms,
    }


def _projection_delta(ms: int, token_usage: dict) -> dict:
    """One projection_delta journal record carrying cumulative tokenUsage.

    Args:
        ms: Epoch-millisecond timestamp.
        token_usage: The session's cumulative tokenUsage mapping.

    Returns:
        The journal record as a dict.
    """
    return {
        "type": "projection_delta",
        "payload": {
            "delta": [
                {
                    "op": "set_envelope",
                    "state": {"session": {"tokenUsage": token_usage}},
                }
            ]
        },
        "time_unix_ms": ms,
    }


class TestParseVibeJournal:
    """Tests for parse_vibe_journal tolerant parsing of journal lines."""

    def _make_session_dir(self, session_dir: Path, cwd: str | Path = "/") -> None:
        """Create a valid session directory structure.

        Args:
            session_dir: The session directory to create.
            cwd: The working directory to record in its metadata.
        """
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text(
            '{"generation": "gen_1"}', encoding="utf-8"
        )
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": session_dir.name,
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(cwd)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))

    def test_empty_journal_yields_none(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        self._make_session_dir(session_dir, tmp_path)
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        # Empty journal returns None when no timestamp found
        assert parse_vibe_journal(session_dir) is None

    def test_single_core_input_record_provides_started(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        self._make_session_dir(session_dir, tmp_path)
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        record = {
            "type": "core_input",
            "time_unix_ms": 1717000000000,  # 2024-05-30T00:00:00Z
        }
        (journal_dir / "records.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )
        run = parse_vibe_journal(session_dir)
        assert run is not None
        assert run.started == _ts(1717000000000)

    def test_nested_time_unix_ms_in_action_result(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        self._make_session_dir(session_dir, tmp_path)
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        record = {
            "type": "action_result",
            "time_unix_ms": 1717000100000,  # 2024-05-30T00:01:40Z
        }
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        run = parse_vibe_journal(session_dir)
        assert run is not None
        assert run.started == _ts(1717000100000)

    def test_ts_ms_fallback_timestamp(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        self._make_session_dir(session_dir, tmp_path)
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        record = {"type": "core_input", "ts_ms": 1717000200000}
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        run = parse_vibe_journal(session_dir)
        assert run is not None
        assert run.started == _ts(1717000200000)

    def test_no_timestamp_returns_none_started(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        self._make_session_dir(session_dir, tmp_path)
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        record = {"type": "projection_delta", "tokens": 100}
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        run = parse_vibe_journal(session_dir)
        assert run is None

    def test_malformed_json_line_is_skipped(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "session_123",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        (journal_dir / "records.jsonl").write_text("not json\n")
        run = parse_vibe_journal(session_dir)
        assert run is None

    def test_missing_type_field_record_is_skipped(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "session_123",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        record = {"note": "no type, no timestamp"}
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        run = parse_vibe_journal(session_dir)
        assert run is None

    def test_duplicate_action_ids_are_deduped_by_last(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "session_123",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        tool = {"type": "tool_call", "name": "Bash", "arguments_json": "{}", "id": "t1"}
        rec1 = _action_result("a1", 1717000000000, parts=[tool])
        rec2 = _action_result("a1", 1717000010000, parts=[tool, tool])
        rec3 = _action_result("a2", 1717000020000, parts=[tool])
        lines = "\n".join(json.dumps(r) for r in [rec1, rec2, rec3]) + "\n"
        (journal_dir / "records.jsonl").write_text(lines)
        run = parse_vibe_journal(session_dir)
        assert run is not None
        # a1 is one completion despite appearing twice
        assert run.turns == 2
        # tool calls counted per completion: 1 (a1 first sighting) + 1 (a2)
        assert run.tool_calls == 2

    def test_token_accumulation_from_projection_delta(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "session_123",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        usage = {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 5}
        rec1 = _action_result("a1", 1717000000000, usage=usage)
        rec2 = _action_result(
            "a2", 1717000010000, usage={"input_tokens": 50, "output_tokens": 5}
        )
        lines = "\n".join(json.dumps(r) for r in [rec1, rec2]) + "\n"
        (journal_dir / "records.jsonl").write_text(lines)
        run = parse_vibe_journal(session_dir)
        assert run is not None
        assert run.tokens.input_tokens == 150
        assert run.tokens.output_tokens == 15
        assert run.tokens.cache_read_tokens == 5
        assert run.tokens.requests == 2

    def test_projection_delta_fallback_when_no_action_results(
        self, tmp_path: Path
    ) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "session_123",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        rec = _projection_delta(
            1717000000000,
            {"inputTokens": 200, "outputTokens": 20, "cachedInputTokens": 7},
        )
        (journal_dir / "records.jsonl").write_text(json.dumps(rec) + "\n")
        run = parse_vibe_journal(session_dir)
        assert run is not None
        assert run.tokens.input_tokens == 200
        assert run.tokens.output_tokens == 20
        assert run.tokens.cache_read_tokens == 7

    def test_idle_gap_clipping_removes_records_after_gap(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "session_123",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        base = datetime(2024, 5, 30, 0, 0, 0, tzinfo=UTC)
        rec1 = {"type": "core_input", "time_unix_ms": int(base.timestamp() * 1000)}
        rec2 = {
            "type": "core_input",
            "time_unix_ms": int((base + timedelta(seconds=200)).timestamp() * 1000),
        }
        rec3 = {
            "type": "core_input",
            "time_unix_ms": int((base + timedelta(seconds=700)).timestamp() * 1000),
        }
        lines = "\n".join(json.dumps(r) for r in [rec1, rec2, rec3]) + "\n"
        (journal_dir / "records.jsonl").write_text(lines)
        run = parse_vibe_journal(session_dir)
        assert run is not None
        # gap rec2→rec3 is 500s > IDLE_GAP_S, clipped; only the 200s gap counts
        assert run.active_s == 200.0


class TestRunsFromVibeSessions:
    """Tests for runs_from_vibe_sessions repo matching and filtering."""

    @pytest.fixture
    def mock_github_slug(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Patch forge.vibe_sessions.github_slug to recognise one fake repo."""
        # Create a minimal git repo for github_slug
        repo_a = tmp_path / "repo_a"
        repo_a.mkdir()
        (repo_a / ".git").mkdir()
        (repo_a / ".git" / "config").write_text(
            '[remote "origin"]\n    url = https://github.com/owner/repo_a.git\n'
        )

        def fake_slug(path: Path) -> str | None:
            if path.resolve() == repo_a.resolve():
                return "owner/repo_a"
            return None

        monkeypatch.setattr("forge.vibe_sessions.github_slug", fake_slug)

    def _make_subagent_session(self, session_dir: Path, cwd: str | Path) -> None:
        """Create a valid subagent session directory.

        Args:
            session_dir: The session directory to create.
            cwd: The working directory to record in its metadata.
        """
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text(
            '{"generation": "gen_1"}', encoding="utf-8"
        )
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": session_dir.name,
                "kind": "subagent",
                "parent_session_id": "session_root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(cwd)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))

    def test_missing_sessions_dir_returns_empty_dict(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        sessions_dir = tmp_path / "sessions"
        assert (
            runs_from_vibe_sessions(root=Path("/repo_a"), sessions_dir=sessions_dir)
            == {}
        )

    @pytest.mark.usefixtures("mock_github_slug")
    def test_root_sessions_are_skipped(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        sessions_dir = tmp_path / "sessions"
        root_session = sessions_dir / "session_root"
        self._make_subagent_session(root_session, "/repo_a")
        # Change kind to root
        gen_path = root_session / "generations" / "gen_1"
        payload = json.loads((gen_path / "runtime-state.json").read_text())
        payload["identity"]["kind"] = "root"
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        runs = runs_from_vibe_sessions(root=Path("/repo_a"), sessions_dir=sessions_dir)
        assert runs == {}

    @pytest.mark.usefixtures("mock_github_slug")
    def test_subagent_sessions_collected_by_cwd_match(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        sessions_dir = tmp_path / "sessions"
        sub_session = sessions_dir / "child-session_123"
        self._make_subagent_session(sub_session, "/repo_a")
        # Add a valid journal with timestamp
        journal_dir = sub_session / "journal"
        journal_dir.mkdir(parents=True)
        record = {"type": "core_input", "time_unix_ms": 1717000000000}
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        runs = runs_from_vibe_sessions(root=Path("/repo_a"), sessions_dir=sessions_dir)
        assert "vibe:child-session_123" in runs
        assert runs["vibe:child-session_123"].session_id == "child-session_123"

    @pytest.mark.usefixtures("mock_github_slug")
    def test_sessions_from_other_repo_excluded(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        sessions_dir = tmp_path / "sessions"
        other_session = sessions_dir / "child-session_other"
        self._make_subagent_session(other_session, "/repo_b")
        runs = runs_from_vibe_sessions(root=Path("/repo_a"), sessions_dir=sessions_dir)
        assert "vibe:child-session_other" not in runs

    @pytest.mark.usefixtures("mock_github_slug")
    def test_missing_cwd_in_metadata_skips_session(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        sessions_dir = tmp_path / "sessions"
        session = sessions_dir / "child-session_no_cwd"
        self._make_subagent_session(session, "/repo_a")
        # Remove cwd from payload
        gen_path = session / "generations" / "gen_1"
        payload = json.loads((gen_path / "runtime-state.json").read_text())
        del payload["session_metadata"]["environment"]["working_directory"]
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        runs = runs_from_vibe_sessions(root=Path("/repo_a"), sessions_dir=sessions_dir)
        assert "vibe:child-session_no_cwd" not in runs

    @pytest.mark.usefixtures("mock_github_slug")
    def test_multiple_sessions_collected(self, tmp_path: Path) -> None:
        """Tests for Vibe journal parsing and run aggregation."""
        sessions_dir = tmp_path / "sessions"
        for name in ["child-a", "child-b"]:
            session = sessions_dir / name
            self._make_subagent_session(session, "/repo_a")
            # Add a valid journal with timestamp
            journal_dir = session / "journal"
            journal_dir.mkdir(parents=True)
            record = {"type": "core_input", "time_unix_ms": 1717000000000}
            (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        runs = runs_from_vibe_sessions(root=Path("/repo_a"), sessions_dir=sessions_dir)
        assert set(runs.keys()) == {"vibe:child-a", "vibe:child-b"}


def _skill_part(name: str, part_id: str = "skill-call-1") -> dict:
    """One ``skill`` tool_call part in Vibe's real nesting.

    Args:
        name: The skill id the call loads.
        part_id: The tool call's id.

    Returns:
        The part as a dict.
    """
    return {
        "type": "tool_call",
        "name": "skill",
        "id": part_id,
        "arguments_json": json.dumps({"name": name}),
    }


def _make_root_session(
    session_dir: Path, cwd: str | Path, *, kind: str = "root"
) -> None:
    """Create a valid session directory with the given identity kind.

    Args:
        session_dir: The session directory to create.
        cwd: The working directory to record in its metadata.
        kind: The identity kind ("root" or "subagent").
    """
    session_dir.mkdir(parents=True)
    (session_dir / "CURRENT").write_text('{"generation": "gen_1"}', encoding="utf-8")
    gen_path = session_dir / "generations" / "gen_1"
    gen_path.mkdir(parents=True)
    payload = {
        "identity": {
            "session_id": session_dir.name,
            "kind": kind,
            "parent_session_id": None if kind == "root" else "root",
        },
        "session_metadata": {
            "environment": {"working_directory": str(cwd)},
        },
    }
    (gen_path / "runtime-state.json").write_text(json.dumps(payload))


def _write_journal(session_dir: Path, records: list) -> None:
    """Write journal records to one jsonl file in the session directory.

    Args:
        session_dir: The session directory.
        records: The records to write, one per line.
    """
    journal_dir = session_dir / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "records.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


class TestParseVibeMainJournal:
    """Tests for parse_vibe_main_journal per-skill token folding."""

    def test_no_timestamp_returns_none(self, tmp_path: Path) -> None:
        """A journal without any timestamp yields None, like parse_vibe_journal."""
        session_dir = tmp_path / "root_1"
        _make_root_session(session_dir, tmp_path)
        _write_journal(session_dir, [{"type": "projection_delta", "payload": {}}])
        assert parse_vibe_main_journal(session_dir) is None

    def test_usage_without_skill_lands_in_no_skill(self, tmp_path: Path) -> None:
        """Completions before any skill call are attributed to NO_SKILL."""
        session_dir = tmp_path / "root_1"
        _make_root_session(session_dir, tmp_path)
        usage = {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 5}
        _write_journal(
            session_dir,
            [_action_result("a1", 1717000000000, usage=usage)],
        )
        session = parse_vibe_main_journal(session_dir)
        assert session is not None
        assert set(session.by_skill) == {NO_SKILL}
        tokens = session.by_skill[NO_SKILL]
        assert tokens.input_tokens == 100
        assert tokens.output_tokens == 10
        assert tokens.cache_read_tokens == 5
        assert tokens.requests == 1
        # first request of the bucket sets its fixed context
        assert tokens.fixed_tokens == 105

    def test_skill_call_switches_cursor_after_its_own_completion(
        self, tmp_path: Path
    ) -> None:
        """The completion carrying the skill call stays on the old skill."""
        session_dir = tmp_path / "root_1"
        _make_root_session(session_dir, tmp_path)
        _write_journal(
            session_dir,
            [
                _action_result(
                    "a1",
                    1717000000000,
                    parts=[_skill_part("pr-manager")],
                    usage={"input_tokens": 100, "output_tokens": 10},
                ),
                _action_result(
                    "a2",
                    1717000010000,
                    usage={"input_tokens": 50, "output_tokens": 5},
                ),
            ],
        )
        session = parse_vibe_main_journal(session_dir)
        assert session is not None
        assert session.by_skill[NO_SKILL].input_tokens == 100
        assert session.by_skill["pr-manager"].input_tokens == 50
        # each bucket's fixed context is its own first request
        assert session.by_skill[NO_SKILL].fixed_tokens == 100
        assert session.by_skill["pr-manager"].fixed_tokens == 50

    def test_duplicate_action_ids_counted_once(self, tmp_path: Path) -> None:
        """A repeated action_result is folded once."""
        session_dir = tmp_path / "root_1"
        _make_root_session(session_dir, tmp_path)
        rec = _action_result(
            "a1", 1717000000000, usage={"input_tokens": 100, "output_tokens": 10}
        )
        _write_journal(session_dir, [rec, rec])
        session = parse_vibe_main_journal(session_dir)
        assert session is not None
        assert session.by_skill[NO_SKILL].requests == 1
        assert session.by_skill[NO_SKILL].input_tokens == 100

    def test_projection_fallback_when_no_completion_usage(self, tmp_path: Path) -> None:
        """Cumulative usage lands in the final cursor bucket as one request."""
        session_dir = tmp_path / "root_1"
        _make_root_session(session_dir, tmp_path)
        _write_journal(
            session_dir,
            [
                _action_result("a1", 1717000000000, parts=[_skill_part("triage")]),
                _projection_delta(
                    1717000010000,
                    {"inputTokens": 200, "outputTokens": 20, "cachedInputTokens": 7},
                ),
            ],
        )
        session = parse_vibe_main_journal(session_dir)
        assert session is not None
        tokens = session.by_skill["triage"]
        assert tokens.requests == 1
        assert tokens.input_tokens == 200
        assert tokens.output_tokens == 20
        assert tokens.cache_read_tokens == 7

    def test_first_skill_part_in_one_completion_wins(self, tmp_path: Path) -> None:
        """Two skill calls in one completion switch to the first one only."""
        session_dir = tmp_path / "root_1"
        _make_root_session(session_dir, tmp_path)
        _write_journal(
            session_dir,
            [
                _action_result(
                    "a1",
                    1717000000000,
                    parts=[_skill_part("first"), _skill_part("second", "skill-call-2")],
                    usage={"input_tokens": 10, "output_tokens": 1},
                ),
                _action_result(
                    "a2", 1717000010000, usage={"input_tokens": 5, "output_tokens": 1}
                ),
            ],
        )
        session = parse_vibe_main_journal(session_dir)
        assert session is not None
        assert session.by_skill["first"].input_tokens == 5
        assert "second" not in session.by_skill

    def test_unparseable_skill_arguments_keep_cursor(self, tmp_path: Path) -> None:
        """A malformed arguments_json does not switch the active skill."""
        session_dir = tmp_path / "root_1"
        _make_root_session(session_dir, tmp_path)
        bad = {
            "type": "tool_call",
            "name": "skill",
            "id": "s1",
            "arguments_json": "not json",
        }
        _write_journal(
            session_dir,
            [
                _action_result(
                    "a1",
                    1717000000000,
                    parts=[bad],
                    usage={"input_tokens": 10, "output_tokens": 1},
                ),
                _action_result(
                    "a2", 1717000010000, usage={"input_tokens": 5, "output_tokens": 1}
                ),
            ],
        )
        session = parse_vibe_main_journal(session_dir)
        assert session is not None
        assert set(session.by_skill) == {NO_SKILL}
        assert session.by_skill[NO_SKILL].input_tokens == 15

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        """A non-JSON line does not abort the fold."""
        session_dir = tmp_path / "root_1"
        _make_root_session(session_dir, tmp_path)
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        good = json.dumps(
            _action_result("a1", 1717000000000, usage={"input_tokens": 10})
        )
        (journal_dir / "records.jsonl").write_text(f"not json\n{good}\n")
        session = parse_vibe_main_journal(session_dir)
        assert session is not None
        assert session.by_skill[NO_SKILL].input_tokens == 10


class TestMainSessionsFromVibe:
    """Tests for main_sessions_from_vibe root-session selection and repo matching."""

    @pytest.fixture
    def mock_github_slug(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Patch forge.vibe_sessions.github_slug to recognise one fake repo."""
        repo_a = tmp_path / "repo_a"
        repo_a.mkdir()
        (repo_a / ".git").mkdir()
        (repo_a / ".git" / "config").write_text(
            '[remote "origin"]\n    url = https://github.com/owner/repo_a.git\n'
        )

        def fake_slug(path: Path) -> str | None:
            if path.resolve() == repo_a.resolve():
                return "owner/repo_a"
            return None

        monkeypatch.setattr("forge.vibe_sessions.github_slug", fake_slug)

    def test_missing_sessions_dir_returns_empty_dict(self, tmp_path: Path) -> None:
        """A nonexistent store yields no sessions."""
        assert (
            main_sessions_from_vibe(
                root=Path("/repo_a"), sessions_dir=tmp_path / "nope"
            )
            == {}
        )

    @pytest.mark.usefixtures("mock_github_slug")
    def test_root_session_collected_by_cwd_match(self, tmp_path: Path) -> None:
        """A root session in the repo is keyed vibe:<session_id>."""
        sessions_dir = tmp_path / "sessions"
        root_session = sessions_dir / "root-uuid"
        _make_root_session(root_session, "/repo_a")
        _write_journal(
            root_session,
            [_action_result("a1", 1717000000000, usage={"input_tokens": 10})],
        )
        sessions = main_sessions_from_vibe(
            root=Path("/repo_a"), sessions_dir=sessions_dir
        )
        assert set(sessions) == {"vibe:root-uuid"}
        assert sessions["vibe:root-uuid"].by_skill[NO_SKILL].input_tokens == 10

    @pytest.mark.usefixtures("mock_github_slug")
    def test_subagent_sessions_are_skipped(self, tmp_path: Path) -> None:
        """Only kind == "root" sessions are main sessions."""
        sessions_dir = tmp_path / "sessions"
        child = sessions_dir / "child-1"
        _make_root_session(child, "/repo_a", kind="subagent")
        _write_journal(child, [{"type": "core_input", "time_unix_ms": 1717000000000}])
        assert (
            main_sessions_from_vibe(root=Path("/repo_a"), sessions_dir=sessions_dir)
            == {}
        )

    @pytest.mark.usefixtures("mock_github_slug")
    def test_sessions_from_other_repo_excluded(self, tmp_path: Path) -> None:
        """A root session in another repo's checkout is not collected."""
        sessions_dir = tmp_path / "sessions"
        other = sessions_dir / "root-other"
        _make_root_session(other, "/repo_b")
        _write_journal(other, [{"type": "core_input", "time_unix_ms": 1717000000000}])
        assert (
            main_sessions_from_vibe(root=Path("/repo_a"), sessions_dir=sessions_dir)
            == {}
        )

    @pytest.mark.usefixtures("mock_github_slug")
    def test_missing_cwd_skips_session(self, tmp_path: Path) -> None:
        """A root session without a recorded cwd cannot be attributed."""
        sessions_dir = tmp_path / "sessions"
        session = sessions_dir / "root-nocwd"
        _make_root_session(session, "/repo_a")
        gen_path = session / "generations" / "gen_1"
        payload = json.loads((gen_path / "runtime-state.json").read_text())
        del payload["session_metadata"]["environment"]["working_directory"]
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        _write_journal(session, [{"type": "core_input", "time_unix_ms": 1717000000000}])
        assert (
            main_sessions_from_vibe(root=Path("/repo_a"), sessions_dir=sessions_dir)
            == {}
        )
