"""Tests for forge.instruction_files (foundation text, includes, flatten)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge.instruction_files import (
    flatten_agents_md,
    foundation_text,
    resolve_includes,
    shipped_path,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_foundation_text_reads_packaged_file() -> None:
    """foundation_text returns the bundled FOUNDATION.md content."""
    text = foundation_text()
    assert "FOUNDATION.md" in text.splitlines()[0]


def test_resolve_includes_inlines_target(tmp_path: Path) -> None:
    """A whole-line @path directive is replaced by the file's content."""
    (tmp_path / "extra.md").write_text("EXTRA RULES\n")
    out = resolve_includes("before\n@extra.md\nafter\n", tmp_path)
    assert out == "before\nEXTRA RULES\nafter\n"


def test_resolve_includes_recurses(tmp_path: Path) -> None:
    """Included files may themselves contain directives."""
    (tmp_path / "inner.md").write_text("INNER\n")
    (tmp_path / "outer.md").write_text("outer start\n@inner.md\nouter end\n")
    out = resolve_includes("@outer.md\n", tmp_path)
    assert out == "outer start\nINNER\nouter end\n"


def test_resolve_includes_missing_target_raises(tmp_path: Path) -> None:
    """A directive pointing at a missing file is a hard error."""
    with pytest.raises(FileNotFoundError, match="not found"):
        resolve_includes("@nope.md\n", tmp_path)


def test_resolve_includes_cycle_raises(tmp_path: Path) -> None:
    """A include cycle is a hard error, not infinite recursion."""
    (tmp_path / "a.md").write_text("@b.md\n")
    (tmp_path / "b.md").write_text("@a.md\n")
    with pytest.raises(ValueError, match="cycle"):
        resolve_includes("@a.md\n", tmp_path)


def test_resolve_includes_ignores_markdown_links_and_prose_mentions(
    tmp_path: Path,
) -> None:
    """Markdown links and @team-style prose are not directives."""
    text = "see [safety](forge-docs/git-safety.md) and ping @team\n"
    assert resolve_includes(text, tmp_path) == text


def test_flatten_agents_md_drops_foundation_directive_and_resolves_rest(
    tmp_path: Path,
) -> None:
    """Flatten composes FOUNDATION + separator + CLAUDE, includes resolved."""
    (tmp_path / "FOUNDATION.md").write_text("FOUNDATION BODY\n")
    (tmp_path / "rules.md").write_text("REPO RULES\n")
    (tmp_path / "CLAUDE.md").write_text(
        "@FOUNDATION.md\n\n@rules.md\n\nconsumer text\n"
    )
    out = flatten_agents_md(tmp_path)
    assert out == "FOUNDATION BODY\n\n---\n\nREPO RULES\nconsumer text\n"


def test_flatten_agents_md_returns_none_without_inputs(tmp_path: Path) -> None:
    """Missing FOUNDATION.md or CLAUDE.md yields None (presence-only fallback)."""
    assert flatten_agents_md(tmp_path) is None
    (tmp_path / "FOUNDATION.md").write_text("x\n")
    assert flatten_agents_md(tmp_path) is None


class TestShippedPath:
    """Tests for the repo-vs-package resolver for forge-shipped sources."""

    def test_local_copy_wins_when_present(self, tmp_path: Path) -> None:
        """A checkout holding the path resolves to its own copy."""
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "local.md").write_text("x\n")
        assert shipped_path(tmp_path, "agents") == agents

    def test_falls_back_to_packaged_copy(self, tmp_path: Path) -> None:
        """A bare consumer repo resolves to the installed package's copy."""
        agents = shipped_path(tmp_path, "agents")
        assert agents != tmp_path / "agents"
        names = {p.name for p in agents.glob("*.md")}
        assert "prior-art.md" in names
        assert "design-checker.md" in names

    def test_packaged_copies_cover_all_sources(self, tmp_path: Path) -> None:
        """Every mapped source ships in the package and resolves."""
        for rel in (".claude-plugin/plugin.json", "claude-hooks", "agents", "skills"):
            assert shipped_path(tmp_path, rel).exists(), rel

    def test_unknown_rel_raises_key_error(self, tmp_path: Path) -> None:
        """A path outside SHIPPED_SOURCES is a programming error, not a fallback."""
        with pytest.raises(KeyError):
            shipped_path(tmp_path, "not-a-source")
