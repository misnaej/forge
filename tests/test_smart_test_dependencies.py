"""Tests for ``forge.smart_test.dependencies`` — import graph and test selection."""

# MOCKING STRATEGY: Graph-level tests (build_graph, select_tests) use a real
# on-disk repo layout via the ``import_chain_repo`` fixture (no git required —
# build_graph is a pure filesystem walk).  Pure-unit tests
# (SelectionPlan.tests_up_to, render_plan, _patch_targets) construct minimal
# in-memory objects with no I/O.  No subprocess or network mocking.
# ``closest_known`` moved to forge.import_graph — see tests/test_import_graph.py.

from __future__ import annotations

import ast
import logging
from typing import TYPE_CHECKING

import pytest

from forge.smart_test.dependencies import (
    SelectionPlan,
    _patch_targets,
    all_test_files,
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


def test_build_graph_type_checking_only_import_creates_edge(
    import_chain_repo: Path,
) -> None:
    """A TYPE_CHECKING-only import still becomes a graph edge (conservative).

    ``build_graph`` always passes ``include_type_checking=True`` to
    :func:`extract_import_targets`, so a change to a module only imported
    under a ``TYPE_CHECKING`` guard still selects the coupled test — a
    safe superset beats a missed selection.
    """
    typeonly_file = import_chain_repo / "tests" / "test_typeonly.py"
    typeonly_file.write_text(
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n"
        "    from myapp.core import x\n\n\n"
        "def test_typeonly():\n    pass\n",
        encoding="utf-8",
    )
    graph = build_graph(import_chain_repo)
    test_name = next(m for m in graph.test_modules if "test_typeonly" in m)
    assert "myapp.core" in graph.imports[test_name]


def test_build_graph_default_omits_ancestor_edges(import_chain_repo: Path) -> None:
    """Without the opt-in, a module gains no edge to its package ``__init__``."""
    graph = build_graph(import_chain_repo)
    assert "myapp" not in graph.imports.get("myapp.core", set())
    assert "myapp" not in graph.imports.get("myapp.service", set())


def test_build_graph_include_ancestor_edges_adds_package_edge(
    import_chain_repo: Path,
) -> None:
    """``include_ancestor_edges=True`` adds an edge from each module to ``myapp``."""
    graph = build_graph(import_chain_repo, include_ancestor_edges=True)
    assert "myapp" in graph.imports["myapp.core"]
    assert "myapp" in graph.imports["myapp.service"]


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


# Plain-src/ control for Shapes A/B: the common src/-container layout
# (scan dir == import root) is covered by
# test_select_tests_depth_1_transitive above — same fixture, same inputs,
# same assertions, so a regression there fails first.


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


# ---------------------------------------------------------------------------
# Ancestor edges — a package __init__ re-exporting a submodule name must
# reach a direct-submodule consumer's test too, not just its own facade
# consumers.
# ---------------------------------------------------------------------------


@pytest.fixture
def facade_reexport_repo(tmp_path: Path) -> Path:
    """A package ``__init__`` that re-exports a name from a submodule.

    Layout::

        <root>/
          pyproject.toml            # source_dirs = ["src"], test_dirs = ["tests"]
          src/pkg/__init__.py
          src/pkg/sub/__init__.py   # from pkg.sub.mod import thing
          src/pkg/sub/mod.py        # thing = 1
          tests/test_facade.py      # from pkg.sub import thing
          tests/test_mod.py         # from pkg.sub.mod import thing

    ``test_facade.py`` couples to the facade (``pkg.sub``); ``test_mod.py``
    couples straight to the submodule (``pkg.sub.mod``) and never imports
    the facade — the ancestor edge is what lets a facade edit reach it.

    Returns:
        The repo root path.
    """
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src"]\ntest_dirs = ["tests"]\n',
        encoding="utf-8",
    )
    pkg = root / "src" / "pkg"
    sub = pkg / "sub"
    sub.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (sub / "__init__.py").write_text(
        "from pkg.sub.mod import thing\n", encoding="utf-8"
    )
    (sub / "mod.py").write_text("thing = 1\n", encoding="utf-8")

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_facade.py").write_text(
        "from pkg.sub import thing\n\n\ndef test_thing():\n    assert thing == 1\n",
        encoding="utf-8",
    )
    (tests_dir / "test_mod.py").write_text(
        "from pkg.sub.mod import thing\n\n\ndef test_thing():\n    assert thing == 1\n",
        encoding="utf-8",
    )
    return root


def test_select_tests_facade_edit_selects_facade_test_at_depth_0(
    facade_reexport_repo: Path,
) -> None:
    """A facade edit at depth 0 selects the test that imports the facade directly."""
    plan = select_tests(facade_reexport_repo, {"src/pkg/sub/__init__.py"}, max_depth=0)
    tests = plan.tests_up_to(0)
    assert any("test_facade" in t for t in tests)


