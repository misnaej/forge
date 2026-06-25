"""Generate a C4 architecture model as Structurizr DSL.

Forge already builds an internal Python import graph
(:func:`forge.audit.deps.build_module_graph`). This generator turns that
graph — plus a human-authored ``[tool.forge.c4]`` config — into a
`Structurizr DSL <https://docs.structurizr.com/dsl>`_ text artifact (the
DSL is Apache-2.0; the Structurizr Lite renderer is MIT). Forge emits the
DSL and renders nothing itself, staying deterministic and lock-in-free —
the Structurizr CLI re-exports the same model to PlantUML, Mermaid, etc.

The deterministic / reasoned split (see
``docs/c4-architecture.md``): the *human* declares the System
Context (people, external systems), Containers, and which modules form
which Component — all in ``[tool.forge.c4]``. The *machine* (this CLI)
derives the Component-to-Component relationships from the import graph:
an edge ``A -> B`` is drawn whenever any module in component ``A`` imports
any module in component ``B``.

Scope: the Code level is intentionally skipped. Components are
distributed across containers by each component's ``container`` field
(defaulting to the first declared container), and import-graph edges are
drawn between components regardless of which containers they live in.
Modules matching no component prefix are reported as a coverage warning,
never silently dropped.

Usage::

    forge-gen-c4                    # write Structurizr DSL
    forge-gen-c4 --format html      # write offline HTML view
    forge-gen-c4 --format mermaid    # write raw Mermaid to stdout
    forge-gen-c4 --check            # verify committed artifact is in sync
    forge-gen-c4 --output -         # write to stdout

The ``--format html`` output is self-contained and offline: it renders the
model via Mermaid, referencing a vendored ``mermaid.min.js`` (MIT) sidecar
written next to the HTML — no Docker, Java, Graphviz, or network needed.

Exit Codes:
    0: The artifact was written (or printed), or it is in sync (``--check``).
    1: ``--check`` detected drift, or ``[tool.forge.c4]`` is missing/empty.
"""

from __future__ import annotations

import argparse
import html
import logging
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from forge.audit.common import Scope
from forge.audit.deps import build_module_graph
from forge.config import resolve_model_section, resolve_tool_roots
from forge.gen_common import check_doc_drift
from forge.git_utils import configure_cli_logging, repo_root


configure_cli_logging()
logger = logging.getLogger(__name__)


DEFAULT_OUTPUT = "docs/architecture.dsl"
DEFAULT_HTML_OUTPUT = "docs/architecture.html"
REGEN_CMD = "forge-gen-c4"
# Vendored Mermaid UMD bundle (MIT), shipped as forge package data. The HTML
# output references it by relative path so the diagram renders fully offline
# with no external tool — see docs/c4-architecture.md.
MERMAID_JS_NAME = "mermaid.min.js"


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
        description: One-line description of the component's responsibility
            (C4 wants every box to carry meaning). Empty when unspecified.
        technology: Technology tag shown in the box (e.g. ``"Python"``).
            Empty when unspecified.
        container: Display name of the owning container. Empty string means
            "attach to the first declared container" (back-compat default).
    """

    name: str
    prefixes: tuple[str, ...]
    description: str = ""
    technology: str = ""
    container: str = ""


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
        containers: Deployable units. Each component attaches to the
            container its ``container`` field names, or the first declared
            container when that field is empty.
        components: Named components with their module prefixes.
        relationships: Human-declared component edges (runtime/subprocess
            "uses" the import graph cannot derive).
        readme: Repo-relative path to a README that carries the managed
            Mermaid C4 block; empty string when not configured.
    """

    system: str
    description: str
    output: str
    persons: tuple[Person, ...] = ()
    externals: tuple[External, ...] = ()
    containers: tuple[Container, ...] = ()
    components: tuple[Component, ...] = ()
    relationships: tuple[Relationship, ...] = ()
    readme: str = ""


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


def _safe_out_path(root: Path, relpath: str) -> Path:
    """Resolve *relpath* under *root*, rejecting paths that escape the repo.

    Args:
        root: Repository root directory.
        relpath: Repository-relative path (may be relative or absolute).

    Returns:
        Absolute path inside the repository.

    Raises:
        ValueError: When the resolved path escapes the repository root.
    """
    candidate = (root / relpath).resolve()
    if not candidate.is_relative_to(root.resolve()):
        msg = f"output path {relpath!r} escapes the repository root"
        raise ValueError(msg)
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


