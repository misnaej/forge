"""Tests for the forge-gen-c4 C4 / Structurizr DSL generator."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from forge.gen_c4 import (
    _BROWSER_APP_PATHS,
    _BROWSER_ENV,
    _EDGE_MODES,
    DEFAULT_PDF_OUTPUT,
    MERMAID_ELK_JS_NAME,
    MERMAID_JS_NAME,
    README_C4_END,
    README_C4_START,
    C4Config,
    Component,
    Container,
    External,
    Person,
    Relationship,
    RenderConfig,
    _build_views,
    _copy_vendored_mermaid,
    _derive_container_edges,
    _edge_endpoints,
    _emit_pdf,
    _external_node_line,
    _externals_with_declared_incoming,
    _find_headless_browser,
    _html_interaction_css,
    _html_interaction_script,
    _IdMaps,
    _includes_derived,
    _mermaid_box,
    _mermaid_init_options,
    _parse_render_config,
    _pdf_page_geometry,
    _print_html_to_pdf,
    _print_page_css,
    _render_mermaid_components_for,
    _render_mermaid_containers,
    _render_mermaid_system_context,
    _resolve_direction,
    _resolve_edge_mode,
    _resolve_endpoint,
    _slug,
    _under_prefix,
    _warn_unknown_relationships,
    assign_components,
    derive_component_edges,
    generate,
    load_c4_config,
    main,
    render_dsl,
    render_html,
    render_mermaid,
    render_readme_block,
    resolve_model_section,
    sync_readme,
)


if TYPE_CHECKING:
    from pathlib import Path


# A minimal standalone c4.toml model used across the file-loading tests.
SAMPLE_MODEL = """\
system = "Demo"
description = "A demo system"
output = "docs/architecture.dsl"

[[person]]
name = "User"
description = "Uses the system"
uses = "operates"

[[external]]
name = "GitHub"
description = "Hosts repos"
relationship = "reads via gh"

[[container]]
name = "app"
technology = "Python"
description = "The app"

[components]
"Core" = ["demo.core"]
"IO" = ["demo.io"]

[[relationship]]
source = "Core"
destination = "IO"
description = "writes via subprocess"
"""


def _write_pyproject(root: Path, body: str) -> None:
    """Write a ``pyproject.toml`` with *body* under the repo root.

    Args:
        root: Temporary repo root directory.
        body: TOML text to write verbatim.
    """
    (root / "pyproject.toml").write_text(body)


def test_slug_makes_safe_identifiers() -> None:
    """_slug lowercases, replaces non-alphanumerics, and avoids leading digits."""
    assert _slug("Pre-commit dispatcher") == "pre_commit_dispatcher"
    assert _slug("Config + shared") == "config_shared"
    assert _slug("4 horsemen").startswith("x")
    assert _slug("***") == ""


def test_under_prefix_respects_dotted_boundaries() -> None:
    """_under_prefix matches a prefix and its dotted children, not lexical ones."""
    assert _under_prefix("forge.audit", "forge.audit")
    assert _under_prefix("forge.audit.deps", "forge.audit")
    assert not _under_prefix("forge.auditor", "forge.audit")


def test_assign_components_longest_prefix_wins() -> None:
    """A more specific prefix claims a module over a broader one."""
    components = (
        Component("Broad", ("demo",)),
        Component("Specific", ("demo.io",)),
    )
    assigned, unmatched = assign_components(
        ["demo.io.reader", "demo.core", "other.thing"],
        components,
    )
    assert assigned["demo.io.reader"] == "Specific"
    assert assigned["demo.core"] == "Broad"
    assert unmatched == ["other.thing"]


def test_derive_component_edges_collapses_module_graph() -> None:
    """Module edges collapse to component edges, dropping self and unassigned."""
    graph = {
        "demo.core": {"demo.io", "demo.core.util"},
        "demo.io": set(),
        "demo.core.util": {"demo.io"},
    }
    assigned = {
        "demo.core": "Core",
        "demo.core.util": "Core",
        "demo.io": "IO",
    }
    edges = derive_component_edges(graph, assigned)
    assert edges == {("Core", "IO")}


def test_resolve_model_section_prefers_external_file(tmp_path: Path) -> None:
    """An explicit config path is read instead of the inline section."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    section = resolve_model_section(tmp_path)
    assert section is not None
    assert section["system"] == "Demo"


def test_resolve_model_section_none_when_unconfigured(tmp_path: Path) -> None:
    """No [tool.forge.c4], no c4.toml → not opted in."""
    _write_pyproject(tmp_path, "[tool.forge]\n")
    assert resolve_model_section(tmp_path) is None


def test_load_c4_config_parses_external_file(tmp_path: Path) -> None:
    """load_c4_config builds the full skeleton from a standalone c4.toml."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    config = load_c4_config(tmp_path)
    assert config is not None
    assert config.system == "Demo"
    assert [p.name for p in config.persons] == ["User"]
    assert config.relationships == (
        Relationship("Core", "IO", "writes via subprocess"),
    )
    assert {c.name for c in config.components} == {"Core", "IO"}


def test_render_dsl_emits_valid_workspace_skeleton(tmp_path: Path) -> None:
    """render_dsl produces a workspace with model, relationships, and views."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    config = load_c4_config(tmp_path)
    assert config is not None
    dsl = render_dsl(config, {("IO", "Core")})
    assert dsl.startswith('workspace "Demo"')
    assert "model {" in dsl
    assert "views {" in dsl
    # Human-declared edge present with its rich label.
    assert "writes via subprocess" in dsl
    assert dsl.endswith("}\n")


def test_render_dsl_suppresses_derived_edge_duplicating_declared(
    tmp_path: Path,
) -> None:
    """A derived edge equal to a declared (source,dest) pair is not doubled."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    config = load_c4_config(tmp_path)
    assert config is not None
    # Declared Core->IO; a derived Core->IO must not add a second arrow.
    dsl = render_dsl(config, {("Core", "IO")})
    assert dsl.count("-> io ") == 1 or dsl.count("core -> io") == 1


# --- #106: per-component container assignment ---

_TWO_CONTAINERS = (
    Container("Applications", "Python", ""),
    Container("Domain libraries", "Python", ""),
)


def _two_container_config(components: tuple[Component, ...]) -> C4Config:
    """Build a 2-container C4Config with the given components.

    Args:
        components: Components to place in the model.

    Returns:
        A minimal two-container :class:`C4Config`.
    """
    return C4Config(
        system="Demo",
        description="",
        output="docs/architecture.dsl",
        containers=_TWO_CONTAINERS,
        components=components,
    )


def test_components_render_in_their_named_container() -> None:
    """Each component renders inside the container its ``container`` field names."""
    config = _two_container_config(
        (
            Component("Leaderboards", ("demo.app",), container="Applications"),
            Component("Core data", ("demo.core",), container="Domain libraries"),
        )
    )
    dsl = render_dsl(config, set())
    app = dsl.index('container "Applications"')
    dom = dsl.index('container "Domain libraries"')
    lead = dsl.index('component "Leaderboards"')
    core = dsl.index('component "Core data"')
    # Leaderboards nests under Applications (before Domain libraries opens);
    # Core data nests under Domain libraries — N populated containers, not one.
    assert app < lead < dom < core


def test_component_without_container_defaults_to_first() -> None:
    """A component with no ``container`` attaches to the first declared container."""
    config = _two_container_config((Component("Orphan", ("demo.x",)),))
    dsl = render_dsl(config, set())
    app = dsl.index('container "Applications"')
    dom = dsl.index('container "Domain libraries"')
    orphan = dsl.index('component "Orphan"')
    assert app < orphan < dom  # inside the first (Applications) block


def test_empty_container_equals_explicit_first_byte_identical() -> None:
    """Omitting ``container`` is byte-identical to naming the first container."""
    omitted = _two_container_config(
        (Component("A", ("demo.a",)), Component("B", ("demo.b",)))
    )
    explicit = _two_container_config(
        (
            Component("A", ("demo.a",), container="Applications"),
            Component("B", ("demo.b",), container="Applications"),
        )
    )
    assert render_dsl(omitted, set()) == render_dsl(explicit, set())


def test_empty_container_equals_explicit_first_mermaid_byte_identical() -> None:
    """The Mermaid renderers default ``container`` exactly like the DSL path.

    Guards the per-view + flat Mermaid output against a refactor that inlines
    the first-container defaulting instead of routing through
    ``_components_for_container`` — a divergence the DSL-only byte-identical
    test would miss.
    """
    omitted = _two_container_config(
        (Component("A", ("demo.a",)), Component("B", ("demo.b",)))
    )
    explicit = _two_container_config(
        (
            Component("A", ("demo.a",), container="Applications"),
            Component("B", ("demo.b",), container="Applications"),
        )
    )
    assert render_mermaid(omitted, set()) == render_mermaid(explicit, set())
    first = explicit.containers[0]
    assert _render_mermaid_components_for(
        omitted, first, 0, set()
    ) == _render_mermaid_components_for(explicit, first, 0, set())


def test_cross_container_edge_renders() -> None:
    """An import edge between components in different containers still renders."""
    config = _two_container_config(
        (
            Component("App", ("demo.app",), container="Applications"),
            Component("Core", ("demo.core",), container="Domain libraries"),
        )
    )
    dsl = render_dsl(config, {("App", "Core")})
    assert "app -> core" in dsl


def test_unknown_container_fails_loudly(tmp_path: Path) -> None:
    """A component naming an undeclared container raises a clear ValueError."""
    model = (
        'system = "Demo"\n'
        "[[container]]\n"
        'name = "Applications"\n'
        "[[component]]\n"
        'name = "Stray"\n'
        'container = "Nope"\n'
        'modules = ["demo.x"]\n'
    )
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(model)
    with pytest.raises(ValueError, match=r"Stray.*Nope"):
        load_c4_config(tmp_path)


def test_duplicate_container_name_fails_loudly(tmp_path: Path) -> None:
    """Two containers sharing a name raise a clear ValueError (no silent merge)."""
    model = (
        'system = "Demo"\n[[container]]\nname = "Dup"\n[[container]]\nname = "Dup"\n'
    )
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(model)
    with pytest.raises(ValueError, match=r"duplicate container.*Dup"):
        load_c4_config(tmp_path)


def test_render_dsl_emits_a_component_view_per_container() -> None:
    """Each declared container gets its own component view, not just the first."""
    config = _two_container_config(
        (
            Component("App", ("demo.app",), container="Applications"),
            Component("Lib", ("demo.lib",), container="Domain libraries"),
        )
    )
    dsl = render_dsl(config, set())
    assert "component applications " in dsl
    assert "component domain_libraries " in dsl


def test_render_mermaid_routes_components_to_correct_subgraph() -> None:
    """Mermaid places each component inside its own container's subgraph."""
    config = _two_container_config(
        (
            Component("App", ("demo.app",), container="Applications"),
            Component("Lib", ("demo.lib",), container="Domain libraries"),
        )
    )
    mermaid = render_mermaid(config, set())
    apps = mermaid.index("subgraph applications")
    dom = mermaid.index("subgraph domain_libraries")
    app_node = mermaid.index('app["')
    lib_node = mermaid.index('lib["')
    assert apps < app_node < dom < lib_node


