"""Tests for ``forge.verify_main_tags`` — tag alignment CLI."""

# MOCKING STRATEGY: Group C tests isolate ``_repair`` by monkeypatching
# ``verify_main_tags._force_move_tag`` with a raise-if-called sentinel, and
# stubbing ``verify_main_tags.is_non_interactive`` /
# ``verify_main_tags.git_auth_mode`` to control the auth-gate branch.
# Group F ``main()`` tests monkeypatch ``verify_main_tags.load_config`` to
# supply a ForgeConfig without a real ``pyproject.toml``, and use real-git
# bare-origin repos for end-to-end push verification.
# Groups A/B/E are pure (no I/O); Groups D/F use real ``git`` subprocesses.
# Monkeypatch targets are always the consuming namespace
# (``verify_main_tags.*``), never the originating module.

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING

from forge import git_utils, verify_main_tags
from forge.config import ForgeConfig


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ---------------------------------------------------------------------------
# Shared git identity — passed as env= to all subprocess helpers so that
# annotated tags (git tag -a) find author/committer without a ~/.gitconfig.
# ---------------------------------------------------------------------------

_GIT_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": os.environ.get("PATH", ""),
}


# ---------------------------------------------------------------------------
# Repo helpers
# ---------------------------------------------------------------------------


def _init_dual_track_repo(base: Path) -> tuple[Path, Path]:
    """Initialize a paired work/bare dual-track git repository under *base*.

    Creates ``base/work`` (git init -b main, initial commit, dev branch) and
    ``base/origin.git`` (bare repo); wires them via ``git remote add origin``
    and pushes both ``main`` and ``dev``.  Mirrors the forge dual-track layout
    (``dev_branch != base_branch``) so tests have a real remote to fetch from
    and push to.

    Args:
        base: Parent directory; must already exist.  ``work`` and
            ``origin.git`` are created inside it.

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


def _write_file_commit(
    repo: Path,
    filename: str,
    content: str,
    message: str,
    *,
    branch: str,
) -> str:
    """Check out *branch*, write *content* to *filename*, commit, return the SHA.

    Args:
        repo: Working-tree root.
        filename: Repo-relative file path to write.
        content: Text content for the file.
        message: Commit message.
        branch: Branch to check out before writing.

    Returns:
        The full 40-char SHA of the new commit.
    """
    subprocess.run(
        ["git", "checkout", "-q", branch], cwd=repo, env=_GIT_ENV, check=True
    )
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=repo, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=_GIT_ENV, check=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _push_tag(repo: Path, tag: str, commit_sha: str, bare: Path) -> None:
    """Create an annotated tag at *commit_sha* and force-push it to *bare*.

    Uses ``-f`` so the helper is idempotent when called twice with the same
    tag name (e.g. in setup helpers that reuse the same tag).

    Args:
        repo: Working-tree root with an ``origin`` remote configured.
        tag: Semver tag name to create (e.g. ``"v1.0.0"``).
        commit_sha: Full SHA of the commit to tag.
        bare: Bare-repo path used as the push destination (equivalent to
            ``origin`` since the remote is wired to this path).
    """
    subprocess.run(
        ["git", "tag", "-f", "-a", tag, "-m", tag, commit_sha],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "--force", str(bare), tag],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
    )


def _dual_track_with_unpromoted_tag(base: Path) -> tuple[Path, Path, str, str]:
    """Set up a dual-track repo where v1.0.0 is tagged on dev but not on main.

    Creates the full dual-track layout (via :func:`_init_dual_track_repo`),
    commits a file on ``dev`` and tags it ``v1.0.0``, then creates an
    identical-tree squash commit on ``main`` without moving the tag.  This
    reproduces the invariant tested by F4-F9: ``origin/main`` has a commit
    whose tree equals ``v1.0.0``'s tree, but the tag still points at the dev
    commit.

    Args:
        base: Parent directory for the ``work`` / ``origin.git``
            subdirectories.

    Returns:
        A ``(work, bare, dev_sha, main_sha)`` tuple where ``dev_sha`` is the
        tagged dev commit and ``main_sha`` is the untagged squash commit on
        ``main`` whose tree equals ``dev_sha``'s tree.
    """
    work, bare = _init_dual_track_repo(base)

    dev_sha = _write_file_commit(
        work, "v1.py", "x = 1\n", "release v1.0.0", branch="dev"
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "dev"], cwd=work, env=_GIT_ENV, check=True
    )
    _push_tag(work, "v1.0.0", dev_sha, bare)

    # Identical-tree squash commit on main via tree checkout.
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "v1.0.0", "--", "."],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "promote v1.0.0"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    main_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=_GIT_ENV, check=True
    )

    return work, bare, dev_sha, main_sha


def _dual_track_promotion_with_extra(
    base: Path, *, filename: str, content: str
) -> tuple[Path, Path, str, str]:
    """Dual-track repo whose main squash adds *filename* atop the tagged tree.

    Like :func:`_dual_track_with_unpromoted_tag`, but the ``main`` squash
    commit reproduces v1.0.0's tree AND writes an extra ``filename`` absent
    from the tag. Exercises release-fingerprint matching: ``CHANGELOG.md``
    is excluded (still a move target), any other path is not (no target,
    reported as unreproduced).

    Args:
        base: Parent directory for the work / bare repos.
        filename: Extra file written on the main squash commit only.
        content: Contents for *filename*.

    Returns:
        A ``(work, bare, dev_sha, main_sha)`` tuple; ``main_sha`` is the
        squash commit whose tree equals v1.0.0's plus *filename*.
    """
    work, bare = _init_dual_track_repo(base)
    dev_sha = _write_file_commit(
        work, "v1.py", "x = 1\n", "release v1.0.0", branch="dev"
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "dev"], cwd=work, env=_GIT_ENV, check=True
    )
    _push_tag(work, "v1.0.0", dev_sha, bare)

    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=work, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "v1.0.0", "--", "."], cwd=work, env=_GIT_ENV, check=True
    )
    (work / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "promote v1.0.0 + extra"],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    main_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=_GIT_ENV, check=True
    )
    return work, bare, dev_sha, main_sha


def test_fix_relocates_when_base_diverges_only_by_changelog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: main squash = v1.0.0 tree + a curated CHANGELOG entry.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``;
        git identity env set so ``git tag -a`` succeeds.
    EXPECTED BEHAVIOR: the release fingerprint ignores ``CHANGELOG.md``, so
        the main squash still reproduces v1.0.0 → ``--fix`` relocates the
        tag onto it. This is the modified-release-branch pattern.
    """
    work, bare, _dev_sha, main_sha = _dual_track_promotion_with_extra(
        tmp_path, filename="CHANGELOG.md", content="## v1.0.0 — curated\n"
    )
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags", "--fix"])
    monkeypatch.chdir(work)
    assert verify_main_tags.main() == 0
    result = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == main_sha


