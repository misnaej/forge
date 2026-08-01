"""Tests for ``forge.release`` — ``forge-release`` single-track tagging CLI."""

# MOCKING STRATEGY: guard-helper tests (``_dirty_tree_error``,
# ``_wrong_branch_error``, ``_cut_release``) exercise real git repos via
# ``tests.conftest``'s ``init_git_repo`` / ``GIT_ENV`` rather than faking
# ``subprocess.run`` — the guards ARE thin git wrappers, so a real repo is
# cheaper to reason about than a mock transcript. ``_wrong_release_model_error``
# and ``_changelog_gate_error`` need no git repo at all (they read config /
# the working tree), so those tests operate on a bare ``tmp_path``.
# ``main()`` tests monkeypatch ``release.load_config`` to supply a
# ``ForgeConfig`` without a real ``pyproject.toml``, mirroring the Group F
# pattern in ``test_verify_main_tags.py``; ``sys.argv`` is patched so argparse
# does not see pytest's own arguments. Monkeypatch targets always use the
# consuming namespace (``release.*``), never ``forge.config`` /
# ``forge.git_utils`` directly.

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from forge import git_utils, release
from forge.config import ForgeConfig
from tests.conftest import GIT_ENV as _GIT_ENV
from tests.conftest import init_git_repo as _init_git_repo
from tests.conftest import init_single_track_repo
from tests.conftest import tag_exists as conftest_tag_exists


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _repo_with_origin(base: Path) -> tuple[Path, Path]:
    """Initialize a work tree + bare origin wired together on ``main`` only.

    Thin alias for :func:`tests.conftest.init_single_track_repo`, kept so
    this suite's call sites read locally; the plumbing lives in conftest.

    Args:
        base: Parent directory; ``work`` and ``origin.git`` are created
            inside it.

    Returns:
        A ``(work, bare)`` tuple of the work-tree and bare-repo paths.
    """
    return init_single_track_repo(base)


def _tag_exists(repo: Path, tag: str) -> bool:
    """Return ``True`` when *tag* exists in *repo* (work tree or bare).

    Args:
        repo: Repo to check (work tree or bare).
        tag: Tag name to look for.

    Returns:
        ``True`` when git reports *tag* in that repo.
    """
    return conftest_tag_exists(repo, tag)


# ---------------------------------------------------------------------------
# _dirty_tree_error
# ---------------------------------------------------------------------------


def test_dirty_tree_error_none_when_clean(tmp_path: Path) -> None:
    """A freshly-committed repo has a clean tree — no error."""
    _init_git_repo(tmp_path)
    assert release._dirty_tree_error(tmp_path) is None


def test_dirty_tree_error_reports_uncommitted_change(tmp_path: Path) -> None:
    """An untracked file makes ``git status --porcelain`` non-empty."""
    _init_git_repo(tmp_path)
    (tmp_path / "untracked.txt").write_text("x")
    error = release._dirty_tree_error(tmp_path)
    assert error is not None
    assert "dirty" in error


# ---------------------------------------------------------------------------
# _wrong_branch_error
# ---------------------------------------------------------------------------


def test_wrong_branch_error_none_when_on_base(tmp_path: Path) -> None:
    """``init_git_repo`` lands on ``main`` — no error against ``base="main"``."""
    _init_git_repo(tmp_path)
    assert release._wrong_branch_error(tmp_path, "main") is None


def test_wrong_branch_error_reports_other_branch(tmp_path: Path) -> None:
    """Checking out a feature branch reports both the branch and base name."""
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    error = release._wrong_branch_error(tmp_path, "main")
    assert error is not None
    assert "feat/x" in error
    assert "main" in error


def test_wrong_branch_error_reports_detached_head(tmp_path: Path) -> None:
    """Detached HEAD names "HEAD" (not the ``'(detached HEAD)'`` fallback text).

    ``git rev-parse --abbrev-ref HEAD`` prints the literal string ``"HEAD"``
    for a detached checkout rather than an empty string, so the
    ``current or '(detached HEAD)'`` fallback in the source never fires here
    — it only guards a truly empty ``rev-parse`` result.
    """
    _init_git_repo(tmp_path)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "checkout", "-q", sha], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    error = release._wrong_branch_error(tmp_path, "main")
    assert error is not None
    assert "HEAD" in error
    assert "main" in error


# ---------------------------------------------------------------------------
# _wrong_release_model_error
# ---------------------------------------------------------------------------


