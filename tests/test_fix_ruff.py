"""Tests for ``forge.fix_ruff``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge import config, fix_ruff
from forge.git_utils import code_health_dir
from tests.conftest import commit_all, git_stdout, init_git_repo


# MOCKING STRATEGY: ``_stub_subprocess`` replaces every subprocess call with a
# canned stub (no git repo needed). The restage tests instead run real git in
# a tmp repo and fake ONLY ruff: ``fix_ruff.subprocess.run`` is patched with a
# closure that rewrites files for ruff argvs and delegates the rest to the
# real ``subprocess.run`` captured at import time. The last two tests
# (``unused_suppression`` / ``ci_mode``) fake nothing: they run real ruff in a
# tmp git repo. An autouse fixture pins ``fix_ruff.is_ci`` to False for every
# test (so ``CI=true`` on a runner changes nothing); the CI-mode test
# re-patches it to True.
_REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin local (repairing) mode so a CI runner's ``CI=true`` cannot flip it."""
    monkeypatch.setattr(fix_ruff, "is_ci", lambda: False)


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
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    fixes: dict[str, str],
    *,
    root: str = "src",
) -> int:
    """Run ``fix_ruff.main`` in *repo* where fake ruff rewrites *fixes* files.

    Git calls hit the real binary; only ruff is faked.

    Args:
        repo: Git repository root where ``fix_ruff.main`` runs.
        fixes: Mapping of repo-relative path to the text the fake ruff writes.
        root: Scan root passed on the command line.
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
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff", root])
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


def test_whole_repo_root_restages_staged_file_anywhere(
    repo_with_committed_sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.`` root counts every staged file as in scope.

    SCENARIO: docs/out.txt staged; ruff, run with root ``.``, rewrites it.
    MOCK SETUP: ruff faked (rewrites files, rc=0); git is real.
    EXPECTED BEHAVIOR: exit 0; the index holds the rewritten docs/out.txt.
    """
    repo = repo_with_committed_sources
    (repo / "docs" / "out.txt").write_text("staged\n", encoding="utf-8")
    git_stdout(repo, "add", "docs/out.txt")
    rc = _run_main_with_fake_ruff(
        monkeypatch, repo, {"docs/out.txt": "fixed\n"}, root="."
    )
    assert rc == 0
    assert git_stdout(repo, "show", ":docs/out.txt") == "fixed\n"
    assert not git_stdout(repo, "diff", "--name-only").strip()


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


def _run_main_with_real_ruff(
    monkeypatch: pytest.MonkeyPatch, repo: Path, *, root: str = "src"
) -> int:
    """Run ``fix_ruff.main`` in *repo* with the real ruff binary.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        repo: Git repository root where ``fix_ruff.main`` runs.
        root: Scan root passed on the command line.

    Returns:
        Exit code of ``fix_ruff.main``.
    """
    monkeypatch.chdir(repo)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff", root])
    return fix_ruff.main()


def test_unused_suppression_fails_and_file_is_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suppression that suppresses nothing fails the run; the file is kept as is.

    Behavior test: pins that forge's own ``ruff.toml`` (copied in here, since
    the contract is that *forge's* config reports this) lists ``RUF100`` as
    unfixable. Detects dropping it, or a return to silent repair.

    SCENARIO: src/mod.py carries ``# noqa: E501`` on a short line.
    MOCK SETUP: none; real ruff and real git in a tmp repo, local mode.
    EXPECTED BEHAVIOR: non-zero exit; file bytes unchanged; ruff.log names RUF100.
    """
    init_git_repo(tmp_path)
    (tmp_path / "ruff.toml").write_bytes(
        (Path(__file__).parent.parent / "ruff.toml").read_bytes()
    )
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "mod.py"
    source.write_bytes(b'"""Doc."""\n\nX = 1  # noqa: E501\n')
    before = source.read_bytes()
    commit_all(tmp_path, "add source")
    assert _run_main_with_real_ruff(monkeypatch, tmp_path) != 0
    assert source.read_bytes() == before
    assert "RUF100" in (code_health_dir(tmp_path) / "ruff.log").read_text()


def test_ci_mode_formatting_only_difference_fails_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In CI a formatting-only difference fails the run and is not repaired.

    Behavior test: with no lint finding, only ``ruff format --check``'s exit
    code can fail the run. Detects that term being removed or inverted.

    SCENARIO: src/mod.py has a formatting defect and no lint finding.
    MOCK SETUP: none; real ruff and real git (default ruff config, so the
        file is lint-clean); ``fix_ruff.is_ci`` patched to True.
    EXPECTED BEHAVIOR: exit 1; file bytes unchanged; ruff.log says reporting only.
    """
    monkeypatch.setattr(fix_ruff, "is_ci", lambda: True)
    init_git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "mod.py"
    source.write_bytes(b"X = [1,2]\n")
    before = source.read_bytes()
    commit_all(tmp_path, "add source")
    assert _run_main_with_real_ruff(monkeypatch, tmp_path) == 1
    assert source.read_bytes() == before
    assert "CI: reporting only" in (code_health_dir(tmp_path) / "ruff.log").read_text()