def test_generate_returns_none_without_config(tmp_path: Path) -> None:
    """Generate signals opt-out by returning None when unconfigured."""
    _write_pyproject(tmp_path, "[tool.forge]\n")
    assert generate(tmp_path, []) is None


def test_main_writes_then_checks_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Main writes the DSL, and a subsequent --check reports in sync.

    SCENARIO: a tiny two-module package with a configured C4 model.
    EXPECTED BEHAVIOR: first run writes docs/architecture.dsl (exit 0),
    and --check on the unchanged file also exits 0.
    """
    pkg = tmp_path / "src" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("from demo import io\n")
    (pkg / "io.py").write_text("X = 1\n")
    _write_pyproject(
        tmp_path,
        '[tool.forge]\nsource_dirs = ["src"]\n\n[tool.forge.c4]\nconfig = "c4.toml"\n',
    )
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    monkeypatch.setattr("forge.gen_c4.repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-gen-c4"])

    assert main() == 0
    assert (tmp_path / "docs" / "architecture.dsl").is_file()

    monkeypatch.setattr("sys.argv", ["forge-gen-c4", "--check"])
    assert main() == 0


RICH_MODEL = """\
system = "Demo"
output = "docs/architecture.dsl"
readme = "README.md"

[[container]]
name = "app"
technology = "Python"
description = "The app"

[[component]]
name = "Core"
technology = "Python"
description = "Does the core work"
modules = ["demo.core"]

[[component]]
name = "IO"
description = "Reads and writes"
modules = ["demo.io"]
"""


TWO_CONTAINER_MODEL = """\
system = "Demo"
description = "A demo system"
output = "docs/architecture.dsl"

[[container]]
name = "Applications"
technology = "Python"
description = "Application layer"

[[container]]
name = "Domain libraries"
technology = "Python"
description = "Library layer"

[[component]]
name = "App"
modules = ["demo.app"]
container = "Applications"

[[component]]
name = "Lib"
modules = ["demo.lib"]
container = "Domain libraries"
"""


def test_rich_component_carries_description_and_technology(tmp_path: Path) -> None:
    """A [[component]] table populates description + technology on the box."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(RICH_MODEL)
    config = load_c4_config(tmp_path)
    assert config is not None
    core = next(c for c in config.components if c.name == "Core")
    assert core.description == "Does the core work"
    assert core.technology == "Python"
    assert core.prefixes == ("demo.core",)


def test_render_mermaid_is_canonical_with_labeled_edges(tmp_path: Path) -> None:
    """Mermaid uses literal tags, bold boxes, and labels every edge."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    config = load_c4_config(tmp_path)
    assert config is not None
    mermaid = render_mermaid(config, {("IO", "Core")})
    assert mermaid.startswith("graph LR")
    assert "<b>Core</b>" in mermaid  # literal tag, not entity-escaped
    assert '-->|"imports"|' in mermaid  # derived edge labeled
    assert '-->|"writes via subprocess"|' in mermaid  # declared edge label


def test_render_html_escapes_mermaid_for_pre_block(tmp_path: Path) -> None:
    """The HTML <pre> double-escapes the Mermaid so textContent decodes back."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    config = load_c4_config(tmp_path)
    assert config is not None
    page = render_html(config, [("System Context", render_mermaid(config, set()))])
    assert '<pre class="mermaid">' in page
    assert 'src="mermaid.min.js"' in page
    assert "&lt;b&gt;" in page  # literal <b> escaped for the <pre>


def test_render_html_has_one_tab_per_view() -> None:
    """render_html emits one button and one pane per view, indexed from 0."""
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("A", "graph LR\n"), ("B", "graph LR\n")])
    assert 'data-pane="0">A</button>' in page
    assert 'data-pane="1">B</button>' in page
    assert page.count('<pre class="mermaid">') == 2
    assert page.count("data-pane=") == 4  # 2 buttons + 2 panes paired


def test_render_mermaid_system_context_excludes_containers_and_components(
    tmp_path: Path,
) -> None:
    """System Context renders persons, system, and externals; no containers.

    The Actors subgraph is expected (Feature E), but container and component
    boxes must not appear at this level.
    """
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    config = load_c4_config(tmp_path)
    assert config is not None
    result = _render_mermaid_system_context(config)
    assert "User" in result  # person name present
    assert "GitHub" in result  # external name present
    assert '["Actors"]' in result  # actors subgraph bands persons (Feature E)
    assert "Core" not in result  # component name absent from system context
    assert "IO" not in result  # component name absent from system context
    assert "app" not in result  # container node absent from system context


def test_render_mermaid_containers_excludes_components() -> None:
    """Container view renders containers; excludes components and nested subgraph."""
    config = _two_container_config(
        (
            Component("CoreModule", ("demo.app",), container="Applications"),
            Component("LibModule", ("demo.lib",), container="Domain libraries"),
        )
    )
    mermaid = _render_mermaid_containers(config, set())
    assert "Applications" in mermaid
    assert "Domain libraries" in mermaid
    assert "CoreModule" not in mermaid
    assert "LibModule" not in mermaid
    assert mermaid.count("subgraph") == 1  # only the system boundary


def test_derive_container_edges_collapses_cross_container_component_edges() -> None:
    """Component edges map to container pairs; same-container edges are dropped."""
    components = (
        Component("Alpha", ("demo.alpha",), container="Applications"),
        Component("Beta", ("demo.beta",), container="Applications"),
        Component("Gamma", ("demo.gamma",), container="Domain libraries"),
    )
    config = _two_container_config(components)
    # Derived import edge crosses containers → mapped to container pair.
    cross = _derive_container_edges(config, {("Alpha", "Gamma")})
    assert cross == {("Applications", "Domain libraries")}
    # Same-container derived edge is dropped.
    same = _derive_container_edges(config, {("Alpha", "Beta")})
    assert same == set()
    # Declared Relationship spanning containers contributes the pair even when edges={}.
    config_with_rel = C4Config(
        system="Demo",
        description="",
        output="docs/architecture.dsl",
        containers=_TWO_CONTAINERS,
        components=components,
        relationships=(Relationship("Alpha", "Gamma", "calls"),),
    )
    declared = _derive_container_edges(config_with_rel, set())
    assert declared == {("Applications", "Domain libraries")}


def test_render_mermaid_components_for_shows_only_container_scope() -> None:
    """Component view for a container renders only that container's nodes and edges."""
    config = _two_container_config(
        (
            Component("CoreModule", ("demo.app",), container="Applications"),
            Component("CoreModule2", ("demo.app2",), container="Applications"),
            Component("LibModule", ("demo.lib",), container="Domain libraries"),
        )
    )
    # Nodes: container-0's own components appear; container-1's are absent.
    result_base = _render_mermaid_components_for(config, config.containers[0], 0, set())
    assert "CoreModule" in result_base
    assert "LibModule" not in result_base
    # Cross-container derived edge excluded; LibModule out of container scope.
    result_cross = _render_mermaid_components_for(
        config, config.containers[0], 0, {("CoreModule", "LibModule")}
    )
    assert "imports" not in result_cross
    # Intra-container derived edge is included (both endpoints in Applications).
    result_intra = _render_mermaid_components_for(
        config, config.containers[0], 0, {("CoreModule", "CoreModule2")}
    )
    assert "imports" in result_intra


def test_main_html_writes_multi_tab_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Main with --format html produces a tabbed HTML file with a pane per C4 view.

    SCENARIO: two-container repo with one component in each container.
    MOCK SETUP: repo_root patched to tmp_path; sys.argv set to --format html.
    EXPECTED BEHAVIOR: main() exits 0, writes docs/architecture.html containing
    tab labels for all four views and exactly four mermaid diagram blocks, plus
    the vendored mermaid.min.js sidecar next to the HTML.
    """
    pkg = tmp_path / "src" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "app.py").write_text("X = 1\n")
    (pkg / "lib.py").write_text("X = 1\n")
    _write_pyproject(
        tmp_path,
        '[tool.forge]\nsource_dirs = ["src"]\n\n[tool.forge.c4]\nconfig = "c4.toml"\n',
    )
    (tmp_path / "c4.toml").write_text(TWO_CONTAINER_MODEL)
    monkeypatch.setattr("forge.gen_c4.repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-gen-c4", "--format", "html"])

    assert main() == 0

    html_path = tmp_path / "docs" / "architecture.html"
    assert html_path.is_file()
    content = html_path.read_text()
    assert "System Context" in content
    assert "Containers" in content
    assert "Applications Components" in content
    assert "Domain libraries Components" in content
    assert content.count('<pre class="mermaid">') == 4
    assert (tmp_path / "docs" / "mermaid.min.js").is_file()


def test_render_readme_block_wraps_mermaid_in_markers() -> None:
    """The README block is marker-delimited and fences the Mermaid."""
    block = render_readme_block("graph LR\n")
    assert block.startswith(README_C4_START)
    assert block.endswith(README_C4_END)
    assert "```mermaid" in block


def test_sync_readme_writes_then_checks(tmp_path: Path) -> None:
    """sync_readme splices the block in, and a re-check reports in sync."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(RICH_MODEL)
    (tmp_path / "README.md").write_text(
        f"# Demo\n\n{README_C4_START}\n{README_C4_END}\n\nrest\n",
    )
    config = load_c4_config(tmp_path)
    assert config is not None
    mermaid = render_mermaid(config, set())
    assert sync_readme(tmp_path, config, mermaid, check=False) == 0
    assert "```mermaid" in (tmp_path / "README.md").read_text()
    assert sync_readme(tmp_path, config, mermaid, check=True) == 0


def test_sync_readme_check_fails_on_missing_markers(tmp_path: Path) -> None:
    """A README without the markers is a configuration error (exit 1)."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(RICH_MODEL)
    (tmp_path / "README.md").write_text("# Demo\nno markers here\n")
    config = load_c4_config(tmp_path)
    assert config is not None
    assert sync_readme(tmp_path, config, "graph LR\n", check=True) == 1


def test_main_check_detects_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--check returns 1 when the committed DSL has drifted.

    SCENARIO: the committed artifact is stale relative to the model.
    EXPECTED BEHAVIOR: --check exits 1 without rewriting the file.
    """
    pkg = tmp_path / "src" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("X = 1\n")
    (pkg / "io.py").write_text("X = 1\n")
    _write_pyproject(
        tmp_path,
        '[tool.forge]\nsource_dirs = ["src"]\n\n[tool.forge.c4]\nconfig = "c4.toml"\n',
    )
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.dsl").write_text("stale\n")
    monkeypatch.setattr("forge.gen_c4.repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-gen-c4", "--check"])
    assert main() == 1


