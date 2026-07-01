"""Tests for ``forge.audit.deps`` dependency-analysis pipeline."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest

from forge.audit import common
from forge.audit.common import Scope
from forge.audit.deps import (
    DepsConfig,
    ModuleNode,
    _abstractness,
    _build_internal_graph,
    _compute_couplings,
    _instability,
    _tarjan_scc,
    render_dependency_tree,
    run,
)


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a src-layout repo and point common at it.

    Returns:
        The repo root path.
    """
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(common, "repo_root", lambda: tmp_path)
    return tmp_path


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` after stripping leading whitespace.

    Args:
        path: Destination file path.
        text: Source text to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip(), encoding="utf-8")


def test_abstractness_counts_abc_base() -> None:
    """A class inheriting from ``ABC`` counts as abstract."""
    tree = ast.parse(
        "from abc import ABC\nclass A(ABC): pass\nclass B: pass\n",
    )
    abs_count, total = _abstractness(tree)
    assert (abs_count, total) == (1, 2)


def test_abstractness_counts_abstractmethod_decorator() -> None:
    """A class with ``@abstractmethod`` is abstract even without ``ABC`` base."""
    tree = ast.parse(
        "class A:\n"
        "    @abstractmethod\n"
        "    def foo(self): ...\n"
        "class B:\n"
        "    def foo(self): ...\n",
    )
    abs_count, total = _abstractness(tree)
    assert (abs_count, total) == (1, 2)


def test_tarjan_scc_finds_two_node_cycle() -> None:
    """A simple A↔B cycle is reported as a single 2-node SCC."""
    graph = {"a": {"b"}, "b": {"a"}, "c": set()}
    sccs = _tarjan_scc(graph)
    cycle_components = [s for s in sccs if len(s) >= 2]
    assert len(cycle_components) == 1
    assert set(cycle_components[0]) == {"a", "b"}


def test_tarjan_scc_handles_acyclic_graph() -> None:
    """A DAG produces only singleton SCCs."""
    graph = {"a": {"b"}, "b": {"c"}, "c": set()}
    sccs = _tarjan_scc(graph)
    assert all(len(s) == 1 for s in sccs)


def test_compute_couplings_assigns_ca_and_ce() -> None:
    """``Ce`` is fan-out from the source; ``Ca`` is fan-in to the target."""
    graph = {"a": {"b", "c"}, "b": {"c"}, "c": set()}
    ca, ce = _compute_couplings(graph)
    assert ce["a"] == 2
    assert ce["b"] == 1
    assert ca["c"] == 2
    assert ca["b"] == 1


def test_instability_zero_when_no_couplings() -> None:
    """A module with no Ca or Ce is treated as stable (I=0)."""
    assert _instability(0, 0) == pytest.approx(0.0)


def test_instability_one_when_only_efferent() -> None:
    """A module with only outgoing dependencies is maximally unstable."""
    assert _instability(0, 4) == pytest.approx(1.0)


def test_instability_half_when_balanced() -> None:
    """Equal Ca/Ce produces I=0.5."""
    assert _instability(2, 2) == pytest.approx(0.5)


def test_build_internal_graph_filters_externals() -> None:
    """External imports (no matching module) are dropped from the graph."""
    modules = {
        "pkg.a": ModuleNode("pkg.a", "src/pkg/a.py", 0, 0),
        "pkg.b": ModuleNode("pkg.b", "src/pkg/b.py", 0, 0),
    }
    raw = {"pkg.a": {"pkg.b", "external.lib"}, "pkg.b": set()}
    graph = _build_internal_graph(modules, raw)
    assert graph["pkg.a"] == {"pkg.b"}
    assert graph["pkg.b"] == set()


def test_run_reports_cycle_as_critical(fake_repo: Path) -> None:
    """Two modules importing each other are flagged CRITICAL."""
    _write(
        fake_repo / "src" / "pkg" / "__init__.py",
        "",
    )
    _write(
        fake_repo / "src" / "pkg" / "a.py",
        "from pkg import b\nclass A: pass\n",
    )
    _write(
        fake_repo / "src" / "pkg" / "b.py",
        "from pkg import a\nclass B: pass\n",
    )
    code = run(Scope.FULL, [fake_repo / "src"], DepsConfig())
    log_path = fake_repo / "code_health" / "audit_deps.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "[CRITICAL]" in log_text
    assert "cyclic dependency" in log_text
    assert code == 1


def test_run_clean_graph_returns_zero(fake_repo: Path) -> None:
    """A DAG with no D outliers yields exit 0 and no findings."""
    _write(fake_repo / "src" / "pkg" / "__init__.py", "")
    _write(fake_repo / "src" / "pkg" / "a.py", "x = 1\n")
    _write(fake_repo / "src" / "pkg" / "b.py", "from pkg import a\n")
    code = run(
        Scope.FULL,
        [fake_repo / "src"],
        DepsConfig(distance_threshold=2.0),
    )
    log_path = fake_repo / "code_health" / "audit_deps.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "# findings: 0" in log_text
    assert code == 0


def test_run_distance_threshold_flags_outliers(fake_repo: Path) -> None:
    """A purely concrete module imported by nothing has D=1, above 0.5."""
    _write(fake_repo / "src" / "pkg" / "__init__.py", "")
    _write(
        fake_repo / "src" / "pkg" / "a.py",
        "from pkg import b\nclass A: pass\n",
    )
    _write(fake_repo / "src" / "pkg" / "b.py", "x = 1\n")
    run(
        Scope.FULL,
        [fake_repo / "src"],
        DepsConfig(distance_threshold=0.5),
    )
    log_path = fake_repo / "code_health" / "audit_deps.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "[LOW]" in log_text
    assert "main sequence" in log_text


def test_run_skips_files_with_syntax_errors(fake_repo: Path) -> None:
    """Parse failures are silently dropped, not propagated."""
    _write(fake_repo / "src" / "pkg" / "__init__.py", "")
    _write(fake_repo / "src" / "pkg" / "good.py", "x = 1\n")
    _write(fake_repo / "src" / "pkg" / "bad.py", "def !!! broken !!!\n")
    code = run(Scope.FULL, [fake_repo / "src"], DepsConfig(distance_threshold=2.0))
    assert code in {0, 1}


def test_render_dependency_tree_is_sorted_and_stable() -> None:
    """Modules and their dependencies render in deterministic sorted order."""
    graph = {
        "pkg.b": {"pkg.a"},
        "pkg.a": set(),
        "pkg.c": {"pkg.b", "pkg.a"},
    }
    tree = render_dependency_tree(graph, [])
    assert tree == ("pkg.a\npkg.b\n└─ pkg.a\npkg.c\n├─ pkg.a\n└─ pkg.b\n")
    assert render_dependency_tree(graph, []) == tree


def test_render_dependency_tree_marks_leaf_module() -> None:
    """A module with no internal dependencies renders as a bare leaf."""
    tree = render_dependency_tree({"pkg.solo": set()}, [])
    assert tree == "pkg.solo\n"


def test_render_dependency_tree_marks_cycle_members() -> None:
    """Modules in a multi-node SCC are tagged ``[cycle]`` wherever they appear."""
    graph = {"pkg.a": {"pkg.b"}, "pkg.b": {"pkg.a"}, "pkg.c": {"pkg.a"}}
    sccs = [["pkg.a", "pkg.b"], ["pkg.c"]]
    tree = render_dependency_tree(graph, sccs)
    assert "pkg.a [cycle]" in tree
    assert "pkg.b [cycle]" in tree
    assert "└─ pkg.a [cycle]" in tree
    # pkg.c is a singleton SCC and must not be tagged.
    assert "pkg.c\n" in tree
    assert "pkg.c [cycle]" not in tree


def test_run_writes_dependency_tree_log(fake_repo: Path) -> None:
    """``run()`` writes ``code_health/audit_deps_tree.log`` on every run."""
    _write(fake_repo / "src" / "pkg" / "__init__.py", "")
    _write(fake_repo / "src" / "pkg" / "a.py", "x = 1\n")
    _write(fake_repo / "src" / "pkg" / "b.py", "from pkg import a\n")
    run(Scope.FULL, [fake_repo / "src"], DepsConfig(distance_threshold=2.0))
    tree_path = fake_repo / "code_health" / "audit_deps_tree.log"
    assert tree_path.exists()
    tree_text = tree_path.read_text(encoding="utf-8")
    assert "# forge-audit-deps dependency tree" in tree_text
    assert "pkg.b" in tree_text
    assert "└─ pkg.a" in tree_text
