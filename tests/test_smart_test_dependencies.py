"""Tests for ``forge.smart_test.dependencies`` — import graph and test selection."""

# MOCKING STRATEGY: Graph-level tests (build_graph, select_tests) use a real
# on-disk repo layout via the ``import_chain_repo`` fixture (no git required —
# build_graph is a pure filesystem walk).  Pure-unit tests (_closest_known,
# SelectionPlan.tests_up_to, render_plan, _patch_targets) construct minimal
# in-memory objects with no I/O.  No subprocess or network mocking.

from __future__ import annotations

import ast
import logging
from typing import TYPE_CHECKING

import pytest

from forge.smart_test.dependencies import (
    SelectionPlan,
    _closest_known,
    _patch_targets,
    build_graph,
    render_plan,
    select_tests,
)


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(code: str) -> ast.Module:
    """Parse a Python source string into an AST module.

    Args:
        code: Valid Python source code.

    Returns:
        The parsed :class:`ast.Module`.
    """
    return ast.parse(code)


# ---------------------------------------------------------------------------
# import_chain_repo fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def import_chain_repo(tmp_path: Path) -> Path:
    """Real on-disk repo with a two-module source tree and two test files.

    Layout::

        <root>/
          pyproject.toml          # [tool.forge] source_dirs + test_dirs
          src/myapp/__init__.py
          src/myapp/core.py       # no internal imports
          src/myapp/service.py    # from myapp.core import x
          tests/test_core.py      # from myapp.core import x
          tests/test_service.py   # from myapp.service import x

    Returns:
        The repo root path.
    """
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src"]\ntest_dirs = ["tests"]\n',
        encoding="utf-8",
    )
    src = root / "src" / "myapp"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "core.py").write_text("x = 1\n", encoding="utf-8")
    (src / "service.py").write_text("from myapp.core import x\n", encoding="utf-8")

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_core.py").write_text(
        "from myapp.core import x\n\n\ndef test_x():\n    assert x == 1\n",
        encoding="utf-8",
    )
    (tests_dir / "test_service.py").write_text(
        "from myapp.service import x\n\n\ndef test_x():\n    assert x == 1\n",
        encoding="utf-8",
    )
    return root


def test_closest_known_exact_match() -> None:
    """An exact module name resolves to itself."""
    assert _closest_known("myapp.core", {"myapp.core", "myapp"}) == "myapp.core"


def test_closest_known_attribute_collapses() -> None:
    """``pkg.mod.attr`` collapses to ``pkg.mod`` when ``attr`` is not a module."""
    assert _closest_known("myapp.core.x", {"myapp.core", "myapp"}) == "myapp.core"


def test_closest_known_submodule_wins_over_package() -> None:
    """The deepest matching prefix wins — submodule beats its package."""
    modules = {"myapp", "myapp.core", "myapp.service"}
    assert _closest_known("myapp.core", modules) == "myapp.core"


def test_closest_known_external_returns_none() -> None:
    """An import not in the internal module set returns ``None``."""
    assert _closest_known("requests.get", {"myapp.core"}) is None


def test_tests_up_to_returns_sorted_union_at_depth() -> None:
    """Tests at depths 0..N are unioned and sorted."""
    plan = SelectionPlan(
        newly_at_depth={0: ["tests/test_b.py"], 1: ["tests/test_a.py"]},
        changed_tests=[],
        max_depth=1,
    )
    assert plan.tests_up_to(1) == ["tests/test_a.py", "tests/test_b.py"]


def test_tests_up_to_depth_beyond_plan_max_returns_same() -> None:
    """A depth argument larger than max_depth returns the same set as max_depth."""
    plan = SelectionPlan(
        newly_at_depth={0: ["tests/test_a.py"], 1: ["tests/test_b.py"]},
        changed_tests=[],
        max_depth=1,
    )
    assert plan.tests_up_to(99) == plan.tests_up_to(1)


def test_tests_up_to_empty_plan() -> None:
    """An empty plan returns an empty list at any depth."""
    plan = SelectionPlan(newly_at_depth={}, changed_tests=[], max_depth=2)
    assert plan.tests_up_to(2) == []


