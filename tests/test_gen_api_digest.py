"""Tests for the forge-gen-api-digest CLI public API."""

from __future__ import annotations

import ast
import logging
import sys
from typing import TYPE_CHECKING

from forge.gen_api_digest import (
    DOC_RELPATH,
    build_digest,
    detect_roots,
    extract_symbols,
    format_signature,
    main,
    render_digest,
)


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


SAMPLE_MODULE = '''\
"""Sample module for digest tests."""


def public_helper(value: int, *, verbose: bool = False) -> str:
    """Return a string for value."""
    return str(value)


def _private_helper(value: int) -> str:
    """Private helper — indexed and marked internal."""
    return str(value)


class PublicThing:
    """A public class."""

    def do_work(self, name: str) -> None:
        """Do the work named name."""

    def _internal(self) -> None:
        """Private method — excluded."""


class _PrivateThing:
    """A private class — indexed and marked internal."""

    def run(self) -> None:
        """Run the private thing."""
'''


def _build_repo_with_module(root: Path) -> None:
    """Create a minimal repo tree with one source module.

    Args:
        root: Directory to populate as the repo root.
    """
    pkg = root / "src" / "sample"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "thing.py").write_text(SAMPLE_MODULE)


def test_detect_roots_prefers_src(tmp_path: Path) -> None:
    """Auto-detection returns src/ when it exists."""
    (tmp_path / "src").mkdir()
    roots = detect_roots(tmp_path, explicit=None)
    assert roots == [tmp_path / "src"]


def test_detect_roots_falls_back_to_packages(tmp_path: Path) -> None:
    """Without src/, auto-detection finds top-level package directories."""
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (tmp_path / "notapkg").mkdir()
    roots = detect_roots(tmp_path, explicit=None)
    assert roots == [pkg]


