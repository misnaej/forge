"""Tests for forge.memory_audit — new-memory counting and the audit stamp."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge import memory_audit


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """Memory directory holding the index plus two notes and a stray file.

    Returns:
        Path to the memory directory.
    """
    path = tmp_path / "memory"
    path.mkdir()
    (path / "MEMORY.md").write_text("- index\n", encoding="utf-8")
    (path / "alpha.md").write_text("alpha\n", encoding="utf-8")
    (path / "beta.md").write_text("beta\n", encoding="utf-8")
    (path / "notes.txt").write_text("not a note\n", encoding="utf-8")
    return path


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Repo directory with no forge config."""
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _write_pyproject(root: Path, body: str) -> None:
    """Write a pyproject.toml file with the given body.

    Args:
        root: Directory to write the file in.
        body: File contents.
    """
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


def test_note_names_excludes_index_and_non_markdown(memory_dir: Path) -> None:
    """note_names excludes MEMORY.md index and non-markdown files."""
    assert memory_audit.note_names(memory_dir) == {"alpha.md", "beta.md"}


def test_new_notes_without_stamp_is_every_note(memory_dir: Path) -> None:
    """new_notes returns all notes when there is no stamp."""
    assert memory_audit.new_notes(memory_dir) == {"alpha.md", "beta.md"}


def test_stamp_then_nothing_new(memory_dir: Path) -> None:
    """After stamping, new_notes returns empty set."""
    memory_audit.write_stamp(memory_dir)

    assert memory_audit.new_notes(memory_dir) == set()


def test_edited_note_is_not_new(memory_dir: Path) -> None:
    """Edited notes are not considered new."""
    memory_audit.write_stamp(memory_dir)
    (memory_dir / "alpha.md").write_text("edited\n", encoding="utf-8")

    assert memory_audit.new_notes(memory_dir) == set()


def test_note_added_after_stamp_is_new(memory_dir: Path) -> None:
    """Notes added after stamping are new."""
    memory_audit.write_stamp(memory_dir)
    (memory_dir / "gamma.md").write_text("gamma\n", encoding="utf-8")

    assert memory_audit.new_notes(memory_dir) == {"gamma.md"}


def test_stamp_round_trips_names(memory_dir: Path) -> None:
    """Stamp stores and retrieves the current note names."""
    memory_audit.write_stamp(memory_dir)

    stamp = memory_audit.read_stamp(memory_dir)

    assert stamp is not None
    assert stamp[1] == memory_audit.note_names(memory_dir)


def test_read_stamp_missing_returns_none(memory_dir: Path) -> None:
    """read_stamp returns None when there is no stamp file."""
    assert memory_audit.read_stamp(memory_dir) is None


def test_read_stamp_empty_file_returns_none(memory_dir: Path) -> None:
    """read_stamp returns None when the stamp file is empty."""
    (memory_dir / memory_audit.STAMP_NAME).write_text("", encoding="utf-8")

    assert memory_audit.read_stamp(memory_dir) is None


def test_status_below_threshold_does_not_offer(
    memory_dir: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status does not offer audit when count is below threshold."""
    _write_pyproject(repo_root, "[tool.forge.memory_audit]\nthreshold = 3\n")

    code = memory_audit.status(memory_dir, repo_root)

    out = capsys.readouterr().out
    assert code == 0
    assert "new memories: 2" in out
    assert "threshold: 3" in out
    assert "offer audit: no" in out
    assert "last audit: never" in out


def test_status_at_threshold_offers(
    memory_dir: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status offers audit when count equals threshold."""
    _write_pyproject(repo_root, "[tool.forge.memory_audit]\nthreshold = 2\n")

    memory_audit.status(memory_dir, repo_root)

    assert "offer audit: yes" in capsys.readouterr().out


def test_status_after_stamp_reports_date_not_never(
    memory_dir: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status reports audit date after stamping, not never."""
    memory_audit.write_stamp(memory_dir)

    memory_audit.status(memory_dir, repo_root)

    out = capsys.readouterr().out
    assert "new memories: 0" in out
    assert "last audit: never" not in out


@pytest.mark.parametrize(
    "body",
    [
        "[tool.forge.memory_audit]\nthreshold = 0\n",
        "[tool.forge.memory_audit]\nthreshold = -2\n",
        '[tool.forge.memory_audit]\nthreshold = "many"\n',
        "[tool.forge.memory_audit]\nthreshold = true\n",
    ],
)
def test_invalid_threshold_falls_back_to_default(repo_root: Path, body: str) -> None:
    """An unusable threshold is replaced by the default, never fatal.

    Args:
        repo_root: Repo directory the config is written into.
        body: ``pyproject.toml`` text carrying the invalid threshold.
    """
    _write_pyproject(repo_root, body)

    assert memory_audit.configured_threshold(repo_root) == 5


def test_threshold_defaults_to_five_without_config(repo_root: Path) -> None:
    """Default threshold is 5 without config."""
    assert memory_audit.configured_threshold(repo_root) == 5


def test_status_names_default_lessons_file(
    memory_dir: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status reports default lessons file path."""
    memory_audit.status(memory_dir, repo_root)

    assert f"lessons file: {repo_root / 'docs/lessons.md'}" in capsys.readouterr().out


def test_status_names_overridden_lessons_file(
    memory_dir: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status reports overridden lessons file path."""
    _write_pyproject(
        repo_root, '[tool.forge.memory_audit]\nlessons_file = "notes/own.md"\n'
    )

    memory_audit.status(memory_dir, repo_root)

    assert f"lessons file: {repo_root / 'notes/own.md'}" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["status", "stamp"])
def test_main_missing_memory_dir_exits_one_with_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """Both subcommands fail clearly when the memory directory is absent.

    Args:
        tmp_path: Pytest ``tmp_path`` fixture directory.
        capsys: Pytest capture fixture (injected).
        command: The subcommand under test.
    """
    missing = tmp_path / "nope"

    code = memory_audit.main([command, "--memory-dir", str(missing)])

    assert code == 1
    assert "memory directory not found" in capsys.readouterr().err
    assert not missing.exists()


def test_main_stamp_writes_stamp_and_reports(
    memory_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Main stamp subcommand writes stamp file and reports success."""
    code = memory_audit.main(["stamp", "--memory-dir", str(memory_dir)])

    assert code == 0
    assert "stamped 2 memories" in capsys.readouterr().out
    assert (memory_dir / memory_audit.STAMP_NAME).is_file()
