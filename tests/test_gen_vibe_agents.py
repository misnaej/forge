"""Tests for ``forge.gen_vibe_agents`` — skills/ -> .agents/skills/.

# MOCKING STRATEGY: no mocks — the ``skills_repo`` fixture is a real tmp
# repo with real ``skills/*/SKILL.md`` files; publishing runs for real.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge import gen_vibe_agents as gen_vibe_agents_module
from forge.gen_vibe_agents import (
    generate_vibe_agents,
    prune_stale_generated,
    publish_skills,
)
from forge.instruction_files import generated_name, renamed_frontmatter


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def skills_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp repo with two skills, one carrying CLAUDE.md/forge: references.

    MOCK SETUP: `gen_vibe_agents.shipped_path` is redirected to this
    repo's own directories. In production that function deliberately
    ignores a repo's directories and always reads the installed package,
    so a repo cannot substitute forge's definitions; these tests need
    controlled inputs, so they inject the source rather than rely on a
    precedence the product does not have.

    Returns:
        The project root path.
    """
    root = tmp_path / "proj"
    alpha = root / "skills" / "alpha"
    beta = root / "skills" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    (alpha / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: x\n---\n\n"
        "Run forge:test-writer; see CLAUDE.md for rules.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gen_vibe_agents_module,
        "shipped_path",
        lambda rel: root / rel,
    )
    (beta / "SKILL.md").write_text(
        "---\nname: beta\ndescription: x\n---\n\nPlain skill body, no references.\n",
        encoding="utf-8",
    )
    return root


def test_publish_copies_every_skill_to_agents_skills(skills_repo: Path) -> None:
    """Each skills/<name>/SKILL.md lands at .agents/skills/forge-<name>/SKILL.md.

    The frontmatter ``name:`` is rewritten to agree with the directory.
    """
    count, _warnings, _references = generate_vibe_agents(skills_repo)
    assert count == 2
    published = skills_repo / ".agents" / "skills"
    for name in ("alpha", "beta"):
        skill = published / generated_name(name) / "SKILL.md"
        assert skill.is_file()
        assert f"name: {generated_name(name)}\n" in skill.read_text(encoding="utf-8")
        assert not (published / name).exists()


def test_claude_md_references_rewritten_to_agents_md(skills_repo: Path) -> None:
    """Published bodies reference AGENTS.md (Vibe's instruction file), not CLAUDE.md."""
    _count, warnings, _references = generate_vibe_agents(skills_repo)
    published_dir = skills_repo / ".agents" / "skills"
    published = (published_dir / "forge-alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert "AGENTS.md" in published
    assert "CLAUDE.md" not in published
    # The spawn form is the prefixed name, same as on the Claude harness.
    assert "Run forge-test-writer;" in published
    assert "forge:test-writer" not in published
    # Both skills are renamed on publish, so both are reported as changed.
    assert warnings == [
        "Updated references in alpha/SKILL.md",
        "Updated references in beta/SKILL.md",
    ]
    # Beta carries no references: its body is its source, only the name differs.
    source = (skills_repo / "skills" / "beta" / "SKILL.md").read_text(encoding="utf-8")
    beta_out = (published_dir / "forge-beta" / "SKILL.md").read_text(encoding="utf-8")
    assert beta_out == renamed_frontmatter(source, "beta")
    assert beta_out.replace("name: forge-beta", "name: beta") == source


def test_check_mode_writes_nothing_but_reports_references(skills_repo: Path) -> None:
    """--check returns 0 and leaves the tree untouched, while still scanning."""
    count, warnings, references = generate_vibe_agents(skills_repo, check_mode=True)
    assert count == 0
    assert warnings == []
    assert not (skills_repo / ".agents").exists()
    assert any("forge:test-writer" in ref for ref in references)
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


# ---------------------------------------------------------------------------
# prune_stale_generated — stale non-prefixed entries in generated dirs (#1044)
# ---------------------------------------------------------------------------


def test_prune_stale_generated_removes_unprefixed_keeps_prefixed(
    tmp_path: Path,
) -> None:
    """SCENARIO: a generated dir holds a prefixed entry, a stale dir and a stale file.

    EXPECTED BEHAVIOR: only the non-prefixed entries are removed; the prefixed
    entry survives; the removed names are returned.
    """
    gen = tmp_path / "gen"
    (gen / "forge-current").mkdir(parents=True)
    stale_dir = gen / "oldskill"
    stale_dir.mkdir()
    (stale_dir / "SKILL.md").write_text("x", encoding="utf-8")
    stale_file = gen / "oldagent.md"
    stale_file.write_text("x", encoding="utf-8")

    stale = prune_stale_generated(gen)

    assert stale == ["oldagent.md", "oldskill"]
    assert not stale_dir.exists()
    assert not stale_file.exists()
    assert (gen / "forge-current").is_dir()


def test_prune_stale_generated_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    """check_mode path: stale names are reported, nothing is removed."""
    gen = tmp_path / "gen"
    (gen / "stale").mkdir(parents=True)
    (gen / "forge-current").mkdir()

    stale = prune_stale_generated(gen, dry_run=True)

    assert stale == ["stale"]
    assert (gen / "stale").is_dir()


def test_generate_vibe_agents_prunes_stale_skill_dirs(
    skills_repo: Path,
) -> None:
    """SCENARIO: the skills output dir holds a pre-rename unprefixed leftover.

    MOCK SETUP: skills_repo fixture; a stale `alpha/` dir planted next to
    where the generator writes `forge-alpha/`.
    EXPECTED BEHAVIOR: the leftover is pruned, the prefixed copy exists, and
    the warning names the pruned entry.
    """
    dest = skills_repo / ".agents" / "skills"
    stale = dest / "alpha"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("---\nname: alpha\n---\n", encoding="utf-8")

    _count, warnings, _refs = generate_vibe_agents(skills_repo)

    assert not stale.exists()
    assert (dest / "forge-alpha").is_dir()
    assert (dest / "forge-beta").is_dir()
    assert any("pruned stale" in w and "alpha" in w for w in warnings)


def test_generate_vibe_agents_check_mode_reports_stale_without_deleting(
    skills_repo: Path,
) -> None:
    """check_mode: the stale dir is reported in warnings and left in place."""
    dest = skills_repo / ".agents" / "skills"
    stale = dest / "alpha"
    stale.mkdir(parents=True)

    _count, warnings, _refs = generate_vibe_agents(skills_repo, check_mode=True)

    assert stale.is_dir()
    assert (dest / "forge-alpha").exists() is False
    assert any("would be pruned" in w and "alpha" in w for w in warnings)
