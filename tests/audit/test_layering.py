"""Tests for ``forge.audit.layering`` layer-composition enforcement."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge.audit import common, layering
from forge.audit.common import Scope, Severity
from forge.audit.deps import ModuleNode
from forge.audit.layering import LayeringConfig, LayerSpec, evaluate, parse_layers, run


if TYPE_CHECKING:
    from pathlib import Path


DOMAIN_MODULE = '"""Core domain module."""\n\nVALUE = 1\n'

GOOD_PIPELINE = (
    '"""Pipeline module composing the domain layer."""\n'
    "\n"
    "from myproj.domain import core\n"
    "\n"
    "USES = core.VALUE\n"
)

PLAIN_MODULE = '"""Plain module with no domain import."""\n\nVALUE = 1\n'

TWO_LAYER_TOML = (
    "[[tool.forge.layering.layer]]\n"
    'name = "domain"\n'
    'package = "myproj.domain"\n'
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "pipelines"\n'
    'package = "myproj.pipelines"\n'
    'composes_all_of = ["domain"]\n'
)

BAD_LAYER_TOML = '[tool.forge.layering]\nlayer = "not-a-list"\n'

NO_LAYERING_TOML = "[tool.forge]\n"


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a repo-like tree and point every ``repo_root()`` seam at it.

    ``layering.run`` resolves its own ``root`` via ``layering.repo_root``
    (imported by name, not proxied through ``common``), but ``write_log`` /
    ``relpath`` / ``iter_files`` (called transitively via ``build_module_graph``
    and ``write_log``) resolve theirs via ``common.repo_root`` — a distinct
    bound name. Both must point at the same fake tree so findings, the log
    file, and module paths all land under ``tmp_path``.

    Returns:
        The repo root path.
    """
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(layering, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(common, "repo_root", lambda: tmp_path)
    return tmp_path


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, creating parent dirs.

    Args:
        path: Destination file path.
        text: Content to write (leading whitespace is stripped).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip(), encoding="utf-8")


def _write_pyproject(root: Path, body: str) -> None:
    """Write ``body`` verbatim as ``pyproject.toml`` under ``root``.

    Args:
        root: Fake repo root.
        body: Full TOML content.
    """
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_layers
# ---------------------------------------------------------------------------


def test_parse_layers_valid_uses_empty_tuple_defaults() -> None:
    """A minimal valid entry parses with `()` defaults for optional keys."""
    raw = {"layer": [{"name": "domain", "package": "myproj.domain"}]}
    specs, errors = parse_layers(raw)
    assert specs == [LayerSpec(name="domain", package="myproj.domain")]
    assert errors == []


def test_parse_layers_missing_name_or_package_reports_error_and_continues() -> None:
    """A malformed entry reports its 1-based index; later entries still parse."""
    raw = {
        "layer": [
            {"package": "myproj.orphan"},
            {"name": "ok", "package": "myproj.ok"},
        ],
    }
    specs, errors = parse_layers(raw)
    assert errors == ["layer #1: needs 'name' and 'package' keys"]
    assert specs == [LayerSpec(name="ok", package="myproj.ok")]


def test_parse_layers_raw_layer_not_list_returns_error() -> None:
    """A non-list `layer` value is rejected outright, no per-entry parsing."""
    specs, errors = parse_layers({"layer": "not-a-list"})
    assert specs == []
    assert errors == ["[tool.forge.layering].layer must be an array of tables"]


def test_parse_layers_composes_all_of_undefined_layer_keeps_spec_reports_error() -> (
    None
):
    """An undefined `composes_all_of` target errors but the spec stays parsed."""
    raw = {
        "layer": [
            {
                "name": "pipelines",
                "package": "myproj.pipelines",
                "composes_all_of": ["domain"],
            },
        ],
    }
    specs, errors = parse_layers(raw)
    assert specs == [
        LayerSpec(
            name="pipelines",
            package="myproj.pipelines",
            composes_all_of=("domain",),
        ),
    ]
    assert errors == [
        "layer 'pipelines': composes_all_of names undefined layer 'domain'",
    ]