def test_wrong_release_model_error_none_for_single_track_no_manifest(
    tmp_path: Path,
) -> None:
    """Single-track config with no plugin manifest is a valid release model."""
    cfg = ForgeConfig(base_branch="main", dev_branch="main")
    assert release._wrong_release_model_error(tmp_path, cfg) is None


def test_wrong_release_model_error_dual_track_wins(tmp_path: Path) -> None:
    """Dual-track config is refused even when a plugin manifest is ALSO present.

    Asserts the checked-first precedence documented on
    ``_wrong_release_model_error``: dual-track disqualifies before the
    manifest check is even reached.
    """
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{"name": "x", "version": "1.2.3"}')
    cfg = ForgeConfig(base_branch="main", dev_branch="dev")
    error = release._wrong_release_model_error(tmp_path, cfg)
    assert error is not None
    assert "Dual-track" in error


def test_wrong_release_model_error_reports_plugin_manifest(tmp_path: Path) -> None:
    """Single-track config with a valid plugin manifest names ``plugin.json``."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{"name": "x", "version": "1.2.3"}')
    cfg = ForgeConfig(base_branch="main", dev_branch="main")
    error = release._wrong_release_model_error(tmp_path, cfg)
    assert error is not None
    assert "plugin.json" in error


# ---------------------------------------------------------------------------
# _changelog_gate_error
# ---------------------------------------------------------------------------


def test_changelog_gate_error_none_when_missing_file_but_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ``CHANGELOG.md`` at all is a warning, not a blocking error."""
    with caplog.at_level(logging.WARNING, logger="forge.release"):
        error = release._changelog_gate_error(tmp_path, "v1.0.0")
    assert error is None
    assert any("CHANGELOG.md" in r.getMessage() for r in caplog.records)


def test_changelog_gate_error_none_when_entry_present(tmp_path: Path) -> None:
    """A ``CHANGELOG.md`` already carrying the tag's heading passes the gate."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.3.0\n")
    assert release._changelog_gate_error(tmp_path, "v1.3.0") is None


def test_changelog_gate_error_reports_missing_entry(tmp_path: Path) -> None:
    """A ``CHANGELOG.md`` missing the tag's heading names the tag in the error."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    error = release._changelog_gate_error(tmp_path, "v1.3.0")
    assert error is not None
    assert "v1.3.0" in error


# ---------------------------------------------------------------------------
# _cut_release
# ---------------------------------------------------------------------------


def test_cut_release_tags_locally_and_warns_without_origin(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ``origin`` remote — tag is created locally and a warning is logged."""
    _init_git_repo(tmp_path)
    with caplog.at_level(logging.WARNING, logger="forge.release"):
        assert release._cut_release(tmp_path, "v1.0.0") == 0
    assert _tag_exists(tmp_path, "v1.0.0")
    assert any("origin" in r.getMessage() for r in caplog.records)


def test_cut_release_tags_and_pushes_when_origin_exists(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``origin`` remote — tag is created and pushed; the bare repo has it."""
    work, bare = _repo_with_origin(tmp_path)
    with caplog.at_level(logging.INFO, logger="forge.release"):
        assert release._cut_release(work, "v1.0.0") == 0
    assert _tag_exists(bare, "v1.0.0")
    assert any("Tagged and pushed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# main() integration (real git; monkeypatched config)
# ---------------------------------------------------------------------------


def test_main_dry_run_reports_next_tag_without_tagging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: an existing ``v1.2.3`` release, ``--bump minor --dry-run``.

    MOCK SETUP: ``load_config`` → single-track ``ForgeConfig``. Real repo
        with an annotated ``v1.2.3`` tag on ``HEAD``.
    EXPECTED BEHAVIOR: returns 0; no new tag created; caplog names ``v1.3.0``.
    """
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "tag", "-a", "v1.2.3", "-m", "v1.2.3"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    monkeypatch.setattr(
        release,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
    )
    monkeypatch.setattr("sys.argv", ["forge-release", "--bump", "minor", "--dry-run"])
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.INFO, logger="forge.release"):
        result = release.main()
    assert result == 0
    assert git_utils.latest_v_tag(tmp_path) == "v1.2.3"
    assert any("v1.3.0" in r.getMessage() for r in caplog.records)


def test_main_first_release_when_no_tags_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: a repo with no ``v*`` tags yet, ``--bump minor --dry-run``.

    MOCK SETUP: ``load_config`` → single-track ``ForgeConfig``. Fresh repo,
        no tags.
    EXPECTED BEHAVIOR: returns 0; caplog names the first-release ``v0.1.0``.
    """
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        release,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
    )
    monkeypatch.setattr("sys.argv", ["forge-release", "--bump", "minor", "--dry-run"])
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.INFO, logger="forge.release"):
        result = release.main()
    assert result == 0
    assert any("v0.1.0" in r.getMessage() for r in caplog.records)


