"""Tests for ``forge.smart_test.dependencies`` — import graph and test selection."""

# MOCKING STRATEGY: Graph-level tests (build_graph, select_tests) use a real
# on-disk repo layout via the ``import_chain_repo`` fixture (no git required —
# build_graph is a pure filesystem walk).  Pure-unit tests (_closest_known,
# SelectionPlan.tests_up_to, render_plan) construct minimal in-memory objects
# with no I/O.  No subprocess or network mocking.

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge.smart_test.dependencies import (
    SelectionPlan,
    _closest_known,
    build_graph,
    render_plan,
    select_tests,
)


if TYPE_CHECKING:
    from pathlib import Path


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
