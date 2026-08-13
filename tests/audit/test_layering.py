"""Tests for ``forge.audit.layering`` layer-composition enforcement."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from forge.audit import common, layering
from forge.audit.common import Scope, Severity
from forge.audit.deps import ModuleNode
from forge.audit.layering import (
    LayeringConfig,
    LayerSpec,
    evaluate,
    main,
    parse_layers,
    run,
)
from tests.audit.conftest import make_fake_repo, write_pyproject


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

TYPE_CHECKING_ONLY_PIPELINE = (
    '"""Pipeline module importing the domain layer only for type hints."""\n'
    "\n"
    "from typing import TYPE_CHECKING\n"
    "\n"
    "if TYPE_CHECKING:\n"
    "    from myproj.domain import core\n"
    "\n"
    "VALUE = 1\n"
)

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

SINGLE_LAYER_NO_MODULES_TOML = (
    '[[tool.forge.layering.layer]]\nname = "domain"\npackage = "myproj.domain"\n'
)

MULTI_PREFIX_NO_MODULES_TOML = (
    "[[tool.forge.layering.layer]]\n"
    'name = "domain"\n'
    'packages = ["myproj.a", "myproj.b"]\n'
)

EMPTY_DOMAIN_POPULATED_PIPELINES_TOML = (
    "[[tool.forge.layering.layer]]\n"
    'name = "domain"\n'
    'package = "myproj.domain"\n'
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "pipelines"\n'
    'package = "myproj.pipelines"\n'
    'composes_all_of = ["domain"]\n'
)


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
    return make_fake_repo(tmp_path, monkeypatch, layering, common)


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, creating parent dirs.

    Args:
        path: Destination file path.
        text: Content to write (leading whitespace is stripped).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip(), encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_layers
# ---------------------------------------------------------------------------


def test_parse_layers_valid_uses_empty_tuple_defaults() -> None:
    """A minimal valid entry parses with `()` defaults for optional keys."""
    raw = {"layer": [{"name": "domain", "package": "myproj.domain"}]}
    specs, errors = parse_layers(raw)
    assert specs == [LayerSpec(name="domain", packages=("myproj.domain",))]
    assert errors == []


def test_parse_layers_missing_name_reports_name_must_be_string_error() -> None:
    """A missing `name` key reports its 1-based index; later entries still parse."""
    raw = {
        "layer": [
            {"package": "myproj.orphan"},
            {"name": "ok", "package": "myproj.ok"},
        ],
    }
    specs, errors = parse_layers(raw)
    assert errors == ["layer #1: 'name' must be a string"]
    assert specs == [LayerSpec(name="ok", packages=("myproj.ok",))]


def test_parse_layers_non_string_name_reports_error() -> None:
    """A non-string `name` value is rejected, not coerced."""
    raw = {"layer": [{"name": 123, "package": "x"}]}
    specs, errors = parse_layers(raw)
    assert specs == []
    assert errors == ["layer #1: 'name' must be a string"]


def test_parse_layers_both_package_and_packages_keys_reports_error() -> None:
    """Both `package` and `packages` present is ambiguous, so it errors."""
    raw = {"layer": [{"name": "x", "package": "a", "packages": ["a"]}]}
    specs, errors = parse_layers(raw)
    assert specs == []
    assert errors == [
        (
            "layer #1: needs exactly one of 'package' (string) "
            "or 'packages' (array of strings)"
        ),
    ]


def test_parse_layers_neither_package_nor_packages_reports_error() -> None:
    """Neither `package` nor `packages` present reports the same needs-one error."""
    raw = {"layer": [{"name": "x"}]}
    specs, errors = parse_layers(raw)
    assert specs == []
    assert errors == [
        (
            "layer #1: needs exactly one of 'package' (string) "
            "or 'packages' (array of strings)"
        ),
    ]


def test_parse_layers_package_non_string_reports_error() -> None:
    """A list-valued `package` is rejected, pointed at `packages` instead."""
    raw = {"layer": [{"name": "x", "package": ["a", "b"]}]}
    specs, errors = parse_layers(raw)
    assert specs == []
    assert errors == [
        (
            "layer #1: 'package' must be a string — use "
            "'packages' for a multi-package layer"
        ),
    ]


def test_parse_layers_packages_empty_list_reports_error() -> None:
    """An empty `packages` array is rejected — a layer needs at least one prefix."""
    raw = {"layer": [{"name": "x", "packages": []}]}
    specs, errors = parse_layers(raw)
    assert specs == []
    assert errors == ["layer #1: 'packages' must be a non-empty array of strings"]


def test_parse_layers_packages_non_string_element_reports_error() -> None:
    """A `packages` array with a non-string element is rejected outright."""
    raw = {"layer": [{"name": "x", "packages": ["a", 2]}]}
    specs, errors = parse_layers(raw)
    assert specs == []
    assert errors == ["layer #1: 'packages' must be a non-empty array of strings"]


def test_parse_layers_packages_not_a_list_reports_error() -> None:
    """A non-list `packages` value is rejected, same message as a bad element."""
    raw = {"layer": [{"name": "x", "packages": "not-a-list"}]}
    specs, errors = parse_layers(raw)
    assert specs == []
    assert errors == ["layer #1: 'packages' must be a non-empty array of strings"]


def test_parse_layers_non_dict_entry_reports_must_be_a_table() -> None:
    """A non-table entry in `layer` is rejected before per-entry parsing."""
    specs, errors = parse_layers({"layer": ["not-a-dict"]})
    assert specs == []
    assert errors == ["layer #1: must be a table"]


def test_parse_layers_duplicate_layer_name_first_wins() -> None:
    """A duplicate layer name errors on the second definition; the first wins."""
    raw = {
        "layer": [
            {"name": "domain", "package": "myproj.d1"},
            {"name": "domain", "package": "myproj.d2"},
        ],
    }
    specs, errors = parse_layers(raw)
    assert specs == [LayerSpec(name="domain", packages=("myproj.d1",))]
    assert errors == [
        "layer #2: duplicate layer name 'domain' (first definition, layer #1, wins)",
    ]


def test_parse_layers_packages_key_parses_multi_package_layer() -> None:
    """The `packages` key parses a multi-prefix layer as an ordered tuple."""
    raw = {
        "layer": [
            {"name": "domain", "packages": ["myproj.models", "myproj.rules"]},
        ],
    }
    specs, errors = parse_layers(raw)
    assert specs == [
        LayerSpec(name="domain", packages=("myproj.models", "myproj.rules")),
    ]
    assert errors == []


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
            packages=("myproj.pipelines",),
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
    layer = LayerSpec(name="pipelines", packages=("myproj.pipelines",))
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
        ("myproj.pipelines", "jobx"): {"myproj.pipelines.jobx"},
        ("myproj.pipelines", "joby"): {"myproj.pipelines.joby"},
    }


def test_direct_children_excludes_package_surface_module() -> None:
    """The module equal to the layer package itself is never a child."""
    layer = LayerSpec(name="pipelines", packages=("myproj.pipelines",))
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
    assert children == {("myproj.pipelines", "jobx"): {"myproj.pipelines.jobx"}}


def test_direct_children_excludes_unrelated_prefix() -> None:
    """A module under a sibling package (`myproj.pipelining`) is excluded."""
    layer = LayerSpec(name="pipelines", packages=("myproj.pipelines",))
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
    layer = LayerSpec(name="pkg", packages=("myproj.pkg",))
    modules = {
        "myproj.pkg.a.sub.mod": ModuleNode(
            name="myproj.pkg.a.sub.mod",
            path="src/myproj/pkg/a/sub/mod.py",
            abstract_classes=0,
            total_classes=0,
        ),
    }
    children = layering._direct_children(layer, modules)
    assert children == {("myproj.pkg", "a"): {"myproj.pkg.a.sub.mod"}}


def test_direct_children_multi_prefix_layer_keys_children_by_prefix() -> None:
    """Same-named children under different prefixes stay distinct entries."""
    layer = LayerSpec(name="domain", packages=("myproj.a", "myproj.b"))
    modules = {
        "myproj.a.x": ModuleNode(
            name="myproj.a.x",
            path="src/myproj/a/x.py",
            abstract_classes=0,
            total_classes=0,
        ),
        "myproj.b.x": ModuleNode(
            name="myproj.b.x",
            path="src/myproj/b/x.py",
            abstract_classes=0,
            total_classes=0,
        ),
    }
    children = layering._direct_children(layer, modules)
    assert children == {
        ("myproj.a", "x"): {"myproj.a.x"},
        ("myproj.b", "x"): {"myproj.b.x"},
    }


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
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(
            name="pipelines",
            packages=("myproj.pipelines",),
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
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(
            name="pipelines",
            packages=("myproj.pipelines",),
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
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(
            name="pipelines",
            packages=("myproj.pipelines",),
            composes_all_of=("domain",),
        ),
    ]
    modules = {
        "myproj.domain.core": _node("myproj.domain.core", "src/myproj/domain/core.py"),
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
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(
            name="pipelines",
            packages=("myproj.pipelines",),
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
    layers = [LayerSpec(name="domain", packages=("myproj.domain",))]
    modules = {
        "myproj.domain.core": _node("myproj.domain.core", "src/myproj/domain/core.py"),
    }
    assert evaluate(layers, modules, {}, escalate_paths=set()) == []


def test_evaluate_target_and_composer_both_empty_no_crash_no_finding() -> None:
    """A defined-but-empty target layer AND an empty composer produce no finding."""
    layers = [
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(
            name="pipelines",
            packages=("myproj.pipelines",),
            composes_all_of=("domain",),
        ),
    ]
    assert evaluate(layers, {}, {}, escalate_paths=set()) == []


def test_evaluate_empty_target_with_populated_composer_no_child_findings() -> None:
    """A defined-but-empty target layer produces no crash and no finding.

    Distinct from the `parse_layers` config-error path (an *undefined* layer
    name) — here `domain` is a valid layer name, it simply has zero modules
    on disk, so every one of `pipelines`'s (populated) direct children is
    skipped rather than reported as a miss.
    """
    layers = [
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(
            name="pipelines",
            packages=("myproj.pipelines",),
            composes_all_of=("domain",),
        ),
    ]
    modules = {
        "myproj.pipelines.x": _node("myproj.pipelines.x", "src/myproj/pipelines/x.py"),
        "myproj.pipelines.y": _node("myproj.pipelines.y", "src/myproj/pipelines/y.py"),
    }
    assert evaluate(layers, modules, {}, escalate_paths=set()) == []


def test_evaluate_multi_prefix_target_satisfied_via_second_prefix_module() -> None:
    """A multi-prefix target layer is satisfied via a module under either prefix."""
    layers = [
        LayerSpec(name="domain", packages=("myproj.domain1", "myproj.domain2")),
        LayerSpec(
            name="pipelines",
            packages=("myproj.pipelines",),
            composes_all_of=("domain",),
        ),
    ]
    modules = {
        "myproj.pipelines.x": _node("myproj.pipelines.x", "src/myproj/pipelines/x.py"),
        "myproj.domain2.core": _node(
            "myproj.domain2.core",
            "src/myproj/domain2/core.py",
        ),
    }
    graph = {
        "myproj.pipelines.x": {"myproj.domain2.core"},
        "myproj.domain2.core": set(),
    }
    assert evaluate(layers, modules, graph, escalate_paths=set()) == []


def test_evaluate_multi_prefix_composer_children_evaluated_independently() -> None:
    """Same-named children under different composer prefixes are evaluated apart."""
    layers = [
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(
            name="pipelines",
            packages=("myproj.p1", "myproj.p2"),
            composes_all_of=("domain",),
        ),
    ]
    modules = {
        "myproj.p1.x": _node("myproj.p1.x", "src/myproj/p1/x.py"),
        "myproj.p2.x": _node("myproj.p2.x", "src/myproj/p2/x.py"),
        "myproj.domain.core": _node("myproj.domain.core", "src/myproj/domain/core.py"),
    }
    graph = {
        "myproj.p1.x": {"myproj.domain.core"},
        "myproj.p2.x": set(),
        "myproj.domain.core": set(),
    }
    findings = evaluate(layers, modules, graph, escalate_paths=set())
    assert len(findings) == 1
    assert findings[0].path == "src/myproj/p2/x.py"


def test_evaluate_exempt_bare_name_covers_same_named_child_under_every_prefix() -> None:
    """A bare exempt name covers a same-named child under every layer prefix."""
    layers = [
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(
            name="pipelines",
            packages=("myproj.p1", "myproj.p2"),
            composes_all_of=("domain",),
            exempt=("legacy",),
        ),
    ]
    modules = {
        "myproj.p1.legacy": _node("myproj.p1.legacy", "src/myproj/p1/legacy.py"),
        "myproj.p2.legacy": _node("myproj.p2.legacy", "src/myproj/p2/legacy.py"),
    }
    findings = evaluate(layers, modules, {}, escalate_paths=set())
    assert len(findings) == 2
    assert all(f.severity is Severity.REVIEW for f in findings)


def test_evaluate_sort_order_high_then_low_then_review_then_path_line() -> None:
    """Findings sort HIGH < LOW < REVIEW, then by (path, line) within a tier."""
    layers = [
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(
            name="a",
            packages=("myproj.a",),
            composes_all_of=("domain",),
            exempt=("z_exempt",),
        ),
    ]
    modules = {
        "myproj.domain.core": _node("myproj.domain.core", "src/myproj/domain/core.py"),
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
        LayerSpec(name="domain", packages=("myproj.domain",)),
        LayerSpec(name="infra", packages=("myproj.infra",)),
        LayerSpec(
            name="pipelines",
            packages=("myproj.pipelines",),
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
# _parse_coverage_config
# ---------------------------------------------------------------------------


def test_parse_coverage_config_defaults_returns_false_empty_no_errors() -> None:
    """An empty table with at least one layer parses to the all-off default."""
    assert layering._parse_coverage_config({}, has_layers=True) == (False, (), [])


def test_parse_coverage_config_non_bool_require_reports_error_and_coerces_false() -> (
    None
):
    """A non-bool `require_all_classified` is rejected, not coerced truthy."""
    require, _allow, errors = layering._parse_coverage_config(
        {"require_all_classified": "yes"},
        has_layers=True,
    )
    assert require is False
    assert errors == [
        "[tool.forge.layering].require_all_classified must be a boolean",
    ]


def test_parse_coverage_config_allow_not_list_reports_error_and_empty_tuple() -> None:
    """A non-list `unclassified_allow` is rejected, coerced to an empty tuple."""
    require, allow, errors = layering._parse_coverage_config(
        {"unclassified_allow": "scripts"},
        has_layers=True,
    )
    assert require is False
    assert allow == ()
    assert errors == [
        "[tool.forge.layering].unclassified_allow must be an array of strings",
    ]


def test_parse_coverage_config_allow_non_string_element_reports_error() -> None:
    """A `unclassified_allow` array with a non-string element is rejected outright."""
    _require, allow, errors = layering._parse_coverage_config(
        {"unclassified_allow": ["scripts", 1]},
        has_layers=True,
    )
    assert allow == ()
    assert errors == [
        "[tool.forge.layering].unclassified_allow must be an array of strings",
    ]


def test_parse_coverage_config_require_true_without_layers_reports_error() -> None:
    """`require_all_classified = true` with no layers configured is an error."""
    require, _allow, errors = layering._parse_coverage_config(
        {"require_all_classified": True},
        has_layers=False,
    )
    assert require is False
    assert errors == [
        (
            "require_all_classified = true needs at least one "
            "[[tool.forge.layering.layer]] — with no layers, nothing "
            "could classify any package"
        ),
    ]


def test_parse_coverage_config_require_true_with_layers_and_valid_allow() -> None:
    """A valid `require_all_classified` + `unclassified_allow` pair parses clean."""
    assert layering._parse_coverage_config(
        {"require_all_classified": True, "unclassified_allow": ["scripts"]},
        has_layers=True,
    ) == (True, ("scripts",), [])


def test_parse_coverage_config_non_bool_require_without_layers_single_error() -> None:
    """Coercing a non-bool `require` to False must not double-fire no-layers too.

    Regression guard: `require and not has_layers` must see the *coerced*
    False, not the original truthy raw value — otherwise a single bad
    config value would report two errors instead of one.
    """
    require, _allow, errors = layering._parse_coverage_config(
        {"require_all_classified": "yes"},
        has_layers=False,
    )
    assert require is False
    assert errors == [
        "[tool.forge.layering].require_all_classified must be a boolean",
    ]


# ---------------------------------------------------------------------------
# _top_level_packages
# ---------------------------------------------------------------------------


def test_top_level_packages_empty_modules_returns_empty_dict() -> None:
    """An empty module set groups to an empty mapping."""
    assert layering._top_level_packages({}) == {}


def test_top_level_packages_nested_modules_group_under_one_top() -> None:
    """Modules sharing a first dotted segment group under that one top."""
    modules = {
        "myproj.a.x": _node("myproj.a.x", "src/myproj/a/x.py"),
        "myproj.a.y": _node("myproj.a.y", "src/myproj/a/y.py"),
    }
    assert layering._top_level_packages(modules) == {
        "myproj": {"myproj.a.x", "myproj.a.y"},
    }


def test_top_level_packages_two_tops_kept_separate() -> None:
    """Modules under distinct first segments group into distinct tops."""
    modules = {
        "myproj.a.x": _node("myproj.a.x", "src/myproj/a/x.py"),
        "other.b.y": _node("other.b.y", "src/other/b/y.py"),
    }
    assert layering._top_level_packages(modules) == {
        "myproj": {"myproj.a.x"},
        "other": {"other.b.y"},
    }


def test_top_level_packages_dotless_module_is_its_own_top() -> None:
    """A dotless module name (no package) is its own top-level entry."""
    modules = {"toplevel": _node("toplevel", "src/toplevel.py")}
    assert layering._top_level_packages(modules) == {"toplevel": {"toplevel"}}


# ---------------------------------------------------------------------------
# _coverage_findings
# ---------------------------------------------------------------------------


def test_coverage_findings_dissolved_package_reports_high_unclassified() -> None:
    """A top-level package no layer prefix reaches is a blocking HIGH finding."""
    layers = [LayerSpec(name="myproj", packages=("myproj",))]
    modules = {
        "myproj.core": _node("myproj.core", "src/myproj/core.py"),
        "orphan.mod": _node("orphan.mod", "src/orphan/mod.py"),
    }
    findings, n_unclassified = layering._coverage_findings(layers, modules, allow=())
    assert n_unclassified == 1
    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert "top-level package 'orphan' is not classified" in findings[0].message


def test_coverage_findings_single_root_shared_first_segment_no_findings() -> None:
    """A layer prefix under the same top as an unrelated sibling classifies both."""
    layers = [LayerSpec(name="pipelines", packages=("myproj.pipelines",))]
    modules = {
        "myproj.pipelines.x": _node("myproj.pipelines.x", "src/myproj/pipelines/x.py"),
        "myproj.other.y": _node("myproj.other.y", "src/myproj/other/y.py"),
    }
    findings, n_unclassified = layering._coverage_findings(layers, modules, allow=())
    assert findings == []
    assert n_unclassified == 0


def test_coverage_findings_allow_listed_reports_review_not_high() -> None:
    """An `unclassified_allow` entry downgrades the finding to REVIEW, non-blocking."""
    modules = {"orphan.mod": _node("orphan.mod", "src/orphan/mod.py")}
    findings, n_unclassified = layering._coverage_findings(
        [],
        modules,
        allow=("orphan",),
    )
    assert n_unclassified == 0
    assert len(findings) == 1
    assert findings[0].severity is Severity.REVIEW
    assert "deliberately unclassified" in findings[0].message


def test_coverage_findings_stale_allow_entry_reports_review_at_pyproject() -> None:
    """An `unclassified_allow` entry matching no discovered package is flagged stale."""
    layers = [LayerSpec(name="myproj", packages=("myproj",))]
    modules = {"myproj.core": _node("myproj.core", "src/myproj/core.py")}
    findings, n_unclassified = layering._coverage_findings(
        layers,
        modules,
        allow=("ghost",),
    )
    assert n_unclassified == 0
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.REVIEW
    assert finding.path == "pyproject.toml"
    assert finding.line == 1
    assert "stale — remove it" in finding.message


def test_coverage_findings_redundant_allow_entry_reports_review_at_pyproject() -> None:
    """An `unclassified_allow` entry naming an already-classified package is redundant.

    Distinct from the stale case (matches no package at all): here the
    package exists and IS classified, so the opt-out never does anything —
    flagged separately so the two dead-entry causes aren't conflated.
    """
    layers = [LayerSpec(name="myproj", packages=("myproj",))]
    modules = {"myproj.core": _node("myproj.core", "src/myproj/core.py")}
    findings, n_unclassified = layering._coverage_findings(
        layers,
        modules,
        allow=("myproj",),
    )
    assert n_unclassified == 0
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.REVIEW
    assert finding.path == "pyproject.toml"
    assert finding.line == 1
    assert "redundant — remove it" in finding.message


def test_coverage_findings_fully_classified_returns_empty() -> None:
    """A tree where every top-level package is classified reports nothing."""
    layers = [LayerSpec(name="myproj", packages=("myproj",))]
    modules = {"myproj.core": _node("myproj.core", "src/myproj/core.py")}
    assert layering._coverage_findings(layers, modules, allow=()) == ([], 0)


def test_coverage_findings_anchor_is_alphabetically_first_module_path() -> None:
    """The finding's anchor path is the top's alphabetically-first module."""
    modules = {
        "orphan.b": _node("orphan.b", "src/orphan/b.py"),
        "orphan.a": _node("orphan.a", "src/orphan/a.py"),
    }
    findings, _n_unclassified = layering._coverage_findings([], modules, allow=())
    assert findings[0].path == "src/orphan/a.py"


def test_coverage_findings_two_unclassified_reports_two_high() -> None:
    """Two unrelated unclassified top-level packages each get their own HIGH."""
    modules = {
        "a.mod": _node("a.mod", "src/a/mod.py"),
        "b.mod": _node("b.mod", "src/b/mod.py"),
    }
    findings, n_unclassified = layering._coverage_findings([], modules, allow=())
    assert n_unclassified == 2
    assert len(findings) == 2
    assert all(f.severity is Severity.HIGH for f in findings)


def test_coverage_findings_sorted_by_top_level_name() -> None:
    """Findings are emitted in sorted top-level-package order, not discovery order."""
    modules = {
        "zebra.mod": _node("zebra.mod", "src/zebra/mod.py"),
        "alpha.mod": _node("alpha.mod", "src/alpha/mod.py"),
    }
    findings, _n_unclassified = layering._coverage_findings([], modules, allow=())
    assert [f.path for f in findings] == ["src/alpha/mod.py", "src/zebra/mod.py"]


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_no_config_logs_nothing_to_enforce_and_skips_added_or_moved_files(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured layers exits 0 and never calls the git-touching seams."""
    write_pyproject(fake_repo, NO_LAYERING_TOML)
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
    write_pyproject(fake_repo, BAD_LAYER_TOML)
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