def test_main_success_creates_and_pushes_tag_when_origin_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: clean, single-track repo with origin and a matching CHANGELOG entry.

    MOCK SETUP: ``load_config`` → single-track ``ForgeConfig``. Real work
        tree + bare origin (:func:`_repo_with_origin`); ``CHANGELOG.md``
        carries a ``## v0.0.1`` heading — the tag ``--bump patch`` computes
        off no prior ``v*`` tags.
    EXPECTED BEHAVIOR: returns 0; ``v0.0.1`` exists on the bare origin.
    """
    work, bare = _repo_with_origin(tmp_path)
    (work / "CHANGELOG.md").write_text("## v0.0.1\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add changelog"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    monkeypatch.setattr(
        release,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
    )
    monkeypatch.setattr("sys.argv", ["forge-release", "--bump", "patch"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.release"):
        result = release.main()
    assert result == 0
    assert _tag_exists(bare, "v0.0.1")


def test_main_success_when_no_changelog_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: clean, single-track repo with origin but NO ``CHANGELOG.md`` at all.

    MOCK SETUP: ``load_config`` → single-track ``ForgeConfig``. Real work
        tree + bare origin (:func:`_repo_with_origin`); no ``CHANGELOG.md``
        is ever committed, so the gate warns instead of blocking.
    EXPECTED BEHAVIOR: returns 0; ``v0.0.1`` exists on the bare origin;
        caplog carries the "skipping the CHANGELOG gate" warning.
    """
    work, bare = _repo_with_origin(tmp_path)
    monkeypatch.setattr(
        release,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
    )
    monkeypatch.setattr("sys.argv", ["forge-release", "--bump", "patch"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.release"):
        result = release.main()
    assert result == 0
    assert _tag_exists(bare, "v0.0.1")
    assert any("skipping the CHANGELOG gate" in r.getMessage() for r in caplog.records)


def test_main_collects_all_guard_failures_and_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: dirty tree + wrong branch + dual-track config, all at once.

    MOCK SETUP: ``load_config`` → dual-track ``ForgeConfig`` (``dev`` !=
        ``main``). Real repo checked out on ``feat/x`` with an untracked
        file.
    EXPECTED BEHAVIOR: returns 1; caplog error records name all three
        failures ("dirty", the branch name, "Dual-track").
    """
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    (tmp_path / "untracked.txt").write_text("x")
    monkeypatch.setattr(
        release,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-release", "--bump", "patch"])
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.ERROR, logger="forge.release"):
        result = release.main()
    assert result == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("dirty" in m for m in errors)
    assert any("feat/x" in m for m in errors)
    assert any("Dual-track" in m for m in errors)


def test_main_changelog_gate_blocks_missing_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: clean single-track repo, but ``CHANGELOG.md`` lacks the computed tag.

    MOCK SETUP: ``load_config`` → single-track ``ForgeConfig``. Fresh repo,
        no prior tags (so ``--bump patch`` computes ``v0.0.1``);
        ``CHANGELOG.md`` only has an unrelated heading.
    EXPECTED BEHAVIOR: returns 1; no tag is created.
    """
    _init_git_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## v9.9.9\n")
    subprocess.run(
        ["git", "add", "CHANGELOG.md"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add changelog"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    monkeypatch.setattr(
        release,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
    )
    monkeypatch.setattr("sys.argv", ["forge-release", "--bump", "patch"])
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.ERROR, logger="forge.release"):
        result = release.main()
    assert result == 1
    assert git_utils.latest_v_tag(tmp_path) is None


def test_main_guard_failure_wins_over_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: a dirty tree combined with ``--dry-run``.

    MOCK SETUP: ``load_config`` → single-track ``ForgeConfig``. Repo with
        an untracked file.
    EXPECTED BEHAVIOR: returns 1 — the guard runs and refuses before
        ``--dry-run`` is even consulted.
    """
    _init_git_repo(tmp_path)
    (tmp_path / "untracked.txt").write_text("x")
    monkeypatch.setattr(
        release,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
    )
    monkeypatch.setattr("sys.argv", ["forge-release", "--bump", "patch", "--dry-run"])
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.ERROR, logger="forge.release"):
        result = release.main()
    assert result == 1


# ---------------------------------------------------------------------------
# --from-changelog
# ---------------------------------------------------------------------------


def _single_track_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``release.load_config`` at a single-track config."""
    monkeypatch.setattr(
        release,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
    )


def test_main_from_changelog_cuts_declared_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: CHANGELOG declares v1.3.0, latest tag v1.2.3, on main.

    MOCK SETUP: real work tree + bare origin; load_config → single-track.
    EXPECTED BEHAVIOR: v1.3.0 tagged and pushed to origin; exit 0.
    """
    work, bare = _repo_with_origin(tmp_path)
    (work / "CHANGELOG.md").write_text("## v1.3.0\n\n- x\n\n## v1.2.3\n")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "changelog"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.2.3", "-m", "v1.2.3"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    _single_track_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    assert release.main() == 0
    assert _tag_exists(work, "v1.3.0")
    assert _tag_exists(bare, "v1.3.0")


def test_main_from_changelog_idempotent_when_already_tagged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Top heading equals the latest tag → exit 0, nothing new cut."""
    work, bare = _repo_with_origin(tmp_path)
    (work / "CHANGELOG.md").write_text("## v1.2.3\n")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "changelog"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.2.3", "-m", "v1.2.3"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    _single_track_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.release"):
        assert release.main() == 0
    assert any("already released" in r.getMessage() for r in caplog.records)
    # The pre-existing local tag was never pushed — origin must stay bare.
    assert not _tag_exists(bare, "v1.2.3")


def test_main_from_changelog_flags_stranded_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entries appended under an already-tagged heading → exit 1, not no-op."""
    work, _bare = _repo_with_origin(tmp_path)
    (work / "CHANGELOG.md").write_text("## v1.2.3\n- released work\n")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "changelog"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.2.3", "-m", "v1.2.3"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    (work / "CHANGELOG.md").write_text(
        "## v1.2.3\n- released work\n- stranded feature\n"
    )
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "stranded"], cwd=work, env=_GIT_ENV, check=True
    )
    _single_track_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.ERROR, logger="forge.release"):
        assert release.main() == 1
    assert any("stranded" in r.getMessage() for r in caplog.records)


def test_main_from_changelog_idempotent_when_ahead_without_changelog_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Commits after the tag that skip the changelog (no-version) still rest."""
    work, _bare = _repo_with_origin(tmp_path)
    (work / "CHANGELOG.md").write_text("## v1.2.3\n")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "changelog"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.2.3", "-m", "v1.2.3"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    (work / "ci.txt").write_text("tweak\n")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "chore [no-version]"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    _single_track_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.release"):
        assert release.main() == 0
    assert any("already released" in r.getMessage() for r in caplog.records)


