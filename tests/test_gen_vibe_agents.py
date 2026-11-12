"""Tests for ``forge.gen_vibe_agents`` — skills/ -> .agents/skills/.

# MOCKING STRATEGY: no mocks — the ``skills_repo`` fixture is a real tmp
# repo with real ``skills/*/SKILL.md`` files; publishing runs for real.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge.gen_vibe_agents import generate_vibe_agents, publish_skills


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def skills_repo(tmp_path: Path) -> Path:
    """A tmp repo with two skills, one carrying CLAUDE.md/forge: references.

    Returns:
        The project root path.
    """
    root = tmp_path / "proj"
    alpha = root / "skills" / "alpha"
    beta = root / "skills" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    (alpha / "SKILL.md").write_text(
        "---\nname: alpha\n---\n\nRun forge:test-writer; see CLAUDE.md for rules.\n",
        encoding="utf-8",
    )
    (beta / "SKILL.md").write_text(
        "---\nname: beta\n---\n\nPlain skill body, no references.\n",
        encoding="utf-8",
    )
    return root


def test_publish_copies_every_skill_to_agents_skills(skills_repo: Path) -> None:
    """Each skills/<name>/SKILL.md lands at .agents/skills/<name>/SKILL.md."""
    count, _warnings, _references = generate_vibe_agents(skills_repo)
    assert count == 2
    published = skills_repo / ".agents" / "skills"
    assert (published / "alpha" / "SKILL.md").is_file()
    assert (published / "beta" / "SKILL.md").is_file()


def test_claude_md_references_rewritten_to_agents_md(skills_repo: Path) -> None:
    """Published bodies reference AGENTS.md (Vibe's instruction file), not CLAUDE.md."""
    _count, warnings, _references = generate_vibe_agents(skills_repo)
    published = (skills_repo / ".agents" / "skills" / "alpha" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "AGENTS.md" in published
    assert "CLAUDE.md" not in published
    # The untouched skill produces no drift warning; alpha does.
    assert warnings == ["Updated references in alpha/SKILL.md"]
    # Beta's body is byte-identical to its source.
    source = (skills_repo / "skills" / "beta" / "SKILL.md").read_text(encoding="utf-8")
    beta_out = (skills_repo / ".agents" / "skills" / "beta" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert beta_out == source


def test_check_mode_writes_nothing_but_reports_references(skills_repo: Path) -> None:
    """--check returns 0 and leaves the tree untouched, while still scanning."""
    count, warnings, references = generate_vibe_agents(skills_repo, check_mode=True)
    assert count == 0
    assert warnings == []
    assert not (skills_repo / ".agents").exists()
    assert any("forge:test" in ref for ref in references)
    assert any("CLAUDE.md" in ref for ref in references)
    assert publish_skills(skills_repo, check_mode=True) == 0
    assert not (skills_repo / ".agents").exists()


def test_missing_local_skills_dir_falls_back_to_packaged_skills(
    tmp_path: Path,
) -> None:
    """A consumer repo without skills/ publishes the packaged skills."""
    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()
    count, warnings, references = generate_vibe_agents(empty_repo)
    assert count == 19
    # Warnings are the CLAUDE.md→AGENTS.md rewrites in shipped skills.
    assert warnings
    assert all("Updated references" in w for w in warnings)
    assert (empty_repo / ".agents" / "skills").is_dir()
    # The packaged agents/ sits next to the packaged skills/, so the
    # reference check still sees the shipped agent docs.
    assert references
