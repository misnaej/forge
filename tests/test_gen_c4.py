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
    page = render_html(config, render_mermaid(config, set()))
    assert '<pre class="mermaid">' in page
    assert f'src="{"mermaid.min.js"}"' in page
    assert "&lt;b&gt;" in page  # literal <b> escaped for the <pre>


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
