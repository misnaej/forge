"""Tests for the forge-gen-c4 C4 / Structurizr DSL generator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge.gen_c4 import (
    Component,
    Relationship,
    _slug,
    _under_prefix,
    assign_components,
    derive_component_edges,
    generate,
    load_c4_config,
    main,
    render_dsl,
    resolve_model_section,
)


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
