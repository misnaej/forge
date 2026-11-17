"""Enforcing tests for the release invariants of ``docs/release-process.md``.

Two invariants, both of which forge used to violate by design:

- §1, §3 — **no tracked file declares a release version.** The version
  lives in the ``v*`` tag alone, so nothing can disagree with it. Forge
  previously kept a copy in a plugin manifest, and both drift directions
  broke a release: the copy ran ahead and the tagging job never fired, or
  a recomputation put it behind and a lower version passed every check.
- §2 — **a tag is cut on every merge**, its increment taken from the
  fragments that merge brought, with an already-tagged fragment never
  counted twice.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from forge import changelog_fragments
from forge.changelog_fragments import main
from tests.conftest import commit_all, git_stdout, init_git_repo


if TYPE_CHECKING:
    import pytest


# Where a release version used to be declared, and must not reappear.
# Re-adding any of these reintroduces the two-sources-of-truth defect
# §1 describes, so the test names them rather than scanning loosely.
_VERSION_DECLARING_PATHS = (
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
)


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


def _fragments_repo(tmp_path: Path) -> Path:
    """Build a pushable fragments-mode repo tagged ``v1.0.0``.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The working repository, with an ``origin`` bare remote.
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
    (repo / "changelog.d").mkdir()
    (repo / "changelog.d" / ".gitkeep").write_text("")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1.0.0 — 2026-01-01\n\n- seed\n"
    )
    commit_all(repo, "seed")
    git_stdout(repo, "tag", "-a", "-m", "v1.0.0", "v1.0.0")
    git_stdout(repo, "push", "-q", "origin", "main", "v1.0.0")
    return repo


def test_no_tracked_file_declares_a_release_version() -> None:
    """Forge's own tree declares its version nowhere — the tag carries it.

    SCENARIO: forge's repository as committed.
    EXPECTED BEHAVIOR: none of the paths that used to hold a version
    exist, and ``pyproject.toml`` declares no static ``version``, so the
    latest tag is the only source setuptools-scm can derive from.
    """
    repo = Path(__file__).resolve().parent.parent
    for rel in _VERSION_DECLARING_PATHS:
        assert not (repo / rel).exists(), (
            f"{rel} is back: a second version source reintroduces the drift "
            "docs/release-process.md §1 exists to remove"
        )
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert "\nversion = " not in pyproject, (
        "pyproject.toml declares a static version; setuptools-scm must "
        "derive it from the latest tag instead"
    )


def test_a_tag_is_cut_per_merge_from_that_merge_s_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each merge tags, taking its level from the fragments it brought.

    SCENARIO: two successive merges, one minor then one patch, each
    followed by the per-merge tagger.
    EXPECTED BEHAVIOR: ``v1.1.0`` then ``v1.1.1`` — the second merge
    bumps from the first merge's tag, and the minor fragment already
    inside ``v1.1.0`` does not bump again.
    """
    repo = _fragments_repo(tmp_path)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)

    _merge_fragment(repo, "a.added.md", "minor")
    assert main(["auto-tag"]) == 0
    assert "v1.1.0" in git_stdout(repo, "tag", "--points-at", "HEAD").split()

    _merge_fragment(repo, "b.fixed.md", "patch")
    assert main(["auto-tag"]) == 0
    assert "v1.1.1" in git_stdout(repo, "tag", "--points-at", "HEAD").split()

    assert sorted(git_stdout(repo, "tag", "--list").split()) == [
        "v1.0.0",
        "v1.1.0",
        "v1.1.1",
    ]


def test_assembly_writes_prose_and_declares_no_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assembly collates fragments and introduces no version declaration.

    SCENARIO: one merge is tagged, then the staging half of the assembly
    runs over the fragment that tag already shipped.
    EXPECTED BEHAVIOR: the fragment is consumed into ``CHANGELOG.md``
    under the tag that shipped it, and no manifest appears.
    """
    repo = _fragments_repo(tmp_path)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)

    _merge_fragment(repo, "a.added.md", "minor")
    assert main(["auto-tag"]) == 0

    assert main(["release", "--date", "2026-02-01"]) == 0

    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.1.0" in changelog
    assert "a.added.md" in changelog
    assert not (repo / "changelog.d" / "a.added.md").exists()
    for rel in _VERSION_DECLARING_PATHS:
        assert not (repo / rel).exists()
