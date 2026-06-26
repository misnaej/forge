"""Tests for ``forge.verify_changelog_history`` — CHANGELOG history guard.

Verifies that the guard correctly detects when a dev-track CHANGELOG blindly
drops curated ``## vX.Y.Z`` entries from origin/<base>.
"""

# MOCKING STRATEGY: Group C ``main()`` tests monkeypatch
# ``verify_changelog_history.load_config`` to supply a ForgeConfig without a
# real ``pyproject.toml``, mirroring the Group F pattern in
# ``test_verify_main_tags.py``. ``sys.argv`` is patched to the CLI name so
# argparse does not interpret pytest's own arguments. Group A (``_headings``)
# is pure; Group B (``_base_is_ancestor``) uses real git. Group C tests that
# exercise fetch/ancestor checks require a real dual-track repo with a bare
# origin; tests that exit before any git call need only a ``chdir(tmp_path)``.
# Monkeypatch targets use the consuming namespace (``verify_changelog_history.*``),
# never the originating module.

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from forge import verify_changelog_history
from forge.config import ForgeConfig
from tests.conftest import GIT_ENV as _GIT_ENV


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# Shared git identity (_GIT_ENV) + ephemeral-repo init live in tests.conftest
# (#85) — passed as env= to all subprocess helpers so commits find an identity
# without a ~/.gitconfig.


# ---------------------------------------------------------------------------
# Repo helpers
# ---------------------------------------------------------------------------


def _init_dual_track_repo(base: Path) -> tuple[Path, Path]:
    """Initialize a paired work/bare dual-track git repository under *base*.

    Creates ``base/work`` (git init -b main, initial commit, dev branch) and
    ``base/origin.git`` (bare repo); wires them via ``git remote add origin``
    and pushes both ``main`` and ``dev``.  Mirrors the forge dual-track layout
    so tests have a real remote to fetch from.

    Args:
        base: Parent directory; must already exist.

    Returns:
        A ``(work, bare)`` tuple of the work-tree and bare-repo paths.
    """
    work = base / "work"
    bare = base / "origin.git"
    work.mkdir()
    bare.mkdir()

    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"],
        ["git", "checkout", "-q", "-b", "dev"],
        ["git", "checkout", "-q", "main"],
    ):
        subprocess.run(cmd, cwd=work, env=_GIT_ENV, check=True)

    subprocess.run(["git", "init", "--bare", "-q"], cwd=bare, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "dev"], cwd=work, env=_GIT_ENV, check=True
    )
    return work, bare


def _setup_main_as_ancestor_repo(
    base: Path, *, local_changelog: str
) -> tuple[Path, Path]:
    """Build a dual-track repo where origin/main IS an ancestor of HEAD.

    Puts a CHANGELOG.md with ``## v1.0.0`` and ``## v1.1.0`` headings on
    ``main`` and pushes it to origin.  Then creates a ``release/v1.2.0``
    branch as a direct child of that commit and writes *local_changelog* to
    ``CHANGELOG.md`` there.  Since HEAD descends from ``origin/main``'s tip,
    ``_base_is_ancestor`` returns ``True`` — the promotion-context trigger
    fires.

    Args:
        base: Parent directory for ``work`` / ``origin.git``.
        local_changelog: Text content for the working-tree ``CHANGELOG.md``
            on the release branch (may have headings missing or added).

    Returns:
        A ``(work, bare)`` tuple.
    """
    work, bare = _init_dual_track_repo(base)

    # Commit CHANGELOG.md with two curated headings on main and push.
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=work, env=_GIT_ENV, check=True
    )
    (work / "CHANGELOG.md").write_text("## v1.1.0\n\n## v1.0.0\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add changelog"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=_GIT_ENV, check=True
    )

    # Branch from main's tip — origin/main is a direct ancestor of HEAD.
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v1.2.0"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    (work / "CHANGELOG.md").write_text(local_changelog)
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "update changelog"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    return work, bare


# ---------------------------------------------------------------------------
# Group A — _headings (pure)
# ---------------------------------------------------------------------------


def test_headings_parses_semver_level2_only() -> None:
    """``_headings`` picks up ``## vX.Y.Z`` lines and ignores other markup."""
    text = (
        "# Changelog\n"
        "\n"
        "## v1.1.0\n"
        "\n"
        "Some prose about v1.1.0.\n"
        "\n"
        "### Sub-heading ignored\n"
        "\n"
        "## v1.0.0\n"
        "\n"
        "Not a heading: ## v0.9.0 inline\n"
    )
    result = verify_changelog_history._headings(text)
    assert result == {"v1.0.0", "v1.1.0"}


def test_headings_returns_empty_when_no_semver_headings() -> None:
    """``_headings`` returns an empty set when no ``## vX.Y.Z`` lines exist."""
    text = "# Changelog\n\nSome unreleased notes.\n\n### Details\n"
    assert verify_changelog_history._headings(text) == set()