def test_select_tests_facade_edit_selects_descendant_test_via_ancestor_edge(
    facade_reexport_repo: Path,
) -> None:
    """A facade edit at depth 1 also reaches a direct-submodule consumer.

    This is the acceptance criterion for ancestor edges: ``test_mod.py``
    never imports the facade (``pkg.sub``), only the submodule
    (``pkg.sub.mod``) — but ``pkg.sub.mod`` implicitly depends on its
    ancestor ``pkg.sub`` (importing it runs ``pkg/sub/__init__.py``), so the
    ancestor edge lets a facade edit reach it one hop out.
    """
    plan = select_tests(facade_reexport_repo, {"src/pkg/sub/__init__.py"}, max_depth=1)
    tests = plan.tests_up_to(1)
    assert any("test_facade" in t for t in tests)
    assert any("test_mod" in t for t in tests)


def test_select_tests_submodule_edit_unaffected_by_ancestor_edges(
    facade_reexport_repo: Path,
) -> None:
    """A submodule edit at depth 0 selects only its own direct-importer test.

    Ancestor edges run in the submodule → package direction only, so
    editing ``pkg.sub.mod`` does not spuriously select ``test_facade.py``.
    """
    plan = select_tests(facade_reexport_repo, {"src/pkg/sub/mod.py"}, max_depth=0)
    assert plan.tests_up_to(0) == ["tests/test_mod.py"]


# ---------------------------------------------------------------------------
# Self-check + ancestor edges — a changed package __init__ with a known
# descendant must NOT trip the source-dir/import-root mismatch warning: the
# ancestor edge gives it an incoming edge via its descendant.
# ---------------------------------------------------------------------------


@pytest.fixture
def descendant_only_import_repo(tmp_path: Path) -> Path:
    """A package ``__init__`` with no direct importer but a known descendant.

    Layout::

        <root>/
          pyproject.toml       # source_dirs = ["src"], test_dirs = ["tests"]
          src/pkg/__init__.py  # empty — nothing imports it directly
          src/pkg/leaf.py      # z = 1
          tests/test_leaf.py   # from pkg.leaf import z

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
    (pkg / "leaf.py").write_text("z = 1\n", encoding="utf-8")

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_leaf.py").write_text(
        "from pkg.leaf import z\n\n\ndef test_z():\n    assert z == 1\n",
        encoding="utf-8",
    )
    return root


def test_self_check_silent_on_package_init_with_known_descendant(
    descendant_only_import_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A package ``__init__`` with a known descendant produces no mismatch warning.

    ``pkg`` has no direct importer, but ``pkg.leaf`` gains an ancestor edge
    to it — that incoming edge is enough to satisfy the self-check.
    """
    with caplog.at_level(logging.WARNING):
        select_tests(descendant_only_import_repo, {"src/pkg/__init__.py"}, max_depth=0)
    assert not any("no importer references" in r.message for r in caplog.records), (
        f"unexpected mismatch warning: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# all_test_files
# ---------------------------------------------------------------------------


@pytest.fixture
def all_test_files_repo(tmp_path: Path) -> Path:
    """Repo with both test-collection-name shapes plus non-test helper files.

    Layout::

        <root>/
          pyproject.toml            # source_dirs = ["src"], test_dirs = ["tests"]
          src/myapp/__init__.py
          src/myapp/test_lookalike.py  # test_*.py under a SOURCE dir — excluded
          tests/test_foo.py            # test_*.py — collected
          tests/bar_test.py            # *_test.py — collected
          tests/conftest.py            # helper — excluded
          tests/helpers.py             # helper — excluded

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
    (src / "test_lookalike.py").write_text("x = 1\n", encoding="utf-8")

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text(
        "def test_foo():\n    pass\n", encoding="utf-8"
    )
    (tests_dir / "bar_test.py").write_text(
        "def test_bar():\n    pass\n", encoding="utf-8"
    )
    (tests_dir / "conftest.py").write_text("", encoding="utf-8")
    (tests_dir / "helpers.py").write_text("", encoding="utf-8")
    return root


def test_all_test_files_collects_both_naming_shapes(all_test_files_repo: Path) -> None:
    """``test_*.py`` and ``*_test.py`` under the test roots are both collected."""
    result = all_test_files(all_test_files_repo)
    assert "tests/test_foo.py" in result
    assert "tests/bar_test.py" in result


def test_all_test_files_excludes_non_test_helpers(all_test_files_repo: Path) -> None:
    """``conftest.py`` and other non-matching helpers under tests/ are excluded."""
    result = all_test_files(all_test_files_repo)
    assert "tests/conftest.py" not in result
    assert "tests/helpers.py" not in result


def test_all_test_files_excludes_test_named_file_under_source_dir(
    all_test_files_repo: Path,
) -> None:
    """A ``test_*.py``-named file under a SOURCE dir is not collected as a test."""
    result = all_test_files(all_test_files_repo)
    assert "src/myapp/test_lookalike.py" not in result