def _parse_components(section: dict) -> tuple[Component, ...]:
    """Parse components from rich ``[[component]]`` tables + the simple map.

    Two forms are accepted. The rich array-of-tables (``[[component]]`` with
    ``name``/``description``/``technology``/``modules``) carries the
    meaningful box content C4 wants. The shorthand ``[components]`` map
    (``"Name" = [prefixes]``) is a quick start where the description falls
    back to the module-prefix list. Rich entries win on name collision.

    Args:
        section: The model table.

    Returns:
        Components in declaration order: rich entries first, then map-only
        entries whose names no rich entry already claimed.
    """
    rich = [
        Component(
            c.get("name", "?"),
            tuple(c.get("modules", [])),
            c.get("description", ""),
            c.get("technology", ""),
            c.get("container", ""),
        )
        for c in _coerce_list(section.get("component"))
    ]
    named = {c.name for c in rich}
    simple = [
        Component(name, tuple(prefixes))
        for name, prefixes in section.get("components", {}).items()
        if isinstance(prefixes, list) and name not in named
    ]
    return tuple(rich + simple)


def _validate_component_containers(
    components: tuple[Component, ...], containers: tuple[Container, ...]
) -> None:
    """Fail loudly on a duplicate container name or an undeclared reference.

    Args:
        components: Parsed components.
        containers: Declared containers.

    Raises:
        ValueError: When two containers share a name (which would silently
            merge in the name→id map and render a component into both), or
            when a component's non-empty ``container`` is not among the
            declared names. (An empty ``container`` is valid — it means the
            first declared container.)
    """
    seen: set[str] = set()
    for container in containers:
        if container.name in seen:
            msg = f"duplicate container name {container.name!r}"
            raise ValueError(msg)
        seen.add(container.name)
    declared = seen
    for comp in components:
        if comp.container and comp.container not in declared:
            names = ", ".join(sorted(declared)) or "(none)"
            msg = (
                f"component {comp.name!r} names unknown container "
                f"{comp.container!r}; declared containers: {names}"
            )
            raise ValueError(msg)