def test_base_diverging_by_non_changelog_is_not_a_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: main squash = v1.0.0 tree + an extra .py file (not CHANGELOG).

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``.
    EXPECTED BEHAVIOR: the extra file changes the release fingerprint, so no
        base commit reproduces the tag → it is reported as unreproduced, not
        drift (verify returns 0), and the tag stays on the dev commit. Proves
        the CHANGELOG exclusion is scoped and does not blanket-match.
    """
    work, bare, dev_sha, _main_sha = _dual_track_promotion_with_extra(
        tmp_path, filename="extra.py", content="y = 2\n"
    )
    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags"])
    monkeypatch.chdir(work)
    assert verify_main_tags.main() == 0
    result = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == dev_sha


# ---------------------------------------------------------------------------
# Group A — _TagState.needs_move (pure)
# ---------------------------------------------------------------------------


def test_tag_state_needs_move_true_when_target_and_current_differ() -> None:
    """``needs_move`` is True when target and current SHAs differ."""
    state = verify_main_tags._TagState(tag="v1.0.0", target="aaa", current="bbb")
    assert state.needs_move is True


def test_tag_state_needs_move_false_when_already_on_target() -> None:
    """``needs_move`` is False when the tag already points at its target."""
    state = verify_main_tags._TagState(tag="v1.0.0", target="aaa", current="aaa")
    assert state.needs_move is False


def test_tag_state_needs_move_false_when_no_target() -> None:
    """``needs_move`` is False when target is None (tag never promoted)."""
    state = verify_main_tags._TagState(tag="v1.0.0", target=None, current="aaa")
    assert state.needs_move is False


# ---------------------------------------------------------------------------
# Group B — _verify (fabricated states)
# ---------------------------------------------------------------------------


def test_verify_returns_zero_when_all_states_aligned() -> None:
    """All tags on their target commit → exit 0."""
    states = [
        verify_main_tags._TagState(tag="v1.0.0", target="aaa", current="aaa"),
        verify_main_tags._TagState(tag="v2.0.0", target="bbb", current="bbb"),
    ]
    assert verify_main_tags._verify(states, "origin/main") == 0


def test_verify_returns_one_when_any_state_misplaced() -> None:
    """At least one misplaced tag → exit 1."""
    states = [
        verify_main_tags._TagState(tag="v1.0.0", target="aaa", current="aaa"),
        verify_main_tags._TagState(tag="v2.0.0", target="ccc", current="ddd"),
    ]
    assert verify_main_tags._verify(states, "origin/main") == 1


# ---------------------------------------------------------------------------
# Group C — _repair (monkeypatch _force_move_tag / auth helpers)
# ---------------------------------------------------------------------------


def test_repair_returns_zero_when_nothing_to_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: all states aligned; repair must short-circuit without pushing.

    MOCK SETUP: ``_force_move_tag`` raises AssertionError if called.
    EXPECTED BEHAVIOR: returns 0; sentinel never fires.
    """
    msg = "_force_move_tag must not be called when nothing to move"

    def _sentinel(*_a: object, **_kw: object) -> None:
        raise AssertionError(msg)

    monkeypatch.setattr(verify_main_tags, "_force_move_tag", _sentinel)
    states = [
        verify_main_tags._TagState(tag="v1.0.0", target="aaa", current="aaa"),
    ]
    assert verify_main_tags._repair(tmp_path, states, "origin/main", dry_run=False) == 0


