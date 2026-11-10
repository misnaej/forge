"""Tests for ``forge.fix_ruff``."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import config, fix_ruff
from forge.git_utils import code_health_dir
from tests.conftest import commit_all, git_stdout, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


# MOCKING STRATEGY: ``_stub_subprocess`` replaces every subprocess call with a
# canned stub (no git repo needed). The restage tests instead run real git in
# a tmp repo and fake ONLY ruff: ``fix_ruff.subprocess.run`` is patched with a
# closure that rewrites files for ruff argvs and delegates the rest to the
# real ``subprocess.run`` captured at import time.
_REAL_RUN = subprocess.run


def _stub_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ruff_check_returncode: int = 0,
    calls: list[list[str]] | None = None,
) -> list[list[str]]:
    """Stub subprocess.run for fix_ruff: ruff format/check + git diff/add.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        ruff_check_returncode: Exit code from ``ruff check --fix``.
        calls: Optional list to collect every argv.

    Returns:
        The list of captured argvs (the same list passed in via ``calls``,
        or a fresh one if none was given).
    """
    captured = calls if calls is not None else []

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        captured.append(cmd)
        if cmd[:2] == ["git", "diff"]:
            # `-z` output: NUL-separated bytes; empty = no changed files.
            return type("P", (), {"returncode": 0, "stdout": b""})()
        if cmd[:2] == ["ruff", "check"]:
            return type(
                "P",
                (),
                {"returncode": ruff_check_returncode, "stdout": "", "stderr": ""},
            )()
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(fix_ruff.subprocess, "run", _fake_run)
    return captured


def test_main_runs_format_and_check_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fix-forge-ruff runs ``ruff format`` then ``ruff check --fix --unsafe-fixes``."""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    calls = _stub_subprocess(monkeypatch)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    assert fix_ruff.main() == 0
    format_calls = [c for c in calls if c[:2] == ["ruff", "format"]]
    check_calls = [c for c in calls if c[:2] == ["ruff", "check"]]
    assert format_calls
    assert "--check" not in format_calls[0]
    assert check_calls
    assert "--fix" in check_calls[0]
    assert "--unsafe-fixes" in check_calls[0]


def test_scope_diff_runs_ruff_on_modified_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--scope diff` targets the modified-file set, not the source dirs."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "changed.py").write_text("x = 1\n", encoding="utf-8")
    calls = _stub_subprocess(monkeypatch)
    monkeypatch.setattr(config, "get_modified_files", lambda **_kw: ["src/changed.py"])
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff", "--scope", "diff"])
    assert fix_ruff.main() == 0
    format_calls = [c for c in calls if c[:2] == ["ruff", "format"]]
    assert "src/changed.py" in format_calls[0]


def test_scope_diff_skips_when_no_modified_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--scope diff` with an empty diff is a clean skip (no ruff run)."""
    monkeypatch.chdir(tmp_path)
    calls = _stub_subprocess(monkeypatch)
    monkeypatch.setattr(config, "get_modified_files", lambda **_kw: [])
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff", "--scope", "diff"])
    assert fix_ruff.main() == 0
    assert not [c for c in calls if c[:1] == ["ruff"]]
    assert (
        "(no modified files — skipped)"
        in (code_health_dir(tmp_path) / "ruff.log").read_text()
    )


def test_main_writes_ruff_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fix-forge-ruff writes ``code_health/ruff.log`` with the section header."""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    _stub_subprocess(monkeypatch)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    fix_ruff.main()
    log_path = code_health_dir(tmp_path) / "ruff.log"
    assert log_path.is_file()
    content = log_path.read_text()
    assert "$ ruff format" in content
    assert "$ ruff check --fix --unsafe-fixes" in content


def test_main_propagates_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``ruff check --fix`` exits non-zero (residue), main returns same code."""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    _stub_subprocess(monkeypatch, ruff_check_returncode=1)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    assert fix_ruff.main() == 1


def test_main_skipped_without_source_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No source dirs → exit 0 and log says (skipped)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    assert fix_ruff.main() == 0
    log = (code_health_dir(tmp_path) / "ruff.log").read_text()
    assert "skipped" in log


def test_main_rejects_path_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``args.dirs`` resolving outside repo root → SystemExit(2)."""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff", "../escape"])
    with pytest.raises(SystemExit) as exc_info:
        fix_ruff.main()
    assert exc_info.value.code == 2


@pytest.fixture
def repo_with_committed_sources(tmp_path: Path) -> Path:
    """Real git repo with src/a.py, src/b.py and docs/out.txt committed."""
    init_git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    (tmp_path / "docs" / "out.txt").write_text("out\n", encoding="utf-8")
    commit_all(tmp_path, "add sources")
    return tmp_path


def _run_main_with_fake_ruff(
    monkeypatch: pytest.MonkeyPatch, repo: Path, fixes: dict[str, str]
) -> int:
    """Run ``fix_ruff.main`` in *repo* where fake ruff rewrites *fixes* files.

    Git calls hit the real binary; only ruff is faked.

    Args:
        repo: Git repository root where ``fix_ruff.main`` runs.
        fixes: Mapping of repo-relative path to the text the fake ruff writes.
    """

    def _run(cmd: list[str], **kwargs: object) -> object:
        if cmd[0] == "ruff":
            for rel, text in fixes.items():
                (repo / rel).write_text(text, encoding="utf-8")
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return _REAL_RUN(cmd, **kwargs)

    monkeypatch.setattr(fix_ruff.subprocess, "run", _run)
    monkeypatch.setattr(fix_ruff, "require_cli", lambda *_a, **_k: None)
    monkeypatch.chdir(repo)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff", "src"])
    return fix_ruff.main()