# ---------------------------------------------------------------------------
# Group B — _base_is_ancestor (real git)
# ---------------------------------------------------------------------------


def test_base_is_ancestor_true_and_false(tmp_path: Path) -> None:
    """``_base_is_ancestor`` returns True/False correctly with real git commits.

    SCENARIO: Three commits — A (initial), C (on 'side' branch diverging from
    A), B (on 'main', child of A).  HEAD is on main at B.  The 'side' branch
    is not reachable from HEAD; the 'base-commit' tag (A) is.
    """
    # A: initial commit; tag it for a reachable-ancestor ref.
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "A"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "tag", "base-commit"], cwd=tmp_path, env=_GIT_ENV, check=True
    )

    # C: divergent commit on 'side', branching from A.
    subprocess.run(
        ["git", "checkout", "-q", "-b", "side"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "C"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )

    # B: commit on 'main', child of A; HEAD lands here.
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "B"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )

    # 'base-commit' (A) is an ancestor of HEAD (B): merge-base(A, B) = A.
    assert verify_changelog_history._base_is_ancestor(tmp_path, "base-commit") is True

    # 'side' (C) is NOT an ancestor of HEAD (B): merge-base(C, B) = A ≠ C.
    assert verify_changelog_history._base_is_ancestor(tmp_path, "side") is False


# ---------------------------------------------------------------------------
# Group C — main() integration (real git + bare origin; monkeypatched config)
# ---------------------------------------------------------------------------


def test_skips_single_branch_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: base_branch == dev_branch; guard must exit immediately.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="main")``.
        ``tmp_path`` has a ``CHANGELOG.md``; no git repo needed — the
        early-exit fires before any git invocation.
    EXPECTED BEHAVIOR: returns 0; caplog contains "single-branch".
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.setattr(
        verify_changelog_history,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-changelog-history"])
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.INFO, logger="forge.verify_changelog_history"):
        result = verify_changelog_history.main()
    assert result == 0
    assert any("single-branch" in r.getMessage() for r in caplog.records)


def test_skips_without_changelog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: dual-track repo with no CHANGELOG.md; guard must self-skip.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``.
        No ``CHANGELOG.md`` in the work tree; the guard returns before any
        git fetch.
    EXPECTED BEHAVIOR: returns 0; caplog contains "no CHANGELOG.md".
    """
    monkeypatch.setattr(
        verify_changelog_history,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-changelog-history"])
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.INFO, logger="forge.verify_changelog_history"):
        result = verify_changelog_history.main()
    assert result == 0
    assert any("no CHANGELOG.md" in r.getMessage() for r in caplog.records)


def test_skips_when_base_not_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: HEAD on dev; origin/main advanced past dev's fork point.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``.
        Real dual-track repo.  ``dev`` commits a CHANGELOG and diverges from
        the initial commit; ``main`` then advances with a new commit and is
        pushed to origin.  HEAD is left on dev, so origin/main is NOT
        reachable from HEAD.
    EXPECTED BEHAVIOR: returns 0; caplog contains "not an ancestor".
    """
    work, _bare = _init_dual_track_repo(tmp_path)

    # On dev: write CHANGELOG.md and commit.
    subprocess.run(["git", "checkout", "-q", "dev"], cwd=work, env=_GIT_ENV, check=True)
    (work / "CHANGELOG.md").write_text("## v1.0.0\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add changelog"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "dev"], cwd=work, env=_GIT_ENV, check=True
    )

    # On main: advance with an extra commit and push → origin/main diverges from dev.
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=work, env=_GIT_ENV, check=True
    )
    (work / "extra.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "extra.py"], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "advance main"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=_GIT_ENV, check=True
    )

    # HEAD on dev: CHANGELOG.md present, but origin/main is not reachable.
    subprocess.run(["git", "checkout", "-q", "dev"], cwd=work, env=_GIT_ENV, check=True)

    monkeypatch.setattr(
        verify_changelog_history,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-changelog-history"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.verify_changelog_history"):
        result = verify_changelog_history.main()
    assert result == 0
    assert any("not an ancestor" in r.getMessage() for r in caplog.records)