def load_c4_config(root: Path) -> C4Config | None:
    """Load the C4 model skeleton for the repo.

    Reads the model table from an external ``c4.toml`` (preferred) or the
    inline ``[tool.forge.c4]`` section — see :func:`resolve_model_section`.

    Args:
        root: Repository root directory.

    Returns:
        A populated :class:`C4Config`, or ``None`` when C4 generation is
        not opted into or the model declares no ``system`` name.

    Raises:
        ValueError: When a component names a container that is not declared.
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
    components = _parse_components(section)
    _validate_component_containers(components, containers)
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
        readme=section.get("readme", ""),
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


def _warn_unknown_relationships(
    config: C4Config, component_ids: dict[str, str]
) -> None:
    """Warn for [[relationship]] entries naming a component that doesn't exist.

    Args:
        config: The C4 model skeleton.
        component_ids: Component name → DSL identifier mapping.
    """
    known = set(component_ids)
    for r in config.relationships:
        missing = {r.source, r.destination} - known
        if missing:
            logger.warning(
                "Declared relationship %r -> %r references unknown component(s) %s "
                "— edge skipped",
                r.source,
                r.destination,
                ", ".join(sorted(missing)),
            )


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
    _warn_unknown_relationships(config, component_ids)

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
        lines += [
            f"                {ids.component_ids[c.name]} = component "
            f"{_q(c.name)} {_q(_component_description(c))} {_q(c.technology)}"
            for c in _components_for_container(config, container, idx)
        ]
        lines.append("            }")
    lines.append("        }")
    lines += [
        f"        {ids.external_ids[e.name]} = softwareSystem {_q(e.name)} "
        f"{_q(e.description)}"
        for e in config.externals
    ]
    return lines


def _components_for_container(
    config: C4Config, container: Container, idx: int
) -> list[Component]:
    """Return the components owned by *container*, in declaration order.

    A component is owned by the container its ``container`` field names; an
    empty field means the first declared container. So with no ``container``
    keys anywhere, every component lands in the first container exactly as
    before — byte-identical output.

    Args:
        config: The model skeleton.
        container: The container being rendered.
        idx: The container's index in ``config.containers`` (``0`` is the
            default owner for components with no explicit ``container``).

    Returns:
        Components whose owning container is *container*, preserving
        ``config.components`` order.
    """
    return [
        c
        for c in config.components
        if c.container == container.name or (not c.container and idx == 0)
    ]


def _component_description(component: Component) -> str:
    """Return a component's box description for C4 rendering.

    Prefers the human-authored description; falls back to the module-prefix
    list so a shorthand ``[components]`` entry still labels its box.

    Args:
        component: The component to describe.

    Returns:
        The description, or a comma-joined prefix list when none was given.
    """
    return component.description or ", ".join(component.prefixes)


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
        f"        {ids.component_ids[src]} -> {ids.component_ids[dst]} {_q('imports')}"
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
        DSL lines for the systemContext, container, and one component view
        per declared container (each scoped to that container), plus the
        default theme.
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
    for container in config.containers:
        cid = container_ids[container.name]
        lines += [
            f"        component {cid} {_q(f'{container.name} Components')} {{",
            "            include *",
            "            autolayout lr",
            "        }",
        ]
    lines += ["        theme default", "    }"]
    return lines


def build_model(
    root: Path,
    roots: list[Path],
) -> tuple[C4Config, set[tuple[str, str]], list[str]] | None:
    """Assemble the C4 model: config, derived edges, and unmatched modules.

    The shared seam behind every output format — it loads the human model
    skeleton and derives the component-to-component edges from the import
    graph, so the DSL and HTML renderers operate on identical inputs.

    Args:
        root: Repository root directory.
        roots: Source-root directories to scan for the import graph.

    Returns:
        Tuple of (config, derived component edges, sorted unmatched module
        names), or ``None`` when C4 generation is not opted into.

    Raises:
        ValueError: When the config is invalid (propagated from
            :func:`load_c4_config`), e.g. a component naming an unknown
            container.
    """
    config = load_c4_config(root)
    if config is None:
        return None
    modules, graph = build_module_graph(Scope.FULL, roots)
    assigned, unmatched = assign_components(sorted(modules), config.components)
    edges = derive_component_edges(graph, assigned)
    return config, edges, unmatched


def generate(root: Path, roots: list[Path]) -> tuple[str, list[str]] | None:
    """Build the DSL text and unmatched-module list for the repo.

    Args:
        root: Repository root directory.
        roots: Source-root directories to scan for the import graph.

    Returns:
        Tuple of (DSL text, sorted unmatched module names), or ``None``
        when ``[tool.forge.c4]`` is absent (the opt-in signal).
    """
    built = build_model(root, roots)
    if built is None:
        return None
    config, edges, unmatched = built
    return render_dsl(config, edges), unmatched


def _m(text: str) -> str:
    """Escape label *text* for safe embedding in a Mermaid node label.

    The Mermaid block lives inside an HTML ``<pre>``, so the browser decodes
    HTML entities before Mermaid parses the text. Escaping ``&``/``<``/``>``
    (but not quotes, which delimit the label) keeps both layers happy.

    Args:
        text: Raw label text (a name, description, or relationship phrase).

    Returns:
        HTML-entity-escaped text safe inside a quoted Mermaid label.
    """
    return html.escape(text, quote=False)


def render_mermaid(config: C4Config, edges: set[tuple[str, str]]) -> str:
    """Render the model as a Mermaid flowchart (offline-renderable).

    A flowchart (``graph LR``) — not Mermaid's experimental C4 diagram type —
    is used deliberately: it is core, non-experimental, and bundled in the
    UMD build, so it renders reliably offline. The container becomes a
    subgraph, components become nodes, and persons/external systems sit
    outside it; the same human-declared + import-derived edges as the DSL.

    Args:
        config: The human-authored model skeleton.
        edges: Derived component-to-component relationships.

    Returns:
        Deterministic Mermaid source ending in a trailing newline.
    """
    alloc = _IdAllocator()
    person_ids = {p.name: alloc.allocate(p.name, "person") for p in config.persons}
    external_ids = {e.name: alloc.allocate(e.name, "ext") for e in config.externals}
    container_ids = {
        c.name: alloc.allocate(c.name, "container") for c in config.containers
    }
    component_ids = {
        c.name: alloc.allocate(c.name, "component") for c in config.components
    }

    lines = ["graph LR"]
    lines += [
        f'    {person_ids[p.name]}(["{_mermaid_box(p.name, "Person", p.description)}"])'
        for p in config.persons
    ]
    lines += [
        f'    {external_ids[e.name]}[["'
        f'{_mermaid_box(e.name, "External system", e.description)}"]]'
        for e in config.externals
    ]
    for idx, container in enumerate(config.containers):
        lines.append(
            f'    subgraph {container_ids[container.name]}["{_m(container.name)}"]'
        )
        lines += [
            f'        {component_ids[c.name]}["'
            f'{_mermaid_box(c.name, c.technology, _component_description(c))}"]'
            for c in _components_for_container(config, container, idx)
        ]
        lines.append("    end")
    ids = {
        "person": person_ids,
        "external": external_ids,
        "container": container_ids,
        "component": component_ids,
    }
    lines += _mermaid_edges(config, ids, edges)
    return "\n".join(lines) + "\n"


def _mermaid_box(name: str, technology: str, description: str) -> str:
    """Build a multi-line Mermaid box label: bold name, technology, description.

    Produces *canonical* Mermaid: the structural tags (``<b>``, ``<br/>``)
    are literal, while the user text is escaped to Mermaid HTML entities so
    characters like ``<`` in "code_health/<step>.log" survive instead of
    being parsed as tags. This canonical form drops straight into a GitHub
    ```` ```mermaid ```` block; :func:`render_html` re-escapes it for the
    HTML ``<pre>`` embedding case.

    Args:
        name: Element display name (bolded).
        technology: Technology/type tag shown in brackets; omitted if empty.
        description: One-line description; omitted if empty.

    Returns:
        A ``<br/>``-separated label with literal tags and entity-escaped text.
    """
    parts = [f"<b>{_m(name)}</b>"]
    if technology:
        parts.append(f"[{_m(technology)}]")
    if description:
        parts.append(_m(description))
    return "<br/>".join(parts)


def _mermaid_edges(
    config: C4Config,
    ids: dict[str, dict[str, str]],
    edges: set[tuple[str, str]],
) -> list[str]:
    """Render the Mermaid relationship lines.

    Args:
        config: The model skeleton.
        ids: Bundled ID mappings: person, external, container, and component
            name → node id.
        edges: Derived component-to-component relationships.

    Returns:
        Mermaid edge lines: person→container, container→external, and the
        declared + derived component edges (declared suppress duplicates).
    """
    person_ids = ids["person"]
    external_ids = ids["external"]
    container_ids = ids["container"]
    component_ids = ids["component"]
    primary = container_ids[config.containers[0].name] if config.containers else None
    lines: list[str] = []
    if primary is not None:
        lines += [
            f'    {person_ids[p.name]} -->|"{_m(p.uses)}"| {primary}'
            for p in config.persons
        ]
        lines += [
            f'    {primary} -->|"{_m(e.relationship)}"| {external_ids[e.name]}'
            for e in config.externals
        ]
    declared = {(r.source, r.destination) for r in config.relationships}
    lines += [
        f'    {component_ids[r.source]} -->|"{_m(r.description)}"| '
        f"{component_ids[r.destination]}"
        for r in config.relationships
        if r.source in component_ids and r.destination in component_ids
    ]
    lines += [
        f'    {component_ids[src]} -->|"imports"| {component_ids[dst]}'
        for src, dst in sorted(edges)
        if (src, dst) not in declared
    ]
    return lines


def _render_mermaid_system_context(config: C4Config) -> str:
    """Render the System Context view: persons, the system, external systems.

    The top C4 level — no containers, no components. Persons (stadium nodes)
    use the system (rounded node); the system relates out to external systems
    (subroutine nodes).

    Args:
        config: The human-authored model skeleton.

    Returns:
        Deterministic Mermaid source ending in a trailing newline.
    """
    alloc = _IdAllocator()
    person_ids = {p.name: alloc.allocate(p.name, "person") for p in config.persons}
    sys_id = alloc.allocate(config.system, "system")
    external_ids = {e.name: alloc.allocate(e.name, "ext") for e in config.externals}
    lines = ["graph LR"]
    lines += [
        f'    {person_ids[p.name]}(["{_mermaid_box(p.name, "Person", p.description)}"])'
        for p in config.persons
    ]
    lines.append(
        f'    {sys_id}("'
        f'{_mermaid_box(config.system, "Software System", config.description)}")'
    )
    lines += [
        f'    {external_ids[e.name]}[["'
        f'{_mermaid_box(e.name, "External system", e.description)}"]]'
        for e in config.externals
    ]
    lines += [
        f'    {person_ids[p.name]} -->|"{_m(p.uses)}"| {sys_id}' for p in config.persons
    ]
    lines += [
        f'    {sys_id} -->|"{_m(e.relationship)}"| {external_ids[e.name]}'
        for e in config.externals
    ]
    return "\n".join(lines) + "\n"


def _derive_container_edges(
    config: C4Config, edges: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Collapse component-level edges to cross-container pairs.

    Maps every component relationship (derived imports + declared edges) to its
    endpoints' owning containers, keeping only pairs that cross a container
    boundary. This is the Container view's relationship summary — never the
    union of component edges (#116).

    Args:
        config: The model skeleton.
        edges: Derived component-to-component import relationships.

    Returns:
        ``(source container, destination container)`` pairs, cross-container
        only (same-container pairs dropped).
    """
    owner: dict[str, str] = {}
    for idx, container in enumerate(config.containers):
        for component in _components_for_container(config, container, idx):
            owner[component.name] = container.name
    component_edges = set(edges) | {
        (r.source, r.destination) for r in config.relationships
    }
    pairs: set[tuple[str, str]] = set()
    for src, dst in component_edges:
        src_owner, dst_owner = owner.get(src), owner.get(dst)
        if src_owner and dst_owner and src_owner != dst_owner:
            pairs.add((src_owner, dst_owner))
    return pairs


def _render_mermaid_containers(
    config: C4Config, container_edges: set[tuple[str, str]]
) -> str:
    """Render the Container view: containers inside the system boundary.

    Shows only the system's containers (plus persons, externals, and their
    relationships) — no component-level boxes or edges. Cross-container
    relationships come summarized in *container_edges* (#116).

    Args:
        config: The model skeleton.
        container_edges: Cross-container pairs from
            :func:`_derive_container_edges`.

    Returns:
        Deterministic Mermaid source ending in a trailing newline.
    """
    alloc = _IdAllocator()
    person_ids = {p.name: alloc.allocate(p.name, "person") for p in config.persons}
    external_ids = {e.name: alloc.allocate(e.name, "ext") for e in config.externals}
    sys_id = alloc.allocate(config.system, "system")
    container_ids = {
        c.name: alloc.allocate(c.name, "container") for c in config.containers
    }
    lines = ["graph LR"]
    lines += [
        f'    {person_ids[p.name]}(["{_mermaid_box(p.name, "Person", p.description)}"])'
        for p in config.persons
    ]
    lines += [
        f'    {external_ids[e.name]}[["'
        f'{_mermaid_box(e.name, "External system", e.description)}"]]'
        for e in config.externals
    ]
    lines.append(f'    subgraph {sys_id}["{_m(config.system)}"]')
    lines += [
        f'        {container_ids[c.name]}["'
        f'{_mermaid_box(c.name, c.technology, c.description)}"]'
        for c in config.containers
    ]
    lines.append("    end")
    primary = container_ids[config.containers[0].name] if config.containers else None
    if primary is not None:
        lines += [
            f'    {person_ids[p.name]} -->|"{_m(p.uses)}"| {primary}'
            for p in config.persons
        ]
        lines += [
            f'    {primary} -->|"{_m(e.relationship)}"| {external_ids[e.name]}'
            for e in config.externals
        ]
    lines += [
        f'    {container_ids[src]} -->|"uses"| {container_ids[dst]}'
        for src, dst in sorted(container_edges)
    ]
    return "\n".join(lines) + "\n"


def _render_mermaid_components_for(
    config: C4Config,
    container: Container,
    idx: int,
    edges: set[tuple[str, str]],
) -> str:
    """Render one container's Component view: its components and their edges.

    Shows only the components owned by *container* and the relationships whose
    **both** endpoints sit in it (so no edge dangles to an unrendered node) —
    declared relationships first, then derived imports (#116).

    Args:
        config: The model skeleton.
        container: The container whose components are rendered.
        idx: The container's index in ``config.containers``.
        edges: Derived component-to-component import relationships.

    Returns:
        Deterministic Mermaid source ending in a trailing newline.
    """
    components = _components_for_container(config, container, idx)
    names = {c.name for c in components}
    alloc = _IdAllocator()
    component_ids = {c.name: alloc.allocate(c.name, "component") for c in components}
    container_id = alloc.allocate(container.name, "container")
    lines = ["graph LR", f'    subgraph {container_id}["{_m(container.name)}"]']
    lines += [
        f'        {component_ids[c.name]}["'
        f'{_mermaid_box(c.name, c.technology, _component_description(c))}"]'
        for c in components
    ]
    lines.append("    end")
    declared = {(r.source, r.destination) for r in config.relationships}
    lines += [
        f'    {component_ids[r.source]} -->|"{_m(r.description)}"| '
        f"{component_ids[r.destination]}"
        for r in config.relationships
        if r.source in names and r.destination in names
    ]
    lines += [
        f'    {component_ids[src]} -->|"imports"| {component_ids[dst]}'
        for src, dst in sorted(edges)
        if src in names and dst in names and (src, dst) not in declared
    ]
    return "\n".join(lines) + "\n"


def render_html(config: C4Config, views: list[tuple[str, str]]) -> str:
    """Wrap the C4 views in a self-contained, offline, tabbed HTML page.

    Each emitted view (System Context, Containers, one per container's
    Components) becomes its own navigable tab mirroring the DSL views, so a
    reader zooms in deliberately instead of facing one flattened diagram
    (#116). References the vendored ``mermaid.min.js`` sidecar by relative
    path, so it renders with no network.

    Mermaid renders every pane while it is still visible, then the tab script
    hides the inactive ones — rendering a Mermaid block inside a
    ``display:none`` element would otherwise produce a zero-size diagram.

    Args:
        config: The model skeleton (page title/description).
        views: ``(tab label, Mermaid source)`` pairs, in display order.

    Returns:
        A complete HTML document ending in a trailing newline.
    """
    buttons = "\n".join(
        f'  <button class="tab" data-pane="{i}">{html.escape(label)}</button>'
        for i, (label, _text) in enumerate(views)
    )
    panes = "\n".join(
        f'  <div class="pane" data-pane="{i}">\n'
        f'<pre class="mermaid">\n{html.escape(text)}</pre>\n  </div>'
        for i, (_label, text) in enumerate(views)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(config.system)} — C4 views</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }}
  h1 {{ margin-bottom: 0.25rem; }}
  p.desc {{ color: #555; margin-top: 0; }}
  .tabbar {{ margin-top: 1.5rem; border-bottom: 1px solid #ccc; }}
  button.tab {{ font: inherit; padding: 0.4rem 0.8rem; border: none;
    background: none; cursor: pointer; color: #555;
    border-bottom: 2px solid transparent; }}
  button.tab.active {{ color: #1a1a1a; border-bottom-color: #1a1a1a; }}
  .views.ready .pane {{ display: none; }}
  .views.ready .pane.active {{ display: block; }}
  .pane {{ margin-top: 1.5rem; }}
</style>
</head>
<body>
<h1>{html.escape(config.system)}</h1>
<p class="desc">{html.escape(config.description)}</p>
<div class="tabbar">
{buttons}
</div>
<div class="views">
{panes}
</div>
<script src="{MERMAID_JS_NAME}"></script>
<script>
// securityLevel 'loose' lets the bold/line-break HTML in box labels render;
// safe here because the model is generated locally from the repo's own config.
mermaid.initialize({{ startOnLoad: false, theme: "neutral", securityLevel: "loose" }});
mermaid.run().then(function () {{
  var views = document.querySelector(".views");
  var tabs = document.querySelectorAll("button.tab");
  var panes = document.querySelectorAll(".pane");
  function show(i) {{
    tabs.forEach(function (t, j) {{ t.classList.toggle("active", j === i); }});
    panes.forEach(function (p, j) {{ p.classList.toggle("active", j === i); }});
  }}
  tabs.forEach(function (t) {{
    t.addEventListener("click", function () {{ show(Number(t.dataset.pane)); }});
  }});
  views.classList.add("ready");
  show(0);
}});
</script>
</body>
</html>
"""


def _copy_vendored_mermaid(dest_dir: Path) -> None:
    """Write the vendored Mermaid bundle next to an emitted HTML file.

    Args:
        dest_dir: Directory the HTML was written to; the sidecar JS lands
            here as ``mermaid.min.js``.
    """
    src = resources.files("forge").joinpath(f"data/{MERMAID_JS_NAME}")
    (dest_dir / MERMAID_JS_NAME).write_bytes(src.read_bytes())


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


README_C4_START = "<!-- forge:c4:start -->"
README_C4_END = "<!-- forge:c4:end -->"


def render_readme_block(mermaid_text: str) -> str:
    """Render the managed README block embedding the Mermaid diagram.

    Args:
        mermaid_text: Canonical Mermaid source (from :func:`render_mermaid`).

    Returns:
        The full marker-delimited block, ready to splice into the README.
    """
    return (
        f"{README_C4_START}\n"
        "<!-- Generated by forge-gen-c4 — do not edit by hand; "
        "regenerate with `forge-gen-c4`. -->\n"
        "```mermaid\n"
        f"{mermaid_text}"
        "```\n"
        f"{README_C4_END}"
    )