# --- CHANGE A: container view system-boundary anchoring ---


def test_container_view_anchors_person_edge_to_system_boundary() -> None:
    """Person with no container routes to system boundary, not first container."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates"),),
        externals=(External("GH", "Hosts", "reads via"),),
        containers=_TWO_CONTAINERS,
    )
    result = _render_mermaid_containers(config, set())
    # alloc order in _render_mermaid_containers: persons → "user", externals → "gh",
    # sys → "demo", containers → "applications"/"domain_libraries"
    person_lines = [ln for ln in result.splitlines() if "user -->" in ln]
    assert len(person_lines) == 1
    assert "demo" in person_lines[0]
    assert "applications" not in person_lines[0]


def test_container_view_external_edge_originates_from_system_boundary() -> None:
    """External relationship originates from system node, not first container."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates"),),
        externals=(External("GH", "Hosts", "reads via"),),
        containers=_TWO_CONTAINERS,
    )
    result = _render_mermaid_containers(config, set())
    # sys_id "demo" is allocated after "user" (person) and "gh" (external)
    assert "demo -->|" in result
    assert "applications -->|" not in result


# --- CHANGE B: Person.container routing ---


def test_load_c4_config_parses_person_container_field(tmp_path: Path) -> None:
    """The [[person]] container field is preserved on the parsed Person dataclass."""
    model = (
        'system = "Demo"\n'
        "[[container]]\n"
        'name = "Applications"\n'
        "[[person]]\n"
        'name = "User"\n'
        'description = "Uses"\n'
        'uses = "operates"\n'
        'container = "Applications"\n'
    )
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(model)
    config = load_c4_config(tmp_path)
    assert config is not None
    assert config.persons[0].container == "Applications"


def test_container_view_routes_person_to_declared_container() -> None:
    """Person whose container names a real container targets it in view."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates", container="Domain libraries"),),
        containers=_TWO_CONTAINERS,
    )
    result = _render_mermaid_containers(config, set())
    # alloc: "user", "demo", "applications", "domain_libraries"
    person_lines = [ln for ln in result.splitlines() if "user -->" in ln]
    assert len(person_lines) == 1
    assert "domain_libraries" in person_lines[0]
    assert "applications" not in person_lines[0]
    assert "demo" not in person_lines[0]


def test_container_view_empty_person_container_falls_back_to_system_boundary() -> None:
    """Empty person container anchors to system boundary in container view."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates", container=""),),
        containers=_TWO_CONTAINERS,
    )
    result = _render_mermaid_containers(config, set())
    person_lines = [ln for ln in result.splitlines() if "user -->" in ln]
    assert len(person_lines) == 1
    assert "demo" in person_lines[0]
    assert "applications" not in person_lines[0]


def test_dsl_routes_person_to_declared_container() -> None:
    """render_dsl emits the person edge targeting the declared container id."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates", container="Domain libraries"),),
        containers=_TWO_CONTAINERS,
    )
    dsl = render_dsl(config, set())
    # alloc: sys "demo", person "user", containers "applications"/"domain_libraries"
    assert "user -> domain_libraries" in dsl
    assert "user -> demo" not in dsl


def test_flat_mermaid_routes_person_to_non_first_declared_container() -> None:
    """render_mermaid routes a person to their declared (non-primary) container."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates", container="Domain libraries"),),
        containers=_TWO_CONTAINERS,
    )
    mermaid = render_mermaid(config, set())
    # render_mermaid has no system node; primary (fallback) is "applications".
    # "Domain libraries" → "domain_libraries" via container_ids lookup.
    person_lines = [ln for ln in mermaid.splitlines() if "user -->" in ln]
    assert len(person_lines) == 1
    assert "domain_libraries" in person_lines[0]
    assert "applications" not in person_lines[0]


def test_flat_mermaid_empty_person_container_falls_back_to_primary() -> None:
    """render_mermaid falls back to primary container when empty."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates"),),
        containers=_TWO_CONTAINERS,
    )
    mermaid = render_mermaid(config, set())
    person_lines = [ln for ln in mermaid.splitlines() if "user -->" in ln]
    assert len(person_lines) == 1
    assert "applications" in person_lines[0]


def test_system_context_ignores_person_container_always_routes_to_system() -> None:
    """System Context routes every person to system node regardless."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates", container="Applications"),),
        containers=_TWO_CONTAINERS,
    )
    result = _render_mermaid_system_context(config)
    # alloc: "user" (person), "demo" (sys) — no containers rendered
    person_lines = [ln for ln in result.splitlines() if "user -->" in ln]
    assert len(person_lines) == 1
    assert "demo" in person_lines[0]
    # The Actors subgraph is now expected (Feature E); verify the declared container
    # target is not present as an edge target — system context never renders containers.
    assert (
        "applications" not in result
    )  # container node absent; person routes to system


# --- CHANGE C: edge-source control ---


def test_resolve_edge_mode_accepts_all_three_valid_modes() -> None:
    """All three edge modes round-trip through _resolve_edge_mode."""
    for mode in _EDGE_MODES:
        assert _resolve_edge_mode(mode) == mode


def test_resolve_edge_mode_raises_on_unknown_value() -> None:
    """An unrecognized mode raises ValueError naming the bad value."""
    for bad in ("mixed", ""):
        with pytest.raises(ValueError, match="unknown edges mode"):
            _resolve_edge_mode(bad)


def test_includes_derived_truth_table() -> None:
    """_includes_derived returns True for imports, False for declared."""
    assert _includes_derived("imports") is True
    assert _includes_derived("both") is True
    assert _includes_derived("declared") is False


def test_load_c4_config_parses_all_edge_mode_fields(tmp_path: Path) -> None:
    """All three edge-mode keys are parsed from the model file into the config."""
    model = (
        'system = "Demo"\n'
        'edges = "both"\n'
        'container_edges = "declared"\n'
        'component_edges = "imports"\n'
    )
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(model)
    config = load_c4_config(tmp_path)
    assert config is not None
    assert config.edges == "both"
    assert config.container_edges == "declared"
    assert config.component_edges == "imports"


def test_load_c4_config_empty_per_view_edge_inherits_global_edges(
    tmp_path: Path,
) -> None:
    """Per-view edges inherit global edges when absent from model."""
    model = 'system = "Demo"\nedges = "both"\n'
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(model)
    config = load_c4_config(tmp_path)
    assert config is not None
    assert config.edges == "both"
    assert config.container_edges == "both"
    assert config.component_edges == "both"


def test_load_c4_config_unknown_edge_mode_raises(tmp_path: Path) -> None:
    """An unrecognized edges value in the model file propagates as ValueError."""
    model = 'system = "Demo"\nedges = "bogus"\n'
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(model)
    with pytest.raises(ValueError, match="unknown edges mode"):
        load_c4_config(tmp_path)


def test_derive_container_edges_include_derived_false_drops_import_pairs() -> None:
    """include_derived=False keeps declared pairs, drops derived."""
    components = (
        Component("Alpha", ("demo.alpha",), container="Applications"),
        Component("Beta", ("demo.beta",), container="Domain libraries"),
    )
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=_TWO_CONTAINERS,
        components=components,
        relationships=(Relationship("Alpha", "Beta", "calls"),),
    )
    derived_edges: set[tuple[str, str]] = {("Beta", "Alpha")}
    # Declared Alpha→Beta + derived Beta→Alpha → both cross-container directions
    both = _derive_container_edges(config, derived_edges, include_derived=True)
    assert both == {
        ("Applications", "Domain libraries"),
        ("Domain libraries", "Applications"),
    }
    # include_derived=False: only the declared Alpha→Beta maps to Apps→Domain
    declared_only = _derive_container_edges(
        config, derived_edges, include_derived=False
    )
    assert declared_only == {("Applications", "Domain libraries")}


def test_render_mermaid_components_include_derived_false_behavior() -> None:
    """include_derived=False omits imports, keeps declared edges."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=(Container("Applications", "Python", ""),),
        components=(
            Component("Alpha", ("demo.alpha",), container="Applications"),
            Component("Beta", ("demo.beta",), container="Applications"),
        ),
        relationships=(Relationship("Alpha", "Beta", "calls"),),
    )
    container = config.containers[0]
    result_false = _render_mermaid_components_for(
        config, container, 0, {("Beta", "Alpha")}, include_derived=False
    )
    assert '"calls"' in result_false
    assert '"imports"' not in result_false

    result_true = _render_mermaid_components_for(
        config, container, 0, {("Beta", "Alpha")}, include_derived=True
    )
    assert '"calls"' in result_true
    assert '"imports"' in result_true


def test_render_dsl_declared_mode_omits_derived_import_edges() -> None:
    """render_dsl with edges='declared' suppresses the import-derived edge block."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        edges="declared",
        containers=(Container("app", "Python", ""),),
        components=(
            Component("Core", ("demo.core",)),
            Component("IO", ("demo.io",)),
        ),
        relationships=(Relationship("Core", "IO", "writes via subprocess"),),
    )
    dsl = render_dsl(config, {("IO", "Core")})
    assert "writes via subprocess" in dsl
    assert '"imports"' not in dsl


