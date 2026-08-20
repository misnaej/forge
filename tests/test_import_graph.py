"""Tests for ``forge.import_graph`` shared AST import primitives."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from forge.import_graph import (
    ancestor_edges,
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


def test_extract_import_targets_excludes_type_checking_bare_name_guard() -> None:
    """``if TYPE_CHECKING:`` (bare name form) imports are excluded by default."""
    tree = ast.parse(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import guarded\n",
    )
    targets = extract_import_targets(tree, "myself")
    assert "guarded" not in targets


def test_extract_import_targets_excludes_type_checking_attribute_guard() -> None:
    """``if typing.TYPE_CHECKING:`` (attribute form) imports are excluded by default."""
    tree = ast.parse(
        "import typing\nif typing.TYPE_CHECKING:\n    import guarded\n",
    )
    targets = extract_import_targets(tree, "myself")
    assert "guarded" not in targets


def test_extract_import_targets_walks_type_checking_else_branch() -> None:
    """The guard's ``else:`` branch is always walked — it runs at runtime."""
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import guarded\n"
        "else:\n"
        "    import runtime_fallback\n",
    )
    targets = extract_import_targets(tree, "myself")
    assert "runtime_fallback" in targets
    assert "guarded" not in targets


def test_extract_import_targets_include_type_checking_opts_in() -> None:
    """``include_type_checking=True`` records the normally-skipped guarded imports."""
    tree = ast.parse(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import guarded\n",
    )
    targets = extract_import_targets(tree, "myself", include_type_checking=True)
    assert "guarded" in targets


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


def test_ancestor_edges_nested_chain() -> None:
    """A three-level package chain maps each module to every known ancestor."""
    assert ancestor_edges({"a", "a.b", "a.b.c"}) == {
        "a.b": {"a"},
        "a.b.c": {"a", "a.b"},
    }


def test_ancestor_edges_omits_module_with_no_known_ancestor() -> None:
    """A module whose ancestors are all unknown is omitted from the result."""
    assert ancestor_edges({"a.b.c"}) == {}


def test_ancestor_edges_partial_known_ancestor() -> None:
    """Only the known ancestor is recorded when an intermediate package is missing."""
    assert ancestor_edges({"a", "a.b.c"}) == {"a.b.c": {"a"}}


def test_ancestor_edges_flat_names_no_dots() -> None:
    """Dot-free module names have no ancestors to record."""
    assert ancestor_edges({"requests", "numpy"}) == {}


def test_ancestor_edges_empty_input_returns_empty_dict() -> None:
    """An empty module set produces an empty edge map."""
    assert ancestor_edges(set()) == {}