def test_detect_roots_rejects_parent_traversal(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An explicit root using `..` escapes the repo and is rejected."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    with caplog.at_level(logging.ERROR):
        roots = detect_roots(repo, explicit=["../outside"])

    assert roots == []
    assert any("outside the repo" in record.getMessage() for record in caplog.records)


def test_detect_roots_rejects_absolute_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An explicit absolute root outside the repo is rejected."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    with caplog.at_level(logging.ERROR):
        roots = detect_roots(repo, explicit=[str(outside)])

    assert roots == []
    assert any("outside the repo" in record.getMessage() for record in caplog.records)


def test_detect_roots_keeps_explicit_root_inside_repo(tmp_path: Path) -> None:
    """An explicit root inside the repo is resolved and kept."""
    (tmp_path / "src").mkdir()
    roots = detect_roots(tmp_path, explicit=["src"])
    assert roots == [(tmp_path / "src").resolve()]


def test_format_signature_reconstructs_annotations() -> None:
    """A function signature is rebuilt from the AST with annotations."""
    tree = ast.parse(SAMPLE_MODULE)
    func = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    assert format_signature(func) == (
        "public_helper(value: int, *, verbose: bool = False) -> str"
    )


def test_extract_symbols_includes_private_helpers() -> None:
    """Private top-level helpers are included in extracted symbols."""
    symbols = extract_symbols(ast.parse(SAMPLE_MODULE))
    names = [sym.signature for sym in symbols]
    assert any("public_helper" in name for name in names)
    assert any("class PublicThing" in name for name in names)
    assert any("_private_helper" in name for name in names)
    assert any("class _PrivateThing" in name for name in names)


def test_extract_symbols_marks_private_as_internal() -> None:
    """Private top-level symbols carry the internal flag; public ones do not."""
    symbols = extract_symbols(ast.parse(SAMPLE_MODULE))
    by_name = {sym.signature: sym for sym in symbols}
    public_fn = next(s for sig, s in by_name.items() if "public_helper" in sig)
    private_fn = next(s for sig, s in by_name.items() if "_private_helper" in sig)
    private_cls = next(s for sig, s in by_name.items() if "class _PrivateThing" in sig)
    assert public_fn.internal is False
    assert private_fn.internal is True
    assert private_cls.internal is True


def test_extract_symbols_skips_top_level_dunders() -> None:
    """Top-level dunder functions are skipped — they are not reuse candidates."""
    source = '''\
"""Module with a top-level dunder."""


def __getattr__(name: str) -> object:
    """Module-level attribute hook."""
    raise AttributeError(name)


def real_helper() -> None:
    """A real helper."""
'''
    symbols = extract_symbols(ast.parse(source))
    names = [sym.signature for sym in symbols]
    assert any("real_helper" in name for name in names)
    assert not any("__getattr__" in name for name in names)


def test_extract_symbols_lists_public_methods_only() -> None:
    """A public class lists its public methods and excludes private ones."""
    symbols = extract_symbols(ast.parse(SAMPLE_MODULE))
    cls = next(sym for sym in symbols if sym.signature == "class PublicThing")
    method_sigs = [sig for sig, _summary in cls.methods]
    assert any("do_work" in sig for sig in method_sigs)
    assert not any("_internal" in sig for sig in method_sigs)


def test_extract_symbols_excludes_private_methods_of_private_class() -> None:
    """A private class still lists only its public methods."""
    source = '''\
"""Module with a private class."""


class _Worker:
    """An internal worker."""

    def run(self) -> None:
        """Public method of a private class."""

    def _step(self) -> None:
        """Private method — excluded even on a private class."""

    def __init__(self) -> None:
        """Dunder method — excluded."""
'''
    symbols = extract_symbols(ast.parse(source))
    cls = next(sym for sym in symbols if sym.signature == "class _Worker")
    method_sigs = [sig for sig, _summary in cls.methods]
    assert any("run" in sig for sig in method_sigs)
    assert not any("_step" in sig for sig in method_sigs)
    assert not any("__init__" in sig for sig in method_sigs)


def test_build_digest_covers_known_module(tmp_path: Path) -> None:
    """The digest covers a module with public symbols and its summary."""
    _build_repo_with_module(tmp_path)
    digests = build_digest(tmp_path, [tmp_path / "src"])
    by_name = {d.dotted: d for d in digests}
    assert "sample.thing" in by_name
    summaries = [sym.summary for sym in by_name["sample.thing"].symbols]
    assert "Return a string for value." in summaries


def test_render_digest_produces_non_empty_doc(tmp_path: Path) -> None:
    """The rendered digest is non-empty and names known symbols."""
    _build_repo_with_module(tmp_path)
    digests = build_digest(tmp_path, [tmp_path / "src"])
    doc = render_digest(digests)
    assert doc.startswith("# API Digest")
    assert doc.endswith("\n")
    assert "do not edit by hand" in doc.lower()
    assert "public_helper" in doc
    assert "class PublicThing" in doc
    assert "_private_helper" in doc


def test_render_digest_marks_internal_symbols(tmp_path: Path) -> None:
    """Internal helpers are tagged `(internal)`; public symbols are not."""
    _build_repo_with_module(tmp_path)
    digests = build_digest(tmp_path, [tmp_path / "src"])
    doc = render_digest(digests)
    private_line = next(line for line in doc.splitlines() if "_private_helper" in line)
    public_line = next(line for line in doc.splitlines() if "public_helper" in line)
    assert "_(internal)_" in private_line
    assert "_(internal)_" not in public_line


def test_main_writes_doc_and_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode writes docs/api-digest.md and exits 0."""
    _build_repo_with_module(tmp_path)
    monkeypatch.setattr("forge.gen_api_digest.repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-gen-api-digest"])
    assert main() == 0
    doc_path = tmp_path / DOC_RELPATH
    assert doc_path.exists()
    content = doc_path.read_text()
    assert content.startswith("# API Digest")
    assert "public_helper" in content


def test_main_check_returns_zero_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--check exits 0 when the committed doc matches the generated content."""
    _build_repo_with_module(tmp_path)
    monkeypatch.setattr("forge.gen_api_digest.repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-gen-api-digest"])
    assert main() == 0

    monkeypatch.setattr(sys, "argv", ["forge-gen-api-digest", "--check"])
    with caplog.at_level(logging.INFO):
        assert main() == 0
    assert any("in sync" in record.getMessage() for record in caplog.records)


def test_main_check_returns_one_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--check exits 1 and logs an error when the committed doc has drifted."""
    _build_repo_with_module(tmp_path)
    monkeypatch.setattr("forge.gen_api_digest.repo_root", lambda: tmp_path)
    doc_path = tmp_path / DOC_RELPATH
    doc_path.parent.mkdir(parents=True)
    doc_path.write_text("# API Digest\n\nstale content\n")

    monkeypatch.setattr(sys, "argv", ["forge-gen-api-digest", "--check"])
    with caplog.at_level(logging.ERROR):
        assert main() == 1
    assert any("out of sync" in record.getMessage() for record in caplog.records)


def test_main_check_returns_one_when_doc_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--check exits 1 and logs an error when the doc does not exist."""
    _build_repo_with_module(tmp_path)
    monkeypatch.setattr("forge.gen_api_digest.repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-gen-api-digest", "--check"])
    with caplog.at_level(logging.ERROR):
        assert main() == 1
    assert any("does not exist" in record.getMessage() for record in caplog.records)
