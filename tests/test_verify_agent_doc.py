"""Tests for ``forge.verify_agent_doc``.

# MOCKING STRATEGY: each test builds a throwaway repo tree under tmp_path
# (pyproject.toml, agents/*.md, skills/*/SKILL.md, claude-hooks/*.sh) and runs
# the real ``_config_doc_path`` / ``_roster`` / ``_check_doc`` functions
# against it. ``main`` tests pin ``repo_root`` to tmp_path and patch
# ``sys.argv`` so argparse does not consume pytest's own arguments.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from forge import verify_agent_doc as vad


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write(path: Path, text: str = "") -> None:
    """Write *text* to *path*, creating parent directories as needed.

    Args:
        path: Destination file path.
        text: Contents to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_repo(root: Path) -> None:
    """Create a repo tree with one real agent, skill, hook, and CLI.

    Args:
        root: Directory to populate as the repo root.
    """
    _write(root / "agents" / "reviewer.md", "# reviewer\n")
    _write(root / "agents" / "_TEMPLATE.md", "# template\n")
    _write(root / "skills" / "ship" / "SKILL.md", "# ship\n")
    _write(root / "claude-hooks" / "block_push.sh", "#!/bin/sh\n")
    _write(
        root / "pyproject.toml",
        '[project.scripts]\nforge-ship = "x:main"\n'
        '[tool.forge.agent_doc]\npath = "docs/agent-architecture.md"\n',
    )


# --- _config_doc_path ---------------------------------------------------


def test_config_doc_path_returns_configured_path(tmp_path: Path) -> None:
    """The configured path string is returned when present."""
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc]\npath = "docs/agent-architecture.md"\n',
    )
    assert vad._config_doc_path(tmp_path) == "docs/agent-architecture.md"


def test_config_doc_path_none_when_table_absent(tmp_path: Path) -> None:
    """None is returned when [tool.forge.agent_doc] is absent."""
    _write(tmp_path / "pyproject.toml", '[project.scripts]\nfoo = "x:main"\n')
    assert vad._config_doc_path(tmp_path) is None


def test_config_doc_path_none_when_pyproject_missing(tmp_path: Path) -> None:
    """None is returned when pyproject.toml does not exist at all."""
    assert vad._config_doc_path(tmp_path) is None


# --- _roster --------------------------------------------------------------


def test_roster_discovers_agents_skills_hooks_clis(tmp_path: Path) -> None:
    """Agents, skills, hooks, and CLIs are all discovered; `_`-prefixed excluded."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    assert roster["agents"] == {"reviewer"}
    assert roster["skills"] == {"ship"}
    assert roster["hooks"] == {"block_push"}
    assert roster["clis"] == {"forge-ship"}


# --- _check_doc -------------------------------------------------------------


def test_check_doc_in_sync_returns_empty(tmp_path: Path) -> None:
    """A doc mentioning every agent and skill, with no dangling refs, is clean."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent runs after ship. Uses forge-ship and block_push."
    assert vad._check_doc(doc, roster) == []


def test_check_doc_flags_unmentioned_agent(tmp_path: Path) -> None:
    """A real agent absent from the doc is reported as a coverage problem."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "Only the ship skill is discussed here."
    problems = vad._check_doc(doc, roster)
    assert "coverage: agent 'reviewer' is not mentioned" in problems


def test_check_doc_flags_unmentioned_skill(tmp_path: Path) -> None:
    """A real skill absent from the doc is reported as a coverage problem."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "Only the reviewer agent is discussed here."
    problems = vad._check_doc(doc, roster)
    assert "coverage: skill 'ship' is not mentioned" in problems


def test_check_doc_hooks_not_required_for_coverage(tmp_path: Path) -> None:
    """A real hook absent from the doc produces no problem (deliberate design)."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent runs the ship skill via forge-ship."
    problems = vad._check_doc(doc, roster)
    assert not any("block_push" in problem for problem in problems)


def test_check_doc_flags_dangling_hook(tmp_path: Path) -> None:
    """A block_* token not in the hook roster is reported as dangling."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent runs the ship skill and guards with block_ghost."
    problems = vad._check_doc(doc, roster)
    assert "dangling: hook 'block_ghost' does not exist" in problems


def test_check_doc_flags_dangling_cli(tmp_path: Path) -> None:
    """A forge-* token not in the CLI roster is reported as dangling."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent runs the ship skill and calls forge-ghost."
    problems = vad._check_doc(doc, roster)
    assert "dangling: CLI 'forge-ghost' does not exist" in problems


def test_check_doc_flags_dangling_skill(tmp_path: Path) -> None:
    """A /name token not in the skill roster is reported as dangling."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent invokes /ghost to finish the ship skill."
    problems = vad._check_doc(doc, roster)
    assert "dangling: skill '/ghost' does not exist" in problems


# --- main -------------------------------------------------------------------


def test_main_self_skips_when_no_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 0 when [tool.forge.agent_doc].path is unset.

    MOCK SETUP: repo_root pinned to an empty tmp_path (no pyproject.toml,
    so _config_doc_path returns None); argv patched so argparse does not
    consume pytest's arguments.
    """
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 0


def test_main_returns_one_when_doc_file_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 1 when the configured doc file does not exist on disk.

    MOCK SETUP: repo_root pinned to tmp_path with a config pointing at a
    doc path that is never created; argv patched.
    """
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc]\npath = "docs/agent-architecture.md"\n',
    )
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 1


def test_main_returns_one_when_out_of_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 1 and reports problems when the doc is out of sync.

    MOCK SETUP: full repo tree with a doc that omits the real skill;
    repo_root pinned to tmp_path and argv patched.
    """
    _build_repo(tmp_path)
    _write(
        tmp_path / "docs" / "agent-architecture.md",
        "Only the reviewer agent is discussed here.",
    )
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 1


def test_main_returns_zero_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 0 when the configured doc covers every agent and skill.

    MOCK SETUP: full repo tree with a doc mentioning the agent, skill,
    hook, and CLI; repo_root pinned to tmp_path and argv patched.
    """
    _build_repo(tmp_path)
    _write(
        tmp_path / "docs" / "agent-architecture.md",
        "The reviewer agent runs after ship. Uses forge-ship and block_push.",
    )
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 0