def test_tests_up_to_deduplication() -> None:
    """A test in both changed_tests and newly_at_depth appears only once."""
    plan = SelectionPlan(
        newly_at_depth={0: ["tests/test_x.py"]},
        changed_tests=["tests/test_x.py"],
        max_depth=0,
    )
    result = plan.tests_up_to(0)
    assert result == ["tests/test_x.py"]


def test_build_graph_populates_module_names(import_chain_repo: Path) -> None:
    """Module names for every .py are in path_of after build_graph."""
    graph = build_graph(import_chain_repo)
    assert "myapp.core" in graph.path_of
    assert "myapp.service" in graph.path_of


def test_build_graph_records_imports(import_chain_repo: Path) -> None:
    """myapp.service imports myapp.core — the edge is in graph.imports."""
    graph = build_graph(import_chain_repo)
    assert "myapp.core" in graph.imports.get("myapp.service", set())


def test_build_graph_marks_test_modules(import_chain_repo: Path) -> None:
    """test_core and test_service are in test_modules; source modules are not."""
    graph = build_graph(import_chain_repo)
    assert any("test_core" in m for m in graph.test_modules)
    assert any("test_service" in m for m in graph.test_modules)
    assert "myapp.core" not in graph.test_modules


def test_build_graph_skips_syntax_error(import_chain_repo: Path) -> None:
    """Files with SyntaxError are skipped; the rest of the graph is intact."""
    bad_file = import_chain_repo / "src" / "myapp" / "broken.py"
    bad_file.write_text("def (:\n", encoding="utf-8")
    graph = build_graph(import_chain_repo)
    # The broken module should not appear; others still do.
    assert "myapp.broken" not in graph.path_of
    assert "myapp.core" in graph.path_of


def test_select_tests_depth_0_direct_importer(import_chain_repo: Path) -> None:
    """A change to core.py at depth 0 selects test_core.py (direct importer)."""
    plan = select_tests(import_chain_repo, {"src/myapp/core.py"}, max_depth=0)
    tests = plan.tests_up_to(0)
    assert any("test_core" in t for t in tests)
    assert not any("test_service" in t for t in tests)


def test_select_tests_depth_1_transitive(import_chain_repo: Path) -> None:
    """A change to core.py at depth 1 selects both test_core and test_service."""
    plan = select_tests(import_chain_repo, {"src/myapp/core.py"}, max_depth=1)
    tests = plan.tests_up_to(1)
    assert any("test_core" in t for t in tests)
    assert any("test_service" in t for t in tests)


def test_select_tests_changed_test_file_at_depth_0(import_chain_repo: Path) -> None:
    """A changed test file appears in changed_tests regardless of imports."""
    plan = select_tests(import_chain_repo, {"tests/test_core.py"}, max_depth=0)
    assert any("test_core" in t for t in plan.changed_tests)


def test_select_tests_changed_file_not_in_graph(import_chain_repo: Path) -> None:
    """A changed file with no graph entry yields an empty selection."""
    plan = select_tests(import_chain_repo, {"src/myapp/ghost.py"}, max_depth=1)
    assert plan.tests_up_to(1) == []
    assert plan.changed_tests == []


def test_select_tests_no_further_importers(import_chain_repo: Path) -> None:
    """A change to service.py at depth 0 selects only test_service, not test_core."""
    plan = select_tests(import_chain_repo, {"src/myapp/service.py"}, max_depth=0)
    tests = plan.tests_up_to(0)
    assert any("test_service" in t for t in tests)
    assert not any("test_core" in t for t in tests)


def test_render_plan_render_with_tests() -> None:
    """Each selected test appears as '  - <path>' with a two-space-dash-space prefix."""
    plan = SelectionPlan(
        newly_at_depth={0: ["tests/test_a.py", "tests/test_b.py"]},
        changed_tests=[],
        max_depth=0,
    )
    output = render_plan(plan, 0)
    assert "📋 Tests covering changed code" in output
    assert "  - tests/test_a.py" in output
    assert "  - tests/test_b.py" in output


