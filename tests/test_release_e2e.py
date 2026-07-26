"""End-to-end exercise of the single-track consumer release path.

Forge itself is dual-track and manifest-versioned, so ``forge-release``
structurally never runs against this repo — the consumer path would
ship with unit tests only. These tests build a real single-track
consumer repo (work tree + bare origin, setuptools-scm shape, canonical
CHANGELOG per ``docs/consumer-release.md``) and drive the documented
recipes end to end: the manual ``--bump`` flow, the ``--from-changelog``
tag-on-merge flow, and the two opt-in changelog pre-commit steps against
the same fixture. They double as a living check that the
``docs/consumer-release.md`` recipe stays executable as written.
"""

# MOCKING STRATEGY: almost none — the point of this module is real git.
# Every scenario runs against a throwaway work tree wired to a local
# bare "origin" (no network, no gh). The only patched seams are
# ``release.load_config`` / config-free defaults (the fixture has no
# ``[tool.forge]`` table, so single-track "main" defaults apply),
# ``sys.argv`` for argparse, and ``precommit.is_ci`` where a scenario
# pins the CI/non-CI branch of the tag-fetch gate.

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from forge import precommit, release
from tests.conftest import GIT_ENV as _GIT_ENV
from tests.conftest import init_git_repo as _init_git_repo


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_CHANGELOG = (
    "# Changelog\n"
    "\n"
    "## v0.2.0\n"
    "\n"
    "### Added\n"
    "\n"
    "- first shipped feature\n"
    "\n"
    "## v0.1.0 — 2026-07-01\n"
    "\n"
    "### Added\n"
    "\n"
    "- initial release\n"
)


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo* and return stripped stdout.

    Args:
        repo: Working directory for the invocation.
        *args: Argv tail after ``git``.

    Returns:
        Trimmed stdout.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _consumer_repo(base: Path) -> tuple[Path, Path]:
    """Build the canonical single-track consumer fixture.

    A work tree wired to a local bare origin, on ``main``, with a
    minimal setuptools-scm-shaped ``pyproject.toml`` (no manual
    ``version =``, no ``[tool.forge]`` table, no plugin manifest), a
    CHANGELOG following the ``docs/consumer-release.md`` convention
    (top heading ``v0.2.0`` declares the next release), and ``v0.1.0``
    already tagged and pushed.

    Args:
        base: Parent directory for ``work`` and ``origin.git``.

    Returns:
        A ``(work, bare)`` tuple.
    """
    work = base / "work"
    bare = base / "origin.git"
    work.mkdir()
    bare.mkdir()
    _init_git_repo(work)
    subprocess.run(["git", "init", "--bare", "-q"], cwd=bare, env=_GIT_ENV, check=True)
    _git(work, "remote", "add", "origin", str(bare))

    (work / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=64", "setuptools-scm>=8"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        'name = "consumer"\n'
        'dynamic = ["version"]\n'
        "\n"
        "[tool.setuptools_scm]\n"
    )
    (work / "CHANGELOG.md").write_text(_CHANGELOG)
    (work / "src").mkdir()
    (work / "src" / "consumer.py").write_text('"""Consumer module."""\n')
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "feat: initial consumer layout")
    _git(work, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
    _git(work, "push", "-q", "origin", "main", "--tags")
    return work, bare


def _bare_has_tag(bare: Path, tag: str) -> bool:
    """Return whether *bare* carries *tag*.

    Args:
        bare: Bare origin repo.
        tag: Tag name.

    Returns:
        ``True`` when the tag exists on the origin side.
    """
    return _git(bare, "tag", "--list", tag) == tag


def test_manual_bump_recipe_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: the documented manual recipe, dry-run then real cut.

    MOCK SETUP: real fixture repo; only sys.argv patched per invocation.
    EXPECTED BEHAVIOR: dry-run names v0.2.0 and cuts nothing; the real
    run cuts an annotated v0.2.0 and pushes it to origin.
    """
    work, bare = _consumer_repo(tmp_path)
    monkeypatch.chdir(work)

    monkeypatch.setattr("sys.argv", ["forge-release", "--bump", "minor", "--dry-run"])
    with caplog.at_level(logging.INFO, logger="forge.release"):
        assert release.main() == 0
    assert any("v0.2.0" in r.getMessage() for r in caplog.records)
    assert not _bare_has_tag(bare, "v0.2.0")

    monkeypatch.setattr("sys.argv", ["forge-release", "--bump", "minor"])
    assert release.main() == 0
    assert _bare_has_tag(bare, "v0.2.0")
    assert _git(work, "cat-file", "-t", "v0.2.0") == "tag"


def test_from_changelog_recipe_end_to_end_with_idempotent_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: the tag-on-merge flow — declared version cut, then rerun.

    MOCK SETUP: real fixture repo; sys.argv patched.
    EXPECTED BEHAVIOR: --from-changelog cuts the declared v0.2.0; an
    immediate rerun (the CI-retry / manual-race shape) exits 0 with the
    already-released message and cuts nothing new.
    """
    work, bare = _consumer_repo(tmp_path)
    monkeypatch.chdir(work)

    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    assert release.main() == 0
    assert _bare_has_tag(bare, "v0.2.0")

    with caplog.at_level(logging.INFO, logger="forge.release"):
        assert release.main() == 0
    assert any("already released" in r.getMessage() for r in caplog.records)


def test_changelog_steps_pass_on_convention_following_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: a feature branch follows the convention — both steps pass.

    MOCK SETUP: real fixture; branch adds code + a bullet under the
    declared (untagged) top heading; is_ci pinned True to skip the
    network-shaped tag fetch (tags are local and current).
    EXPECTED BEHAVIOR: changelog_version and changelog_updated both pass.
    """
    work, _bare = _consumer_repo(tmp_path)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    _git(work, "checkout", "-q", "-b", "feat/x")
    (work / "src" / "feature.py").write_text('"""New feature."""\n')
    (work / "CHANGELOG.md").write_text(
        _CHANGELOG.replace(
            "- first shipped feature", "- first shipped feature\n- new feature"
        )
    )
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "feat: new feature with changelog bullet")

    version_result = precommit.step_changelog_version(work)
    assert version_result.passed, version_result.output
    updated_result = precommit.step_changelog_updated(work)
    assert updated_result.passed, updated_result.output


def test_changelog_steps_catch_missing_entry_and_stranded_bullet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: the two failure modes the steps exist for, on real git.

    MOCK SETUP: real fixture. First a branch changing code without a
    CHANGELOG edit; then v0.2.0 gets tagged out from under the branch
    (the #212 race shape) while its bullet sits under that heading.
    EXPECTED BEHAVIOR: changelog_updated fails the first; after the tag
    lands, changelog_version flags the branch's bullet as stranded.
    """
    work, _bare = _consumer_repo(tmp_path)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    _git(work, "checkout", "-q", "-b", "feat/x")
    (work / "src" / "feature.py").write_text('"""New feature."""\n')
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "feat: code change, no changelog")

    updated_result = precommit.step_changelog_updated(work)
    assert not updated_result.passed
    assert "CHANGELOG.md entry" in updated_result.output

    # The branch now adds its bullet under the still-declared v0.2.0...
    (work / "CHANGELOG.md").write_text(
        _CHANGELOG.replace(
            "- first shipped feature", "- first shipped feature\n- late bullet"
        )
    )
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "docs: changelog bullet")
    assert precommit.step_changelog_updated(work).passed

    # ...and meanwhile the release is cut on main (tag lands under the
    # open branch — no conflict, no signal except the step).
    _git(work, "tag", "-a", "v0.2.0", "-m", "v0.2.0", "main")

    version_result = precommit.step_changelog_version(work)
    assert not version_result.passed
    assert "stranded" in version_result.output


def test_changelog_version_step_accepts_posttag_equality_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right after a cut, top heading == latest tag is the valid state.

    The convention's equality window: on main, immediately after
    ``forge-release`` tagged the declared version, the step must pass —
    the next PR (not a release PR) opens the following heading.
    """
    work, _bare = _consumer_repo(tmp_path)
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    monkeypatch.setattr("sys.argv", ["forge-release", "--from-changelog"])
    monkeypatch.chdir(work)
    assert release.main() == 0

    result = precommit.step_changelog_version(work)
    assert result.passed, result.output