def _cached(repo: Path) -> set[str]:
    """Return the set of paths staged in the index of *repo*.

    Args:
        repo: Git repository root whose index is read.

    Returns:
        Staged paths relative to the repo root.
    """
    return set(git_stdout(repo, "diff", "--cached", "--name-only").split())


def test_left_out_modified_file_stays_unstaged(
    repo_with_committed_sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruff-modified file the author did not stage is not swept into the index.

    SCENARIO: src/a.py staged; ruff rewrites both src/a.py and src/b.py.
    MOCK SETUP: ruff faked (rewrites files, rc=0); git is real.
    EXPECTED BEHAVIOR: exit 0; only src/a.py staged; src/b.py unstaged.
    """
    repo = repo_with_committed_sources
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    git_stdout(repo, "add", "src/a.py")
    rc = _run_main_with_fake_ruff(
        monkeypatch, repo, {"src/a.py": "a = 3\n", "src/b.py": "b = 3\n"}
    )
    assert rc == 0
    assert _cached(repo) == {"src/a.py"}
    assert git_stdout(repo, "diff", "--name-only").split() == ["src/b.py"]


def test_staged_file_gets_fix_staged_and_outside_roots_untouched(
    repo_with_committed_sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged file's fix is indexed; a staged file outside roots is not rewritten.

    SCENARIO: src/a.py and docs/out.txt staged; ruff rewrites both, roots=src.
    MOCK SETUP: ruff faked (rewrites files, rc=0); git is real.
    EXPECTED BEHAVIOR: index holds the fixed src/a.py; docs/out.txt index blob
        unchanged, its rewrite left unstaged.
    """
    repo = repo_with_committed_sources
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    (repo / "docs" / "out.txt").write_text("out2\n", encoding="utf-8")
    git_stdout(repo, "add", "src/a.py", "docs/out.txt")
    docs_blob = git_stdout(repo, "rev-parse", ":docs/out.txt")
    rc = _run_main_with_fake_ruff(
        monkeypatch, repo, {"src/a.py": "a = 3\n", "docs/out.txt": "out3\n"}
    )
    assert rc == 0
    assert git_stdout(repo, "show", ":src/a.py") == "a = 3\n"
    assert git_stdout(repo, "rev-parse", ":docs/out.txt") == docs_blob


def test_ruff_rewriting_partly_staged_file_refuses(
    repo_with_committed_sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruff rewriting a staged-then-edited file fails the run and stages nothing.

    SCENARIO: src/a.py staged, then edited again; ruff rewrites it.
    MOCK SETUP: ruff faked (rewrites files, rc=0); git is real.
    EXPECTED BEHAVIOR: exit 1; ruff.log names src/a.py; index blob unchanged.
    """
    repo = repo_with_committed_sources
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    git_stdout(repo, "add", "src/a.py")
    (repo / "src" / "a.py").write_text("a = 22\n", encoding="utf-8")
    blob = git_stdout(repo, "rev-parse", ":src/a.py")
    rc = _run_main_with_fake_ruff(monkeypatch, repo, {"src/a.py": "a = 3\n"})
    assert rc == 1
    assert "src/a.py" in (code_health_dir(repo) / "ruff.log").read_text()
    assert git_stdout(repo, "rev-parse", ":src/a.py") == blob


def test_untouched_partly_staged_file_keeps_unstaged_edits_unstaged(
    repo_with_committed_sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partly-staged file ruff leaves alone passes without staging its extra edits.

    SCENARIO: src/a.py staged, then edited again; ruff rewrites nothing.
    MOCK SETUP: ruff faked (rewrites no file, rc=0); git is real.
    EXPECTED BEHAVIOR: exit 0; index blob unchanged; edit still unstaged.
    """
    repo = repo_with_committed_sources
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    git_stdout(repo, "add", "src/a.py")
    (repo / "src" / "a.py").write_text("a = 22\n", encoding="utf-8")
    blob = git_stdout(repo, "rev-parse", ":src/a.py")
    rc = _run_main_with_fake_ruff(monkeypatch, repo, {})
    assert rc == 0
    assert git_stdout(repo, "rev-parse", ":src/a.py") == blob
    assert git_stdout(repo, "diff", "--name-only").split() == ["src/a.py"]


def test_unreadable_git_status_stages_nothing_and_logs_note(
    repo_with_committed_sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable git status: nothing re-staged, exit 0, log says so.

    SCENARIO: src/a.py modified unstaged; status snapshot unavailable.
    MOCK SETUP: ruff faked (rewrites src/a.py); ``staged_unstaged_paths``
        patched to return None; git is real.
    EXPECTED BEHAVIOR: exit 0; index empty; ruff.log has the note.
    """
    repo = repo_with_committed_sources
    monkeypatch.setattr(fix_ruff, "staged_unstaged_paths", lambda _root: None)
    rc = _run_main_with_fake_ruff(monkeypatch, repo, {"src/a.py": "a = 3\n"})
    assert rc == 0
    assert _cached(repo) == set()
    log = (code_health_dir(repo) / "ruff.log").read_text()
    assert "git status unreadable; nothing re-staged." in log