def test_render_mermaid_declared_mode_omits_derived_import_edges() -> None:
    """render_mermaid with edges='declared' suppresses the import-derived edge lines."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        edges="declared",
        containers=(Container("app", "Python", ""),),
        components=(
            Component("Core", ("demo.core",)),
            Component("IO", ("demo.io",)),
        ),
        relationships=(Relationship("Core", "IO", "writes via subprocess"),),
    )
    mermaid = render_mermaid(config, {("IO", "Core")})
    assert "writes via subprocess" in mermaid
    assert '"imports"' not in mermaid


def test_main_html_container_declared_suppresses_derived_in_container_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container pane suppresses derived edges when container_edges=declared.

    SCENARIO: two-container repo; Alpha and Beta in Applications, Gamma in Domain
    libraries; alpha.py imports beta.py and gamma.py; container_edges="declared"
    with no declared relationships in the model.
    MOCK SETUP: repo_root patched to tmp_path; sys.argv set to --format html.
    EXPECTED BEHAVIOR: main() exits 0; the Containers pane (data-pane="1") has
    no &quot;uses&quot; (derived cross-container edges suppressed by the declared
    mode); the Applications Components pane (data-pane="2") has &quot;imports&quot;
    because component_edges inherits the default "imports" global mode.
    """
    pkg = tmp_path / "src" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "alpha.py").write_text("from demo import beta, gamma\n")
    (pkg / "beta.py").write_text("X = 1\n")
    (pkg / "gamma.py").write_text("X = 1\n")
    model = (
        'system = "Demo"\n'
        'container_edges = "declared"\n'
        "[[container]]\n"
        'name = "Applications"\n'
        "[[container]]\n"
        'name = "Domain libraries"\n'
        "[[component]]\n"
        'name = "Alpha"\n'
        'modules = ["demo.alpha"]\n'
        'container = "Applications"\n'
        "[[component]]\n"
        'name = "Beta"\n'
        'modules = ["demo.beta"]\n'
        'container = "Applications"\n'
        "[[component]]\n"
        'name = "Gamma"\n'
        'modules = ["demo.gamma"]\n'
        'container = "Domain libraries"\n'
    )
    _write_pyproject(
        tmp_path,
        '[tool.forge]\nsource_dirs = ["src"]\n\n[tool.forge.c4]\nconfig = "c4.toml"\n',
    )
    (tmp_path / "c4.toml").write_text(model)
    monkeypatch.setattr("forge.gen_c4.repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-gen-c4", "--format", "html"])

    assert main() == 0

    content = (tmp_path / "docs" / "architecture.html").read_text()
    p1 = content.index('<div class="pane" data-pane="1">')
    p2 = content.index('<div class="pane" data-pane="2">')
    p3 = content.index('<div class="pane" data-pane="3">')
    container_pane = content[p1:p2]
    app_components_pane = content[p2:p3]

    assert "&quot;uses&quot;" not in container_pane
    assert "&quot;imports&quot;" in app_components_pane


# --- CHANGE D: generalized relationship endpoints ---


def test_resolve_endpoint_resolves_each_element_kind() -> None:
    """_resolve_endpoint resolves component, container, external, person."""
    ids = _IdMaps(
        sys_id="demo",
        person_ids={"User": "user"},
        external_ids={"GitHub": "github"},
        container_ids={"App": "app"},
        component_ids={"Core": "core"},
    )
    assert _resolve_endpoint("Core", ids, "Demo") == "core"
    assert _resolve_endpoint("App", ids, "Demo") == "app"
    assert _resolve_endpoint("GitHub", ids, "Demo") == "github"
    assert _resolve_endpoint("User", ids, "Demo") == "user"
    assert _resolve_endpoint("Demo", ids, "Demo") == "demo"
    assert _resolve_endpoint("Unknown", ids, "Demo") is None


def test_warn_unknown_relationships_only_warns_on_unmatched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_warn_unknown_relationships warns only on unmatched endpoints.

    SCENARIO: one config whose relationship names a valid container→external pair;
    another config whose relationship names a nonexistent element.
    MOCK SETUP: _IdMaps built inline; no I/O or monkeypatching.
    EXPECTED BEHAVIOR: the valid config produces no warning records; the invalid
    config produces exactly one WARNING whose message contains the unknown name.
    """
    ids_known = _IdMaps(
        sys_id="demo",
        person_ids={},
        external_ids={"GitHub": "github"},
        container_ids={"App": "app"},
        component_ids={},
    )
    config_valid = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=(Container("App", "Python", ""),),
        externals=(External("GitHub", "Hosts", "reads via"),),
        relationships=(Relationship("App", "GitHub", "calls"),),
    )
    with caplog.at_level(logging.WARNING, logger="forge.gen_c4"):
        _warn_unknown_relationships(config_valid, ids_known)
    assert not caplog.records

    caplog.clear()
    ids_partial = _IdMaps("demo", {}, {}, {"App": "app"}, {})
    config_invalid = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        relationships=(Relationship("App", "Phantom", "calls"),),
    )
    with caplog.at_level(logging.WARNING, logger="forge.gen_c4"):
        _warn_unknown_relationships(config_invalid, ids_partial)
    assert len(caplog.records) == 1
    assert "Phantom" in caplog.records[0].getMessage()


def test_dsl_renders_container_to_container_relationship() -> None:
    """render_dsl resolves and emits a declared relationship between two containers."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=_TWO_CONTAINERS,
        relationships=(Relationship("Applications", "Domain libraries", "builds on"),),
    )
    dsl = render_dsl(config, set())
    # alloc: sys "demo", containers "applications"/"domain_libraries"
    assert "applications -> domain_libraries" in dsl
    assert '"builds on"' in dsl


def test_dsl_renders_component_to_external_relationship() -> None:
    """render_dsl resolves and emits a declared component→external relationship."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=(Container("App", "Python", ""),),
        components=(Component("Core", ("demo.core",)),),
        externals=(External("GitHub", "Hosts", "reads via"),),
        relationships=(Relationship("Core", "GitHub", "publishes to"),),
    )
    dsl = render_dsl(config, set())
    # alloc: sys "demo", external "github", container "app", component "core"
    assert "core -> github" in dsl
    assert '"publishes to"' in dsl


def test_flat_mermaid_renders_generalized_relationship() -> None:
    """render_mermaid emits a declared container→container edge with the given label."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=_TWO_CONTAINERS,
        relationships=(Relationship("Applications", "Domain libraries", "builds on"),),
    )
    mermaid = render_mermaid(config, set())
    # render_mermaid sys_id is empty; container names resolve via container_ids
    assert 'applications -->|"builds on"| domain_libraries' in mermaid


def test_dsl_component_to_component_relationship_still_renders() -> None:
    """Component→component relationship renders in render_dsl."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=(Container("app", "Python", ""),),
        components=(
            Component("Core", ("demo.core",)),
            Component("IO", ("demo.io",)),
        ),
        relationships=(Relationship("Core", "IO", "writes via"),),
    )
    dsl = render_dsl(config, set())
    assert "core -> io" in dsl
    assert '"writes via"' in dsl


def test_dsl_person_target_resolves_to_component() -> None:
    """Person whose container names a component targets it in DSL."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates", container="Core"),),
        containers=(Container("App", "Python", ""),),
        components=(Component("Core", ("demo.core",)),),
    )
    dsl = render_dsl(config, set())
    # _person_node checks component_ids first; "Core" → "core", not sys "demo"
    assert "user -> core" in dsl
    assert "user -> demo" not in dsl


def test_container_view_person_to_component_routes_to_owning_container() -> None:
    """Person targeting component routes to owning container in view."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates", container="Core"),),
        containers=(Container("App", "Python", ""),),
        components=(Component("Core", ("demo.core",)),),
    )
    result = _render_mermaid_containers(config, set())
    # _container_level_maps maps component "Core" → owning container id "app";
    # _person_node("Core", maps, fallback=sys_id) → "app"
    person_lines = [ln for ln in result.splitlines() if "user -->" in ln]
    assert len(person_lines) == 1
    assert "app" in person_lines[0]
    assert "core" not in person_lines[0]
    assert "demo" not in person_lines[0]


def test_container_view_renders_declared_container_edge_not_duplicate_component() -> (
    None
):
    """Container view renders container edges; component edges skipped."""
    components = (
        Component("Alpha", ("demo.alpha",), container="Applications"),
        Component("Beta", ("demo.beta",), container="Domain libraries"),
    )
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=_TWO_CONTAINERS,
        components=components,
        relationships=(
            Relationship("Applications", "Domain libraries", "links"),
            Relationship("Alpha", "Beta", "calls"),
        ),
    )
    container_edges = _derive_container_edges(config, set())
    result = _render_mermaid_containers(config, container_edges)
    # container→container declared rel renders with its label
    assert '"links"' in result
    # component→component declared rel is skipped by _container_view_declared
    assert '"calls"' not in result


def test_component_view_renders_component_to_external_peripheral() -> None:
    """Component view draws external node and edge for component→external."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=(Container("App", "Python", ""),),
        components=(Component("Core", ("demo.core",)),),
        externals=(External("GitHub", "Hosts", "reads via"),),
        relationships=(Relationship("Core", "GitHub", "publishes to"),),
    )
    result = _render_mermaid_components_for(
        config, config.containers[0], 0, set(), include_derived=True
    )
    assert "GitHub" in result
    assert '"publishes to"' in result
    # Alloc order: component "Core"→"core", container "App"→"app",
    # then peripheral external "GitHub"→"github".  Edge direction is
    # component→external, not reversed.
    assert 'core -->|"publishes to"| github' in result


def test_component_view_renders_actor_to_component_peripheral() -> None:
    """Component view draws person node and edge for person→component."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        persons=(Person("User", "Uses", "operates"),),
        containers=(Container("App", "Python", ""),),
        components=(Component("Core", ("demo.core",)),),
        relationships=(Relationship("User", "Core", "submits to"),),
    )
    result = _render_mermaid_components_for(
        config, config.containers[0], 0, set(), include_derived=True
    )
    assert "User" in result
    assert '"submits to"' in result
    # Alloc order: component "Core"→"core", container "App"→"app",
    # then peripheral person "User"→"user".  Edge direction is
    # person→component, not reversed.
    assert 'user -->|"submits to"| core' in result


def test_unknown_relationship_endpoint_skipped_in_dsl() -> None:
    """Unresolvable relationship endpoint omitted from render_dsl."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=(Container("App", "Python", ""),),
        components=(Component("Core", ("demo.core",)),),
        relationships=(Relationship("Core", "Phantom", "does stuff"),),
    )
    dsl = render_dsl(config, set())
    assert "does stuff" not in dsl


# --- FEATURE E: HTML layout + direction + actor banding ---


def test_render_html_sets_flowchart_usemaxwidth_false() -> None:
    """render_html sets mermaid flowchart useMaxWidth: false.

    The init options are emitted as JSON, so the key is double-quoted.
    """
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("V", "graph LR\n")])
    assert '"useMaxWidth": false' in page


def test_render_html_wraps_panes_in_scroll_container() -> None:
    """render_html wraps each pane's mermaid block in a diagram-scroll container."""
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("V", "graph LR\n")])
    assert 'class="diagram-scroll"' in page


def test_resolve_direction_accepts_lr_and_tb() -> None:
    """_resolve_direction returns LR and TB unchanged."""
    assert _resolve_direction("LR") == "LR"
    assert _resolve_direction("TB") == "TB"


def test_resolve_direction_raises_on_unknown() -> None:
    """_resolve_direction raises ValueError for unknown directions."""
    with pytest.raises(ValueError, match="unknown direction"):
        _resolve_direction("sideways")