def test_passes_when_all_base_headings_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: release branch descended from origin/main; local CHANGELOG complete.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``.
        origin/main CHANGELOG has ``## v1.0.0`` and ``## v1.1.0``; the
        release branch adds ``## v1.2.0`` but keeps both prior headings.
        HEAD is one commit ahead of origin/main (direct child), so
        ``_base_is_ancestor`` returns True.
    EXPECTED BEHAVIOR: returns 0; caplog contains "preserved".
    """
    local_content = "## v1.2.0\n\n## v1.1.0\n\n## v1.0.0\n"
    work, _bare = _setup_main_as_ancestor_repo(tmp_path, local_changelog=local_content)

    monkeypatch.setattr(
        verify_changelog_history,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-changelog-history"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.verify_changelog_history"):
        result = verify_changelog_history.main()
    assert result == 0
    assert any("preserved" in r.getMessage() for r in caplog.records)


def test_fails_when_base_heading_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: release branch dropped ``## v1.0.0`` via blind conflict resolution.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``.
        origin/main CHANGELOG has ``## v1.0.0`` and ``## v1.1.0``; the
        release branch has only ``## v1.1.0`` and ``## v1.2.0`` — simulating
        a ``git checkout --ours`` that erased main's v1.0.0 entry.
    EXPECTED BEHAVIOR: returns 1; an error log record names "v1.0.0".
    """
    local_content = "## v1.2.0\n\n## v1.1.0\n"  # v1.0.0 silently dropped
    work, _bare = _setup_main_as_ancestor_repo(tmp_path, local_changelog=local_content)

    monkeypatch.setattr(
        verify_changelog_history,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-changelog-history"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.verify_changelog_history"):
        result = verify_changelog_history.main()
    assert result == 1
    error_messages = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
    ]
    assert any("v1.0.0" in m for m in error_messages)


def test_skips_when_no_changelog_on_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: origin/main exists and is an ancestor of HEAD, but has no CHANGELOG.md.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``.
        A non-CHANGELOG file is committed on ``main`` and pushed to origin
        (origin/main has no ``CHANGELOG.md``).  A release branch is created
        from that tip so origin/main IS an ancestor of HEAD; a local
        ``CHANGELOG.md`` is written on the branch (local checks pass), but
        ``git show origin/main:CHANGELOG.md`` returns empty.
    EXPECTED BEHAVIOR: returns 0; caplog contains "no CHANGELOG".
    """
    work, _bare = _init_dual_track_repo(tmp_path)

    # On main: commit a non-CHANGELOG file and push — origin/main has no CHANGELOG.md.
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=work, env=_GIT_ENV, check=True
    )
    (work / "version.py").write_text("__version__ = '1.0.0'\n")
    subprocess.run(["git", "add", "version.py"], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add version"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=_GIT_ENV, check=True
    )

    # Branch from main's tip — origin/main is a direct ancestor of HEAD.
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v1.0.0"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    # Write a local CHANGELOG.md so the local-file check passes.
    (work / "CHANGELOG.md").write_text("## v1.0.0\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add changelog"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )

    monkeypatch.setattr(
        verify_changelog_history,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-changelog-history"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.verify_changelog_history"):
        result = verify_changelog_history.main()
    assert result == 0
    assert any("no CHANGELOG" in r.getMessage() for r in caplog.records)


def test_fails_when_multiple_headings_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: local CHANGELOG keeps only a new heading, drops both base entries.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``.
        origin/main CHANGELOG has ``## v1.0.0`` and ``## v1.1.0``; the
        release branch has only ``## v1.2.0`` — both prior headings erased.
    EXPECTED BEHAVIOR: returns 1; the error record names both "v1.0.0" and
        "v1.1.0"; the plural word "entries" appears (len(missing) != 1 branch).
    """
    local_content = "## v1.2.0\n"  # both v1.0.0 and v1.1.0 silently dropped
    work, _bare = _setup_main_as_ancestor_repo(tmp_path, local_changelog=local_content)

    monkeypatch.setattr(
        verify_changelog_history,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-changelog-history"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.verify_changelog_history"):
        result = verify_changelog_history.main()
    assert result == 1
    error_messages = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
    ]
    assert any("v1.0.0" in m for m in error_messages)
    assert any("v1.1.0" in m for m in error_messages)
    assert any("entries" in m for m in error_messages)


def test_passes_when_headings_reordered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: local CHANGELOG has the same headings as origin/main but reversed.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``.
        origin/main CHANGELOG lists ``## v1.1.0`` before ``## v1.0.0``; the
        release branch lists them in the opposite order.  Since ``_headings``
        returns a set, order is irrelevant — both produce
        ``{"v1.0.0", "v1.1.0"}`` and the difference is empty.
    EXPECTED BEHAVIOR: returns 0; caplog contains "preserved".
    """
    # Reversed order relative to origin/main's "## v1.1.0\n\n## v1.0.0\n".
    local_content = "## v1.0.0\n\n## v1.1.0\n"
    work, _bare = _setup_main_as_ancestor_repo(tmp_path, local_changelog=local_content)

    monkeypatch.setattr(
        verify_changelog_history,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-changelog-history"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.INFO, logger="forge.verify_changelog_history"):
        result = verify_changelog_history.main()
    assert result == 0
    assert any("preserved" in r.getMessage() for r in caplog.records)