def test_render_plan_render_empty() -> None:
    """An empty plan renders a '(none' notice rather than test paths."""
    plan = SelectionPlan(newly_at_depth={}, changed_tests=[], max_depth=0)
    output = render_plan(plan, 0)
    assert "(none" in output


def test_render_plan_header_includes_depth_number() -> None:
    """The header names the depth tier."""
    plan = SelectionPlan(newly_at_depth={}, changed_tests=[], max_depth=2)
    output = render_plan(plan, 2)
    assert "depth 2" in output


# ---------------------------------------------------------------------------
# _patch_targets — unit tests (no I/O, pure AST)
# ---------------------------------------------------------------------------


def test_patch_targets_simple_patch() -> None:
    """``patch("pkg.mod.attr")`` yields the raw target string."""
    tree = _parse('patch("pkg.mod.attr")')
    assert _patch_targets(tree) == {"pkg.mod.attr"}


def test_patch_targets_decorator_form() -> None:
    """A ``@patch(...)`` decorator is extracted just like a call-form patch."""
    tree = _parse('@patch("pkg.mod.func")\ndef test_x():\n    pass\n')
    assert _patch_targets(tree) == {"pkg.mod.func"}


def test_patch_targets_patch_dict_sys_modules() -> None:
    """``patch.dict("sys.modules", {"pkg.a": None})`` yields the dict keys."""
    tree = _parse('patch.dict("sys.modules", {"pkg.a": None, "pkg.b": None})')
    assert _patch_targets(tree) == {"pkg.a", "pkg.b"}


def test_patch_targets_patch_dict_non_sys_modules() -> None:
    """``patch.dict("pkg.registry", {...})`` yields the first string arg as target."""
    tree = _parse('patch.dict("pkg.registry", {"key": "val"})')
    assert _patch_targets(tree) == {"pkg.registry"}


def test_patch_targets_three_segment_callee() -> None:
    """3-segment patch callee target is correctly extracted."""
    tree = _parse('unittest.mock.patch("pkg.mod.x")')
    assert _patch_targets(tree) == {"pkg.mod.x"}


def test_patch_targets_mock_prefix() -> None:
    """``mock.patch("pkg.mod.x")`` is recognized (mock. prefix tolerated)."""
    tree = _parse('mock.patch("pkg.mod.x")')
    assert _patch_targets(tree) == {"pkg.mod.x"}


def test_patch_targets_mocker_prefix() -> None:
    """``mocker.patch("pkg.mod.y")`` is recognized (mocker. prefix tolerated)."""
    tree = _parse('mocker.patch("pkg.mod.y")')
    assert _patch_targets(tree) == {"pkg.mod.y"}


def test_patch_targets_patch_object_skipped() -> None:
    """``patch.object`` is skipped (reachable via import)."""
    tree = _parse('patch.object(SomeClass, "method")')
    assert _patch_targets(tree) == set()


def test_patch_targets_no_string_arg_skipped() -> None:
    """``patch(some_var)`` (no string literal) is silently skipped."""
    tree = _parse("patch(some_variable)")
    assert _patch_targets(tree) == set()


def test_patch_targets_empty_module() -> None:
    """An empty module yields an empty target set."""
    tree = _parse("")
    assert _patch_targets(tree) == set()


# ---------------------------------------------------------------------------
# patch_only_repo fixture + select_tests follow_mock_patches
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_only_repo(tmp_path: Path) -> Path:
    """Repo where test_b patches pkg.b but has NO import of it.

    Layout::

        <root>/
          pyproject.toml
          src/pkg/__init__.py
          src/pkg/a.py          # x = 1
          src/pkg/b.py          # thing = 42
          tests/test_a.py       # from pkg.a import x
          tests/test_b.py       # @patch("pkg.b.thing") only — no import

    Returns:
        The repo root path.
    """
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src"]\ntest_dirs = ["tests"]\n',
        encoding="utf-8",
    )
    pkg = root / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "b.py").write_text("thing = 42\n", encoding="utf-8")

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text(
        "from pkg.a import x\n\n\ndef test_a():\n    assert x == 1\n",
        encoding="utf-8",
    )
    # test_b patches pkg.b.thing but never imports pkg.b.
    (tests_dir / "test_b.py").write_text(
        "from unittest.mock import patch\n\n\n"
        '@patch("pkg.b.thing")\n'
        "def test_b(mock_thing):\n"
        "    pass\n",
        encoding="utf-8",
    )
    return root