def test_load_c4_config_parses_direction(tmp_path: Path) -> None:
    """load_c4_config parses the direction field and defaults to LR when absent."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text('system = "Demo"\ndirection = "TB"\n')
    config = load_c4_config(tmp_path)
    assert config is not None
    assert config.direction == "TB"

    (tmp_path / "c4.toml").write_text('system = "Demo"\n')
    config_default = load_c4_config(tmp_path)
    assert config_default is not None
    assert config_default.direction == "LR"


def test_load_c4_config_unknown_direction_raises(tmp_path: Path) -> None:
    """load_c4_config raises ValueError when the direction value is unrecognized."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text('system = "Demo"\ndirection = "diagonal"\n')
    with pytest.raises(ValueError, match="unknown direction"):
        load_c4_config(tmp_path)


def test_render_mermaid_uses_configured_direction() -> None:
    """render_mermaid uses graph TB when direction=TB and graph LR at default."""
    config_tb = C4Config(system="Demo", description="", output="", direction="TB")
    assert render_mermaid(config_tb, set()).splitlines()[0] == "graph TB"

    config_lr = C4Config(system="Demo", description="", output="")
    assert render_mermaid(config_lr, set()).splitlines()[0] == "graph LR"


def test_dsl_uses_configured_direction() -> None:
    """render_dsl emits autolayout tb when direction=TB and autolayout lr by default."""
    config_tb = C4Config(system="Demo", description="", output="", direction="TB")
    assert "autolayout tb" in render_dsl(config_tb, set())

    config_lr = C4Config(system="Demo", description="", output="")
    assert "autolayout lr" in render_dsl(config_lr, set())


def test_per_view_renderers_use_configured_direction() -> None:
    """Per-view renderers use configured direction in graph header."""
    config = C4Config(
        system="Demo",
        description="",
        output="",
        direction="TB",
        persons=(Person("User", "Uses", "operates"),),
        containers=_TWO_CONTAINERS,
    )
    assert _render_mermaid_system_context(config).splitlines()[0] == "graph TB"
    assert _render_mermaid_containers(config, set()).splitlines()[0] == "graph TB"


def test_container_view_groups_persons_in_actors_subgraph() -> None:
    """_render_mermaid_containers groups persons; flat render does not."""
    config = C4Config(
        system="Demo",
        description="",
        output="",
        persons=(Person("User", "Uses", "operates"),),
        containers=_TWO_CONTAINERS,
    )
    result = _render_mermaid_containers(config, set())
    assert "subgraph" in result
    assert '["Actors"]' in result
    # Person node id "user" must appear inside the Actors block, not scattered outside.
    actors_start = result.index('["Actors"]')
    end_marker = result.index("end", actors_start)
    actors_block = result[actors_start:end_marker]
    assert "user" in actors_block

    # The flat render_mermaid has no system node and does not use _actors_subgraph.
    flat = render_mermaid(config, set())
    assert '["Actors"]' not in flat


def test_system_context_groups_persons_in_actors_subgraph() -> None:
    """_render_mermaid_system_context groups persons in Actors."""
    config = C4Config(
        system="Demo",
        description="",
        output="",
        persons=(Person("User", "Uses", "operates"),),
    )
    result = _render_mermaid_system_context(config)
    assert '["Actors"]' in result
    # Person node id "user" must appear inside the Actors block, not scattered outside.
    # Alloc order: "user" (person), "demo" (sys), then actors_id "actors".
    actors_start = result.index('["Actors"]')
    end_marker = result.index("end", actors_start)
    actors_block = result[actors_start:end_marker]
    assert "user" in actors_block


# --- FEATURE G: externals band ---


def test_container_view_renders_externals_as_flat_nodes() -> None:
    """Externals are flat nodes (no extra subgraph) — one fewer cluster for dagre.

    Wrapping externals in an ``External Systems`` subgraph gave dagre a third
    sibling cluster to mis-rank and tangle; emitting them flat (as the System
    Context view does) keeps only the system-boundary subgraph in this view.
    """
    config = C4Config(
        system="Demo",
        description="",
        output="",
        externals=(External("GitHub", "Hosts", "reads via gh"),),
        containers=_TWO_CONTAINERS,
    )
    result = _render_mermaid_containers(config, set())
    # The external node is rendered as a flat subroutine node, NOT inside any
    # "External Systems" subgraph. The only subgraph here is the system boundary.
    assert '["External Systems"]' not in result
    assert 'github[["' in result
    assert result.count("subgraph") == 1  # only the system-boundary cluster


# --- FEATURE F: generic external-edge suppression ---


def test_externals_with_declared_incoming_returns_targeted_only() -> None:
    """_externals_with_declared_incoming returns targeted externals."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        externals=(
            External("E1", "First external", "uses"),
            External("E2", "Second external", "uses"),
        ),
        relationships=(Relationship("SomeComp", "E1", "publishes to"),),
    )
    result = _externals_with_declared_incoming(config)
    assert result == {"E1"}


def test_dsl_keeps_generic_and_renders_specific_when_external_declared() -> None:
    """render_dsl emits both the generic sys→external and the specific declared edge.

    DSL is the full Structurizr model, not a single view. Structurizr scopes
    each relationship to the views that include it. The generic system→external
    edge and any specific declared edge to the same external therefore coexist
    in the DSL; neither suppresses the other.
    """
    config = C4Config(
        system="Sys",
        description="",
        output="out.dsl",
        containers=(Container("App", "Python", ""),),
        components=(Component("Worker", ("demo.worker",)),),
        externals=(External("Store", "A data store", "uses"),),
        relationships=(Relationship("Worker", "Store", "publishes to"),),
    )
    dsl = render_dsl(config, set())
    # alloc: sys_id="sys", external_ids={"Store": "store"},
    # container_ids={"App": "app"}, component_ids={"Worker": "worker"}
    # Both the generic sys→store and the specific worker→store must be present.
    assert "worker -> store" in dsl
    assert '"publishes to"' in dsl
    assert "sys -> store" in dsl


def test_dsl_keeps_generic_system_edge_for_untargeted_external() -> None:
    """render_dsl keeps generic sys→external when untargeted."""
    config = C4Config(
        system="Sys",
        description="",
        output="out.dsl",
        containers=(Container("App", "Python", ""),),
        components=(Component("Worker", ("demo.worker",)),),
        externals=(External("Store", "A data store", "uses"),),
    )
    dsl = render_dsl(config, set())
    # alloc: sys_id="sys", external "store"; no relationships → generic kept
    assert "sys -> store" in dsl
    assert '"uses"' in dsl


def test_container_view_suppresses_generic_external_edge_when_declared() -> None:
    """_render_mermaid_containers suppresses generic sys→external when declared."""
    config = C4Config(
        system="Sys",
        description="",
        output="out.dsl",
        containers=(Container("App", "Python", ""),),
        components=(Component("Worker", ("demo.worker",)),),
        externals=(External("Store", "A data store", "uses"),),
        relationships=(Relationship("Worker", "Store", "publishes to"),),
    )
    result = _render_mermaid_containers(config, set())
    # alloc: persons (none), external "store", sys "sys", container "app"
    # component "Worker" maps to container "app"
    # declared edge: app -->|"publishes to"| store
    assert 'sys -->|"uses"| store' not in result
    assert 'app -->|"publishes to"| store' in result


def test_container_view_keeps_generic_external_edge_when_untargeted() -> None:
    """_render_mermaid_containers keeps generic sys→external when untargeted."""
    config = C4Config(
        system="Sys",
        description="",
        output="out.dsl",
        containers=(Container("App", "Python", ""),),
        components=(Component("Worker", ("demo.worker",)),),
        externals=(External("Store", "A data store", "uses"),),
    )
    result = _render_mermaid_containers(config, set())
    # alloc: external "store", sys_id "sys"; no declared targets → generic emitted
    assert 'sys -->|"uses"| store' in result


def test_default_no_declared_relationships_keeps_all_generic_external_edges() -> None:
    """Config with externals, no relationships emits generic edges."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        containers=(Container("App", "Python", ""),),
        externals=(External("GitHub", "Hosts repos", "reads via gh"),),
    )
    dsl = render_dsl(config, set())
    # alloc in render_dsl: sys_id "demo", external_ids={"GitHub": "github"}
    assert "demo -> github" in dsl

    result = _render_mermaid_containers(config, set())
    # alloc in _render_mermaid_containers: external "github", sys_id "demo"
    assert 'demo -->|"reads via gh"| github' in result


def test_system_context_keeps_generic_external_even_when_declared() -> None:
    """System Context view preserves generic edge when external is a declared target.

    The System Context view has no container or component nodes, so it never
    renders the specific declared edge (that belongs to the Component view
    peripherals). The generic sys→external arrow is therefore the only incoming
    edge an external can receive in this view and must always be emitted —
    suppression is not applied here.
    """
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        externals=(External("Store", "A data store", "uses"),),
        components=(Component("Worker", ("demo.worker",)),),
        relationships=(Relationship("Worker", "Store", "publishes to"),),
    )
    result = _render_mermaid_system_context(config)
    # Alloc order (no persons): sys_id "demo", external "store".
    # "Store" is a declared destination, but the context view keeps the generic
    # edge because no specific component→external edge is rendered here.
    assert 'demo -->|"uses"| store' in result


def test_system_context_keeps_generic_external_when_untargeted() -> None:
    """_render_mermaid_system_context keeps generic edge when untargeted."""
    config = C4Config(
        system="Demo",
        description="",
        output="out.dsl",
        externals=(External("Store", "A data store", "uses"),),
    )
    result = _render_mermaid_system_context(config)
    # Alloc: sys_id "demo", external "store"; no declared targets
    assert 'demo -->|"uses"| store' in result


# --- FEATURE G: ELK layout engine ---


def test_render_html_loads_and_registers_elk() -> None:
    """render_html output contains all three parts of the v11 ELK wiring.

    Pins the vendored loader script tag (MERMAID_ELK_JS_NAME), the
    registerLayoutLoaders call, and ``"layout": c4layout`` in mermaid.initialize
    together so a regression that drops any single piece (e.g. removing the
    script tag while keeping the init call) is caught immediately rather than
    silently degrading to dagre at runtime. The dagre-default string is also
    checked to confirm the init variable is visible before the try block runs.

    The ``"layout"`` key is double-quoted (JSON format) while ``c4layout`` is a
    bare JS variable reference, so the page-computed value wins at runtime.
    """
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("V", "graph LR\n")])
    assert f'<script src="{MERMAID_ELK_JS_NAME}">' in page
    assert "registerLayoutLoaders" in page
    assert '"layout": c4layout' in page
    assert "NETWORK_SIMPLEX" in page
    # c4layout is declared as var c4layout = requestedLayout and then conditionally
    # set to "dagre" inside the ELK try-block; the bare assignment (not the var decl)
    # is the dagre fallback marker.
    assert 'c4layout = "dagre"' in page


