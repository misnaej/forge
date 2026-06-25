"""Tests for ``forge.verify_plugin_version``."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from forge import verify_plugin_version
from tests.conftest import GIT_ENV as _GIT_ENV
from tests.conftest import init_git_repo as _init_git_repo


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write_plugin(repo: Path, version: str) -> None:
    """Write a minimal ``.claude-plugin/plugin.json`` with the given version.

    Args:
        repo: Repository directory.
        version: Version string for ``plugin.json["version"]``.
    """
    plugin_dir = repo / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "x", "version": version})
    )


def _write_plugin_overwrite(repo: Path, version: str) -> None:
    """Overwrite an existing ``plugin.json`` with a new version.

    Companion to :func:`_write_plugin` for tests that bump the version
    post-tag (mkdir would fail on the second call).

    Args:
        repo: Repository directory.
        version: Version string for ``plugin.json["version"]``.
    """
    (repo / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": version})
    )


def test_parse_semver_round_trip() -> None:
    """``_parse_semver`` handles plain, v-prefixed, and pre-release suffixes."""
    assert verify_plugin_version._parse_semver("1.2.3") == (1, 2, 3)
    assert verify_plugin_version._parse_semver("v1.2.3") == (1, 2, 3)
    assert verify_plugin_version._parse_semver("v1.2.3-rc1") == (1, 2, 3)
    assert verify_plugin_version._parse_semver("1.2.3+build") == (1, 2, 3)


def test_parse_semver_rejects_invalid() -> None:
    """``_parse_semver`` returns ``None`` on non-X.Y.Z input."""
    assert verify_plugin_version._parse_semver("1.2") is None
    assert verify_plugin_version._parse_semver("v1.x.3") is None
    assert verify_plugin_version._parse_semver("") is None


def test_skipped_without_plugin_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No plugin.json → exit 0 and log says (skipped)."""
    _init_git_repo(tmp_path)
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-plugin-version"])
    assert verify_plugin_version.main() == 0
    log = (tmp_path / "code_health" / "plugin_version.log").read_text()
    assert "no .claude-plugin/plugin.json" in log


def test_skipped_without_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No git tags → exit 0 and log says (skipped)."""
    _init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-plugin-version"])
    assert verify_plugin_version.main() == 0
    log = (tmp_path / "code_health" / "plugin_version.log").read_text()
    assert "no git tags" in log


def test_fail_when_version_not_strictly_greater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin.json version <= latest tag → exit 1.

    The post-tag commit must actually change file content for the guard
    to fire — empty / ``-s ours`` commits with identical trees are
    correctly treated as the release state itself (covered separately
    in ``test_skipped_when_tree_matches_tag_via_ours_merge`` and
    ``test_skipped_on_release_commit``).
    """
    _init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add plugin"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, check=True)
    # Real content change post-tag — must bump plugin.json or fail.
    (tmp_path / "other.txt").write_text("post-tag work\n")
    subprocess.run(["git", "add", "other.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "post-tag content"], cwd=tmp_path, check=True
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-plugin-version"])
    assert verify_plugin_version.main() == 1
    log = (tmp_path / "code_health" / "plugin_version.log").read_text()
    assert "must be strictly greater" in log


def test_pass_when_version_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin.json version > latest tag → exit 0 via the success path.

    HEAD must carry a content change vs the tag's tree so the
    tree-equality skip does not short-circuit; this test asserts the
    actual comparison log.
    """
    _init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add plugin"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, check=True)
    # Bump plugin.json post-tag — real content change, tree differs.
    _write_plugin_overwrite(tmp_path, "1.0.1")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "bump to 1.0.1"], cwd=tmp_path, check=True
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-plugin-version"])
    assert verify_plugin_version.main() == 0
    log = (tmp_path / "code_health" / "plugin_version.log").read_text()
    assert "> latest tag" in log


def test_skipped_on_release_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When HEAD is the tagged commit, plugin.json may equal the tag."""
    _init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add plugin"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-plugin-version"])
    assert verify_plugin_version.main() == 0
    log = (tmp_path / "code_health" / "plugin_version.log").read_text()
    assert "release tag" in log