def _splice_readme(readme_text: str, block: str) -> str | None:
    """Replace the managed C4 block in *readme_text* with *block*.

    Args:
        readme_text: Current README contents.
        block: New marker-delimited block (from :func:`render_readme_block`).

    Returns:
        The updated README text, or ``None`` when the start/end markers are
        absent or malformed (caller treats that as a configuration error).
    """
    start = readme_text.find(README_C4_START)
    end = readme_text.find(README_C4_END)
    if start == -1 or end == -1 or end < start:
        return None
    return readme_text[:start] + block + readme_text[end + len(README_C4_END) :]


def _readme_path(root: Path, config: C4Config) -> Path:
    """Return the configured README path under *root*.

    Args:
        root: Repository root directory.
        config: The model config (its ``readme`` key names the file).

    Returns:
        Absolute path to the README the C4 block is managed in.
    """
    return _safe_out_path(root, config.readme)


def sync_readme(root: Path, config: C4Config, mermaid_text: str, *, check: bool) -> int:
    """Write or verify the managed C4 block inside the configured README.

    Args:
        root: Repository root directory.
        config: The model config (must have a non-empty ``readme``).
        mermaid_text: Canonical Mermaid source to embed.
        check: When ``True``, verify the block is in sync without writing.

    Returns:
        ``0`` on success / in-sync; ``1`` when the README or its markers are
        missing, or (in check mode) the embedded block has drifted.
    """
    path = _readme_path(root, config)
    if not path.is_file():
        logger.error("README %s not found for C4 block.", config.readme)
        return 1
    current = path.read_text()
    updated = _splice_readme(current, render_readme_block(mermaid_text))
    if updated is None:
        logger.error(
            "README %s lacks the %s / %s markers — add them where the diagram "
            "should appear.",
            config.readme,
            README_C4_START,
            README_C4_END,
        )
        return 1
    if check:
        if updated != current:
            logger.error(
                "README %s C4 block is out of sync — run `%s`.",
                config.readme,
                REGEN_CMD,
            )
            return 1
        return 0
    path.write_text(updated)
    logger.info("Updated C4 block in %s.", config.readme)
    return 0


