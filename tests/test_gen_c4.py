"""Tests for the forge-gen-c4 C4 / Structurizr DSL generator."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge.gen_c4 import (
    README_C4_END,
    README_C4_START,
    C4Config,
    Component,
    Container,
    Relationship,
    _derive_container_edges,
    _render_mermaid_components_for,
    _render_mermaid_containers,
    _render_mermaid_system_context,
    _slug,
    _under_prefix,
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
    """System Context: persons and externals; no subgraph or component nodes."""
    _write_pyproject(tmp_path, '[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text(SAMPLE_MODEL)
    config = load_c4_config(tmp_path)
    assert config is not None
    result = _render_mermaid_system_context(config)
    assert "User" in result  # person name present
    assert "GitHub" in result  # external name present
    assert "subgraph" not in result
    assert "Core" not in result  # component name must be absent
    assert "IO" not in result  # component name must be absent


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
