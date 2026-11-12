"""Tests for agent_profile merge_vibe_runs and related helpers.

Verifies merge semantics: vibe-prefixed keys, ledger wins on collision.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from forge.agent_profile import (
    AgentRun,
    _vibe_run_to_agent_run,
    load_sessions,
    merge_vibe_runs,
)
from forge.agent_profile_tokens import NO_SKILL
from forge.vibe_sessions import parse_vibe_journal


def _ts(ms: int) -> datetime:
    """Epoch milliseconds to a UTC datetime (tests compute, never hardcode).

    Args:
        ms: Epoch milliseconds.

    Returns:
        The corresponding UTC datetime.
    """
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class TestVibeRunToAgentRun:
    """Tests for _vibe_run_to_agent_run conversion."""

    def test_converts_vibe_run_to_agent_run(self, tmp_path: Path) -> None:
        """Tests for _vibe_run_to_agent_run conversion."""
        started = _ts(1717000000000)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        sub_session = sessions_dir / "s1"
        sub_session.mkdir(parents=True)
        (sub_session / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = sub_session / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "s1",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = sub_session / "journal"
        journal_dir.mkdir(parents=True)
        record = {"type": "core_input", "time_unix_ms": 1717000000000}
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        vibe_run = parse_vibe_journal(sub_session)
        assert vibe_run is not None
        agent_run = _vibe_run_to_agent_run("vibe:s1", vibe_run)
        assert agent_run.session_id == "s1"
        assert agent_run.started == started
        assert agent_run.agent_id == "vibe:s1"


class TestMergeVibeRuns:
    """Tests for merge_vibe_runs ledger-wins semantics and vibe: prefix handling."""

    def test_empty_vibe_runs_yields_no_change(self) -> None:
        """Tests for merge_vibe_runs ledger-wins semantics and vibe: prefix handling."""
        runs = {}
        merge_vibe_runs(runs, root=Path("/repo"), sessions_dir=None)
        assert runs == {}

    def test_single_vibe_run_becomes_agent_run(self, tmp_path: Path) -> None:
        """Tests for _vibe_run_to_agent_run conversion."""
        _ts(1717000000000)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        sub_session = sessions_dir / "s1"
        sub_session.mkdir(parents=True)
        (sub_session / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = sub_session / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "s1",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = sub_session / "journal"
        journal_dir.mkdir(parents=True)
        record = {"type": "core_input", "time_unix_ms": 1717000000000}
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        runs = {}
        merge_vibe_runs(runs, root=tmp_path, sessions_dir=sessions_dir)
        assert len(runs) == 1
        assert "vibe:s1" in runs
        assert runs["vibe:s1"].session_id == "s1"

    def test_ledger_wins_over_vibe_run(self, tmp_path: Path) -> None:
        """Tests for merge_vibe_runs ledger-wins semantics and vibe: prefix handling."""
        started = _ts(1717000000000)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        sub_session = sessions_dir / "s1"
        sub_session.mkdir(parents=True)
        (sub_session / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = sub_session / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "s1",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = sub_session / "journal"
        journal_dir.mkdir(parents=True)
        record = {"type": "core_input", "time_unix_ms": 1717000000000}
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        runs = {
            "vibe:s1": AgentRun(
                agent_id="vibe:s1",
                agent_type="my-agent",
                session_id="s1",
                started=started,
                ended=started,
            )
        }
        merge_vibe_runs(runs, root=tmp_path, sessions_dir=sessions_dir)
        # merge_vibe_runs does not overwrite existing runs; ledger wins.
        assert runs["vibe:s1"].agent_type == "my-agent"

    def test_vibe_prefixed_keys_preserved_in_agent_run(self, tmp_path: Path) -> None:
        """Tests for merge_vibe_runs ledger-wins semantics and vibe: prefix handling."""
        _ts(1717000000000)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        sub_session = sessions_dir / "s1"
        sub_session.mkdir(parents=True)
        (sub_session / "CURRENT").write_text("gen_1", encoding="utf-8")
        gen_path = sub_session / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "s1",
                "kind": "subagent",
                "parent_session_id": "root",
            },
            "session_metadata": {
                "environment": {"working_directory": str(tmp_path)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = sub_session / "journal"
        journal_dir.mkdir(parents=True)
        record = {"type": "core_input", "time_unix_ms": 1717000000000}
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        runs = {}
        merge_vibe_runs(runs, root=tmp_path, sessions_dir=sessions_dir)
        assert "vibe:s1" in runs

    def test_multiple_vibe_runs_merged(self, tmp_path: Path) -> None:
        """Tests for merge_vibe_runs ledger-wins semantics and vibe: prefix handling."""
        _ts(1717000000000)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        for name in ("s1", "s2"):
            sub_session = sessions_dir / name
            sub_session.mkdir(parents=True)
            (sub_session / "CURRENT").write_text("gen_1", encoding="utf-8")
            gen_path = sub_session / "generations" / "gen_1"
            gen_path.mkdir(parents=True)
            payload = {
                "identity": {
                    "session_id": name,
                    "kind": "subagent",
                    "parent_session_id": "root",
                },
                "session_metadata": {"cwd": str(tmp_path)},
            }
            (gen_path / "runtime-state.json").write_text(json.dumps(payload))
            journal_dir = sub_session / "journal"
            journal_dir.mkdir(parents=True)
            record = {"type": "core_input", "time_unix_ms": 1717000000000}
            (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")
        runs = {}
        merge_vibe_runs(runs, root=tmp_path, sessions_dir=sessions_dir)
        assert set(runs.keys()) == {"vibe:s1", "vibe:s2"}


class TestLoadSessionsVibe:
    """Tests for load_sessions merging Claude transcripts and Vibe root sessions."""

    @staticmethod
    def _make_root_session(session_dir: Path, cwd: str | Path) -> None:
        """Create a valid root session directory.

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
                "kind": "root",
                "parent_session_id": None,
            },
            "session_metadata": {
                "environment": {"working_directory": str(cwd)},
            },
        }
        (gen_path / "runtime-state.json").write_text(json.dumps(payload))
        journal_dir = session_dir / "journal"
        journal_dir.mkdir(parents=True)
        record = {
            "type": "action_result",
            "payload": {
                "action_id": "a1",
                "result": {
                    "result": {
                        "parts": [],
                        "usage": {"input_tokens": 42, "output_tokens": 4},
                    }
                },
            },
            "time_unix_ms": 1717000000000,
        }
        (journal_dir / "records.jsonl").write_text(json.dumps(record) + "\n")

    def test_claude_and_vibe_sessions_merged(self, tmp_path: Path) -> None:
        """Claude-keyed and vibe:-keyed main sessions coexist in one report."""
        transcripts = tmp_path / "projects"
        transcripts.mkdir()
        session_id = "claude-session"
        record = {
            "type": "assistant",
            "message": {
                "id": "m1",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "content": [],
            },
        }
        (transcripts / f"{session_id}.jsonl").write_text(json.dumps(record) + "\n")
        runs = [AgentRun(agent_id="a1", agent_type="t", session_id=session_id)]
        sessions_dir = tmp_path / "unified"
        self._make_root_session(sessions_dir / "root-uuid", tmp_path)
        sessions = load_sessions(
            transcripts, runs, root=tmp_path, vibe_sessions_dir=sessions_dir
        )
        assert sessions[session_id].by_skill[NO_SKILL].input_tokens == 10
        vibe = sessions["vibe:root-uuid"]
        assert vibe.by_skill[NO_SKILL].input_tokens == 42

    def test_without_root_only_claude_sessions_loaded(self, tmp_path: Path) -> None:
        """root=None keeps the Vibe store out of the sessions."""
        transcripts = tmp_path / "projects"
        transcripts.mkdir()
        session_id = "claude-session"
        record = {
            "type": "assistant",
            "message": {
                "id": "m1",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "content": [],
            },
        }
        (transcripts / f"{session_id}.jsonl").write_text(json.dumps(record) + "\n")
        runs = [AgentRun(agent_id="a1", agent_type="t", session_id=session_id)]
        sessions_dir = tmp_path / "unified"
        self._make_root_session(sessions_dir / "root-uuid", tmp_path)
        sessions = load_sessions(transcripts, runs, vibe_sessions_dir=sessions_dir)
        assert set(sessions) == {session_id}