def test_run_layer_matching_zero_modules_reports_high_config_error(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A layer whose prefix matches no modules is one loud config error."""
    write_pyproject(fake_repo, SINGLE_LAYER_NO_MODULES_TOML)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert "[HIGH] pyproject.toml:1" in log_text
    assert "layer 'domain' matches no modules under 'myproj.domain'" in log_text
    assert code == 1


def test_run_multi_prefix_layer_matching_zero_modules_lists_all_prefixes(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-prefix layer matching nothing lists every configured prefix."""
    write_pyproject(fake_repo, MULTI_PREFIX_NO_MODULES_TOML)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert "matches no modules under 'myproj.a', 'myproj.b'" in log_text
    assert code == 1


def test_run_empty_target_layer_produces_single_config_error_and_no_child_findings(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty target layer yields one config error, not N per-child misses."""
    write_pyproject(fake_repo, EMPTY_DOMAIN_POPULATED_PIPELINES_TOML)
    _write(fake_repo / "src" / "myproj" / "pipelines" / "jobx.py", PLAIN_MODULE)
    _write(fake_repo / "src" / "myproj" / "pipelines" / "joby.py", PLAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert "layer 'domain' matches no modules under 'myproj.domain'" in log_text
    assert "[LOW]" not in log_text
    assert "[HIGH] src/myproj/pipelines" not in log_text
    assert "1 config error(s)." in log_text
    assert "0 blocking violation(s)" in log_text
    assert code == 1


def test_run_layer_matching_at_least_one_module_no_config_error(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A layer whose prefix matches at least one module reports no config error."""
    write_pyproject(fake_repo, SINGLE_LAYER_NO_MODULES_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert "matches no modules" not in log_text
    assert code == 0


def test_run_changed_scope_filters_low_keeps_high(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGED scope drops an unchanged LOW finding but keeps an added HIGH one."""
    write_pyproject(fake_repo, TWO_LAYER_TOML)
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
    write_pyproject(fake_repo, BAD_LAYER_TOML)
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
    """A HIGH finding survives the CHANGED filter regardless of anchor path.

    The finding's anchor is the child's alphabetically-first module ("a"),
    but only the non-anchor module ("b") is added/moved. With the
    child-membership filter, the escalated member also satisfies
    membership, so today TWO conditions keep this finding alive; the
    severity OR-clause remains as deliberate defense-in-depth for any
    future refactor that decouples the escalate set from the changed set.
    This test pins the outcome (HIGH survives), not which clause fires.
    """
    write_pyproject(fake_repo, TWO_LAYER_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
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
    write_pyproject(fake_repo, TWO_LAYER_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(fake_repo / "src" / "myproj" / "pipelines" / "good.py", GOOD_PIPELINE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0
    assert "# findings: 0" in log_text


def test_run_type_checking_only_import_does_not_satisfy_composes_all_of(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TYPE_CHECKING-only import must not satisfy `composes_all_of`.

    It never runs, so counting it would be the silent-false-pass case the
    default (`include_type_checking=False`) guards against.
    """
    write_pyproject(fake_repo, TWO_LAYER_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(
        fake_repo / "src" / "myproj" / "pipelines" / "typeonly.py",
        TYPE_CHECKING_ONLY_PIPELINE,
    )
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0  # LOW (baseline) findings never block
    assert "[LOW] src/myproj/pipelines/typeonly.py" in log_text
    assert "does not compose layer 'domain'" in log_text


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


def test_run_changed_scope_low_survives_via_non_anchor_child_member(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing a non-anchor module of a violating child keeps its LOW finding.

    Regression: the CHANGED filter compared only the finding's anchor path
    (the child's alphabetically-first module) against the changed set, so a
    baseline finding vanished whenever the developer touched any *other*
    module of the same child. Membership is per child, mirroring
    forge-audit-dup's prior-art semantics.
    """
    write_pyproject(fake_repo, TWO_LAYER_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(
        fake_repo / "src" / "myproj" / "pipelines" / "jobs" / "alpha.py",
        PLAIN_MODULE,
    )
    _write(
        fake_repo / "src" / "myproj" / "pipelines" / "jobs" / "beta.py",
        PLAIN_MODULE,
    )
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    monkeypatch.setattr(
        layering,
        "select_diff_files",
        lambda _root: ["src/myproj/pipelines/jobs/beta.py"],
    )
    code = run(Scope.CHANGED, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert "[LOW]" in log_text
    assert "child 'jobs'" in log_text
    assert code == 0


# ---------------------------------------------------------------------------
# run — require_all_classified coverage gate
# ---------------------------------------------------------------------------


REQUIRE_NO_LAYERS_TOML = "[tool.forge.layering]\nrequire_all_classified = true\n"

REQUIRE_DISSOLVE_TOML = (
    "[tool.forge.layering]\n"
    "require_all_classified = true\n"
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "domain"\n'
    'package = "myproj.domain"\n'
)

REQUIRE_ALLOW_TOML = (
    "[tool.forge.layering]\n"
    "require_all_classified = true\n"
    'unclassified_allow = ["scripts"]\n'
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "domain"\n'
    'package = "myproj.domain"\n'
)

REQUIRE_STALE_ALLOW_TOML = (
    "[tool.forge.layering]\n"
    "require_all_classified = true\n"
    'unclassified_allow = ["ghost"]\n'
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "domain"\n'
    'package = "myproj.domain"\n'
)

LAYER_NO_REQUIRE_TOML = (
    '[[tool.forge.layering.layer]]\nname = "domain"\npackage = "myproj.domain"\n'
)

TWO_LAYER_REQUIRE_TOML = (
    "[tool.forge.layering]\n"
    "require_all_classified = true\n"
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "domain"\n'
    'package = "myproj.domain"\n'
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "pipelines"\n'
    'package = "myproj.pipelines"\n'
    'composes_all_of = ["domain"]\n'
)


def test_run_require_without_layers_exits_one_with_needs_at_least_one(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`require_all_classified = true` with zero layers is a config error.

    Not a no-op.
    """
    write_pyproject(fake_repo, REQUIRE_NO_LAYERS_TOML)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 1
    assert "require_all_classified = true needs at least one" in log_text
    assert "nothing to enforce" not in log_text


def test_run_dissolved_package_reports_high_unclassified_and_exits_one(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package promoted to a new top-level prefix with no layer is caught."""
    write_pyproject(fake_repo, REQUIRE_DISSOLVE_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(fake_repo / "src" / "orphan" / "mod.py", PLAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 1
    assert "[HIGH]" in log_text
    assert "'orphan' is not classified" in log_text


def test_run_single_root_layer_prefix_classifies_sibling_package(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A layer prefix under the same top as an unrelated sibling classifies both.

    Single-root layout: `myproj.pipelines` shares its first dotted segment
    with `myproj.other`, so `myproj.other` never needs its own layer entry
    just to satisfy the coverage gate.
    """
    write_pyproject(
        fake_repo,
        "[tool.forge.layering]\n"
        "require_all_classified = true\n"
        "\n"
        "[[tool.forge.layering.layer]]\n"
        'name = "pipelines"\n'
        'package = "myproj.pipelines"\n',
    )
    _write(fake_repo / "src" / "myproj" / "pipelines" / "x.py", PLAIN_MODULE)
    _write(fake_repo / "src" / "myproj" / "other" / "y.py", PLAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0
    assert "is not classified" not in log_text


def test_run_allow_listed_package_reports_review_and_exits_zero(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `unclassified_allow` entry keeps the gate green with a visible REVIEW."""
    write_pyproject(fake_repo, REQUIRE_ALLOW_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(fake_repo / "src" / "scripts" / "tool.py", PLAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0
    assert "[REVIEW]" in log_text
    assert "deliberately unclassified" in log_text
    assert "unclassified package(s)." not in log_text


def test_run_stale_allow_entry_reports_review_and_exits_zero(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale `unclassified_allow` entry (matches nothing) is a non-blocking REVIEW."""
    write_pyproject(fake_repo, REQUIRE_STALE_ALLOW_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0
    assert "stale — remove it" in log_text


def test_run_composition_and_coverage_high_findings_both_counted_in_summary(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composition HIGH and a coverage HIGH are both reflected, distinctly.

    In the summary.
    """
    write_pyproject(fake_repo, TWO_LAYER_REQUIRE_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    bad_pipeline = fake_repo / "src" / "myproj" / "pipelines" / "bad.py"
    _write(bad_pipeline, PLAIN_MODULE)
    _write(fake_repo / "src" / "orphan" / "mod.py", PLAIN_MODULE)
    monkeypatch.setattr(
        layering,
        "added_or_moved_files",
        lambda **_kw: ["src/myproj/pipelines/bad.py"],
    )
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 1
    assert "1 blocking violation(s) on added/moved modules" in log_text
    assert "1 unclassified package(s)." in log_text


def test_run_changed_scope_drops_untouched_allow_review(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGED scope drops an allow-listed REVIEW whose package was not touched."""
    write_pyproject(fake_repo, REQUIRE_ALLOW_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(fake_repo / "src" / "scripts" / "tool.py", PLAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    monkeypatch.setattr(layering, "select_diff_files", lambda _root: [])
    code = run(Scope.CHANGED, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0
    assert "deliberately unclassified" not in log_text


def test_run_changed_scope_keeps_touched_allow_review(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGED scope keeps an allow-listed REVIEW whose package WAS touched."""
    write_pyproject(fake_repo, REQUIRE_ALLOW_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    scripts_path = fake_repo / "src" / "scripts" / "tool.py"
    _write(scripts_path, PLAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    monkeypatch.setattr(
        layering,
        "select_diff_files",
        lambda _root: ["src/scripts/tool.py"],
    )
    code = run(Scope.CHANGED, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0
    assert "deliberately unclassified" in log_text


def test_run_changed_scope_keeps_unclassified_high_with_empty_diff(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking unclassified HIGH survives CHANGED filtering even with no diff.

    Mirrors the config-error regression guard: HIGH findings always survive
    the CHANGED-scope filter, whether or not anything was actually touched.
    """
    write_pyproject(fake_repo, REQUIRE_DISSOLVE_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(fake_repo / "src" / "orphan" / "mod.py", PLAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    monkeypatch.setattr(layering, "select_diff_files", lambda _root: [])
    code = run(Scope.CHANGED, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 1
    assert "'orphan' is not classified" in log_text


def test_run_flag_off_default_ignores_unclassified_package(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `require_all_classified` unset (default off).

    Coverage is never evaluated.
    """
    write_pyproject(fake_repo, LAYER_NO_REQUIRE_TOML)
    _write(fake_repo / "src" / "myproj" / "domain" / "core.py", DOMAIN_MODULE)
    _write(fake_repo / "src" / "orphan" / "mod.py", PLAIN_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    code = run(Scope.FULL, [fake_repo / "src"], LayeringConfig())
    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert code == 0
    assert "is not classified" not in log_text
    assert "unclassified" not in log_text


def test_summary_separates_config_errors_from_blocking_violations() -> None:
    """The summary never misattributes config errors as added/moved blocks.

    Mirrors dup's direct `_summary` tests: with one real HIGH violation and
    one config-error HIGH in the list, the blocking count stays 1 and the
    config errors get their own clause.
    """
    findings = [
        layering.Finding(
            audit="layering",
            severity=Severity.HIGH,
            path="src/p/child.py",
            line=1,
            message="does not compose layer 'domain' [added/moved module]",
        ),
        layering.Finding(
            audit="layering",
            severity=Severity.HIGH,
            path="pyproject.toml",
            line=1,
            message="layer 'a': composes_all_of names undefined layer 'x'",
        ),
        layering.Finding(
            audit="layering",
            severity=Severity.LOW,
            path="src/p/other.py",
            line=1,
            message="does not compose layer 'domain'",
        ),
    ]
    summary = layering._summary(2, 3, findings, n_config_errors=1)
    assert "1 blocking violation(s) on added/moved modules" in summary
    assert "1 pre-existing" in summary
    assert "1 config error(s)." in summary

    # Both clauses non-zero together: n_block must subtract each exactly
    # once, not double-subtract one HIGH finding counted under both.
    findings_both = [
        *findings,
        layering.Finding(
            audit="layering",
            severity=Severity.HIGH,
            path="src/orphan/mod.py",
            line=1,
            message="top-level package 'orphan' is not classified",
        ),
    ]
    summary_both = layering._summary(
        2,
        3,
        findings_both,
        n_config_errors=1,
        n_unclassified=1,
    )
    assert "1 blocking violation(s) on added/moved modules" in summary_both
    assert "1 config error(s)." in summary_both
    assert "1 unclassified package(s)." in summary_both


# ---------------------------------------------------------------------------
# main — root resolution (regression #295: mirrored test packages evaluated
# as layer children when DEFAULT_ROOTS included test dirs)
# ---------------------------------------------------------------------------


ZEROSHOT_MODULE = (
    '"""Zero-shot module composing nothing (regression #295 shape)."""\n\nVALUE = 1\n'
)

BIOSEQ_CORE_MODULE = '"""bioseq target-layer module."""\n\nVALUE = 1\n'

ZEROSHOT_BIOSEQ_TOML = (
    "[tool.forge]\n"
    'source_dirs = ["src"]\n'
    'test_dirs = ["test"]\n'
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "bioseq"\n'
    'package = "bioseq"\n'
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "zeroshot"\n'
    'package = "zeroshot"\n'
    'composes_all_of = ["bioseq"]\n'
)

ZEROSHOT_BIOSEQ_PATHS_TOML = (
    "[tool.forge.layering]\n"
    'paths = ["codebase"]\n'
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "bioseq"\n'
    'package = "bioseq"\n'
    "\n"
    "[[tool.forge.layering.layer]]\n"
    'name = "zeroshot"\n'
    'package = "zeroshot"\n'
    'composes_all_of = ["bioseq"]\n'
)


def test_main_no_roots_excludes_mirrored_test_package(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``--roots``: only source-side modules are evaluated as layer children.

    Regression: DEFAULT_ROOTS included test dirs, so a `test/` package
    mirroring a source namespace (`test/zeroshot/`) was scanned alongside
    `src/zeroshot/` and evaluated as a `zeroshot` layer child too, producing
    spurious findings anchored at a test path. `main()` now routes a
    roots-less invocation through `resolve_tool_roots(root, "layering")`
    (source_dirs only, no `include_tests`), so `test/zeroshot/helper.py` is
    never scanned — while the genuine src-side violation (`zeroshot` not
    composing `bioseq`) still surfaces.
    """
    write_pyproject(fake_repo, ZEROSHOT_BIOSEQ_TOML)
    _write(fake_repo / "src" / "zeroshot" / "mod.py", ZEROSHOT_MODULE)
    _write(fake_repo / "src" / "bioseq" / "core.py", BIOSEQ_CORE_MODULE)
    _write(fake_repo / "test" / "zeroshot" / "helper.py", ZEROSHOT_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    monkeypatch.setattr(sys, "argv", ["forge-audit-layering"])

    assert main() == 0

    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert "test/zeroshot" not in log_text
    assert "[LOW] src/zeroshot/mod.py" in log_text
    assert "does not compose layer 'bioseq'" in log_text


def test_main_explicit_roots_overrides_source_only_resolution(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``--roots test`` stays the highest override, above resolve_tool_roots.

    Same tree as the no-``--roots`` regression case, but pointed explicitly
    at `test/`: the mirrored `test/zeroshot/helper.py` module IS scanned and
    evaluated as a layer child, proving `--roots` bypasses the source-only
    routing entirely rather than being merged with it.
    """
    write_pyproject(fake_repo, ZEROSHOT_BIOSEQ_TOML)
    _write(fake_repo / "src" / "zeroshot" / "mod.py", ZEROSHOT_MODULE)
    _write(fake_repo / "src" / "bioseq" / "core.py", BIOSEQ_CORE_MODULE)
    _write(fake_repo / "test" / "zeroshot" / "helper.py", ZEROSHOT_MODULE)
    _write(fake_repo / "test" / "bioseq" / "core.py", BIOSEQ_CORE_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    monkeypatch.setattr(sys, "argv", ["forge-audit-layering", "--roots", "test"])

    assert main() == 0

    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert "[LOW] test/zeroshot/helper.py" in log_text
    assert "does not compose layer 'bioseq'" in log_text


def test_main_no_roots_granular_paths_key_beats_auto_detect(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[tool.forge.layering].paths`` is honored with ``source_dirs`` unset.

    `resolve_tool_roots`'s granular per-tool override outranks
    `[tool.forge].source_dirs` / auto-detect. Here `source_dirs` is unset
    and the fixture-created (empty) `src/` is the only thing auto-detect
    would find, so a finding anchored under `codebase/` — a directory name
    auto-detect never considers — is only possible if the resolution
    actually reached the `paths = ["codebase"]` key.
    """
    write_pyproject(fake_repo, ZEROSHOT_BIOSEQ_PATHS_TOML)
    _write(fake_repo / "codebase" / "zeroshot" / "mod.py", ZEROSHOT_MODULE)
    _write(fake_repo / "codebase" / "bioseq" / "core.py", BIOSEQ_CORE_MODULE)
    monkeypatch.setattr(layering, "added_or_moved_files", lambda **_kw: [])
    monkeypatch.setattr(sys, "argv", ["forge-audit-layering"])

    assert main() == 0

    log_text = (fake_repo / "code_health" / "audit_layering.log").read_text(
        encoding="utf-8",
    )
    assert "[LOW] codebase/zeroshot/mod.py" in log_text
    assert "does not compose layer 'bioseq'" in log_text