def test_repair_dry_run_logs_moves_without_calling_force_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: one misplaced tag; dry_run=True must log but not push.

    MOCK SETUP: ``_force_move_tag`` raises AssertionError if called; auth
        functions are NOT patched (the auth gate must be skipped in dry-run
        regardless of auth state).
    EXPECTED BEHAVIOR: returns 0; sentinel never fires; caplog includes a
        ``[dry-run]`` notice for the tag.
    """
    msg = "_force_move_tag must not be called in dry-run"

    def _sentinel(*_a: object, **_kw: object) -> None:
        raise AssertionError(msg)

    monkeypatch.setattr(verify_main_tags, "_force_move_tag", _sentinel)
    states = [
        verify_main_tags._TagState(
            tag="v1.0.0", target="abc123456", current="def789012"
        ),
    ]
    with caplog.at_level(logging.DEBUG, logger="forge.verify_main_tags"):
        result = verify_main_tags._repair(tmp_path, states, "origin/main", dry_run=True)
    assert result == 0
    assert any(
        "dry-run" in r.getMessage() and "v1.0.0" in r.getMessage()
        for r in caplog.records
    )


def test_repair_returns_one_in_non_interactive_no_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: one misplaced tag; non-interactive with no git auth.

    MOCK SETUP: ``is_non_interactive`` → True; ``git_auth_mode`` → "none";
        ``_force_move_tag`` raises if called.
    EXPECTED BEHAVIOR: auth gate fires; returns 1; sentinel never invoked.
    """
    msg = "_force_move_tag must not be called when auth blocked"

    def _sentinel(*_a: object, **_kw: object) -> None:
        raise AssertionError(msg)

    monkeypatch.setattr(verify_main_tags, "is_non_interactive", lambda: True)
    monkeypatch.setattr(verify_main_tags, "git_auth_mode", lambda: "none")
    monkeypatch.setattr(verify_main_tags, "_force_move_tag", _sentinel)
    states = [
        verify_main_tags._TagState(
            tag="v1.0.0", target="abc123456", current="def789012"
        ),
    ]
    assert verify_main_tags._repair(tmp_path, states, "origin/main", dry_run=False) == 1