def test_select_tests_follow_mock_patches_selects_patch_only(
    patch_only_repo: Path,
) -> None:
    """Test-only patch (no import) is selected at depth 0 via patch-edge.

    The test file has no ``import pkg.b`` statement; static analysis alone
    would miss it. The patch-target edge must bridge the gap at depth 0.
    """
    plan = select_tests(
        patch_only_repo, {"src/pkg/b.py"}, max_depth=0, follow_mock_patches=True
    )
    tests = plan.tests_up_to(0)
    assert any("test_b" in t for t in tests), f"test_b not found in {tests}"


def test_select_tests_no_follow_mock_patches_misses_patch_only(
    patch_only_repo: Path,
) -> None:
    """With ``follow_mock_patches=False``, a patch-only test is NOT selected.

    Without the opt-in, the static import graph has no edge from test_b to
    pkg.b, so a change to pkg/b.py leaves test_b out of the selection.
    """
    plan = select_tests(
        patch_only_repo, {"src/pkg/b.py"}, max_depth=0, follow_mock_patches=False
    )
    tests = plan.tests_up_to(0)
    assert not any("test_b" in t for t in tests), (
        f"test_b unexpectedly selected: {tests}"
    )


# ---------------------------------------------------------------------------
# Import-root vs source-dir naming — regression fixtures
#
# source_dirs does double duty: "dirs whose .py to scan" (a broad list every
# path-tool shares) AND "the sys.path roots to strip when naming modules"
# (what smart_test needs — the *import* roots). Those coincide for a src/
# layout and diverge for the two shapes below. When they diverge the changed
# module is named with the wrong prefix, no reverse edge connects, and the
# gate reports zero tests and passes green — a silent false negative.
# ---------------------------------------------------------------------------


@pytest.fixture
def package_as_source_dir_repo(tmp_path: Path) -> Path:
    """Shape A — a ``source_dirs`` entry that is itself an import root package.

    ``libs`` is listed in ``source_dirs`` and is *itself a package* (carries
    ``__init__.py``), so the sys.path root is the repo root, not ``libs/``:
    ``libs/thing/core.py`` is imported as ``libs.thing.core``. Stripping the
    ``libs/`` scan-dir prefix would misname it ``thing.core`` and disconnect
    the test edge.

    Layout::

        <root>/
          pyproject.toml            # source_dirs = ["src", "libs"]
          libs/__init__.py          # libs IS a package
          libs/thing/__init__.py
          libs/thing/core.py        # x = 1
          tests/test_thing.py       # from libs.thing.core import x

    Returns:
        The repo root path.
    """
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src", "libs"]\ntest_dirs = ["tests"]\n',
        encoding="utf-8",
    )
    libs = root / "libs"
    thing = libs / "thing"
    thing.mkdir(parents=True)
    (libs / "__init__.py").write_text("", encoding="utf-8")
    (thing / "__init__.py").write_text("", encoding="utf-8")
    (thing / "core.py").write_text("x = 1\n", encoding="utf-8")

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_thing.py").write_text(
        "from libs.thing.core import x\n\n\ndef test_x():\n    assert x == 1\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def nested_src_root_repo(tmp_path: Path) -> Path:
    """Shape B — a ``source_dirs`` entry holding a nested ``*/src`` import root.

    ``projects`` is listed in ``source_dirs`` but the real sys.path root is
    ``projects/APP/src``: ``runner.py`` is imported as ``pkg.runner``.
    Stripping the ``projects/`` scan-dir prefix would misname it
    ``APP.src.pkg.runner`` and disconnect the test edge.

    Layout::

        <root>/
          pyproject.toml            # source_dirs = ["src", "projects"]
          projects/APP/src/pkg/__init__.py
          projects/APP/src/pkg/runner.py    # def run(): ...
          tests/test_runner.py              # from pkg.runner import run

    Returns:
        The repo root path.
    """
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src", "projects"]\ntest_dirs = ["tests"]\n',
        encoding="utf-8",
    )
    pkg = root / "projects" / "APP" / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "runner.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_runner.py").write_text(
        "from pkg.runner import run\n\n\ndef test_run():\n    assert run() == 1\n",
        encoding="utf-8",
    )
    return root


