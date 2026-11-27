"""Tests for ``forge.gen_config_doc`` (the config-table generator)."""

from __future__ import annotations

import dataclasses
import sys
from typing import TYPE_CHECKING

import pytest

from forge import gen_config_doc
from forge.forge_config import CONFIG_KEYS, DOC_GROUPS
from forge.gen_c4 import RenderConfig
from forge.gen_common import (
    CONFIG_DOC,
    CONFIG_DOC_END,
    CONFIG_DOC_START,
    config_region_markers,
)


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo_with_config_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp repo whose config doc holds stale text in every managed region."""
    doc = tmp_path / CONFIG_DOC
    doc.parent.mkdir(parents=True)
    groups = "".join(
        f"{start}\nstale\n{end}\n"
        for start, end in (config_region_markers(group) for group in DOC_GROUPS)
    )
    doc.write_text(
        f"intro\n{CONFIG_DOC_START}\nstale\n{CONFIG_DOC_END}\n{groups}outro\n",
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
    assert text.endswith("outro\n")
    assert "stale" not in text
    for key in CONFIG_KEYS:
        assert f".{key.path[-1]}`" in text
    assert _run(monkeypatch, "--check") == 0


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        ("", "unset"),
        ("unset (step skipped)", "unset (step skipped)"),
        ("main", '`"main"`'),
        (False, "`false`"),
        (True, "`true`"),
        (("a", "b"), '`["a", "b"]`'),
        ("a|b c", r"a\|b c"),
        (None, "unset"),
        ([], "`[]`"),
        ({}, "`{}`"),
    ],
    ids=[
        "empty",
        "prose",
        "literal-string",
        "false-as-toml",
        "true-as-toml",
        "tuple-as-toml-array",
        "pipe-escaped",
        "none-is-unset",
        "empty-list-literal",
        "empty-dict-literal",
    ],
)
def test_render_default_reads_prose_as_description_not_value(
    default: object,
    expected: str,
) -> None:
    """Prose defaults render plain; literals render as code; pipes are escaped.

    Args:
        default: The config key default handed to the renderer.
        expected: The table cell the renderer must produce.
    """
    assert gen_config_doc._render_default(default) == expected


def test_render_group_table_raises_when_no_key_names_group() -> None:
    """A group no registered key names raises rather than rendering a bare header."""
    with pytest.raises(ValueError, match="no registered key"):
        gen_config_doc.render_group_table("no-such-group")


def test_render_group_table_mermaid_column_only_where_declared() -> None:
    """The Mermaid column appears for c4-render and is absent for c4-pdf."""
    pdf_header = gen_config_doc.render_group_table("c4-pdf").splitlines()[1]
    render_header = gen_config_doc.render_group_table("c4-render").splitlines()[1]
    assert "Mermaid" not in pdf_header
    assert "Mermaid" in render_header


def test_registry_doc_groups_are_registered_and_mermaid_complete() -> None:
    """Every doc group is a DOC_GROUPS member; Mermaid-column groups fill every cell.

    BEHAVIOR: a key in a group whose header carries the Mermaid column but
    which declares no setting would render a blank cell under that heading.
    """
    documented = [k for k in CONFIG_KEYS if k.doc is not None]
    assert {k.doc.group for k in documented} <= set(DOC_GROUPS)
    mermaid_groups = {k.doc.group for k in documented if k.doc.mermaid}
    blank = [
        ".".join(k.path)
        for k in documented
        if k.doc.group in mermaid_groups and not k.doc.mermaid
    ]
    assert blank == []


def test_render_config_fields_and_registry_agree() -> None:
    """Every RenderConfig field has a registry key with the same default, and back.

    BEHAVIOR: the consumer reference is generated from CONFIG_KEYS, so a
    render field added without a registry entry (or a registry entry with no
    field) must fail here rather than ship an undocumented or phantom key.
    ``forge.forge_config`` cannot import ``forge.gen_c4``, so the comparison
    can only live in a test.
    """
    fields = {f.name: f for f in dataclasses.fields(RenderConfig)}
    registered = {
        k.path[-1]: k
        for k in CONFIG_KEYS
        if k.path[:4] == ("tool", "forge", "c4", "render")
    }
    assert sorted(set(fields) ^ set(registered)) == [], (
        f"only in RenderConfig: {sorted(set(fields) - set(registered))}; "
        f"only in CONFIG_KEYS: {sorted(set(registered) - set(fields))}"
    )
    for name, field in fields.items():
        default = (
            field.default_factory()
            if field.default is dataclasses.MISSING
            else field.default
        )
        assert registered[name].default == default, name
