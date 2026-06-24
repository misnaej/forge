"""Generate a C4 architecture model as Structurizr DSL.

Forge already builds an internal Python import graph
(:func:`forge.audit.deps.build_module_graph`). This generator turns that
graph — plus a human-authored ``[tool.forge.c4]`` config — into a
`Structurizr DSL <https://docs.structurizr.com/dsl>`_ text artifact (the
DSL is Apache-2.0; the Structurizr Lite renderer is MIT). Forge emits the
DSL and renders nothing itself, staying deterministic and lock-in-free —
the Structurizr CLI re-exports the same model to PlantUML, Mermaid, etc.

The deterministic / reasoned split (see
``docs/proposals/c4-generator.md``): the *human* declares the System
Context (people, external systems), Containers, and which modules form
which Component — all in ``[tool.forge.c4]``. The *machine* (this CLI)
derives the Component-to-Component relationships from the import graph:
an edge ``A -> B`` is drawn whenever any module in component ``A`` imports
any module in component ``B``.

Scope (v1): the Code level is intentionally skipped, and component
dependencies are inferred within a single container (the first one
declared). Modules matching no component prefix are reported as a
coverage warning, never silently dropped.

Usage::

    forge-gen-c4              # write the output (default: docs/architecture.dsl)
    forge-gen-c4 --check      # verify the committed DSL is in sync; do not write
    forge-gen-c4 --output -   # write the DSL to stdout

Exit Codes:
    0: The DSL was written (or printed), or it is in sync (``--check``).
    1: ``--check`` detected drift, or ``[tool.forge.c4]`` is missing/empty.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from forge.audit.common import Scope
from forge.audit.deps import build_module_graph
from forge.config import read_pyproject_raw, resolve_tool_roots
from forge.gen_common import check_doc_drift
from forge.git_utils import configure_cli_logging, repo_root


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


DEFAULT_OUTPUT = "docs/architecture.dsl"
REGEN_CMD = "forge-gen-c4"


@dataclass(frozen=True)
class Person:
    """A C4 actor — someone who uses the system (System Context level).

    Attributes:
        name: Display name (e.g. ``"Forge developer"``).
        description: One-line role description.
        uses: Label for this person's relationship to the system.
    """

    name: str
    description: str
    uses: str


@dataclass(frozen=True)
class External:
    """An external software system the system depends on.

    Attributes:
        name: Display name (e.g. ``"GitHub"``).
        description: One-line description.
        relationship: Label for the system → external relationship.
    """

    name: str
    description: str
    relationship: str


@dataclass(frozen=True)
class Container:
    """A deployable unit inside the system (Container level).

    Attributes:
        name: Display name (e.g. ``"forge-scripts"``).
        technology: Technology tag (e.g. ``"Python pip package"``).
        description: One-line description.
    """

    name: str
    technology: str
    description: str


@dataclass(frozen=True)
class Component:
    """A named component and the module prefixes that constitute it.

    Attributes:
        name: Display name (e.g. ``"Audit suite"``).
        prefixes: Dotted module prefixes whose modules belong here.
    """

    name: str
    prefixes: tuple[str, ...]


@dataclass(frozen=True)
class Relationship:
    """A human-declared component-to-component relationship.

    Captures runtime/subprocess "uses" edges that the import graph cannot
    see — e.g. the pre-commit dispatcher shelling out to each verifier.

    Attributes:
        source: Source component display name.
        destination: Destination component display name.
        description: Relationship label.
    """

    source: str
    destination: str
    description: str


@dataclass(frozen=True)
class C4Config:
    """The human-authored ``[tool.forge.c4]`` model skeleton.

    Attributes:
        system: System name (System Context level).
        description: One-line system description.
        output: Repo-relative path for the emitted DSL.
        persons: External actors.
        externals: External software systems.
        containers: Deployable units; v1 attaches all components to the
            first one.
        components: Named components with their module prefixes.
        relationships: Human-declared component edges (runtime/subprocess
            "uses" the import graph cannot derive).
    """

    system: str
    description: str
    output: str
    persons: tuple[Person, ...] = ()
    externals: tuple[External, ...] = ()
    containers: tuple[Container, ...] = ()
    components: tuple[Component, ...] = ()
    relationships: tuple[Relationship, ...] = ()


@dataclass(frozen=True)
class _IdMaps:
    """Maps display names to unique DSL-safe identifiers.

    Attributes:
        sys_id: Identifier for the in-scope software system.
        person_ids: Person name → DSL identifier.
        external_ids: External-system name → DSL identifier.
        container_ids: Container name → DSL identifier.
        component_ids: Component name → DSL identifier.
    """

    sys_id: str
    person_ids: dict[str, str]
    external_ids: dict[str, str]
    container_ids: dict[str, str]
    component_ids: dict[str, str]


@dataclass
class _IdAllocator:
    """Allocates unique, DSL-safe identifiers from display names.

    Attributes:
        used: Identifiers already handed out, for collision suffixing.
    """

    used: set[str] = field(default_factory=set)

    def allocate(self, name: str, fallback: str) -> str:
        """Return a unique identifier derived from *name*.

        Args:
            name: Display name to slugify.
            fallback: Stem used when *name* slugifies to empty.

        Returns:
            A unique ``[a-z0-9_]`` identifier not starting with a digit.
        """
        stem = _slug(name) or fallback
        candidate = stem
        suffix = 2
        while candidate in self.used:
            candidate = f"{stem}_{suffix}"
            suffix += 1
        self.used.add(candidate)
        return candidate


def _slug(name: str) -> str:
    """Slugify *name* into a DSL-safe identifier fragment.

    Args:
        name: Arbitrary display name.

    Returns:
        Lowercased ``[a-z0-9_]`` string with a leading letter guaranteed
        (an ``x`` is prefixed when the slug would start with a digit), or
        an empty string when *name* has no alphanumeric content.
    """
    out = "".join(ch if ch.isalnum() else "_" for ch in name.lower())
    out = "_".join(part for part in out.split("_") if part)
    if out and out[0].isdigit():
        out = f"x{out}"
    return out


def _q(text: str) -> str:
    """Quote *text* as a Structurizr DSL string literal.

    Args:
        text: Raw text that may contain double quotes.

    Returns:
        The text wrapped in double quotes with inner quotes escaped.
    """
    return '"' + text.replace('"', '\\"') + '"'


def _coerce_list(raw: object) -> list[dict]:
    """Return *raw* as a list of dicts, tolerating a single table.

    Args:
        raw: A value from the parsed TOML (list, dict, or missing).

    Returns:
        A list of dict entries; ``[]`` when *raw* is absent or malformed.
    """
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


DEFAULT_MODEL_FILE = "c4.toml"


def _read_toml_file(path: Path) -> dict | None:
    """Parse a standalone TOML file, degrading to ``None`` on any failure.

    Args:
        path: Path to the TOML model file.

    Returns:
        Parsed table, or ``None`` when the file is missing, unreadable, or
        not valid TOML.
    """
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        logger.exception("Could not read C4 model file %s", path)
        return None


def resolve_model_section(root: Path) -> dict | None:
    """Locate the C4 model table — external file or inline pyproject.

    Resolution, highest precedence first:

    1. ``[tool.forge.c4].config`` — an explicit path to a standalone TOML
       model file (the model's tables live at that file's top level).
    2. A conventional ``c4.toml`` at the repo root (used when present and
       ``[tool.forge.c4]`` carries no inline ``system``).
    3. The inline ``[tool.forge.c4]`` table itself.

    Keeping the verbose model out of ``pyproject.toml`` is the point of
    (1)/(2): a Structurizr model is its own artifact, like ``ruff.toml``.

    Args:
        root: Repository root directory.

    Returns:
        The model table dict, or ``None`` when C4 generation is not opted
        into (no section, no file, and no inline ``system``).
    """
    section = read_pyproject_raw(root).get("tool", {}).get("forge", {}).get("c4", {})
    configured = section.get("config")
    if configured:
        return _read_toml_file(root / configured)
    if not section.get("system"):
        return _read_toml_file(root / DEFAULT_MODEL_FILE)
    return section


def load_c4_config(root: Path) -> C4Config | None:
    """Load the C4 model skeleton for the repo.

    Reads the model table from an external ``c4.toml`` (preferred) or the
    inline ``[tool.forge.c4]`` section — see :func:`resolve_model_section`.

    Args:
        root: Repository root directory.

    Returns:
        A populated :class:`C4Config`, or ``None`` when C4 generation is
        not opted into or the model declares no ``system`` name.
    """
    section = resolve_model_section(root)
    if not section or not section.get("system"):
        return None
    persons = tuple(
        Person(p.get("name", "?"), p.get("description", ""), p.get("uses", "uses"))
        for p in _coerce_list(section.get("person"))
    )
    externals = tuple(
        External(
            e.get("name", "?"), e.get("description", ""), e.get("relationship", "uses")
        )
        for e in _coerce_list(section.get("external"))
    )
    containers = tuple(
        Container(c.get("name", "?"), c.get("technology", ""), c.get("description", ""))
        for c in _coerce_list(section.get("container"))
    )
    raw_components = section.get("components", {})
    components = tuple(
        Component(name, tuple(prefixes))
        for name, prefixes in raw_components.items()
        if isinstance(prefixes, list)
    )
    relationships = tuple(
        Relationship(
            r.get("source", "?"),
            r.get("destination", "?"),
            r.get("description", "uses"),
        )
        for r in _coerce_list(section.get("relationship"))
    )
    return C4Config(
        system=section["system"],
        description=section.get("description", ""),
        output=section.get("output", DEFAULT_OUTPUT),
        persons=persons,
        externals=externals,
        containers=containers,
        components=components,
        relationships=relationships,
    )


def assign_components(
    modules: list[str],
    components: tuple[Component, ...],
) -> tuple[dict[str, str], list[str]]:
    """Map each module to a component by longest-prefix match.

    Args:
        modules: Dotted module names discovered in the import graph.
        components: Configured components with their prefixes.

    Returns:
        Tuple of (module → component name for matched modules, sorted list
        of unmatched module names).
    """
    pairs = sorted(
        ((prefix, comp.name) for comp in components for prefix in comp.prefixes),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    assigned: dict[str, str] = {}
    unmatched: list[str] = []
    for module in modules:
        match = next(
            (name for prefix, name in pairs if _under_prefix(module, prefix)),
            None,
        )
        if match is None:
            unmatched.append(module)
        else:
            assigned[module] = match
    return assigned, sorted(unmatched)


def _under_prefix(module: str, prefix: str) -> bool:
    """Return whether *module* equals *prefix* or is a dotted child of it.

    Args:
        module: Dotted module name (e.g. ``"forge.audit.deps"``).
        prefix: Configured component prefix (e.g. ``"forge.audit"``).

    Returns:
        True when *module* is *prefix* itself or nested beneath it, so a
        ``forge.audit`` prefix matches ``forge.audit`` and
        ``forge.audit.deps`` but not ``forge.auditor``.
    """
    return module == prefix or module.startswith(f"{prefix}.")


def derive_component_edges(
    graph: dict[str, set[str]],
    assigned: dict[str, str],
) -> set[tuple[str, str]]:
    """Collapse module-level import edges to component-level edges.

    Args:
        graph: Module → imported-modules adjacency map.
        assigned: Module → component-name mapping.

    Returns:
        Set of ``(source-component, target-component)`` pairs, excluding
        self-edges and any edge touching an unassigned module.
    """
    edges: set[tuple[str, str]] = set()
    for src, targets in graph.items():
        src_comp = assigned.get(src)
        if src_comp is None:
            continue
        for target in targets:
            dst_comp = assigned.get(target)
            if dst_comp is not None and dst_comp != src_comp:
                edges.add((src_comp, dst_comp))
    return edges


def render_dsl(
    config: C4Config,
    edges: set[tuple[str, str]],
) -> str:
    """Render the full Structurizr DSL workspace text.

    Args:
        config: The human-authored model skeleton.
        edges: Derived component-to-component relationships.

    Returns:
        Deterministic multi-line DSL ending in a trailing newline.
    """
    alloc = _IdAllocator()
    sys_id = alloc.allocate(config.system, "system")
    person_ids = {p.name: alloc.allocate(p.name, "person") for p in config.persons}
    external_ids = {e.name: alloc.allocate(e.name, "system") for e in config.externals}
    container_ids = {
        c.name: alloc.allocate(c.name, "container") for c in config.containers
    }
    component_ids = {
        c.name: alloc.allocate(c.name, "component") for c in config.components
    }
    ids = _IdMaps(sys_id, person_ids, external_ids, container_ids, component_ids)

    lines = [
        f"workspace {_q(config.system)} {_q(config.description)} {{",
        "",
        "    model {",
    ]
    lines += _render_model(config, ids)
    lines += _render_relationships(config, ids, edges)
    lines.append("    }")
    lines.append("")
    lines += _render_views(config, ids.sys_id, ids.container_ids)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _render_model(config: C4Config, ids: _IdMaps) -> list[str]:
    """Render the ``model`` block's element declarations.

    Args:
        config: The model skeleton.
        ids: All DSL identifier mappings.

    Returns:
        Indented DSL lines declaring persons, the system with its
        containers and components, and external systems.
    """
    lines = [
        f"        {ids.person_ids[p.name]} = person {_q(p.name)} {_q(p.description)}"
        for p in config.persons
    ]
    lines.append(
        f"        {ids.sys_id} = softwareSystem {_q(config.system)} "
        f"{_q(config.description)} {{"
    )
    for idx, container in enumerate(config.containers):
        cid = ids.container_ids[container.name]
        lines.append(
            f"            {cid} = container {_q(container.name)} "
            f"{_q(container.description)} {_q(container.technology)} {{"
        )
        # v1: all components attach to the first container.
        if idx == 0:
            lines += [
                f"                {ids.component_ids[c.name]} = component "
                f"{_q(c.name)} {_q(_component_summary(c))}"
                for c in config.components
            ]
        lines.append("            }")
    lines.append("        }")
    lines += [
        f"        {ids.external_ids[e.name]} = softwareSystem {_q(e.name)} "
        f"{_q(e.description)}"
        for e in config.externals
    ]
    return lines


def _component_summary(component: Component) -> str:
    """Return a one-line description naming a component's module prefixes.

    Args:
        component: The component to describe.

    Returns:
        A short string listing the configured prefixes.
    """
    return ", ".join(component.prefixes)


def _render_relationships(
    config: C4Config,
    ids: _IdMaps,
    edges: set[tuple[str, str]],
) -> list[str]:
    """Render the relationship statements of the ``model`` block.

    Args:
        config: The model skeleton.
        ids: All DSL identifier mappings.
        edges: Derived component-to-component relationships.

    Returns:
        Indented DSL relationship lines (person→system, system→external,
        component→component), each sorted for determinism.
    """
    lines = ["", "        # relationships"]
    lines += [
        f"        {ids.person_ids[p.name]} -> {ids.sys_id} {_q(p.uses)}"
        for p in config.persons
    ]
    lines += [
        f"        {ids.sys_id} -> {ids.external_ids[e.name]} {_q(e.relationship)}"
        for e in config.externals
    ]
    # Human-declared component edges (runtime/subprocess "uses"). Rendered
    # first; any derived import edge with the same (source, destination) is
    # suppressed so the diagram shows one arrow with the richer label.
    declared_pairs = {(r.source, r.destination) for r in config.relationships}
    lines += [
        f"        {ids.component_ids[r.source]} -> "
        f"{ids.component_ids[r.destination]} {_q(r.description)}"
        for r in config.relationships
        if r.source in ids.component_ids and r.destination in ids.component_ids
    ]
    lines += [
        f"        {ids.component_ids[src]} -> {ids.component_ids[dst]} {_q('uses')}"
        for src, dst in sorted(edges)
        if (src, dst) not in declared_pairs
    ]
    return lines


def _render_views(
    config: C4Config,
    sys_id: str,
    container_ids: dict[str, str],
) -> list[str]:
    """Render the ``views`` block.

    Args:
        config: The model skeleton.
        sys_id: Identifier for the in-scope software system.
        container_ids: Container name → DSL identifier.

    Returns:
        DSL lines for the systemContext, container, and (when a container
        exists) component views, plus the default theme.
    """
    lines = [
        "    views {",
        f"        systemContext {sys_id} {_q('SystemContext')} {{",
        "            include *",
        "            autolayout lr",
        "        }",
        f"        container {sys_id} {_q('Containers')} {{",
        "            include *",
        "            autolayout lr",
        "        }",
    ]
    if config.containers:
        primary = container_ids[config.containers[0].name]
        lines += [
            f"        component {primary} {_q('Components')} {{",
            "            include *",
            "            autolayout lr",
            "        }",
        ]
    lines += ["        theme default", "    }"]
    return lines


def generate(root: Path, roots: list[Path]) -> tuple[str, list[str]] | None:
    """Build the DSL text and unmatched-module list for the repo.

    Args:
        root: Repository root directory.
        roots: Source-root directories to scan for the import graph.

    Returns:
        Tuple of (DSL text, sorted unmatched module names), or ``None``
        when ``[tool.forge.c4]`` is absent (the opt-in signal).
    """
    config = load_c4_config(root)
    if config is None:
        return None
    modules, graph = build_module_graph(Scope.FULL, roots)
    assigned, unmatched = assign_components(sorted(modules), config.components)
    edges = derive_component_edges(graph, assigned)
    return render_dsl(config, edges), unmatched


def _warn_unmatched(unmatched: list[str]) -> None:
    """Log a coverage warning naming modules in no component.

    Args:
        unmatched: Module names that matched no component prefix.
    """
    if unmatched:
        logger.warning(
            "%d module(s) match no [tool.forge.c4.components] prefix and are "
            "absent from the diagram: %s",
            len(unmatched),
            ", ".join(unmatched),
        )


def main() -> int:
    """Generate or verify the C4 Structurizr DSL artifact.

    Returns:
        Exit code: ``0`` on a successful write/print or in-sync check;
        ``1`` on drift, a missing artifact, or absent ``[tool.forge.c4]``.
    """
    parser = argparse.ArgumentParser(
        prog="forge-gen-c4",
        description=(
            "Generate a C4 architecture model (Structurizr DSL) from the "
            "import graph + [tool.forge.c4] config."
        ),
    )
    parser.add_argument(
        "--roots",
        nargs="*",
        default=None,
        help="Source dirs to scan. Defaults to the repo's configured source roots.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed DSL is in sync; do not write.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Override the output path. Use '-' to write to stdout.",
    )
    args = parser.parse_args()

    root = repo_root()
    roots = _resolve_roots(root, args.roots)
    result = generate(root, roots)
    if result is None:
        logger.error(
            "No [tool.forge.c4] config found — add it to pyproject.toml to "
            "enable C4 generation (see docs/proposals/c4-generator.md).",
        )
        return 1
    dsl, unmatched = result
    _warn_unmatched(unmatched)

    config = load_c4_config(root)
    out_relpath = args.output or (config.output if config else DEFAULT_OUTPUT)

    if args.check:
        return check_doc_drift(root, out_relpath, dsl, REGEN_CMD)
    if out_relpath == "-":
        sys.stdout.write(dsl)
        return 0
    out_path = root / out_relpath
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dsl)
    logger.info("Wrote %s (%d component edges).", out_relpath, dsl.count(" -> "))
    return 0


def _resolve_roots(root: Path, explicit: list[str] | None) -> list[Path]:
    """Resolve the source roots to scan for the import graph.

    Args:
        root: Repository root directory.
        explicit: Roots passed via ``--roots``, or ``None`` to use the
            repo's configured source roots.

    Returns:
        Existing source-root directories inside the repo.
    """
    if explicit:
        return [root / r for r in explicit if (root / r).is_dir()]
    return [root / d for d in resolve_tool_roots(root, "c4")]


if __name__ == "__main__":
    raise SystemExit(main())