def test_parse_layers_empty_raw_returns_empty_lists() -> None:
    """An empty `[tool.forge.layering]` table parses to `([], [])`."""
    assert parse_layers({}) == ([], [])


# ---------------------------------------------------------------------------
# _direct_children
# ---------------------------------------------------------------------------


def test_direct_children_groups_by_next_segment() -> None:
    """Two modules sharing a first segment group under one child."""
    layer = LayerSpec(name="pipelines", package="myproj.pipelines")
    modules = {
        "myproj.pipelines.jobx": ModuleNode(
            name="myproj.pipelines.jobx",
            path="src/myproj/pipelines/jobx.py",
            abstract_classes=0,
            total_classes=0,
        ),
        "myproj.pipelines.joby": ModuleNode(
            name="myproj.pipelines.joby",
            path="src/myproj/pipelines/joby.py",
            abstract_classes=0,
            total_classes=0,
        ),
    }
    children = layering._direct_children(layer, modules)
    assert children == {
        "jobx": {"myproj.pipelines.jobx"},
        "joby": {"myproj.pipelines.joby"},
    }


def test_direct_children_excludes_package_surface_module() -> None:
    """The module equal to the layer package itself is never a child."""
    layer = LayerSpec(name="pipelines", package="myproj.pipelines")
    modules = {
        "myproj.pipelines": ModuleNode(
            name="myproj.pipelines",
            path="src/myproj/pipelines/__init__.py",
            abstract_classes=0,
            total_classes=0,
        ),
        "myproj.pipelines.jobx": ModuleNode(
            name="myproj.pipelines.jobx",
            path="src/myproj/pipelines/jobx.py",
            abstract_classes=0,
            total_classes=0,
        ),
    }
    children = layering._direct_children(layer, modules)
    assert children == {"jobx": {"myproj.pipelines.jobx"}}


def test_direct_children_excludes_unrelated_prefix() -> None:
    """A module under a sibling package (`myproj.pipelining`) is excluded."""
    layer = LayerSpec(name="pipelines", package="myproj.pipelines")
    modules = {
        "myproj.pipelining.other": ModuleNode(
            name="myproj.pipelining.other",
            path="src/myproj/pipelining/other.py",
            abstract_classes=0,
            total_classes=0,
        ),
    }
    assert layering._direct_children(layer, modules) == {}


def test_direct_children_deep_nesting_groups_under_first_segment() -> None:
    """A deeply nested module groups under its first segment past the package."""
    layer = LayerSpec(name="pkg", package="myproj.pkg")
    modules = {
        "myproj.pkg.a.sub.mod": ModuleNode(
            name="myproj.pkg.a.sub.mod",
            path="src/myproj/pkg/a/sub/mod.py",
            abstract_classes=0,
            total_classes=0,
        ),
    }
    children = layering._direct_children(layer, modules)
    assert children == {"a": {"myproj.pkg.a.sub.mod"}}


# ---------------------------------------------------------------------------
# _closure
# ---------------------------------------------------------------------------


def test_closure_transitive_chain() -> None:
    """A closure follows a multi-hop chain a -> b -> c."""
    graph = {"a": {"b"}, "b": {"c"}, "c": set()}
    assert layering._closure(graph, {"a"}) == {"a", "b", "c"}


def test_closure_cycle_terminates() -> None:
    """A two-node import cycle terminates instead of looping forever."""
    graph = {"a": {"b"}, "b": {"a"}}
    assert layering._closure(graph, {"a"}) == {"a", "b"}


def test_closure_no_edge_seed_returns_itself() -> None:
    """A seed absent from the graph (no outgoing edges) returns just itself."""
    assert layering._closure({}, {"x"}) == {"x"}