def test_render_html_elk_has_dagre_fallback() -> None:
    """The ELK loader registration runs inside a try/catch with a dagre default.

    A missing or erroring ELK loader must not break the page — the try/catch
    leaves c4layout as "dagre" so mermaid.initialize still receives a valid
    layout value. Both the guard (try {) and the safe default ("dagre") must
    be present in the emitted HTML.
    """
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("V", "graph LR\n")])
    assert "try {" in page
    assert '"dagre"' in page


def test_copy_vendored_writes_both_mermaid_and_elk_sidecars(tmp_path: Path) -> None:
    """_copy_vendored_mermaid writes non-empty files for both JS bundles.

    Exercises the real package-data copy via importlib.resources. Both the
    base Mermaid UMD bundle and the ELK IIFE layout loader must land as
    non-empty files next to the HTML so the page renders fully offline —
    a zero-byte or missing sidecar would silently break diagrams or ELK layout.
    """
    _copy_vendored_mermaid(tmp_path)
    assert (tmp_path / MERMAID_JS_NAME).is_file()
    assert (tmp_path / MERMAID_ELK_JS_NAME).is_file()
    assert (tmp_path / MERMAID_JS_NAME).stat().st_size > 0
    assert (tmp_path / MERMAID_ELK_JS_NAME).stat().st_size > 0


# --- #140: RenderConfig defaults and _parse_render_config ---


def test_parse_render_config_absent_render_key_returns_defaults() -> None:
    """_parse_render_config with no render key returns the all-defaults RenderConfig."""
    cfg = _parse_render_config({})
    assert cfg.wrapping_width == 220
    assert cfg.merge_edges is False
    assert cfg.force_node_model_order is True
    assert cfg.node_placement_strategy == "NETWORK_SIMPLEX"
    assert cfg.theme_colors == {}
    assert cfg.theme == "neutral"
    assert cfg.font_size is None


def test_parse_render_config_populated_keys_override_defaults() -> None:
    """_parse_render_config reads supplied keys, overriding defaults."""
    cfg = _parse_render_config(
        {"render": {"wrapping_width": 350, "font_size": 16, "theme": "base"}}
    )
    assert cfg.wrapping_width == 350
    assert cfg.font_size == 16
    assert cfg.theme == "base"


def test_parse_render_config_unknown_keys_ignored() -> None:
    """_parse_render_config silently ignores unrecognized keys; no exception raised."""
    cfg = _parse_render_config({"render": {"bogus_key": 99, "another": "value"}})
    assert cfg.wrapping_width == 220  # defaults preserved when unknowns are present


def test_parse_render_config_non_dict_render_returns_defaults() -> None:
    """_parse_render_config falls back to RenderConfig() when render is not a dict."""
    assert _parse_render_config({"render": "bogus"}) == RenderConfig()
    assert _parse_render_config({"render": 42}) == RenderConfig()


def test_parse_render_config_theme_colors_non_dict_returns_empty() -> None:
    """_parse_render_config coerces non-dict theme_colors to an empty dict."""
    assert _parse_render_config({"render": {"theme_colors": "red"}}).theme_colors == {}
    assert _parse_render_config({"render": {"theme_colors": [1, 2]}}).theme_colors == {}


def test_parse_render_config_theme_colors_dict_preserved() -> None:
    """_parse_render_config preserves a valid theme_colors mapping verbatim."""
    colors = {"primaryColor": "#ff0000", "lineColor": "#333"}
    cfg = _parse_render_config({"render": {"theme_colors": colors}})
    assert cfg.theme_colors == colors


def test_load_c4_config_parses_render_section(tmp_path: Path) -> None:
    """load_c4_config reads [render] keys into config.render."""
    toml_text = 'system = "Demo"\n[render]\nwrapping_width = 350\nfont_size = 16\n'
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(toml_text)
    config = load_c4_config(tmp_path)
    assert config is not None
    assert config.render.wrapping_width == 350
    assert config.render.font_size == 16


def test_render_config_is_frozen() -> None:
    """RenderConfig raises FrozenInstanceError on attempted attribute mutation."""
    rc = RenderConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rc.wrapping_width = 999


# --- #140: _mermaid_init_options ---


def test_mermaid_init_options_always_emitted_keys_present() -> None:
    """_mermaid_init_options emits required always-on keys for default config."""
    out = _mermaid_init_options(RenderConfig(), layout_var="c4layout")
    assert '"wrappingWidth": 220' in out
    assert '"markdownAutoWrap": true' in out
    assert '"useMaxWidth": false' in out
    assert "NETWORK_SIMPLEX" in out
    assert '"forceNodeModelOrder": true' in out
    assert '"mergeEdges": false' in out


def test_mermaid_init_options_layout_var_is_bare_reference() -> None:
    """_mermaid_init_options emits layout as bare variable (not string).

    The dagre fallback in the HTML page sets `c4layout` at runtime; emitting the
    variable name unquoted lets the page-computed value win rather than hard-coding
    a string literal "c4layout" that would break the ELK / dagre switch.
    """
    out = _mermaid_init_options(RenderConfig(), layout_var="c4layout")
    assert '"layout": c4layout' in out
    assert '"layout": "c4layout"' not in out


def test_mermaid_init_options_optional_keys_absent_with_defaults() -> None:
    """Optional pass-through keys are absent from the output when at their defaults."""
    out = _mermaid_init_options(RenderConfig(), layout_var="c4layout")
    for key in (
        "fontSize",
        "nodeSpacing",
        "rankSpacing",
        "fontFamily",
        "htmlLabels",
        "themeCSS",
        "considerModelOrder",
        "cycleBreakingStrategy",
    ):
        assert key not in out, f"expected {key!r} absent with default RenderConfig"


def test_mermaid_init_options_font_size_emitted_when_set() -> None:
    """_mermaid_init_options emits fontSize when font_size is provided."""
    out = _mermaid_init_options(RenderConfig(font_size=14), layout_var="c4layout")
    assert '"fontSize": 14' in out


def test_mermaid_init_options_node_spacing_emitted_when_set() -> None:
    """_mermaid_init_options emits nodeSpacing when node_spacing is provided."""
    out = _mermaid_init_options(RenderConfig(node_spacing=50), layout_var="c4layout")
    assert '"nodeSpacing": 50' in out


def test_mermaid_init_options_rank_spacing_emitted_when_set() -> None:
    """_mermaid_init_options emits rankSpacing when rank_spacing is provided."""
    out = _mermaid_init_options(RenderConfig(rank_spacing=20), layout_var="c4layout")
    assert '"rankSpacing": 20' in out


def test_mermaid_init_options_font_family_emitted_when_set() -> None:
    """_mermaid_init_options emits fontFamily when font_family is provided."""
    out = _mermaid_init_options(
        RenderConfig(font_family="monospace"), layout_var="c4layout"
    )
    assert '"fontFamily": "monospace"' in out


def test_mermaid_init_options_html_labels_emitted_when_set() -> None:
    """_mermaid_init_options emits htmlLabels when html_labels is explicitly False."""
    out = _mermaid_init_options(RenderConfig(html_labels=False), layout_var="c4layout")
    assert '"htmlLabels": false' in out


def test_mermaid_init_options_custom_css_emitted_when_set() -> None:
    """_mermaid_init_options emits themeCSS when custom_css is provided."""
    out = _mermaid_init_options(
        RenderConfig(custom_css=".node{}"), layout_var="c4layout"
    )
    assert '"themeCSS": ".node{}"' in out


def test_mermaid_init_options_consider_model_order_emitted_when_set() -> None:
    """_mermaid_init_options emits considerModelOrder when set."""
    out = _mermaid_init_options(
        RenderConfig(consider_model_order="PREFER_EDGES"), layout_var="c4layout"
    )
    assert '"considerModelOrder": "PREFER_EDGES"' in out


def test_mermaid_init_options_theme_variables_only_when_base_and_colors() -> None:
    """ThemeVariables is emitted only when theme=='base' AND theme_colors is non-empty.

    Three sub-cases: neutral+colors (no emit), base+empty-colors (no emit),
    base+non-empty-colors (emit). Mermaid ignores themeVariables unless theme is
    'base', so emitting them under other themes would be misleading dead weight.
    """
    colors = {"primaryColor": "#ff0000"}
    # neutral theme with colors: NOT emitted (Mermaid ignores them anyway)
    out_neutral = _mermaid_init_options(
        RenderConfig(theme="neutral", theme_colors=colors), layout_var="c4layout"
    )
    assert "themeVariables" not in out_neutral
    # base theme with empty colors: NOT emitted (nothing to configure)
    out_base_empty = _mermaid_init_options(
        RenderConfig(theme="base", theme_colors={}), layout_var="c4layout"
    )
    assert "themeVariables" not in out_base_empty
    # base theme with non-empty colors: emitted
    out_base_colors = _mermaid_init_options(
        RenderConfig(theme="base", theme_colors=colors), layout_var="c4layout"
    )
    assert "themeVariables" in out_base_colors


# --- #140 / #124: _mermaid_box + regression guards ---


def test_mermaid_box_html_branch_exact() -> None:
    """_mermaid_box returns the canonical <b>/<br/> HTML-tag form by default."""
    assert (
        _mermaid_box("Core", "Python", "Does things")
        == "<b>Core</b><br/>[Python]<br/>Does things"
    )


def test_mermaid_box_markdown_branch_exact() -> None:
    """_mermaid_box(markdown=True) returns a backtick-fenced markdown-string label."""
    assert (
        _mermaid_box("Core", "Python", "Does things", markdown=True)
        == "`**Core**\n[Python]\nDoes things`"
    )


def test_mermaid_box_entity_escaping_html_branch() -> None:
    """_mermaid_box HTML form entity-escapes <, >, and & in all three fields."""
    result = _mermaid_box("A<B", "C&D", "x>y")
    assert "A&lt;B" in result
    assert "C&amp;D" in result
    assert "x&gt;y" in result


def test_mermaid_box_entity_escaping_markdown_branch() -> None:
    """_mermaid_box markdown form entity-escapes <, >, and & in all three fields."""
    result = _mermaid_box("A<B", "C&D", "x>y", markdown=True)
    assert "A&lt;B" in result
    assert "C&amp;D" in result
    assert "x&gt;y" in result


def test_mermaid_box_missing_technology_omits_bracket() -> None:
    """_mermaid_box omits the [technology] bracket when technology is empty."""
    assert "[" not in _mermaid_box("Core", "", "Desc")
    assert "[" not in _mermaid_box("Core", "", "Desc", markdown=True)


def test_mermaid_box_missing_description_two_part_label() -> None:
    """_mermaid_box produces a two-part label when description is empty."""
    assert _mermaid_box("Core", "Python", "") == "<b>Core</b><br/>[Python]"
    assert _mermaid_box("Core", "Python", "", markdown=True) == "`**Core**\n[Python]`"


