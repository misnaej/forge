"""Tests for the verify-forge-repo-structure CLI public API."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from forge.verify_repo_structure import (
    extract_paths_from_markdown,
    main,
    verify_structure,
)


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


IN_SYNC_MARKDOWN = """\
# Repo Structure

## Forge Package (`src/forge/`)

1. **CLI Modules**
   - precommit.py: pre-commit dispatcher

## Configuration Files

1. **Documentation**
   - README.md: main documentation
   - REPO_STRUCTURE.md: this file
"""

DRIFTED_MARKDOWN = """\
# Repo Structure

## Forge Package (`src/forge/`)

1. **CLI Modules**
   - precommit.py: pre-commit dispatcher
   - ghost.py: does not exist on disk
"""


def _build_in_sync_repo(root: Path) -> None:
    """Create a minimal repo tree matching IN_SYNC_MARKDOWN.

    Args:
        root: Directory to populate as the repo root.
    """
    (root / "src" / "forge").mkdir(parents=True)
    (root / "src" / "forge" / "precommit.py").write_text("")
    (root / "README.md").write_text("")
    (root / "REPO_STRUCTURE.md").write_text(IN_SYNC_MARKDOWN)


def test_extract_paths_from_markdown_resolves_section_relative_files() -> None:
    """Indented .py references resolve against the enclosing section path."""
    paths = extract_paths_from_markdown(IN_SYNC_MARKDOWN)
    assert "src/forge" in paths
    assert "src/forge/precommit.py" in paths
    assert "README.md" in paths


def test_verify_structure_reports_in_sync(tmp_path: Path) -> None:
    """A REPO_STRUCTURE.md matching the tree yields no drift."""
    _build_in_sync_repo(tmp_path)
    not_found, not_documented, total = verify_structure(tmp_path)
    assert not_found == set()
    assert not_documented == set()
    assert total > 0


def test_verify_structure_reports_documented_but_missing(tmp_path: Path) -> None:
    """A documented path absent from disk is reported as drift."""
    (tmp_path / "src" / "forge").mkdir(parents=True)
    (tmp_path / "src" / "forge" / "precommit.py").write_text("")
    (tmp_path / "REPO_STRUCTURE.md").write_text(DRIFTED_MARKDOWN)
    not_found, _not_documented, _total = verify_structure(tmp_path)
    assert "src/forge/ghost.py" in not_found


def test_main_returns_zero_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit code is 0 when REPO_STRUCTURE.md matches the repo tree."""
    _build_in_sync_repo(tmp_path)
    monkeypatch.setattr(
        "forge.verify_repo_structure.repo_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(sys, "argv", ["verify-forge-repo-structure"])
    assert main() == 0


def test_main_returns_one_when_drift_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exit code is 1 and drift is logged when a documented path is missing."""
    (tmp_path / "src" / "forge").mkdir(parents=True)
    (tmp_path / "src" / "forge" / "precommit.py").write_text("")
    (tmp_path / "REPO_STRUCTURE.md").write_text(DRIFTED_MARKDOWN)
    monkeypatch.setattr(
        "forge.verify_repo_structure.repo_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(sys, "argv", ["verify-forge-repo-structure"])
    with caplog.at_level(logging.INFO):
        result = main()
    assert result == 1
    assert any("DRIFT DETECTED" in record.getMessage() for record in caplog.records)


def test_main_returns_one_when_repo_structure_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exit code is 1 and an error is logged when REPO_STRUCTURE.md is absent."""
    monkeypatch.setattr(
        "forge.verify_repo_structure.repo_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(sys, "argv", ["verify-forge-repo-structure"])
    with caplog.at_level(logging.WARNING):
        result = main()
    assert result == 1
    assert any(
        "REPO_STRUCTURE.md not found" in record.getMessage()
        for record in caplog.records
    )


def test_verify_structure_flags_undocumented_must_document_item(tmp_path: Path) -> None:
    """Verify disk item absent from markdown is caught as important_not_documented."""
    _build_in_sync_repo(tmp_path)
    # `docs` is in MUST_DOCUMENT but is not referenced by IN_SYNC_MARKDOWN.
    (tmp_path / "docs").mkdir()
    _documented_not_found, important_not_documented, _total = verify_structure(tmp_path)
    assert "docs" in important_not_documented


def test_extract_paths_from_markdown_empty_returns_empty() -> None:
    """Empty markdown yields no paths."""
    assert extract_paths_from_markdown("") == set()


def test_extract_paths_from_markdown_prose_only_returns_empty() -> None:
    """Markdown with no path-like tokens yields no paths."""
    assert extract_paths_from_markdown("# Title\n\nJust prose, no paths.\n") == set()