def test_closure_multi_seed_union() -> None:
    """Multiple seeds union their independent reachable sets."""
    graph = {"a": {"b"}, "c": {"d"}}
    assert layering._closure(graph, {"a", "c"}) == {"a", "b", "c", "d"}


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def _node(name: str, path: str) -> ModuleNode:
    """Build a `ModuleNode` with zero abstractness stats (irrelevant here).

    Args:
        name: Dotted module name.
        path: Repo-relative source path.

    Returns:
        A `ModuleNode` for use in hand-built `evaluate()` fixtures.
    """
    return ModuleNode(name=name, path=path, abstract_classes=0, total_classes=0)


def test_evaluate_satisfied_via_indirect_closure_no_finding() -> None:
    """A child reaching the target layer only transitively produces no finding."""
    layers = [
        LayerSpec(name="domain", package="myproj.domain"),
        LayerSpec(
            name="pipelines",
            package="myproj.pipelines",
            composes_all_of=("domain",),
        ),
    ]
    modules = {
        "myproj.pipelines.x": _node("myproj.pipelines.x", "src/myproj/pipelines/x.py"),
        "myproj.other.util": _node("myproj.other.util", "src/myproj/other/util.py"),
        "myproj.domain.core": _node("myproj.domain.core", "src/myproj/domain/core.py"),
    }
    graph = {
        "myproj.pipelines.x": {"myproj.other.util"},
        "myproj.other.util": {"myproj.domain.core"},
        "myproj.domain.core": set(),
    }
    findings = evaluate(layers, modules, graph, escalate_paths=set())
    assert findings == []


def test_evaluate_violated_reports_low_no_suffix() -> None:
    """An unreached composes_all_of target reports LOW with no escalation suffix."""
    layers = [
        LayerSpec(name="domain", package="myproj.domain"),
        LayerSpec(
            name="pipelines",
            package="myproj.pipelines",
            composes_all_of=("domain",),
        ),
    ]
    modules = {
        "myproj.pipelines.x": _node("myproj.pipelines.x", "src/myproj/pipelines/x.py"),
        "myproj.domain.core": _node("myproj.domain.core", "src/myproj/domain/core.py"),
    }
    findings = evaluate(layers, modules, {}, escalate_paths=set())
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.LOW
    assert "does not compose layer" in finding.message
    assert "[added/moved module]" not in finding.message


