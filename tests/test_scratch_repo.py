"""Tests for ``forge.scratch_repo`` (real git in ``tmp_path``, no mocks)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forge import scratch_repo
from tests.conftest import GIT_ENV, init_git_repo


def _git(repo: Path, *args: str) -> str:
    """Run real git in *repo* and return trimmed stdout.

    Args:
        repo: Working directory.
        *args: Git argv tail.

    Returns:
        Trimmed stdout.
    """
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with tracked `a.txt` / `sub/b.txt`, one commit, and cwd set to it.

    Returns:
        The repo root.
    """
    repo = tmp_path / "source"
    repo.mkdir()
    init_git_repo(repo)
    (repo / "a.txt").write_text("alpha\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.txt").write_text("beta\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "content")
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def outside_parent(tmp_path: Path) -> Path:
    """A directory outside every git work tree to hold scratch copies.

    Returns:
        The directory.
    """
    parent = tmp_path / "scratch-parent"
    parent.mkdir()
    return parent


def _state(repo: Path) -> tuple[str, str]:
    """Snapshot a repo's observable state.

    Args:
        repo: Repo root.

    Returns:
        Porcelain status (untracked included) and HEAD sha.
    """
    return _git(repo, "status", "--porcelain", "--untracked-files=all"), _git(
        repo, "rev-parse", "HEAD"
    )


def test_snapshot_ref_matches_committed_tree(
    source_repo: Path, outside_parent: Path
) -> None:
    """A ref snapshot has the ref's tree in one commit and leaves the source alone."""
    (source_repo / "a.txt").write_text("uncommitted\n")
    before = _state(source_repo)

    dest = scratch_repo.snapshot(ref="HEAD", worktree=False, parent=outside_parent)

    assert _git(dest, "rev-list", "--count", "HEAD") == "1"
    assert _git(dest, "rev-parse", "HEAD^{tree}") == _git(
        source_repo, "rev-parse", "HEAD^{tree}"
    )
    assert (dest / "a.txt").read_text() == "alpha\n"
    assert _state(source_repo) == before


def test_snapshot_worktree_copies_tracked_edits_only(
    source_repo: Path, outside_parent: Path
) -> None:
    """--worktree captures a modified tracked file, not untracked files."""
    (source_repo / "a.txt").write_text("uncommitted\n")
    (source_repo / "untracked.txt").write_text("nope\n")
    before = _state(source_repo)

    dest = scratch_repo.snapshot(ref=None, worktree=True, parent=outside_parent)

    assert (dest / "a.txt").read_text() == "uncommitted\n"
    assert (dest / "sub" / "b.txt").read_text() == "beta\n"
    assert not (dest / "untracked.txt").exists()
    assert _state(source_repo) == before


def test_snapshot_outside_a_checkout_is_refused(
    tmp_path: Path, outside_parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot needs a source repo to copy."""
    bare_dir = tmp_path / "not-a-repo"
    bare_dir.mkdir()
    monkeypatch.chdir(bare_dir)
    with pytest.raises(scratch_repo.ScratchError, match="inside a git checkout"):
        scratch_repo.snapshot(ref=None, worktree=False, parent=outside_parent)


def test_check_parent_accepts_ignored_dir_in_work_tree(source_repo: Path) -> None:
    """A gitignored directory inside the repo is an allowed parent."""
    (source_repo / ".gitignore").write_text("scratch/\n")
    ignored = source_repo / "scratch"
    ignored.mkdir()
    assert scratch_repo._check_parent(ignored) == ignored.resolve()


def test_check_parent_accepts_dir_outside_any_work_tree(outside_parent: Path) -> None:
    """A directory in no work tree needs no ignore rule."""
    assert scratch_repo._check_parent(outside_parent) == outside_parent.resolve()


def test_check_parent_refuses_missing_directory(tmp_path: Path) -> None:
    """A nonexistent parent is a refusal, not a crash."""
    with pytest.raises(scratch_repo.ScratchError, match="does not exist"):
        scratch_repo._check_parent(tmp_path / "missing")


def test_snapshot_worktree_skips_files_behind_a_symlinked_directory(
    source_repo: Path, outside_parent: Path, tmp_path: Path
) -> None:
    """A tracked dir swapped for a symlink must not leak the link target's files."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "b.txt").write_text("secret\n")
    (source_repo / "sub" / "b.txt").unlink()
    (source_repo / "sub").rmdir()
    (source_repo / "sub").symlink_to(elsewhere, target_is_directory=True)

    dest = scratch_repo.snapshot(ref=None, worktree=True, parent=outside_parent)

    assert not (dest / "sub" / "b.txt").exists()
    assert (dest / "a.txt").read_text() == "alpha\n"


def test_main_refuses_ref_that_looks_like_an_option(
    source_repo: Path, outside_parent: Path, tmp_path: Path
) -> None:
    """`--ref=--output=<path>` is a ref name, never an option handed to git."""
    target = tmp_path / "injected.tar"
    code = scratch_repo.main(
        ["snapshot", f"--ref=--output={target}", "--parent", str(outside_parent)]
    )
    assert code == 1
    assert not target.exists()
    assert list(outside_parent.iterdir()) == []


def test_main_refuses_parent_inside_dot_git(
    source_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scratch copy is never created inside the repository's own .git."""
    git_dir = source_repo / ".git"
    before = sorted(p.name for p in git_dir.iterdir())
    code = scratch_repo.main(["snapshot", "--parent", str(git_dir)])
    assert code == 1
    assert capsys.readouterr().out == ""
    assert sorted(p.name for p in git_dir.iterdir()) == before


@pytest.mark.parametrize(
    "commit",
    [{"branch": "-x"}, {"branch": "feat", "from": "--help"}],
)
def test_build_refuses_option_shaped_branch_names(
    commit: dict[str, str], outside_parent: Path
) -> None:
    """A branch or start point beginning with `-` is refused, not passed to git.

    Args:
        commit: Extra spec keys on the second commit.
    """
    spec = json.dumps(
        [{"message": "root", "branch": "main"}, {"message": "next", **commit}]
    )
    with pytest.raises(scratch_repo.ScratchError):
        scratch_repo.build(spec, parent=outside_parent)
    assert list(outside_parent.iterdir()) == []


@pytest.mark.parametrize(
    "rel", ["../x", "/abs/x", "a/../../x", ".git/hooks/x", "a/.git/config"]
)
def test_build_refuses_spec_path_escape(
    rel: str, tmp_path: Path, outside_parent: Path
) -> None:
    """A spec path cannot leave the repo or reach into .git; nothing is left behind.

    Args:
        rel: A hostile spec path.
    """
    spec = json.dumps([{"message": "m", "write": {rel: "x"}}])
    before = sorted(p.name for p in tmp_path.iterdir())

    with pytest.raises(scratch_repo.ScratchError):
        scratch_repo.build(spec, parent=outside_parent)

    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert list(outside_parent.iterdir()) == []


def test_build_makes_branches_and_commits(outside_parent: Path) -> None:
    """A two-branch, three-commit spec yields that history and tip contents."""
    spec = json.dumps(
        [
            {"message": "root", "branch": "main", "write": {"f.txt": "root\n"}},
            {
                "message": "feat work",
                "branch": "feat",
                "write": {"f.txt": "feat\n", "d/g.txt": "g\n"},
            },
            {
                "message": "main work",
                "branch": "main",
                "write": {"m.txt": "m\n"},
                "delete": ["f.txt"],
            },
        ]
    )

    dest = scratch_repo.build(spec, parent=outside_parent)

    assert sorted(
        _git(dest, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    ) == [
        "feat",
        "main",
    ]
    assert _git(dest, "log", "--format=%s", "main").splitlines() == [
        "main work",
        "root",
    ]
    assert _git(dest, "log", "--format=%s", "feat").splitlines() == [
        "feat work",
        "root",
    ]
    assert _git(dest, "show", "feat:f.txt") == "feat"
    assert _git(dest, "show", "feat:d/g.txt") == "g"
    assert _git(dest, "show", "main:m.txt") == "m"
    assert _git(dest, "ls-tree", "--name-only", "main").split() == ["m.txt"]
    assert _git(dest, "branch", "--show-current") == "main"


@pytest.mark.parametrize(
    "spec",
    ["not json", "[]", '[{"no": "message"}]'],
)
def test_build_refuses_malformed_spec(spec: str, outside_parent: Path) -> None:
    """A spec that is not a non-empty list of messaged commits is refused.

    Args:
        spec: Malformed spec text.
    """
    with pytest.raises(scratch_repo.ScratchError):
        scratch_repo.build(spec, parent=outside_parent)


def test_failure_mid_build_removes_the_destination(outside_parent: Path) -> None:
    """A build failing after its first commit still removes the destination.

    The directory already holds a commit when the second commit's spec path
    is refused, so cleanup must cover a populated repo, not just an empty one.
    """
    spec = json.dumps(
        [
            {"message": "ok", "write": {"a": "1"}},
            {"message": "bad", "write": {"../escape": "x"}},
        ]
    )
    with pytest.raises(scratch_repo.ScratchError):
        scratch_repo.build(spec, parent=outside_parent)
    assert list(outside_parent.iterdir()) == []


def test_main_ignores_inherited_git_env_overrides(
    source_repo: Path,
    outside_parent: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GIT_DIR and friends pointing at another repo cannot redirect the writes.

    Also pins the CLI contract: exit 0 and stdout is exactly the new path.
    """
    outer = tmp_path / "outer"
    outer.mkdir()
    init_git_repo(outer)
    outer_before = _state(outer)
    outer_refs = _git(outer, "for-each-ref")
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outer))
    monkeypatch.setenv("GIT_INDEX_FILE", str(outer / ".git" / "index"))

    code = scratch_repo.main(
        ["snapshot", "--ref", "HEAD", "--parent", str(outside_parent)]
    )

    assert code == 0
    dest = Path(capsys.readouterr().out.strip())
    assert dest.parent == outside_parent.resolve()
    assert (dest / "a.txt").read_text() == "alpha\n"
    assert _state(outer) == outer_before
    assert _git(outer, "for-each-ref") == outer_refs
    assert not (outer / "a.txt").exists()


def test_main_build_reads_spec_file(
    tmp_path: Path, outside_parent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`build --spec <file>` builds from the file's JSON."""
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps([{"message": "m", "write": {"x": "1"}}]))
    code = scratch_repo.main(
        ["build", "--spec", str(spec_file), "--parent", str(outside_parent)]
    )
    dest = Path(capsys.readouterr().out.strip())
    assert code == 0
    assert (dest / "x").read_text() == "1"


def test_main_refusal_returns_one_without_stdout(
    source_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused request exits 1, prints no path, and leaves no copy behind."""
    code = scratch_repo.main(["snapshot", "--parent", str(source_repo / "sub")])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert list((source_repo / "sub").iterdir()) == [source_repo / "sub" / "b.txt"]
