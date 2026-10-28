"""Tests for ``forge.gen_config_doc`` (the config-table generator)."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from forge import gen_config_doc
from forge.forge_config import CONFIG_KEYS
from forge.gen_common import CONFIG_DOC, CONFIG_DOC_END, CONFIG_DOC_START


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo_with_config_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp repo whose config doc holds stale table text between the markers."""
    doc = tmp_path / CONFIG_DOC
    doc.parent.mkdir(parents=True)
    doc.write_text(
        f"intro\n{CONFIG_DOC_START}\nstale\n{CONFIG_DOC_END}\noutro\n",
    )
    monkeypatch.setattr(gen_config_doc, "repo_root", lambda: tmp_path)
    return tmp_path


def _run(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    """Run the CLI's ``main`` with the given argv and return its exit code.

    Args:
        monkeypatch: Pytest fixture used to set ``sys.argv``.
        *args: Command-line arguments passed after the program name.

    Returns:
        The exit code returned by ``main``.
    """
    monkeypatch.setattr(sys, "argv", ["forge-gen-config-doc", *args])
    return gen_config_doc.main()


def test_check_exits_1_on_stale_table(
    repo_with_config_doc: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--check fails when the table between the markers has drifted."""
    assert _run(monkeypatch, "--check") == 1
    assert "stale" in (repo_with_config_doc / CONFIG_DOC).read_text()


@pytest.mark.parametrize("args", [("--check",), ()], ids=["check", "write"])
def test_exits_1_when_markers_are_missing(
    repo_with_config_doc: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    """Both --check and a write fail on a doc without markers, leaving it alone.

    Args:
        args: CLI arguments for the run (``--check`` or none for a write).
    """
    doc = repo_with_config_doc / CONFIG_DOC
    doc.write_text("no markers here\n")
    assert _run(monkeypatch, *args) == 1
    assert doc.read_text() == "no markers here\n"


def test_write_then_check_roundtrips(
    repo_with_config_doc: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write fixes the table; --check then passes and text outside is kept."""
    assert _run(monkeypatch) == 0
    text = (repo_with_config_doc / CONFIG_DOC).read_text()
    assert text.startswith(f"intro\n{CONFIG_DOC_START}\n")
    assert text.endswith(f"{CONFIG_DOC_END}\noutro\n")
    assert "stale" not in text
    for key in CONFIG_KEYS:
        assert f".{key.path[-1]}`" in text
    assert _run(monkeypatch, "--check") == 0
