"""Behavior tests for vibe_sessions journal parsing and repo matching.

Covers timestamp DFS, action dedupe, token accumulation + projection
fallback, idle-gap clipping, malformed lines, and cwd/slug repo matching.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge.vibe_sessions import (
    parse_vibe_journal,
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