def test_repair_skips_non_interactive_check_in_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: non-interactive no-auth environment but dry_run=True.

    MOCK SETUP: ``is_non_interactive`` → True; ``git_auth_mode`` → "none";
        ``_force_move_tag`` raises if called.
    EXPECTED BEHAVIOR: auth gate must NOT fire in dry-run; returns 0.
    """
    msg = "_force_move_tag must not be called in dry-run"

    def _sentinel(*_a: object, **_kw: object) -> None:
        raise AssertionError(msg)

    monkeypatch.setattr(verify_main_tags, "is_non_interactive", lambda: True)
    monkeypatch.setattr(verify_main_tags, "git_auth_mode", lambda: "none")
    monkeypatch.setattr(verify_main_tags, "_force_move_tag", _sentinel)
    states = [
        verify_main_tags._TagState(
            tag="v1.0.0", target="abc123456", current="def789012"
        ),
    ]
    assert verify_main_tags._repair(tmp_path, states, "origin/main", dry_run=True) == 0


def test_repair_proceeds_to_push_when_non_interactive_but_has_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: non-interactive env with SSH auth; repair must proceed.

    MOCK SETUP: ``is_non_interactive`` → True; ``git_auth_mode`` → "ssh";
        ``_force_move_tag`` replaced with a recording sentinel that appends
        to a list without raising; one misplaced tag state.
    EXPECTED BEHAVIOR: auth gate passes (SSH counts as valid auth);
        returns 0; sentinel was invoked.
    """
    called: list[bool] = []

    def _recording_sentinel(_repo: Path, _tag: str, _sha: str) -> None:
        called.append(True)

    monkeypatch.setattr(verify_main_tags, "is_non_interactive", lambda: True)
    monkeypatch.setattr(verify_main_tags, "git_auth_mode", lambda: "ssh")
    monkeypatch.setattr(verify_main_tags, "_force_move_tag", _recording_sentinel)
    states = [
        verify_main_tags._TagState(
            tag="v1.0.0", target="abc123456", current="def789012"
        ),
    ]
    result = verify_main_tags._repair(tmp_path, states, "origin/main", dry_run=False)
    assert result == 0
    assert called  # sentinel was invoked — auth gate passed, push proceeded


# ---------------------------------------------------------------------------
# Group D — _minor_tags / _base_tree_index (real git, no origin needed)
# ---------------------------------------------------------------------------


def test_minor_tags_returns_only_patch_zero_tags_sorted(tmp_path: Path) -> None:
    """Only vX.Y.0 tags are returned, sorted ascending by semver."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    for tag in ("v1.0.0", "v1.0.1", "v2.0.0", "v2.1.0", "v1.1.0"):
        subprocess.run(["git", "tag", tag], cwd=tmp_path, env=_GIT_ENV, check=True)
    assert verify_main_tags._minor_tags(tmp_path) == [
        "v1.0.0",
        "v1.1.0",
        "v2.0.0",
        "v2.1.0",
    ]


def test_minor_tags_returns_empty_when_no_v_tags(tmp_path: Path) -> None:
    """Repo with no ``v*`` tags at all returns an empty list."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert verify_main_tags._minor_tags(tmp_path) == []