def _emit_mermaid(
    config: C4Config, edges: set[tuple[str, str]], output: str | None
) -> int:
    """Print or write the canonical Mermaid source.

    Args:
        config: The model skeleton.
        edges: Derived component edges.
        output: Output path, ``"-"``/``None`` for stdout.

    Returns:
        Always ``0`` (a pure render with no drift semantics).
    """
    mermaid = render_mermaid(config, edges)
    if not output or output == "-":
        sys.stdout.write(mermaid)
    else:
        Path(output).write_text(mermaid)
    return 0


def _emit_html(
    root: Path, config: C4Config, edges: set[tuple[str, str]], args: argparse.Namespace
) -> int:
    """Write or verify the offline HTML view (+ vendored Mermaid sidecar).

    Args:
        root: Repository root directory.
        config: The model skeleton.
        edges: Derived component edges.
        args: Parsed CLI args (``check``, ``output``).

    Returns:
        Exit code from the write or the drift check.
    """
    container_edges = _derive_container_edges(config, edges)
    views = [
        ("System Context", _render_mermaid_system_context(config)),
        ("Containers", _render_mermaid_containers(config, container_edges)),
    ]
    views += [
        (
            f"{container.name} Components",
            _render_mermaid_components_for(config, container, idx, edges),
        )
        for idx, container in enumerate(config.containers)
    ]
    content = render_html(config, views)
    out_relpath = args.output or DEFAULT_HTML_OUTPUT
    if args.check:
        return check_doc_drift(root, out_relpath, content, f"{REGEN_CMD} --format html")
    if out_relpath == "-":
        sys.stdout.write(content)
        return 0
    out_path = _safe_out_path(root, out_relpath)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content)
    _copy_vendored_mermaid(out_path.parent)
    logger.info(
        "Wrote %s + %s sidecar (renders offline).", out_relpath, MERMAID_JS_NAME
    )
    return 0