def test_shape_a_package_as_source_dir_selects_coupled_test(
    package_as_source_dir_repo: Path,
) -> None:
    """Shape A: changing ``libs/thing/core.py`` selects its coupled test.

    Regression: the ``libs/`` prefix must NOT be stripped when naming the
    module — importers reference ``libs.thing.core``, so the changed file
    must resolve to that same dotted name for the reverse edge to connect.
    """
    plan = select_tests(package_as_source_dir_repo, {"libs/thing/core.py"}, max_depth=1)
    tests = plan.tests_up_to(1)
    assert any("test_thing" in t for t in tests), f"test_thing not found in {tests}"


def test_shape_b_nested_src_root_selects_coupled_test(
    nested_src_root_repo: Path,
) -> None:
    """Shape B: changing a nested ``*/src`` module selects its coupled test.

    Regression: ``projects/APP/src/pkg/runner.py`` must resolve to
    ``pkg.runner`` (its real import root is ``projects/APP/src``), matching
    the importer's ``from pkg.runner import run``.
    """
    plan = select_tests(
        nested_src_root_repo, {"projects/APP/src/pkg/runner.py"}, max_depth=1
    )
    tests = plan.tests_up_to(1)
    assert any("test_runner" in t for t in tests), f"test_runner not found in {tests}"


def test_src_container_control_unaffected(import_chain_repo: Path) -> None:
    """Control: the plain ``src/`` layout keeps resolving and selecting.

    The naming fix for Shapes A/B must not change behavior for the common
    ``src/``-container layout, where scan dir and import root coincide.
    """
    plan = select_tests(import_chain_repo, {"src/myapp/core.py"}, max_depth=1)
    tests = plan.tests_up_to(1)
    assert any("test_core" in t for t in tests)
    assert any("test_service" in t for t in tests)


# ---------------------------------------------------------------------------
# Self-check warning — a changed module named with no importer in the graph
# is the fingerprint of a source-dir/import-root mismatch; warn loudly instead
# of selecting zero silently.
# ---------------------------------------------------------------------------


@pytest.fixture
def orphan_changed_repo(tmp_path: Path) -> Path:
    """``src/`` repo with a source module that no test or module imports.

    Layout adds ``src/myapp/orphan.py`` (referenced by nobody) to the base
    two-module tree, so a change to it names a real module with zero
    importers — the mismatch fingerprint the self-check must flag.

    Returns:
        The repo root path.
    """
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src"]\ntest_dirs = ["tests"]\n',
        encoding="utf-8",
    )
    src = root / "src" / "myapp"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "core.py").write_text("x = 1\n", encoding="utf-8")
    (src / "orphan.py").write_text("y = 2\n", encoding="utf-8")

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_core.py").write_text(
        "from myapp.core import x\n\n\ndef test_x():\n    assert x == 1\n",
        encoding="utf-8",
    )
    return root


def test_self_check_warns_on_module_with_no_importer(
    orphan_changed_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A changed module that no importer references logs a mismatch warning."""
    with caplog.at_level(logging.WARNING):
        select_tests(orphan_changed_repo, {"src/myapp/orphan.py"}, max_depth=1)
    assert any("no importer references" in r.message for r in caplog.records), (
        f"expected mismatch warning, got {[r.message for r in caplog.records]}"
    )


def test_self_check_silent_on_module_with_importer(
    import_chain_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A changed module that IS imported produces no mismatch warning."""
    with caplog.at_level(logging.WARNING):
        select_tests(import_chain_repo, {"src/myapp/core.py"}, max_depth=1)
    assert not any("no importer references" in r.message for r in caplog.records), (
        f"unexpected mismatch warning: {[r.message for r in caplog.records]}"
    )