def test_base_tree_index_newest_commit_wins_on_tree_collision(
    tmp_path: Path,
) -> None:
    """When two commits share a tree, the newer commit is the index value.

    ``git log`` emits newest-first, so the first occurrence wins — which
    is the most recent commit reproducing that tree.
    """
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    # M1: introduce a.py → tree T1.
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "M1"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    m1 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # MID: add b.py → tree T2 (breaks T1 identity).
    (tmp_path / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "b.py"], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "MID"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    # M2: remove b.py → tree T1 again (same as M1).
    subprocess.run(["git", "rm", "-q", "b.py"], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "M2 same tree as M1"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    m2 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    index = verify_main_tags._base_tree_index(tmp_path, "HEAD")

    # The index is keyed by release fingerprint (tree content minus
    # CHANGELOG.md), not raw tree SHA. M1 and M2 share a tree → share a
    # fingerprint; the newest commit (M2) wins the collision.
    m1_fingerprint = git_utils.release_tree_fingerprint(tmp_path, m1)
    assert index.get(m1_fingerprint) == m2


# ---------------------------------------------------------------------------
# Group E — _report_unreproduced (fabricated states + caplog)
# ---------------------------------------------------------------------------


def test_report_unreproduced_warns_pending_but_ignores_ancient(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unreproduced tag ABOVE the aligned line WARNs; BELOW it is ancient → INFO only.

    SCENARIO: v2.0.0 is aligned (on base); v1.0.0 is unreproduced and below
    it (ancient gap — never promoted, can't backfill); v2.1.0 is
    unreproduced and above it (genuinely pending promotion).
    EXPECTED: v2.1.0 warns, v1.0.0 does NOT warn (logs INFO "ancient"),
    v2.0.0 (aligned) produces nothing.
    """
    states = [
        verify_main_tags._TagState(tag="v1.0.0", target=None, current="abc123"),
        verify_main_tags._TagState(tag="v2.0.0", target="def456", current="def456"),
        verify_main_tags._TagState(tag="v2.1.0", target=None, current="ghi789"),
    ]
    with caplog.at_level(logging.INFO, logger="forge.verify_main_tags"):
        verify_main_tags._report_unreproduced(states, "origin/main")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("v2.1.0" in m for m in warnings)
    assert not any("v1.0.0" in m for m in warnings)
    assert any("v1.0.0" in m and "ancient" in m for m in infos)
    assert not any("v2.0.0" in m for m in warnings)


# ---------------------------------------------------------------------------
# Group F — main() end-to-end (real git + bare origin; monkeypatched config)
# ---------------------------------------------------------------------------


def test_main_skips_single_branch_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: base_branch == dev_branch; CLI must skip immediately.

    MOCK SETUP: ``load_config`` returns ``ForgeConfig(base="main",
        dev="main")``.  No git repo is required; the early-exit fires before
        any git invocation.
    EXPECTED BEHAVIOR: returns 0.
    """
    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags"])
    monkeypatch.chdir(tmp_path)
    assert verify_main_tags.main() == 0


def test_main_skips_when_no_minor_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: dual-track repo with no vX.Y.0 tags; CLI returns 0 immediately.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base_branch="main",
        dev_branch="dev")``.  Real dual-track git repo via
        ``_init_dual_track_repo`` (no minor tags present).
    EXPECTED BEHAVIOR: returns 0; nothing to check.
    """
    work, _bare = _init_dual_track_repo(tmp_path)
    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags"])
    monkeypatch.chdir(work)
    assert verify_main_tags.main() == 0


def test_main_verify_exits_zero_when_all_minor_tags_on_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: v1.0.0 already sits on the main squash commit; verify exits 0.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base_branch="main",
        dev_branch="dev")``.  Real dual-track repo via
        ``_dual_track_with_unpromoted_tag`` with the tag moved to the main
        squash commit via ``_push_tag``.
    EXPECTED BEHAVIOR: verify detects no drift; returns 0.
    """
    work, bare, _dev_sha, main_sha = _dual_track_with_unpromoted_tag(tmp_path)
    # Move the tag onto the main commit so it IS correctly placed.
    _push_tag(work, "v1.0.0", main_sha, bare)
    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags"])
    monkeypatch.chdir(work)
    assert verify_main_tags.main() == 0


def test_verify_exits_nonzero_when_minor_tag_off_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: v1.0.0 tagged on dev; main has identical-tree squash but tag unmoved.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base="main", dev="dev")``.
    EXPECTED BEHAVIOR: verify detects drift and returns 1.
    """
    work, _bare, _dev_sha, _main_sha = _dual_track_with_unpromoted_tag(tmp_path)
    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags"])
    monkeypatch.chdir(work)
    assert verify_main_tags.main() == 1


def test_fix_relocates_minor_tag_to_base_commit_and_pushes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: ``--fix`` moves v1.0.0 from the dev commit to the main squash.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base_branch="main",
        dev_branch="dev")``.  Git identity env vars set so ``git tag -a``
        succeeds; real dual-track repo via
        ``_dual_track_with_unpromoted_tag``.
    EXPECTED BEHAVIOR: returns 0; bare origin's v1.0.0 resolves to the
        main squash commit SHA after the fix.
    """
    work, bare, _dev_sha, main_sha = _dual_track_with_unpromoted_tag(tmp_path)
    # run_git (used by _force_move_tag) inherits process env — supply identity.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags", "--fix"])
    monkeypatch.chdir(work)
    assert verify_main_tags.main() == 0
    # Confirm the bare repo's v1.0.0 now resolves to the main squash SHA.
    result = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == main_sha


def test_fix_is_idempotent_second_run_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: running ``--fix`` twice; the second run is a no-op.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base_branch="main",
        dev_branch="dev")``.  Git identity env vars set; real dual-track
        repo via ``_dual_track_with_unpromoted_tag``.
    EXPECTED BEHAVIOR: both runs return 0; the tag remains on the main
        squash commit SHA after the second run (idempotent).
    """
    work, bare, _dev_sha, main_sha = _dual_track_with_unpromoted_tag(tmp_path)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.chdir(work)

    monkeypatch.setattr("sys.argv", ["forge-check-main-tags", "--fix"])
    assert verify_main_tags.main() == 0

    # Second run — tag already sits on main_sha.
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags", "--fix"])
    assert verify_main_tags.main() == 0

    result = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == main_sha


def test_dry_run_does_not_mutate_tag_in_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: ``--dry-run`` with a drifted tag; no mutations pushed to origin.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base_branch="main",
        dev_branch="dev")``.  Real dual-track repo via
        ``_dual_track_with_unpromoted_tag``; tag deliberately left on the
        dev commit.
    EXPECTED BEHAVIOR: returns 0; origin's v1.0.0 SHA is unchanged after
        the run (still the dev commit SHA).
    """
    work, bare, dev_sha, _main_sha = _dual_track_with_unpromoted_tag(tmp_path)

    before = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags", "--dry-run"])
    monkeypatch.chdir(work)
    assert verify_main_tags.main() == 0

    after = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert after == before == dev_sha