def test_external_node_line_forwards_markdown_flag() -> None:
    """_external_node_line passes the markdown flag through to _mermaid_box.

    Default (markdown=False) produces a label with literal HTML <b> tags; the
    per-view HTML renderers pass markdown=True, which produces a backtick-fenced
    label so Mermaid's markdownAutoWrap can reflow long descriptions.
    """
    ext = External("GitHub", "Hosts repos", "reads via gh")
    default_result = _external_node_line("github", ext)
    assert "<b>" in default_result
    assert "`**" not in default_result
    md_result = _external_node_line("github", ext, markdown=True)
    assert "`**" in md_result
    assert "<b>" not in md_result


def test_flat_render_mermaid_uses_html_tags_not_markdown_strings() -> None:
    """render_mermaid uses literal HTML <b> tags; backtick markdown must not appear.

    Guards the flat (README/stdout) renderer against accidental migration to the
    markdown-string form, which is reserved for the per-view HTML renderers that
    pair with the markdownAutoWrap Mermaid option.
    """
    config = C4Config(
        system="Demo",
        description="",
        output="",
        containers=(Container("App", "Python", "An app"),),
        components=(Component("Core", ("demo.core",), description="Core component"),),
    )
    mermaid = render_mermaid(config, set())
    assert "<b>" in mermaid
    assert "`**" not in mermaid


def test_per_view_renderers_use_markdown_strings_in_node_definitions() -> None:
    """Per-view renderers emit markdown strings; no HTML <b> in node-definition lines.

    Checks all three per-view renderers: _render_mermaid_system_context,
    _render_mermaid_containers, and _render_mermaid_components_for. Every line
    that defines a node (contains `["`) must carry a backtick markdown label and
    must not contain literal HTML <b> tags — a regression would silently break
    Mermaid's markdownAutoWrap label-overflow fix.
    """
    config = C4Config(
        system="Demo",
        description="",
        output="",
        persons=(Person("User", "Uses", "operates"),),
        externals=(External("GitHub", "Hosts", "reads via gh"),),
        containers=(Container("App", "Python", "An app"),),
        components=(Component("Core", ("demo.core",), description="Core component"),),
    )
    for label, source in [
        ("system_context", _render_mermaid_system_context(config)),
        ("containers", _render_mermaid_containers(config, set())),
        (
            "components",
            _render_mermaid_components_for(config, config.containers[0], 0, set()),
        ),
    ]:
        def_lines = [ln for ln in source.splitlines() if '["' in ln]
        assert def_lines, f"{label}: expected node-definition lines containing '[\"'"
        assert all("<b>" not in ln for ln in def_lines), (
            f"{label}: found literal '<b>' in a node-definition line"
        )
        assert any("`**" in ln for ln in def_lines), (
            f"{label}: expected backtick markdown '`**' in node-definition line"
        )


# --- #124: render_html new injections ---


def test_render_html_contains_init_options_and_interactivity() -> None:
    """render_html injects init options and c4-focus-mode."""
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("V", "graph LR\n")])
    assert '"wrappingWidth": 220' in page
    assert "markdownAutoWrap" in page
    assert "c4WireView" in page
    assert "c4-focus-mode" in page
    assert "mermaid.initialize(" in page


# --- #137: _find_headless_browser ---


def test_find_headless_browser_env_existing_path_returns_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_find_headless_browser returns FORGE_C4_BROWSER when it is an executable file.

    SCENARIO: FORGE_C4_BROWSER points to an executable file on disk.
    MOCK SETUP: a real stub file (chmod +x) is created under tmp_path; env var set.
    EXPECTED BEHAVIOR: _find_headless_browser returns that exact path string.
    """
    fake_browser = tmp_path / "my_browser"
    fake_browser.write_text("")
    fake_browser.chmod(0o755)
    monkeypatch.setenv(_BROWSER_ENV, str(fake_browser))
    assert _find_headless_browser() == str(fake_browser)


def test_find_headless_browser_env_non_executable_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_find_headless_browser rejects a FORGE_C4_BROWSER that is not executable.

    SCENARIO: the override names a real file that lacks the execute bit.
    MOCK SETUP: a stub file is created (mode 0o644) and pointed at by the env var.
    EXPECTED BEHAVIOR: returns None — a non-executable path is not a browser.
    """
    fake_browser = tmp_path / "not_exec"
    fake_browser.write_text("")
    fake_browser.chmod(0o644)
    monkeypatch.setenv(_BROWSER_ENV, str(fake_browser))
    assert _find_headless_browser() is None


def test_find_headless_browser_env_missing_path_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_find_headless_browser returns None when FORGE_C4_BROWSER names a missing path.

    SCENARIO: FORGE_C4_BROWSER is set but the named file does not exist.
    MOCK SETUP: env var set to a path that does not exist on disk.
    EXPECTED BEHAVIOR: returns None; a missing override is not usable.
    """
    monkeypatch.setenv(_BROWSER_ENV, "/nonexistent/path/to/browser")
    assert _find_headless_browser() is None


def test_find_headless_browser_app_path_exists_returns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_find_headless_browser returns the first matching macOS app-bundle path.

    SCENARIO: no env override; one well-known app-bundle path exists.
    MOCK SETUP: FORGE_C4_BROWSER unset; Path.exists returns True only for the
        first entry of _BROWSER_APP_PATHS.
    EXPECTED BEHAVIOR: returns that first app-bundle path.
    """
    monkeypatch.delenv(_BROWSER_ENV, raising=False)
    target = _BROWSER_APP_PATHS[0]
    monkeypatch.setattr("forge.gen_c4.Path.exists", lambda self: str(self) == target)
    assert _find_headless_browser() == target


def test_find_headless_browser_shutil_which_returns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_find_headless_browser falls through to shutil.which when no app path exists.

    SCENARIO: no env override; no app-bundle paths present; shutil.which finds chrome.
    MOCK SETUP: Path.exists→False for all paths; shutil.which returns
        '/usr/bin/chrome' for the 'chrome' command name.
    EXPECTED BEHAVIOR: returns the path reported by shutil.which.
    """
    monkeypatch.delenv(_BROWSER_ENV, raising=False)
    monkeypatch.setattr("forge.gen_c4.Path.exists", lambda _self: False)
    monkeypatch.setattr(
        shutil, "which", lambda cmd: "/usr/bin/chrome" if cmd == "chrome" else None
    )
    assert _find_headless_browser() == "/usr/bin/chrome"


def test_find_headless_browser_nothing_found_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_find_headless_browser returns None when no browser is discoverable anywhere.

    SCENARIO: no env override, no app-bundle paths, shutil.which finds nothing.
    MOCK SETUP: FORGE_C4_BROWSER unset; Path.exists→False; shutil.which→None.
    EXPECTED BEHAVIOR: returns None.
    """
    monkeypatch.delenv(_BROWSER_ENV, raising=False)
    monkeypatch.setattr("forge.gen_c4.Path.exists", lambda _self: False)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert _find_headless_browser() is None


# --- #137: _emit_pdf ---


def test_emit_pdf_no_browser_returns_1_with_actionable_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_emit_pdf returns 1 and logs an actionable error when no browser is found.

    SCENARIO: headless browser discovery finds nothing.
    MOCK SETUP: forge.gen_c4._find_headless_browser patched to return None.
    EXPECTED BEHAVIOR: _emit_pdf returns 1; the error log names FORGE_C4_BROWSER
        so the user knows how to provide an explicit override.
    """
    monkeypatch.setattr("forge.gen_c4._find_headless_browser", lambda: None)
    config = C4Config(system="Test", description="", output="")
    args = argparse.Namespace(output=None, check=False)
    with caplog.at_level(logging.ERROR, logger="forge.gen_c4"):
        result = _emit_pdf(tmp_path, config, set(), args)
    assert result == 1
    assert _BROWSER_ENV in caplog.text


def test_emit_pdf_success_writes_pdf_and_returns_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_emit_pdf returns 0 and the PDF exists when the headless print succeeds.

    SCENARIO: browser found; headless print-to-PDF completes without error.
    MOCK SETUP: _find_headless_browser patched to a fake path; _print_html_to_pdf
        replaced by a stub that writes a minimal bytes file at its pdf_path arg.
    EXPECTED BEHAVIOR: _emit_pdf returns 0; docs/architecture.pdf exists under
        tmp_path (the DEFAULT_PDF_OUTPUT destination).
    """

    def _fake_print(browser: str, html_path: object, pdf_path: object) -> None:
        pdf_path.write_bytes(b"%PDF-1.4 stub")  # type: ignore[union-attr]

    monkeypatch.setattr("forge.gen_c4._find_headless_browser", lambda: "/fake/chrome")
    monkeypatch.setattr("forge.gen_c4._print_html_to_pdf", _fake_print)
    config = C4Config(system="Test", description="", output="")
    args = argparse.Namespace(output=None, check=False)
    result = _emit_pdf(tmp_path, config, set(), args)
    assert result == 0
    assert (tmp_path / DEFAULT_PDF_OUTPUT).is_file()


def test_emit_pdf_subprocess_error_returns_1_and_logs_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_emit_pdf returns 1 and logs the failure when the browser process errors.

    SCENARIO: browser found; headless print raises CalledProcessError.
    MOCK SETUP: _find_headless_browser patched to a fake path; _print_html_to_pdf
        raises subprocess.CalledProcessError(1, 'chrome', stderr=b'boom').
    EXPECTED BEHAVIOR: _emit_pdf returns 1; error referencing the browser failure
        is logged so the stderr detail is surfaced to the user.
    """

    def _raise_cpe(browser: str, html_path: object, pdf_path: object) -> None:
        raise subprocess.CalledProcessError(1, "chrome", stderr=b"boom")

    monkeypatch.setattr("forge.gen_c4._find_headless_browser", lambda: "/fake/chrome")
    monkeypatch.setattr("forge.gen_c4._print_html_to_pdf", _raise_cpe)
    config = C4Config(system="Test", description="", output="")
    args = argparse.Namespace(output=None, check=False)
    with caplog.at_level(logging.ERROR, logger="forge.gen_c4"):
        result = _emit_pdf(tmp_path, config, set(), args)
    assert result == 1
    assert "Headless browser" in caplog.text


def test_emit_pdf_check_mode_is_noop_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_emit_pdf with --check writes nothing and never invokes the browser.

    SCENARIO: --check requested for the binary, non-deterministic PDF artifact.
    MOCK SETUP: _find_headless_browser patched to raise if called, proving the
        check path returns before any browser discovery or write.
    EXPECTED BEHAVIOR: returns 0; no PDF is written at DEFAULT_PDF_OUTPUT.
    """

    def _boom() -> str:
        msg = "browser discovery must not run in --check mode"
        raise AssertionError(msg)

    monkeypatch.setattr("forge.gen_c4._find_headless_browser", _boom)
    config = C4Config(system="Test", description="", output="")
    args = argparse.Namespace(output=None, check=True)
    assert _emit_pdf(tmp_path, config, set(), args) == 0
    assert not (tmp_path / DEFAULT_PDF_OUTPUT).exists()