def test_evaluate_violated_added_non_anchor_module_reports_high_with_suffix() -> None:
    """`added_here` checks ALL of a child's modules, not just the sorted anchor."""
    layers = [
        LayerSpec(name="domain", package="myproj.domain"),
        LayerSpec(
            name="pipelines",
            package="myproj.pipelines",
            composes_all_of=("domain",),
        ),
    ]
    modules = {
        "myproj.pipelines.x.a": _node(
            "myproj.pipelines.x.a",
            "src/myproj/pipelines/x/a.py",
        ),
        "myproj.pipelines.x.b": _node(
            "myproj.pipelines.x.b",
            "src/myproj/pipelines/x/b.py",
        ),
    }
    # Only the non-anchor module ("b" sorts after "a") is in escalate_paths.
    findings = evaluate(
        layers,
        modules,
        {},
        escalate_paths={"src/myproj/pipelines/x/b.py"},
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.HIGH
    assert finding.path == "src/myproj/pipelines/x/a.py"
    assert "[added/moved module]" in finding.message


def test_evaluate_exempt_child_reports_single_review_finding() -> None:
    """An exempt child emits exactly one REVIEW finding, never LOW or HIGH."""
    layers = [
        LayerSpec(name="domain", package="myproj.domain"),
        LayerSpec(
            name="pipelines",
            package="myproj.pipelines",
            composes_all_of=("domain",),
            exempt=("legacy",),
        ),
    ]
    modules = {
        "myproj.pipelines.legacy": _node(
            "myproj.pipelines.legacy",
            "src/myproj/pipelines/legacy.py",
        ),
    }
    findings = evaluate(
        layers,
        modules,
        {},
        escalate_paths={"src/myproj/pipelines/legacy.py"},
    )
    assert len(findings) == 1
    assert findings[0].severity is Severity.REVIEW
    assert "exempt" in findings[0].message


def test_evaluate_layer_without_composes_all_of_yields_no_findings() -> None:
    """A layer that names no `composes_all_of` targets is never evaluated."""
    layers = [LayerSpec(name="domain", package="myproj.domain")]
    modules = {
        "myproj.domain.core": _node("myproj.domain.core", "src/myproj/domain/core.py"),
    }
    assert evaluate(layers, modules, {}, escalate_paths=set()) == []


def test_evaluate_composes_all_of_target_with_zero_modules_no_crash_no_finding() -> (
    None
):
    """A defined-but-empty target layer produces no crash and no finding.

    Distinct from the `parse_layers` config-error path (an *undefined* layer
    name) — here `domain` is a valid layer name, it simply has zero modules
    on disk, so `pipelines` has zero direct children to evaluate.
    """
    layers = [
        LayerSpec(name="domain", package="myproj.domain"),
        LayerSpec(
            name="pipelines",
            package="myproj.pipelines",
            composes_all_of=("domain",),
        ),
    ]
    assert evaluate(layers, {}, {}, escalate_paths=set()) == []


def test_evaluate_sort_order_high_then_low_then_review_then_path_line() -> None:
    """Findings sort HIGH < LOW < REVIEW, then by (path, line) within a tier."""
    layers = [
        LayerSpec(name="domain", package="myproj.domain"),
        LayerSpec(
            name="a",
            package="myproj.a",
            composes_all_of=("domain",),
            exempt=("z_exempt",),
        ),
    ]
    modules = {
        "myproj.a.z_exempt": _node("myproj.a.z_exempt", "src/myproj/a/z_exempt.py"),
        "myproj.a.high2": _node("myproj.a.high2", "src/myproj/a/high2.py"),
        "myproj.a.high1": _node("myproj.a.high1", "src/myproj/a/high1.py"),
        "myproj.a.low1": _node("myproj.a.low1", "src/myproj/a/low1.py"),
    }
    escalate = {"src/myproj/a/high1.py", "src/myproj/a/high2.py"}
    findings = evaluate(layers, modules, {}, escalate_paths=escalate)
    severities = [f.severity for f in findings]
    assert severities == [
        Severity.HIGH,
        Severity.HIGH,
        Severity.LOW,
        Severity.REVIEW,
    ]
    assert [f.path for f in findings[:2]] == [
        "src/myproj/a/high1.py",
        "src/myproj/a/high2.py",
    ]


def test_evaluate_child_missing_one_of_two_targets_reports_one_finding() -> None:
    """A child satisfying one of two composed targets misses only the other."""
    layers = [
        LayerSpec(name="domain", package="myproj.domain"),
        LayerSpec(name="infra", package="myproj.infra"),
        LayerSpec(
            name="pipelines",
            package="myproj.pipelines",
            composes_all_of=("domain", "infra"),
        ),
    ]
    modules = {
        "myproj.domain.core": _node("myproj.domain.core", "src/myproj/domain/core.py"),
        "myproj.infra.io": _node("myproj.infra.io", "src/myproj/infra/io.py"),
        "myproj.pipelines.x": _node("myproj.pipelines.x", "src/myproj/pipelines/x.py"),
    }
    graph = {
        "myproj.pipelines.x": {"myproj.domain.core"},
        "myproj.domain.core": set(),
        "myproj.infra.io": set(),
    }
    findings = evaluate(layers, modules, graph, escalate_paths=set())
    assert len(findings) == 1
    assert "infra" in findings[0].message
    assert "domain" not in findings[0].message


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_no_config_logs_nothing_to_enforce_and_skips_added_or_moved_files(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured layers exits 0 and never calls the git-touching seams."""
    _write_pyproject(fake_repo, NO_LAYERING_TOML)
    calls: list[dict] = []
    monkeypatch.setattr(
        layering,
        "added_or_moved_files",
        lambda **kw: calls.append(kw) or [],
    )
    monkeypatch.setattr(
        layering,
        "select_diff_files",
        lambda *_a: (_ for _ in ()).throw(
            AssertionError("select_diff_files should not be called"),
        ),
    )
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0
    assert "nothing to enforce" in log_text
    assert calls == []


def test_run_config_error_reports_high_at_pyproject_and_exercises_mocks(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config-only error (no valid layers) still exits 1 and runs the pipeline.

    The early return only fires when BOTH layers and errors are empty — a
    non-empty errors list with an empty layers list must still exercise
    `added_or_moved_files`.
    """
    _write_pyproject(fake_repo, BAD_LAYER_TOML)
    calls: list[dict] = []
    monkeypatch.setattr(
        layering,
        "added_or_moved_files",
        lambda **kw: calls.append(kw) or [],
    )
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 1
    assert "[HIGH] pyproject.toml:1" in log_text
    assert "must be an array of tables" in log_text
    assert len(calls) == 1


def test_run_changed_scope_filters_low_keeps_high(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGED scope drops an unchanged LOW finding but keeps an added HIGH one."""
    _write_pyproject(fake_repo, TWO_LAYER_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(fake_repo / "src" / "myproj" / "pipelines" / "lowchild.py", PLAIN_MODULE)
    _write(fake_repo / "src" / "myproj" / "pipelines" / "highchild.py", PLAIN_MODULE)
    high_path = "src/myproj/pipelines/highchild.py"
    monkeypatch.setattr(
        layering,
        "added_or_moved_files",
        lambda **_kw: [high_path],
    )
    monkeypatch.setattr(layering, "select_diff_files", lambda _root: [])
    code = run(Scope.CHANGED, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 1
    assert "lowchild.py" not in log_text
    assert "highchild.py" in log_text
    assert "[HIGH]" in log_text


def test_run_changed_scope_config_error_survives_empty_select_diff(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config-error HIGH survives the CHANGED filter even with an empty diff.

    Regression guard: config-error findings are appended AFTER the CHANGED
    scope filter runs, so they must never be dropped by it.
    """
    _write_pyproject(fake_repo, BAD_LAYER_TOML)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    monkeypatch.setattr(layering, "select_diff_files", lambda _root: [])
    code = run(Scope.CHANGED, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 1
    assert "[HIGH] pyproject.toml:1" in log_text


def test_run_changed_scope_high_survives_when_anchor_not_in_changed_or_escalate(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HIGH finding survives the CHANGED filter via the severity OR-clause.

    The finding's anchor is the child's alphabetically-first module ("a"),
    but only the non-anchor module ("b") is added/moved, and neither is in
    the (empty) changed-file set — the finding must still survive because
    it is HIGH, independent of path membership.
    """
    _write_pyproject(fake_repo, TWO_LAYER_TOML)
    _write(fake_repo / "src" / "myproj" / "pipelines" / "x" / "a.py", PLAIN_MODULE)
    _write(fake_repo / "src" / "myproj" / "pipelines" / "x" / "b.py", PLAIN_MODULE)
    b_path = "src/myproj/pipelines/x/b.py"
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [b_path])
    monkeypatch.setattr(layering, "select_diff_files", lambda _root: [])
    code = run(Scope.CHANGED, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 1
    assert "[HIGH] src/myproj/pipelines/x/a.py" in log_text


def test_run_clean_tree_valid_config_exits_zero(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree that fully composes its configured layers exits 0."""
    _write_pyproject(fake_repo, TWO_LAYER_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(fake_repo / "src" / "myproj" / "pipelines" / "good.py", GOOD_PIPELINE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0
    assert "# findings: 0" in log_text


def test_parse_layers_rejects_string_composes_all_of() -> None:
    """A bare string for composes_all_of is a clear config error, not char-split.

    Regression: tuple("domain") silently produced per-character "undefined
    layer" noise instead of naming the actual mistake.
    """
    specs, errors = parse_layers(
        {"layer": [{"name": "a", "package": "p", "composes_all_of": "domain"}]},
    )
    assert specs == []
    assert len(errors) == 1
    assert "must be arrays" in errors[0]