def test_minor_with_no_base_tree_match_is_warned_not_counted_as_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: v1.0.0 on dev with no matching squash on main; WARNING logged.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base_branch="main",
        dev_branch="dev")``.  Real dual-track repo via
        ``_init_dual_track_repo``; file committed on dev and tagged
        ``v1.0.0``; main branch has no matching squash commit.
    EXPECTED BEHAVIOR: returns 0 (not drift — the minor was never
        promoted); WARNING contains "v1.0.0" and "promote".
    """
    work, _bare = _init_dual_track_repo(tmp_path)
    dev_sha = _write_file_commit(
        work, "v1.py", "x = 1\n", "release v1.0.0", branch="dev"
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "dev"], cwd=work, env=_GIT_ENV, check=True
    )
    bare = tmp_path / "origin.git"
    _push_tag(work, "v1.0.0", dev_sha, bare)

    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags"])
    monkeypatch.chdir(work)
    with caplog.at_level(logging.WARNING, logger="forge.verify_main_tags"):
        result = verify_main_tags.main()
    assert result == 0
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("v1.0.0" in m for m in messages)
    assert any("promote" in m for m in messages)


def test_fix_non_interactive_no_auth_returns_one_without_pushing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: ``--fix`` in non-interactive env with no git auth must abort.

    MOCK SETUP: ``is_non_interactive`` → True; ``git_auth_mode`` → "none";
        ``load_config`` → dual-track ForgeConfig.
    EXPECTED BEHAVIOR: returns 1; origin tag SHA is unchanged.
    """
    work, bare, dev_sha, _main_sha = _dual_track_with_unpromoted_tag(tmp_path)

    before = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    monkeypatch.setattr(verify_main_tags, "is_non_interactive", lambda: True)
    monkeypatch.setattr(verify_main_tags, "git_auth_mode", lambda: "none")
    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags", "--fix"])
    monkeypatch.chdir(work)
    assert verify_main_tags.main() == 1

    after = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert after == before == dev_sha


def test_main_fix_dry_run_combination_does_not_mutate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: ``--fix --dry-run`` together; dry-run wins, no mutations pushed.

    MOCK SETUP: ``load_config`` → ``ForgeConfig(base_branch="main",
        dev_branch="dev")``.  Real dual-track repo via
        ``_dual_track_with_unpromoted_tag`` with v1.0.0 on the dev commit.
    EXPECTED BEHAVIOR: returns 0; bare origin's v1.0.0 SHA is unchanged
        (dry-run short-circuits before any push).
    """
    work, bare, dev_sha, _main_sha = _dual_track_with_unpromoted_tag(tmp_path)

    before = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    monkeypatch.setattr(
        verify_main_tags,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="dev"),
    )
    monkeypatch.setattr("sys.argv", ["forge-check-main-tags", "--fix", "--dry-run"])
    monkeypatch.chdir(work)
    assert verify_main_tags.main() == 0

    after = subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert after == before == dev_sha