def _emit_dsl(
    root: Path, config: C4Config, edges: set[tuple[str, str]], args: argparse.Namespace
) -> int:
    """Write or verify the canonical DSL artifact and the README C4 block.

    The DSL is the committed source of truth; when ``[tool.forge.c4].readme``
    is set, the README's managed Mermaid block is kept in lockstep — so a
    structural change that is not regenerated fails ``--check`` at the next
    commit / PR.

    Args:
        root: Repository root directory.
        config: The model skeleton.
        edges: Derived component edges.
        args: Parsed CLI args (``check``, ``output``).

    Returns:
        Exit code: non-zero if the DSL or the README block is missing or
        (in check mode) has drifted.
    """
    dsl = render_dsl(config, edges)
    out_relpath = args.output or (config.output or DEFAULT_OUTPUT)
    if out_relpath == "-":
        sys.stdout.write(dsl)
        return 0
    if args.check:
        rc = check_doc_drift(root, out_relpath, dsl, REGEN_CMD)
        if config.readme:
            rc = max(
                rc,
                sync_readme(root, config, render_mermaid(config, edges), check=True),
            )
        return rc
    out_path = _safe_out_path(root, out_relpath)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dsl)
    logger.info("Wrote %s (%d component edges).", out_relpath, dsl.count(" -> "))
    if config.readme:
        return sync_readme(root, config, render_mermaid(config, edges), check=False)
    return 0