# --- #137: _print_html_to_pdf ---


def test_print_html_to_pdf_invokes_browser_correctly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_print_html_to_pdf builds the correct subprocess argv for the headless browser.

    SCENARIO: subprocess.run is replaced so no real browser is launched.
    MOCK SETUP: subprocess.run replaced with a call-capturing no-op; html_path is
        a real tmp_path file so as_uri() produces a valid file:// URL.
    EXPECTED BEHAVIOR: captured argv[0] is the browser path; '--headless' is
        present; a '--print-to-pdf=<pdf_path>' token is present; the file:// URI
        of html_path appears as the last positional argument; check=True and a
        timeout kwarg are passed to subprocess.run.
    """
    calls: list[dict] = []

    def _fake_run(cmd: list, **kwargs: object) -> None:
        calls.append({"cmd": list(cmd), "kwargs": kwargs})

    monkeypatch.setattr(subprocess, "run", _fake_run)

    html_path = tmp_path / "arch.html"
    html_path.write_text("<html></html>")
    pdf_path = tmp_path / "arch.pdf"

    _print_html_to_pdf("/fake/browser", html_path, pdf_path)

    assert len(calls) == 1
    argv = calls[0]["cmd"]
    kwargs = calls[0]["kwargs"]
    assert argv[0] == "/fake/browser"
    assert "--headless" in argv
    assert f"--print-to-pdf={pdf_path}" in argv
    assert html_path.as_uri() in argv
    assert kwargs.get("check") is True
    assert "timeout" in kwargs


# --- #137: _build_views ---


def test_build_views_labels_and_graph_sources() -> None:
    """_build_views returns ordered tab labels with graph-starting sources."""
    config = _two_container_config(
        (
            Component("App", ("demo.app",), container="Applications"),
            Component("Lib", ("demo.lib",), container="Domain libraries"),
        )
    )
    views = _build_views(config, set())
    labels = [label for label, _ in views]
    assert labels == [
        "System Context",
        "Containers",
        "Applications Components",
        "Domain libraries Components",
    ]
    for label, source in views:
        assert source.startswith("graph "), (
            f"view '{label}' source does not start with 'graph '"
        )


# --- #137: print CSS surface ---


def test_render_html_has_print_media_rules() -> None:
    """render_html includes @media print, @page, and view-title CSS rules."""
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("V", "graph LR\n")])
    assert "@media print" in page
    assert "@page" in page
    assert 'class="view-title"' in page


# --- #137: argparse / main wiring for the pdf format ---


def test_main_pdf_format_no_browser_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() with --format pdf exits 1 when no headless browser is found.

    SCENARIO: C4 config is present and valid; --format pdf is requested; browser
        discovery returns None.
    MOCK SETUP: repo_root patched to tmp_path; _find_headless_browser patched to
        return None; sys.argv set to ['forge-gen-c4', '--format', 'pdf'].
    EXPECTED BEHAVIOR: main() returns 1, confirming that the pdf format is wired
        through _parse_args and routed to _emit_pdf.
    """
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text('system = "Demo"\n')
    monkeypatch.setattr("forge.gen_c4.repo_root", lambda: tmp_path)
    monkeypatch.setattr("forge.gen_c4._find_headless_browser", lambda: None)
    monkeypatch.setattr("sys.argv", ["forge-gen-c4", "--format", "pdf"])
    assert main() == 1


# --- #137: tunable PDF page setup (pdf_* keys) + fit-to-page geometry ---


def test_parse_render_config_pdf_keys_default() -> None:
    """_parse_render_config defaults reproduce A4 landscape contain at 10mm."""
    cfg = _parse_render_config({})
    assert cfg.pdf_page_size == "A4"
    assert cfg.pdf_orientation == "landscape"
    assert cfg.pdf_fit == "contain"
    assert cfg.pdf_margin == 10


def test_parse_render_config_pdf_keys_override() -> None:
    """_parse_render_config reads each pdf_* key from the render table."""
    cfg = _parse_render_config(
        {
            "render": {
                "pdf_page_size": "A3",
                "pdf_orientation": "portrait",
                "pdf_fit": "width",
                "pdf_margin": 20,
            }
        }
    )
    assert cfg.pdf_page_size == "A3"
    assert cfg.pdf_orientation == "portrait"
    assert cfg.pdf_fit == "width"
    assert cfg.pdf_margin == 20


def test_pdf_page_geometry_a4_landscape_default() -> None:
    """A4 landscape: 297x210mm page, 10mm margin, printable px at 96dpi."""
    page_w, page_h, margin, w_px, h_px = _pdf_page_geometry(RenderConfig())
    assert (page_w, page_h, margin) == (297, 210, 10)
    # (297 - 20) * 96/25.4 ~= 1047; height also loses the title reserve.
    assert w_px == 1047
    assert h_px < round((210 - 20) * 96 / 25.4)  # title reserve subtracted


def test_pdf_page_geometry_portrait_swaps_dimensions() -> None:
    """Portrait orientation makes the short edge the page width."""
    page_w, page_h, _margin, _w, _h = _pdf_page_geometry(
        RenderConfig(pdf_orientation="portrait")
    )
    assert (page_w, page_h) == (210, 297)


def test_pdf_page_geometry_unknown_size_falls_back_to_a4() -> None:
    """An unrecognized pdf_page_size resolves to A4 rather than raising."""
    assert _pdf_page_geometry(RenderConfig(pdf_page_size="Nonsense")) == (
        _pdf_page_geometry(RenderConfig())
    )


def test_pdf_page_geometry_margin_widens_when_smaller() -> None:
    """A smaller margin yields a larger printable width."""
    _w0 = _pdf_page_geometry(RenderConfig(pdf_margin=10))[3]
    _w1 = _pdf_page_geometry(RenderConfig(pdf_margin=5))[3]
    assert _w1 > _w0


def test_print_page_css_contain_uses_scale_var() -> None:
    """Contain fit drives the SVG off the JS-measured --c4-print-scale var."""
    css = _print_page_css(RenderConfig(pdf_fit="contain"))
    assert "--c4-print-scale" in css
    # `zoom` (not transform) so the layout box reflows and the title stays on
    # the diagram's own page.
    assert "zoom: var(--c4-print-scale" in css


def test_print_page_css_width_caps_to_page_width() -> None:
    """Width fit caps the SVG to the page width and uses no print scale var."""
    css = _print_page_css(RenderConfig(pdf_fit="width"))
    assert "--c4-print-scale" not in css
    assert "max-width: 100%" in css


def test_print_page_css_page_rule_reflects_size_and_margin() -> None:
    """The @page rule carries the resolved size (mm) and margin."""
    css = _print_page_css(
        RenderConfig(pdf_page_size="Letter", pdf_orientation="portrait", pdf_margin=20)
    )
    assert "@page { size: 216mm 279mm; margin: 20mm; }" in css


def test_render_html_emits_page_setup_and_print_config() -> None:
    """render_html injects the @page rule and the window.c4Print fit config."""
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("V", "graph LR\n")])
    assert "@page { size: 297mm 210mm; margin: 10mm; }" in page
    assert "window.c4Print" in page
    assert '"fit": "contain"' in page


# --- #124: click-to-open-tab map + edge-id-based hover incidence ---


def test_render_html_emits_tab_map_by_container_id() -> None:
    """render_html maps each container's node id (slug) to its Component pane."""
    config = _two_container_config(())
    views = _build_views(config, set())
    page = render_html(config, views)
    # Containers occupy panes 2 and 3 (after System Context + Containers views).
    assert '"id": "applications", "pane": 2' in page
    assert '"id": "domain_libraries", "pane": 3' in page
    assert "window.c4ShowPane = show;" in page


def test_render_html_tab_map_prefix_names_get_distinct_ids() -> None:
    """A container whose name prefixes another's still maps to its own exact id."""
    config = C4Config(
        system="S",
        description="",
        output="",
        containers=(Container("Foo", "", ""), Container("Foo server", "", "")),
    )
    page = render_html(config, _build_views(config, set()))
    # Exact, distinct ids — never a substring/prefix match.
    assert '"id": "foo", "pane": 2' in page
    assert '"id": "foo_server", "pane": 3' in page


def test_edge_endpoints_extracts_exact_pairs() -> None:
    """_edge_endpoints reads exact [src, tgt] ids from the edge lines, in order."""
    text = (
        "graph LR\n"
        '    foo -->|"uses"| bar\n'
        '    foo_server -->|"reads"| foo\n'
        '    bar["label with --> arrow inside"]\n'
    )
    assert _edge_endpoints(text) == [["foo", "bar"], ["foo_server", "foo"]]


def test_edge_endpoints_prefix_ids_not_confused() -> None:
    """An edge between prefix-overlapping ids resolves to the exact endpoints.

    `foo` is a prefix of `foo_server`; parsing the rendered edge DOM id
    `L_foo_foo_server_0_0` would ambiguously split to foo->foo, but the source
    line carries the ids as separate tokens, so the endpoints are exact.
    """
    text = 'graph LR\n    foo -->|"calls"| foo_server\n'
    assert _edge_endpoints(text) == [["foo", "foo_server"]]


def test_html_interaction_script_resolves_edges_by_exact_id() -> None:
    """Hover incidence uses the Python-emitted exact endpoints, not id/name parsing."""
    script = _html_interaction_script()
    # Exact per-pane endpoints, keyed by id.
    assert "window.c4Edges" in script
    assert "endpoints[i]" in script
    assert "tabs[i].id === id" in script
    # The ambiguous edge-id parser and class hooks must be gone.
    assert "edgeEnds" not in script
    assert "replace(/^L_/" not in script
    assert "LS-" not in script


def test_interaction_has_no_magnify_and_scopes_edge_labels() -> None:
    """No click-magnify; hover dims edge labels except the highlighted ones."""
    script = _html_interaction_script()
    css = _html_interaction_css()
    # Click-magnify was removed: no scaling, scroll, or magnify class/handlers.
    assert "c4-magnify" not in script
    assert "c4-magnify" not in css
    assert "scrollIntoView" not in script
    # Only the hovered connection's text stays: non-incident labels dim, the
    # highlighted (c4-on) labels are restored to full opacity.
    assert "svg.c4-focus-mode g.edgeLabel { opacity: 0.1; }" in css
    assert "svg.c4-focus-mode g.edgeLabel.c4-on { opacity: 1; }" in css
    # The script maps labels to edges and toggles their highlight class.
    assert "g.edgeLabels g.edgeLabel" in script
    assert 'labels[i].classList.toggle("c4-on"' in script
