"""Enforcing test for the release-tag invariant of ``docs/release-process.md`` §4.

The invariant: every release tag ``vX``'s own tree has ``plugin.json ==
X`` and a ``CHANGELOG.md`` whose top heading is ``vX``. The scenario
drives a fragments-mode plugin repo through merges (``auto-tag``), the
assembly commit, and the tagger, then checks every tag's own tree.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from forge import changelog_fragments, next_prep, upgrade
from forge.changelog import top_release_heading
from forge.changelog_fragments import main
from tests.conftest import commit_all, git_stdout, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _merge_fragment(repo: Path, name: str, bump: str) -> None:
    """Land one fragment on main and push, as a merged PR would.

    Args:
        repo: Repository working directory.
        name: Fragment filename (e.g., "a.added.md").
        bump: Version bump type ("major", "minor", or "patch").
    """
    (repo / "changelog.d" / name).write_text(f"bump: {bump}\n- {name}\n")
    commit_all(repo, f"feat: {name}")
    git_stdout(repo, "push", "-q", "origin", "main")


def test_every_tag_matches_manifest_and_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tag's own tree carries the manifest version and changelog heading it names.

    SCENARIO: plugin repo with ``auto = "merge"`` (which the manifest must
    override); two fragment merges, then the assembly commit, then the tagger.
    EXPECTED BEHAVIOR: only ``v1.0.0`` and ``v1.1.0`` exist, and each one's
    tree agrees with itself and passes the upgrade documentation check.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    git_stdout(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    init_git_repo(repo)
    git_stdout(repo, "remote", "add", "origin", str(origin))
    (repo / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\n\n'
        '[tool.forge.changelog]\nmode = "fragments"\n\n'
        '[tool.forge.release]\nauto = "merge"\n'
    )
    (repo / ".claude-plugin").mkdir()
    (repo / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.0.0"}, indent=2) + "\n"
    )
    (repo / "changelog.d").mkdir()
    (repo / "changelog.d" / ".gitkeep").write_text("")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1.0.0 — 2026-01-01\n\n- seed\n"
    )
    commit_all(repo, "seed")
    git_stdout(repo, "tag", "-a", "-m", "v1.0.0", "v1.0.0")
    git_stdout(repo, "push", "-q", "origin", "main", "v1.0.0")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)

    _merge_fragment(repo, "a.added.md", "minor")
    assert main(["auto-tag"]) == 0
    _merge_fragment(repo, "b.fixed.md", "patch")
    assert main(["auto-tag"]) == 0

    assert main(["release", "--date", "2026-02-01"]) == 0
    commit_all(repo, "chore(release): assemble v1.1.0")
    git_stdout(repo, "push", "-q", "origin", "main")

    decision = next_prep._maybe_tag_release(repo)
    assert decision.tag == "v1.1.0"
    assert "v1.1.0" in git_stdout(repo, "tag", "--points-at", "HEAD").split()

    tags = sorted(git_stdout(repo, "tag", "--list").split())
    assert tags == ["v1.0.0", "v1.1.0"]
    for tag in tags:
        manifest = json.loads(
            git_stdout(repo, "show", f"{tag}:.claude-plugin/plugin.json")
        )
        changelog = git_stdout(repo, "show", f"{tag}:CHANGELOG.md")
        assert manifest["version"] == tag.removeprefix("v")
        assert top_release_heading(changelog) == tag
        assert upgrade._undocumented_release_refusal(tag, changelog) is None