def test_main_from_changelog_stale_heading_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Top heading behind the latest tag → guard failure, exit 1."""
    work, _bare = _repo_with_origin(tmp_path)
    (work / "CHANGELOG.md").write_text("## v1.1.0\n")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "changelog"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.2.3", "-m", "v1.2.3"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    _single_track_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.ERROR, logger="forge.release"):
        assert release.main() == 1
    assert any("behind the latest tag" in r.getMessage() for r in caplog.records)


def test_main_from_changelog_requires_changelog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No CHANGELOG.md → error naming the requirement, exit 1."""
    work, _bare = _repo_with_origin(tmp_path)
    _single_track_cfg(monkeypatch)
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.ERROR, logger="forge.release"):
        assert release.main() == 1
    assert any("needs a CHANGELOG.md" in r.getMessage() for r in caplog.records)


def test_main_from_changelog_ci_allows_detached_tip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: CI merge-event checkout — detached HEAD at origin/main tip.

    MOCK SETUP: real repo + origin; HEAD detached at the pushed tip;
        release.is_ci → True.
    EXPECTED BEHAVIOR: branch guard swaps to the tip check; tag cut; exit 0.
    """
    work, bare = _repo_with_origin(tmp_path)
    (work / "CHANGELOG.md").write_text("## v0.2.0\n")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "changelog"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "--detach", "HEAD"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=work, env=_GIT_ENV, check=True)
    _single_track_cfg(monkeypatch)
    monkeypatch.setattr(release, "is_ci", lambda: True)
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    assert release.main() == 0
    assert _tag_exists(bare, "v0.2.0")


def test_main_from_changelog_ci_refuses_non_tip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Detached HEAD at an older commit than origin's tip → exit 1."""
    work, _bare = _repo_with_origin(tmp_path)
    (work / "CHANGELOG.md").write_text("## v0.2.0\n")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "changelog"], cwd=work, env=_GIT_ENV, check=True
    )
    first = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (work / "later.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "later"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "--detach", first],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=work, env=_GIT_ENV, check=True)
    _single_track_cfg(monkeypatch)
    monkeypatch.setattr(release, "is_ci", lambda: True)
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.ERROR, logger="forge.release"):
        assert release.main() == 1
    assert any("not the tip" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# _cut_release — race tolerance + return contract
# ---------------------------------------------------------------------------


def test_cut_release_race_tolerant_push_failure_with_remote_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: push fails but the tag exists remotely — concurrent cut.

    MOCK SETUP: run_git faked; "push" raises CalledProcessError, the
        post-failure "tag --list" reports the tag present.
    EXPECTED BEHAVIOR: race_tolerant=True → return 0 + concurrency log.
    """

    def _fake_git(*args: str, **_kw: object) -> str:
        if args[0] == "push":
            raise subprocess.CalledProcessError(1, ["git", "push"])
        if args[0] == "remote":
            return "https://example.invalid/origin.git"
        if args[:2] == ("tag", "--list") and len(args) == 3:
            return args[2]
        return ""

    monkeypatch.setattr(release, "run_git", _fake_git)
    monkeypatch.setattr(release, "create_annotated_tag", lambda *_a, **_kw: None)
    with caplog.at_level(logging.INFO, logger="forge.release"):
        result = release._cut_release(tmp_path, "v1.0.0", race_tolerant=True)
    assert result == 0
    assert any("appeared concurrently" in r.getMessage() for r in caplog.records)


def test_cut_release_push_failure_without_tolerance_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Push failure on the strict (--bump) path → error + exit 1."""

    def _fake_git(*args: str, **_kw: object) -> str:
        if args[0] == "push":
            raise subprocess.CalledProcessError(1, ["git", "push"])
        if args[0] == "remote":
            return "https://example.invalid/origin.git"
        return ""

    monkeypatch.setattr(release, "run_git", _fake_git)
    monkeypatch.setattr(release, "create_annotated_tag", lambda *_a, **_kw: None)
    with caplog.at_level(logging.ERROR, logger="forge.release"):
        result = release._cut_release(tmp_path, "v1.0.0", race_tolerant=False)
    assert result == 1
    assert any("failed" in r.getMessage() for r in caplog.records)


def test_cut_release_race_tolerant_push_failure_without_remote_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Push fails and no concurrent tag exists → still exit 1."""

    def _fake_git(*args: str, **_kw: object) -> str:
        if args[0] == "push":
            raise subprocess.CalledProcessError(1, ["git", "push"])
        if args[0] == "remote":
            return "https://example.invalid/origin.git"
        return ""  # tag --list and ls-remote report nothing

    monkeypatch.setattr(release, "run_git", _fake_git)
    monkeypatch.setattr(release, "create_annotated_tag", lambda *_a, **_kw: None)
    assert release._cut_release(tmp_path, "v1.0.0", race_tolerant=True) == 1


def test_main_from_changelog_model_guard_beats_idempotency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wrong release model is reported even when the declared tag exists.

    SCENARIO: dual-track repo with a stale tag matching the CHANGELOG
        top heading — misconfiguration must not be masked by the
        already-released short-circuit.
    MOCK SETUP: real repo + tag v1.0.0; load_config → dual-track.
    EXPECTED BEHAVIOR: exit 1 naming the promotion flow, not "already
        released" exit 0.
    """
    work, _bare = _repo_with_origin(tmp_path)
    (work / "CHANGELOG.md").write_text("## v1.0.0\n")
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "changelog"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.0.0", "-m", "v1.0.0"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    monkeypatch.setattr(
        release,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.ERROR, logger="forge.release"):
        assert release.main() == 1
    assert any("Dual-track" in r.getMessage() for r in caplog.records)
