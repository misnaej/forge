"""Tests for Vibe session metadata reading and frontmatter parsing."""

import dataclasses
import json
from pathlib import Path

import pytest

from forge.vibe_metadata import (
    VibeSessionMeta,
    _frontmatter,
    _meta_json,
    default_sessions_dir,
    read_session_meta,
)


class TestFrontmatterParsing:
    """Tests for YAML-ish frontmatter parsing."""

    def test_empty_text_yields_empty_dict(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        assert _frontmatter("") == {}

    def test_no_frontmatter_yields_empty_dict(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        assert _frontmatter("some text") == {}

    def test_single_line_frontmatter(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        text = "---\nkey: value\n---\nrest"
        assert _frontmatter(text) == {"key": "value"}

    def test_multiline_frontmatter(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        text = "---\nfoo: bar\nbaz: qux\n---\nrest"
        assert _frontmatter(text) == {"foo": "bar", "baz": "qux"}

    def test_frontmatter_with_blank_lines_before_separator(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        text = "---\n\nfoo: bar\n---\nrest"
        assert _frontmatter(text) == {"foo": "bar"}

    def test_frontmatter_with_blank_lines_after_separator(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        text = "---\nfoo: bar\n---\n\nrest"
        assert _frontmatter(text) == {"foo": "bar"}

    def test_frontmatter_with_extra_leading_trailing_dashes(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        text = "----\nfoo: bar\n----\nrest"
        assert _frontmatter(text) == {}

    def test_frontmatter_with_colon_in_value(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        text = "---\nkey: value: with colon\n---"
        assert _frontmatter(text) == {"key": "value: with colon"}

    def test_frontmatter_with_whitespace_around_colon(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        text = "---\nkey : value \n---"
        assert _frontmatter(text) == {"key": "value"}

    def test_frontmatter_without_separator_after(self) -> None:
        """Tests for YAML-ish frontmatter parsing."""
        text = "---\nkey: value\nrest"
        assert _frontmatter(text) == {}


class TestReadSessionMeta:
    """Tests for read_session_meta tolerant reading of session metadata."""

    def test_read_nonexistent_session_dir_returns_none(self, tmp_path: Path) -> None:
        """Tests for read_session_meta tolerant reading of session metadata."""
        session_dir = tmp_path / "session_123"
        assert read_session_meta(session_dir) is None

    def test_read_missing_generation_pointer_returns_none(self, tmp_path: Path) -> None:
        """Tests for read_session_meta tolerant reading of session metadata."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text("gen_2", encoding="utf-8")
        gen_path = session_dir / "generations" / "gen_2"
        gen_path.mkdir(parents=True)
        # missing runtime-state.json
        assert read_session_meta(session_dir) is None

    def test_read_corrupt_runtime_state_json_returns_none(self, tmp_path: Path) -> None:
        """Tests for read_session_meta tolerant reading of session metadata."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text(
            '{"generation": "gen_1"}', encoding="utf-8"
        )
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        (gen_path / "runtime-state.json").write_text("{incomplete", encoding="utf-8")
        assert read_session_meta(session_dir) is None

    def test_read_non_dict_runtime_state_returns_none(self, tmp_path: Path) -> None:
        """Tests for read_session_meta tolerant reading of session metadata."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text(
            '{"generation": "gen_1"}', encoding="utf-8"
        )
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        (gen_path / "runtime-state.json").write_text(
            '"just a string"', encoding="utf-8"
        )
        assert read_session_meta(session_dir) is None

    def test_read_valid_runtime_state_returns_meta(self, tmp_path: Path) -> None:
        """Tests for read_session_meta tolerant reading of session metadata."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text(
            '{"generation": "gen_1"}', encoding="utf-8"
        )
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "identity": {
                "session_id": "session_123",
                "kind": "root",
                "parent_session_id": None,
            },
            "session_metadata": {
                "cwd": "/some/path",
            },
            "core_system_instructions": (
                "---\nname: my-agent\nmodel: sonnet\n---\nInstructions text."
            ),
        }
        (gen_path / "runtime-state.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        meta = read_session_meta(session_dir)
        assert meta is not None
        assert meta.session_id == "session_123"
        assert meta.kind == "root"
        assert meta.parent_session_id is None
        assert meta.cwd == "/some/path"
        assert meta.agent_name == "my-agent"
        assert meta.model == "sonnet"

    def test_read_meta_json_root_session(self, tmp_path: Path) -> None:
        """Tests for read_session_meta tolerant reading of session metadata."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text(
            '{"generation": "gen_1"}', encoding="utf-8"
        )
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "session_id": "session_123",
            "kind": "root",
            "parent_session_id": None,
            "cwd": "/some/path",
            "core_system_instructions": "---\nagent: my-agent\n---\nInstructions.",
        }
        (gen_path / "runtime-state.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        meta_json = {"root_key": "root_value"}
        (session_dir / "meta.json").write_text(json.dumps(meta_json), encoding="utf-8")
        result = _meta_json(session_dir)
        assert result == meta_json

    def test_meta_json_missing_returns_none(self, tmp_path: Path) -> None:
        """Tests for read_session_meta tolerant reading of session metadata."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text(
            '{"generation": "gen_1"}', encoding="utf-8"
        )
        gen_path = session_dir / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {
            "session_id": "session_123",
            "kind": "root",
            "parent_session_id": None,
            "cwd": "/some/path",
        }
        (gen_path / "runtime-state.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        assert _meta_json(session_dir) is None

    def test_meta_json_corrupt_returns_none(self, tmp_path: Path) -> None:
        """Tests for read_session_meta tolerant reading of session metadata."""
        session_dir = tmp_path / "session_123"
        session_dir.mkdir(parents=True)
        (session_dir / "CURRENT").write_text(
            '{"generation": "gen_1"}', encoding="utf-8"
        )
        gen_path = tmp_path / "generations" / "gen_1"
        gen_path.mkdir(parents=True)
        payload = {"session_id": "session_123"}
        (gen_path / "runtime-state.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        (session_dir / "meta.json").write_text("{bad", encoding="utf-8")
        assert _meta_json(session_dir) is None


class TestDefaultSessionsDir:
    """Tests for default_sessions_dir with VIBE_HOME env override."""

    def test_default_sessions_dir_without_env_uses_home_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tests for default_sessions_dir with VIBE_HOME env override."""
        # Ensure env is unset
        monkeypatch.delenv("VIBE_HOME", raising=False)
        # Ensure HOME is set to tmp_path for fallback
        monkeypatch.setenv("HOME", str(tmp_path))
        expected = tmp_path / ".vibe" / "logs" / "session" / "unified"
        assert default_sessions_dir() == expected

    def test_default_sessions_dir_with_vibe_home_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tests for default_sessions_dir with VIBE_HOME env override."""
        vibe_home = tmp_path / "custom_vibe"
        monkeypatch.setenv("VIBE_HOME", str(vibe_home))
        expected = vibe_home / "logs" / "session" / "unified"
        assert default_sessions_dir() == expected

    def test_default_sessions_dir_vibe_home_overrides_xdg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tests for default_sessions_dir with VIBE_HOME env override."""
        monkeypatch.delenv("VIBE_HOME", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        vibe_home = tmp_path / "custom_vibe"
        monkeypatch.setenv("VIBE_HOME", str(vibe_home))
        expected = vibe_home / "logs" / "session" / "unified"
        assert default_sessions_dir() == expected


class TestVibeSessionMetaDataclass:
    """Tests for VibeSessionMeta dataclass."""

    def test_vibe_session_meta_frozen(self) -> None:
        """Tests for VibeSessionMeta dataclass."""
        meta = VibeSessionMeta(
            session_id="s1",
            kind="root",
            parent_session_id=None,
            cwd="/cwd",
            agent_name=None,
            model=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            meta.session_id = "s2"  # type: ignore[misc]

    def test_vibe_session_meta_equality(self) -> None:
        """Tests for VibeSessionMeta dataclass."""
        a = VibeSessionMeta(
            session_id="s1",
            kind="root",
            parent_session_id=None,
            cwd="/cwd",
            agent_name=None,
            model=None,
        )
        b = VibeSessionMeta(
            session_id="s1",
            kind="root",
            parent_session_id=None,
            cwd="/cwd",
            agent_name=None,
            model=None,
        )
        assert a == b

    def test_vibe_session_meta_hashable(self) -> None:
        """Tests for VibeSessionMeta dataclass."""
        meta = VibeSessionMeta(
            session_id="s1",
            kind="root",
            parent_session_id=None,
            cwd="/cwd",
            agent_name=None,
            model=None,
        )
        s = {meta}
        assert len(s) == 1
