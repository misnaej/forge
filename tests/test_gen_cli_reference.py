"""Tests for the forge-gen-cli-reference CLI public API."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from forge.gen_cli_reference import (
    DOC_RELPATH,
    CliEntry,
    capture_help,
    discover_clis,
    main,
    render_reference,
)


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_discover_clis_returns_sorted_named_entries() -> None:
    """Discovery yields forge CLIs as sorted CliEntry tuples."""
    entries = discover_clis()
    assert entries, "expected forge-scripts to ship console scripts"
    names = [entry.name for entry in entries]
    assert names == sorted(names)
    assert "forge-precommit" in names
    by_name = {entry.name: entry for entry in entries}
    assert by_name["forge-precommit"].module == "forge.precommit"


def test_capture_help_returns_usage_text() -> None:
    """Capturing --help for a real CLI yields its argparse usage block."""
    entry = CliEntry(name="forge-precommit", module="forge.precommit")
    help_text = capture_help(entry)
    assert "usage:" in help_text


def test_capture_help_placeholder_on_bad_module() -> None:
    """A non-importable module yields a placeholder line, not a crash."""
    entry = CliEntry(name="ghost-cli", module="forge.does_not_exist")
    help_text = capture_help(entry)
    assert "ghost-cli" in help_text


def test_capture_help_is_width_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``capture_help`` pins ``COLUMNS`` so output does not vary by caller width.

    argparse's HelpFormatter honors the ambient ``COLUMNS`` so the
    captured output is environment-dependent unless we pin it.
    ``capture_help`` overrides ``COLUMNS`` in the subprocess env to
    keep the output byte-identical regardless of caller width — the
    drift gate (``forge-gen-cli-reference --check``) is otherwise
    unwinnable on any runner whose width differs from where the
    committed reference was generated.
    """
    entry = CliEntry(name="forge-precommit", module="forge.precommit")
    monkeypatch.setenv("COLUMNS", "40")
    narrow = capture_help(entry)
    monkeypatch.setenv("COLUMNS", "200")
    wide = capture_help(entry)
    assert narrow == wide, (
        "capture_help output must be identical regardless of caller "
        "$COLUMNS — see CLI_REFERENCE_COLUMNS pin"
    )


def test_render_reference_covers_every_cli() -> None:
    """The rendered doc is non-empty and has a section per CLI."""
    entries = [
        CliEntry(name="forge-precommit", module="forge.precommit"),
        CliEntry(name="forge-doctor", module="forge.doctor"),
    ]
    doc = render_reference(entries)
    assert doc.startswith("# CLI Reference")
    assert doc.endswith("\n")
    for entry in entries:
        assert f"## {entry.name}" in doc


def test_render_reference_includes_generated_note() -> None:
    """The rendered doc warns it is generated and how to regenerate it."""
    doc = render_reference([CliEntry(name="forge-doctor", module="forge.doctor")])
    assert "do not edit by hand" in doc.lower()
    assert "forge-gen-cli-reference" in doc


def test_main_writes_doc_and_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode writes docs/cli-reference.md and exits 0."""
    monkeypatch.setattr(
        "forge.gen_cli_reference.repo_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(sys, "argv", ["forge-gen-cli-reference"])
    assert main() == 0
    doc_path = tmp_path / DOC_RELPATH
    assert doc_path.exists()
    content = doc_path.read_text()
    assert content.startswith("# CLI Reference")
    assert "## forge-precommit" in content


def test_main_check_returns_zero_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--check exits 0 when the committed doc matches the generated content."""
    monkeypatch.setattr(
        "forge.gen_cli_reference.repo_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(sys, "argv", ["forge-gen-cli-reference"])
    assert main() == 0

    monkeypatch.setattr(sys, "argv", ["forge-gen-cli-reference", "--check"])
    with caplog.at_level(logging.INFO):
        assert main() == 0
    assert any("in sync" in record.getMessage() for record in caplog.records)


def test_main_check_returns_one_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--check exits 1 and logs an error when the committed doc has drifted."""
    monkeypatch.setattr(
        "forge.gen_cli_reference.repo_root",
        lambda: tmp_path,
    )
    doc_path = tmp_path / DOC_RELPATH
    doc_path.parent.mkdir(parents=True)
    doc_path.write_text("# CLI Reference\n\nstale content\n")

    monkeypatch.setattr(sys, "argv", ["forge-gen-cli-reference", "--check"])
    with caplog.at_level(logging.ERROR):
        assert main() == 1
    assert any("out of sync" in record.getMessage() for record in caplog.records)


def test_main_check_returns_one_when_doc_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--check exits 1 and logs an error when the doc does not exist."""
    monkeypatch.setattr(
        "forge.gen_cli_reference.repo_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(sys, "argv", ["forge-gen-cli-reference", "--check"])
    with caplog.at_level(logging.ERROR):
        assert main() == 1
    assert any("does not exist" in record.getMessage() for record in caplog.records)