def test_skipped_when_tree_matches_tag_via_ours_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``-s ours`` merge whose tree equals the tag's tree skips the guard.

    Models the dual-track promotion scenario: dev merges main with ``-s
    ours`` to absorb past promotion squash commits without changing any
    file content. HEAD is a new commit SHA, but its tree equals the
    tag's tree — the guard must skip.
    """
    env = _GIT_ENV
    _init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add plugin"], cwd=tmp_path, env=env, check=True
    )
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, env=env, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "other"], cwd=tmp_path, env=env, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "divergent"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, env=env, check=True)
    subprocess.run(
        ["git", "merge", "-q", "-s", "ours", "other", "-m", "merge other"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-plugin-version"])
    assert verify_plugin_version.main() == 0
    log = (tmp_path / "code_health" / "plugin_version.log").read_text()
    assert "release tag" in log


def test_main_skips_when_head_reproduces_older_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staged dev→main promotion of a minor BELOW the global-max tag skips.

    Regression lock for the #43 ancestry→global tag switch: an earlier
    guard compared HEAD only against the *latest* tag, which made
    promoting any minor below the global-max impossible (the release
    branch's tree never equals the latest tag's tree). Reproduces the
    real ``v1.22.0`` (plugin.json 1.22.0) promoted while ``v1.23.0`` is
    already tagged. Do not narrow ``_is_release_commit`` back to one tag.
    """
    env = _GIT_ENV
    _init_git_repo(tmp_path)
    # Older release v1.0.0.
    _write_plugin(tmp_path, "1.0.0")
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "v1.0.0"], cwd=tmp_path, env=env, check=True
    )
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, env=env, check=True)
    # Newer release v1.1.0 — becomes the global-max tag.
    _write_plugin_overwrite(tmp_path, "1.1.0")
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "v1.1.0"], cwd=tmp_path, env=env, check=True
    )
    subprocess.run(["git", "tag", "v1.1.0"], cwd=tmp_path, env=env, check=True)
    # Release branch reproducing v1.0.0's tree (plugin.json 1.0.0, BELOW
    # the global-max tag v1.1.0).
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v1.0.0"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "v1.0.0", "--", "."],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "promote v1.0.0"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-plugin-version"])
    # HEAD's tree == v1.0.0's tree (an older tag) → guard skips, even
    # though plugin.json 1.0.0 < latest tag v1.1.0.
    assert verify_plugin_version.main() == 0
    log = (tmp_path / "code_health" / "plugin_version.log").read_text()
    assert "release tag" in log


def _make_two_releases(repo: Path, env: dict[str, str]) -> None:
    """Tag v1.0.0 (plugin 1.0.0 + a.py) then v1.1.0 (global-max).

    Shared setup for the CHANGELOG-insensitivity tests: leaves the repo on
    a fresh ``release/v1.0.0`` branch whose tree reproduces the v1.0.0 tag,
    with ``plugin.json`` at 1.0.0 (below the global-max tag v1.1.0).

    Args:
        repo: Repository directory (already git-init'd on ``main``).
        env: Git author/committer environment for deterministic commits.
    """
    _write_plugin(repo, "1.0.0")
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "v1.0.0"], cwd=repo, env=env, check=True
    )
    subprocess.run(["git", "tag", "v1.0.0"], cwd=repo, env=env, check=True)
    _write_plugin_overwrite(repo, "1.1.0")
    subprocess.run(["git", "add", "."], cwd=repo, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "v1.1.0"], cwd=repo, env=env, check=True
    )
    subprocess.run(["git", "tag", "v1.1.0"], cwd=repo, env=env, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v1.0.0"], cwd=repo, env=env, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "v1.0.0", "--", "."], cwd=repo, env=env, check=True
    )


def test_skips_when_release_branch_only_adds_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release branch that finalizes the @main CHANGELOG still skips.

    The exact CI break that motivated CHANGELOG-insensitive matching: a
    ``release/v1.0.0`` branch reproduces the v1.0.0 tag's tree but adds a
    curated ``CHANGELOG.md`` entry. Its ``plugin.json`` (1.0.0) sits below
    the global-max tag v1.1.0, so without the CHANGELOG exclusion the guard
    would fall back to version comparison and FAIL. The release fingerprint
    ignores ``CHANGELOG.md``, so HEAD still reproduces v1.0.0 → skip.
    """
    env = _GIT_ENV
    _init_git_repo(tmp_path)
    _make_two_releases(tmp_path, env)
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0 — curated\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "promote v1.0.0 + changelog"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-plugin-version"])
    assert verify_plugin_version.main() == 0
    log = (tmp_path / "code_health" / "plugin_version.log").read_text()
    assert "release tag" in log


def test_fails_when_release_branch_changes_non_changelog_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abuse guard: a non-CHANGELOG diff is NOT release-equal, so it fails.

    Same setup as the skip case, but the release branch edits ``a.py``
    (not ``CHANGELOG.md``). The release fingerprint then differs from every
    tag, so HEAD is a real content change with ``plugin.json`` 1.0.0 ≤
    latest tag v1.1.0 → the guard fails. Proves the exclusion is scoped to
    ``CHANGELOG.md`` and does not blanket-skip modified release branches.
    """
    env = _GIT_ENV
    _init_git_repo(tmp_path)
    _make_two_releases(tmp_path, env)
    (tmp_path / "a.py").write_text("x = 999\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "promote v1.0.0 + code change"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-plugin-version"])
    assert verify_plugin_version.main() == 1
