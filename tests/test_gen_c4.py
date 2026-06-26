"""Tests for the forge-gen-c4 C4 / Structurizr DSL generator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from forge.gen_c4 import (
    _EDGE_MODES,
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
    _copy_vendored_mermaid,
    _derive_container_edges,
    _externals_with_declared_incoming,
    _IdMaps,
    _includes_derived,
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
    """render_html sets mermaid flowchart useMaxWidth: false."""
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("V", "graph LR\n")])
    assert "useMaxWidth: false" in page


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
    registerLayoutLoaders call, and ``layout: c4layout`` in mermaid.initialize
    together so a regression that drops any single piece (e.g. removing the
    script tag while keeping the init call) is caught immediately rather than
    silently degrading to dagre at runtime. The dagre-default string is also
    checked to confirm the init variable is visible before the try block runs.
    """
    config = C4Config(system="Test", description="", output="")
    page = render_html(config, [("V", "graph LR\n")])
    assert f'<script src="{MERMAID_ELK_JS_NAME}">' in page
    assert "registerLayoutLoaders" in page
    assert "layout: c4layout" in page
    assert "NETWORK_SIMPLEX" in page
    assert 'var c4layout = "dagre"' in page


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