def main() -> int:
    """Generate or verify the C4 artifacts (DSL + README block, or HTML).

    Returns:
        Exit code: ``0`` on a successful write/print or in-sync check;
        ``1`` on drift, a missing artifact, absent ``[tool.forge.c4]``,
        or an invalid model configuration (e.g. an unknown container name).
    """
    args = _parse_args()
    root = repo_root()
    try:
        built = build_model(root, _resolve_roots(root, args.roots))
    except ValueError:
        logger.exception("Invalid C4 model")
        return 1
    if built is None:
        logger.error(
            "No [tool.forge.c4] config found — add it to pyproject.toml (or a "
            "c4.toml) to enable C4 generation (see docs/c4-architecture.md).",
        )
        return 1
    config, edges, unmatched = built
    _warn_unmatched(unmatched)

    if args.format == "mermaid":
        return _emit_mermaid(config, edges, args.output)
    if args.format == "html":
        return _emit_html(root, config, edges, args)
    return _emit_dsl(root, config, edges, args)


def _parse_args() -> argparse.Namespace:
    """Parse the ``forge-gen-c4`` command-line arguments.

    Returns:
        Parsed arguments with ``format``, ``roots``, ``check``, ``output``.
    """
    parser = argparse.ArgumentParser(
        prog="forge-gen-c4",
        description=(
            "Generate a C4 architecture model from the import graph + a "
            "[tool.forge.c4] / c4.toml model. Emits Structurizr DSL (default), "
            "a self-contained offline HTML view, or raw Mermaid."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("dsl", "html", "mermaid"),
        default="dsl",
        help=(
            "Output: 'dsl' (Structurizr + README block, default), "
            "'html' (offline view), or 'mermaid' (raw Mermaid to stdout)."
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
        help="Verify the committed artifact is in sync; do not write.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Override the output path. Use '-' to write to stdout.",
    )
    return parser.parse_args()


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
