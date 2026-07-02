"""Tests for ``forge.import_graph`` shared AST import primitives."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from forge.import_graph import (
    closest_known,
    extract_import_targets,
    resolve_module_name,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_resolve_module_name_strips_src_prefix(tmp_path: Path) -> None:
    """A file under ``src/foo/bar/baz.py`` resolves to ``foo.bar.baz``."""
    f = tmp_path / "src" / "foo" / "bar" / "baz.py"
    f.parent.mkdir(parents=True)
    f.write_text("", encoding="utf-8")
    assert resolve_module_name(f, [tmp_path / "src"]) == "foo.bar.baz"


def test_resolve_module_name_handles_init(tmp_path: Path) -> None:
    """``__init__.py`` resolves to the parent package name."""
    f = tmp_path / "src" / "pkg" / "__init__.py"
    f.parent.mkdir(parents=True)
    f.write_text("", encoding="utf-8")
    assert resolve_module_name(f, [tmp_path / "src"]) == "pkg"


def test_resolve_module_name_returns_none_for_outsider(tmp_path: Path) -> None:
    """A file outside every package root resolves to ``None``."""
    f = tmp_path / "elsewhere.py"
    f.write_text("", encoding="utf-8")
    assert resolve_module_name(f, [tmp_path / "src"]) is None


def test_resolve_module_name_first_matching_root_wins(tmp_path: Path) -> None:
    """The first root the path is under determines the dotted name."""
    f = tmp_path / "src" / "pkg" / "mod.py"
    f.parent.mkdir(parents=True)
    f.write_text("", encoding="utf-8")
    # ``tmp_path`` would yield ``src.pkg.mod``; ``src`` yields ``pkg.mod``.
    assert resolve_module_name(f, [tmp_path / "src", tmp_path]) == "pkg.mod"


def test_extract_import_targets_picks_up_absolute_imports() -> None:
    """``import X.Y`` and ``from X.Y import Z`` both record ``X.Y``."""
    tree = ast.parse("import a.b\nfrom a.c import d\n")
    targets = extract_import_targets(tree, "myself")
    assert "a.b" in targets
    assert "a.c" in targets


def test_extract_import_targets_emits_both_module_and_member() -> None:
    """``from X import Y`` emits both ``X`` and the ``X.Y`` candidate."""
    tree = ast.parse("from pkg import thing\n")
    targets = extract_import_targets(tree, "myself")
    assert "pkg" in targets
    assert "pkg.thing" in targets


def test_extract_import_targets_resolves_relative_imports() -> None:
    """``from . import X`` resolves against the current module path."""
    tree = ast.parse("from . import sib\nfrom .sub import thing\n")
    targets = extract_import_targets(tree, "pkg.mod")
    assert "pkg" in targets
    assert "pkg.sub" in targets


def test_extract_import_targets_ignores_star_import_member() -> None:
    """``from X import *`` records ``X`` but not an ``X.*`` candidate."""
    tree = ast.parse("from pkg import *\n")
    targets = extract_import_targets(tree, "myself")
    assert "pkg" in targets
    assert "pkg.*" not in targets


def test_closest_known_exact_match() -> None:
    """An exact module name resolves to itself."""
    assert closest_known("myapp.core", {"myapp.core", "myapp"}) == "myapp.core"


def test_closest_known_attribute_collapses() -> None:
    """``pkg.mod.attr`` collapses to ``pkg.mod`` when ``attr`` is not a module."""
    assert closest_known("myapp.core.x", {"myapp.core", "myapp"}) == "myapp.core"


def test_closest_known_submodule_wins_over_package() -> None:
    """The deepest matching prefix wins — submodule beats its package."""
    modules = {"myapp", "myapp.core", "myapp.service"}
    assert closest_known("myapp.core", modules) == "myapp.core"


def test_closest_known_walks_up_to_shallowest_package() -> None:
    """A deep target with only a top-level package known walks up to it."""
    assert closest_known("foo.bar.baz", {"foo"}) == "foo"


def test_closest_known_external_returns_none() -> None:
    """An import not in the internal module set returns ``None``."""
    assert closest_known("requests.get", {"myapp.core"}) is None
