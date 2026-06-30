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
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, fields
from importlib import resources
from pathlib import Path

from forge.audit.common import Scope
from forge.audit.deps import build_module_graph
from forge.config import resolve_model_section, resolve_tool_roots
from forge.gen_common import check_doc_drift
from forge.git_utils import configure_cli_logging, repo_root
from forge.run_context import progress_logger


configure_cli_logging()
logger = logging.getLogger(__name__)


DEFAULT_OUTPUT = "docs/architecture.dsl"
DEFAULT_HTML_OUTPUT = "docs/architecture.html"
DEFAULT_PDF_OUTPUT = "docs/architecture.pdf"
REGEN_CMD = "forge-gen-c4"
# Headless-browser print-to-PDF backend. Mermaid is a JS library, so a
# browser engine must execute it to produce a vector PDF; forge reuses the same
# offline HTML the `html` format emits, then drives an already-installed Chrome/
# Chromium/Edge/Brave with --headless --print-to-pdf. No new dependency, no
# network: the page and its sidecars are written to a temp dir and loaded over
# file://. FORGE_C4_BROWSER overrides the auto-detected executable.
_BROWSER_ENV = "FORGE_C4_BROWSER"
# macOS app-bundle binaries, tried before PATH commands.
_BROWSER_APP_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
# Linux/Windows PATH command names for the same Chromium-family browsers.
_BROWSER_COMMANDS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
    "chrome",
)
# Mermaid's async run() needs wall-clock to finish before the page is printed;
# Chrome's virtual-time budget (ms) advances time deterministically until the
# render settles. Generous so large models complete.
_PDF_VIRTUAL_TIME_BUDGET_MS = 15000
# Hard ceiling (seconds) on the headless print so a wedged browser can't hang a
# commit or CI run.
_PDF_TIMEOUT_S = 120
# Portrait (short_edge_mm, long_edge_mm) for the supported `pdf_page_size` keys
# (uppercased on lookup). Landscape swaps the pair. Unknown sizes fall back to A4.
_PAGE_SIZES_MM = {
    "A4": (210, 297),
    "A3": (297, 420),
    "A5": (148, 210),
    "LETTER": (216, 279),
    "LEGAL": (216, 356),
    "TABLOID": (279, 432),
}
# CSS px per millimetre at the 96-dpi reference resolution browsers print at.
_PX_PER_MM = 96 / 25.4
# Vertical px the per-page view title + its gap consume, reserved out of the
# printable height so the fit-to-page scale leaves room for the heading.
_PDF_TITLE_RESERVE_PX = 34
# Vendored Mermaid UMD bundle (MIT), shipped as forge package data. The HTML
# output references it by relative path so the diagram renders fully offline
# with no external tool — see docs/c4-architecture.md.
MERMAID_JS_NAME = "mermaid.min.js"
# Pre-bundled (esbuild IIFE) mermaid v11 ELK layout loader — a classic-script
# global, so it loads from file:// (the ESM build's dynamic import() does not).
# ELK lays out the Container view's cross-cluster edges cleanly where dagre
# tangles them; the HTML registers it and falls back to dagre if absent.
MERMAID_ELK_JS_NAME = "mermaid-layout-elk.iife.min.js"
# Edge-source modes for which relationships render. Declared [[relationship]]
# edges always render (explicit authorship); the mode gates only the
# import-derived edges: "imports"/"both" include them, "declared" excludes them.
_EDGE_MODES = ("imports", "declared", "both")
# Global Mermaid/DSL graph direction. "LR" (left-to-right) is the default and
# keeps the byte-identical baseline; "TB" lays the diagram out top-to-bottom.
_DIRECTIONS = ("LR", "TB")


def _resolve_direction(value: str) -> str:
    """Validate a graph-direction string, failing loudly on an unknown value.

    Args:
        value: Candidate direction from the config.

    Returns:
        *value* unchanged when it is a recognized direction.

    Raises:
        ValueError: When *value* is not one of :data:`_DIRECTIONS`.
    """
    if value not in _DIRECTIONS:
        msg = f"unknown direction {value!r}; expected one of {_DIRECTIONS}"
        raise ValueError(msg)
    return value


def _includes_derived(mode: str) -> bool:
    """Return whether *mode* includes the import-derived edges.

    Args:
        mode: A validated edge mode from :data:`_EDGE_MODES`.

    Returns:
        True for ``"imports"`` / ``"both"``; False for ``"declared"`` (which
        renders only the human-declared edges).
    """
    return mode != "declared"


def _resolve_edge_mode(value: str) -> str:
    """Validate an edge-mode string, failing loudly on an unknown value.

    Args:
        value: Candidate mode from the config.

    Returns:
        *value* unchanged when it is a recognized mode.

    Raises:
        ValueError: When *value* is not one of :data:`_EDGE_MODES`.
    """
    if value not in _EDGE_MODES:
        msg = f"unknown edges mode {value!r}; expected one of {_EDGE_MODES}"
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class Person:
    """A C4 actor — someone who uses the system (System Context level).

    Attributes:
        name: Display name (e.g. ``"Forge developer"``).
        description: One-line role description.
        uses: Label for this person's relationship to the system.
        container: Display name of a specific container this person targets
            in the Container view. Empty string anchors the person to the
            system boundary instead (back-compat default).
    """

    name: str
    description: str
    uses: str
    container: str = ""


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
class RenderConfig:
    """Consumer-tunable knobs for the offline HTML view (``[tool.forge.c4.render]``).

    Each field maps to a key the generated page feeds into
    ``mermaid.initialize(...)`` — see :func:`_mermaid_init_options` for the
    key→Mermaid-scope mapping. Defaults reproduce forge's shipped look (ELK
    layout tuning plus the label-wrap fix), so an absent section renders the
    bug-fixed default with no configuration. Pass-through fields default to a
    sentinel (``None`` / ``""`` / empty) that omits the key, leaving Mermaid's
    own default untouched rather than pinning it.

    Attributes:
        wrapping_width: ``flowchart.wrappingWidth`` — px width the description
            wraps at (the overflow fix). Mermaid auto-sizes the box to the
            wrapped label.
        html_labels: ``htmlLabels`` — render labels as HTML; ``None`` omits the
            key. Set ``False`` to avoid a Firefox rendering bug where certain
            nodes show empty labels.
        font_family: ``fontFamily`` — empty omits (keeps Mermaid's stack).
        font_size: ``fontSize`` — ``None`` omits.
        node_spacing: ``flowchart.nodeSpacing`` — ``None`` omits.
        rank_spacing: ``flowchart.rankSpacing`` — ``None`` omits.
        padding: ``flowchart.padding`` — ``None`` omits.
        diagram_padding: ``flowchart.diagramPadding`` — ``None`` omits.
        custom_css: ``themeCSS`` — raw-CSS escape hatch; empty omits.
        layout: top-level ``layout`` — ``elk`` (any ``elk.*``) attempts the
            vendored ELK loader with a dagre fallback; ``dagre`` uses dagre.
        node_placement_strategy: ``elk.nodePlacementStrategy``.
        force_node_model_order: ``elk.forceNodeModelOrder``.
        merge_edges: ``elk.mergeEdges``.
        consider_model_order: ``elk.considerModelOrder`` — empty omits.
        cycle_breaking_strategy: ``elk.cycleBreakingStrategy`` — empty omits.
        theme: ``theme`` — must be ``base`` for ``theme_colors`` to apply.
        theme_colors: ``themeVariables`` (hex values) — applied only when
            ``theme == "base"``; empty omits.
        pdf_page_size: ``--format pdf`` page size — ``"A4"`` / ``"A3"`` /
            ``"Letter"`` / ``"Legal"`` (unknown falls back to A4).
        pdf_orientation: ``"landscape"`` (default) or ``"portrait"``.
        pdf_fit: ``"contain"`` (default) scales each diagram to fit the whole
            page — width AND height, aspect ratio preserved; ``"width"`` fits
            width only, so a tall diagram can still exceed the page height.
        pdf_margin: page margin in millimetres (default 10).
    """

    wrapping_width: int = 220
    html_labels: bool | None = None
    font_family: str = ""
    font_size: int | None = None
    node_spacing: int | None = None
    rank_spacing: int | None = None
    padding: int | None = None
    diagram_padding: int | None = None
    custom_css: str = ""
    layout: str = "elk"
    node_placement_strategy: str = "NETWORK_SIMPLEX"
    force_node_model_order: bool = True
    merge_edges: bool = False
    consider_model_order: str = ""
    cycle_breaking_strategy: str = ""
    theme: str = "neutral"
    theme_colors: dict[str, str] = field(default_factory=dict)
    pdf_page_size: str = "A4"
    pdf_orientation: str = "landscape"
    pdf_fit: str = "contain"
    pdf_margin: float = 10


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
        edges: Global edge-source mode (:data:`_EDGE_MODES`) — whether
            import-derived edges render. Declared edges always render.
        container_edges: Per-view override of ``edges`` for the Container
            view; empty string inherits ``edges``.
        component_edges: Per-view override of ``edges`` for the Component
            views; empty string inherits ``edges``.
        direction: Global graph layout direction (:data:`_DIRECTIONS`) —
            ``"LR"`` (left-to-right, default) or ``"TB"`` (top-to-bottom).
        render: Offline-HTML rendering knobs from ``[tool.forge.c4.render]``;
            defaults reproduce the shipped look (see :class:`RenderConfig`).
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
    edges: str = "imports"
    container_edges: str = ""
    component_edges: str = ""
    direction: str = "LR"
    render: RenderConfig = field(default_factory=RenderConfig)


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


def _parse_render_config(section: dict) -> RenderConfig:
    """Build the HTML :class:`RenderConfig` from the model's ``render`` table.

    The ``[tool.forge.c4.render]`` keys are named identically to the
    :class:`RenderConfig` fields, so each matching key is passed straight through
    and the **dataclass field defaults are the single source of defaults** (no
    second copy to drift). Unknown keys are dropped (tolerant); a ``render`` that
    is absent or not a table yields the all-default config; a malformed
    (non-table) ``theme_colors`` is dropped so its ``{}`` default stands.

    Args:
        section: The resolved ``[tool.forge.c4]`` model table.

    Returns:
        A populated :class:`RenderConfig`; defaults when ``render`` is absent or
        not a table.
    """
    raw = section.get("render")
    if not isinstance(raw, dict):
        return RenderConfig()
    known = {f.name for f in fields(RenderConfig)}
    kwargs = {key: value for key, value in raw.items() if key in known}
    if not isinstance(kwargs.get("theme_colors"), dict):
        kwargs.pop("theme_colors", None)
    return RenderConfig(**kwargs)


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
        Person(
            p.get("name", "?"),
            p.get("description", ""),
            p.get("uses", "uses"),
            p.get("container", ""),
        )
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
    edges = _resolve_edge_mode(section.get("edges", "imports"))
    direction = _resolve_direction(section.get("direction", "LR"))
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
        edges=edges,
        container_edges=_resolve_edge_mode(section.get("container_edges", edges)),
        component_edges=_resolve_edge_mode(section.get("component_edges", edges)),
        direction=direction,
        render=_parse_render_config(section),
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


def _resolve_endpoint(name: str, ids: _IdMaps, system: str) -> str | None:
    """Resolve a relationship endpoint name to its DSL/Mermaid identifier.

    An endpoint may name *any* declared element, not only a component. The id
    maps are consulted in precedence order — component, container, external,
    person — so a component wins a name collision; finally the in-scope system
    itself matches by name.

    Args:
        name: Endpoint display name from a ``[[relationship]]`` or a person's
            ``container`` target.
        ids: All identifier mappings for the view.
        system: The in-scope software system's display name.

    Returns:
        The matching element's identifier, or ``None`` when *name* names no
        declared element.
    """
    for mapping in (
        ids.component_ids,
        ids.container_ids,
        ids.external_ids,
        ids.person_ids,
    ):
        if name in mapping:
            return mapping[name]
    if name == system:
        return ids.sys_id
    return None


def _externals_with_declared_incoming(config: C4Config) -> set[str]:
    """Return external names that are the destination of a declared edge.

    The generic ``primary -> external`` ("uses") arrow is auto-emitted per
    external. In the flat single diagram every declared edge renders in the
    same picture, so when a ``[[relationship]]`` already targets that external
    the generic arrow would double the specific one; this set marks which
    externals to suppress it for. Per-view renderers that show only a subset of
    declared edges (the Container view) compute their own narrower suppression
    set instead — see :func:`_container_view_declared`.

    Args:
        config: The model skeleton.

    Returns:
        Display names of externals that appear as the ``destination`` of at
        least one declared relationship.
    """
    return {
        e.name
        for e in config.externals
        if any(r.destination == e.name for r in config.relationships)
    }


def _declared_edges(config: C4Config, ids: _IdMaps) -> list[tuple[str, str, str]]:
    """Resolve each declared relationship to a rendered ``(src, dst, label)``.

    Both endpoints are resolved against all element id maps via
    :func:`_resolve_endpoint`; an edge is produced only when both resolve to a
    rendered node (a falsy id — e.g. the system in a view with no system node —
    counts as unresolved). The unresolved-name case is already reported by
    :func:`_warn_unknown_relationships`.

    Args:
        config: The model skeleton.
        ids: The view's identifier mappings.

    Returns:
        ``(source id, destination id, description)`` triples in relationship-
        declaration order, skipping relationships whose endpoints don't both
        resolve.
    """
    out: list[tuple[str, str, str]] = []
    for r in config.relationships:
        src = _resolve_endpoint(r.source, ids, config.system)
        dst = _resolve_endpoint(r.destination, ids, config.system)
        if src and dst:
            out.append((src, dst, r.description))
    return out


def _person_node(target: str, ids: _IdMaps, *, fallback: str) -> str:
    """Resolve a person's relationship target to a rendered node id.

    A person points at the component or container named in their ``container``
    field; an empty or otherwise-typed field falls back to *fallback* (the
    system or its boundary, depending on the view). Components are tried before
    containers, mirroring :func:`_resolve_endpoint`'s precedence.

    Args:
        target: The person's ``container`` field — a component or container
            display name, or an empty string.
        ids: The view's identifier mappings.
        fallback: Node id to use when *target* is empty or names neither a
            component nor a container in this view.

    Returns:
        The resolved component / container node id, or *fallback*.
    """
    if target in ids.component_ids:
        return ids.component_ids[target]
    if target in ids.container_ids:
        return ids.container_ids[target]
    return fallback


def _warn_unknown_relationships(config: C4Config, ids: _IdMaps) -> None:
    """Warn for [[relationship]] endpoints naming no declared element.

    A relationship endpoint may name any declared element — a person,
    container, component, external system, or the system itself. Each endpoint
    that resolves against none of those is warned and its edge skipped at
    render time.

    Args:
        config: The C4 model skeleton.
        ids: All identifier mappings for the model.
    """
    for r in config.relationships:
        for name in (r.source, r.destination):
            if _resolve_endpoint(name, ids, config.system) is None:
                logger.warning(
                    "Declared relationship %r -> %r references unknown element "
                    "%r — edge skipped",
                    r.source,
                    r.destination,
                    name,
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
    _warn_unknown_relationships(config, ids)

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
        Indented DSL relationship lines (person→component/container/system,
        system→external, then any declared edge whose endpoints both resolve,
        then derived component→component imports). The derived import edges are
        omitted when ``config.edges`` is ``"declared"``.
    """
    lines = ["", "        # relationships"]
    lines += [
        f"        {ids.person_ids[p.name]} -> "
        f"{_person_node(p.container, ids, fallback=ids.sys_id)} {_q(p.uses)}"
        for p in config.persons
    ]
    # The DSL is the full model, not a single view: the generic system ->
    # external arrow and any specific declared edge to that external coexist
    # here, and Structurizr scopes each to the views that render it. Emit the
    # generic for every external; per-view suppression is a Mermaid concern.
    lines += [
        f"        {ids.sys_id} -> {ids.external_ids[e.name]} {_q(e.relationship)}"
        for e in config.externals
    ]
    # Human-declared edges (runtime/subprocess "uses"). Each endpoint may name
    # any declared element — component, container, external, person, or the
    # system; only edges whose endpoints both resolve are emitted. Rendered
    # first; any derived import edge with the same (source, destination) is
    # suppressed so the diagram shows one arrow with the richer label.
    declared_pairs = {(r.source, r.destination) for r in config.relationships}
    lines += [
        f"        {src} -> {dst} {_q(desc)}"
        for src, dst, desc in _declared_edges(config, ids)
    ]
    if _includes_derived(config.edges):
        lines += [
            f"        {ids.component_ids[src]} -> "
            f"{ids.component_ids[dst]} {_q('imports')}"
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
    layout = f"            autolayout {config.direction.lower()}"
    lines = [
        "    views {",
        f"        systemContext {sys_id} {_q('SystemContext')} {{",
        "            include *",
        layout,
        "        }",
        f"        container {sys_id} {_q('Containers')} {{",
        "            include *",
        layout,
        "        }",
    ]
    for container in config.containers:
        cid = container_ids[container.name]
        lines += [
            f"        component {cid} {_q(f'{container.name} Components')} {{",
            "            include *",
            layout,
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


def _external_node_line(
    node_id: str, ext: External, *, indent: str = "    ", markdown: bool = False
) -> str:
    """Render the flat ``[[...]]`` node line for one external system.

    Single source of truth for the external-node representation — the doubled
    bracket shape and the ``"External system"`` technology tag — shared by every
    view that draws externals as flat nodes (System Context, the flat renderer,
    Container, per-container Component peripherals).

    Args:
        node_id: The allocated Mermaid node id for the external.
        ext: The external system to render.
        indent: Leading whitespace for the view's nesting level.
        markdown: Forwarded to :func:`_mermaid_box` so HTML callers get
            markdown-string labels; the flat renderer keeps HTML-tag labels.

    Returns:
        A single Mermaid node-declaration line (no trailing newline).
    """
    box = _mermaid_box(ext.name, "External system", ext.description, markdown=markdown)
    return f'{indent}{node_id}[["{box}"]]'


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

    lines = [f"graph {config.direction}"]
    lines += [
        f'    {person_ids[p.name]}(["{_mermaid_box(p.name, "Person", p.description)}"])'
        for p in config.persons
    ]
    lines += [_external_node_line(external_ids[e.name], e) for e in config.externals]
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


def _mermaid_box(
    name: str, technology: str, description: str, *, markdown: bool = False
) -> str:
    """Build a multi-line Mermaid box label: bold name, technology, description.

    Two forms share one escaping primitive (:func:`_m`, which entity-escapes
    ``<``/``>``/``&`` so text like "code_health/<step>.log" survives both the
    Mermaid parse and :func:`render_html`'s ``<pre>`` re-escape):

    * **HTML tags** (``markdown=False``, the default) — ``<b>``/``<br/>`` are
      literal. This canonical form drops straight into a GitHub ```` ```mermaid
      ```` block and feeds the README managed block + ``--format mermaid``.
    * **Markdown string** (``markdown=True``) — a backtick-fenced label
      (``**name**`` + newline-separated lines). Mermaid wraps the description
      at ``flowchart.wrappingWidth`` and auto-sizes the box, fixing the
      overflow the HTML form suffers; used only by the per-view HTML renderers.

    Args:
        name: Element display name (bolded).
        technology: Technology/type tag shown in brackets; omitted if empty.
        description: One-line description; omitted if empty.
        markdown: Emit a Mermaid markdown-string label instead of HTML tags.

    Returns:
        The label: a ``<br/>``-separated HTML-tag string, or a backtick-fenced
        newline-separated markdown string, both with entity-escaped text.
    """
    if markdown:
        parts = [f"**{_m(name)}**"]
        if technology:
            parts.append(f"[{_m(technology)}]")
        if description:
            parts.append(_m(description))
        return "`" + "\n".join(parts) + "`"
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
        Mermaid edge lines: person→container/component, container→external, and
        the declared (endpoints resolved against every element) + derived
        component edges (declared suppress duplicates). The derived edges are
        omitted when ``config.edges`` is ``"declared"``.
    """
    person_ids = ids["person"]
    external_ids = ids["external"]
    container_ids = ids["container"]
    component_ids = ids["component"]
    primary = container_ids[config.containers[0].name] if config.containers else None
    # The flat diagram has no system node, so sys_id is empty: a relationship
    # naming the system resolves to a falsy id and is skipped by _declared_edges.
    maps = _IdMaps("", person_ids, external_ids, container_ids, component_ids)
    lines: list[str] = []
    if primary is not None:
        lines += [
            f'    {person_ids[p.name]} -->|"{_m(p.uses)}"| '
            f"{_person_node(p.container, maps, fallback=primary)}"
            for p in config.persons
        ]
        declared_targets = _externals_with_declared_incoming(config)
        lines += [
            f'    {primary} -->|"{_m(e.relationship)}"| {external_ids[e.name]}'
            for e in config.externals
            if e.name not in declared_targets
        ]
    declared = {(r.source, r.destination) for r in config.relationships}
    lines += [
        f'    {src} -->|"{_m(desc)}"| {dst}'
        for src, dst, desc in _declared_edges(config, maps)
    ]
    if _includes_derived(config.edges):
        lines += [
            f'    {component_ids[src]} -->|"imports"| {component_ids[dst]}'
            for src, dst in sorted(edges)
            if (src, dst) not in declared
        ]
    return lines


def _actors_subgraph(
    config: C4Config, person_ids: dict[str, str], alloc: _IdAllocator
) -> list[str]:
    """Wrap the person nodes in a top-level ``Actors`` subgraph block.

    Groups a view's actors into one labelled subgraph so they cluster
    visually instead of scattering across the canvas (per-view HTML only).
    The group id is allocated from the view's own allocator so it never
    collides with a person or container id.

    Args:
        config: The model skeleton.
        person_ids: Person name → node id for this view.
        alloc: The view's id allocator, extended in place with the group id.

    Returns:
        Mermaid lines for the actors subgraph, or ``[]`` when there are no
        persons. The person edge lines stay with their caller, outside this
        block.
    """
    if not config.persons:
        return []
    actors_id = alloc.allocate("Actors", "actors")
    lines = [f'    subgraph {actors_id}["Actors"]']
    lines += [
        f"        {person_ids[p.name]}"
        f'(["{_mermaid_box(p.name, "Person", p.description, markdown=True)}"])'
        for p in config.persons
    ]
    lines.append("    end")
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
    lines = [f"graph {config.direction}"]
    lines += _actors_subgraph(config, person_ids, alloc)
    box_label = _mermaid_box(
        config.system, "Software System", config.description, markdown=True
    )
    lines.append(f'    {sys_id}("{box_label}")')
    lines += [
        _external_node_line(external_ids[e.name], e, markdown=True)
        for e in config.externals
    ]
    lines += [
        f'    {person_ids[p.name]} -->|"{_m(p.uses)}"| {sys_id}' for p in config.persons
    ]
    # The System Context view renders no specific declared edges (no
    # container/component nodes exist here), so the generic system -> external
    # arrow is the only incoming edge an external can have — emit it for every
    # external, even a declared destination. Per-view suppression lives in the
    # views that actually render the specific edge (flat + Container).
    lines += [
        f'    {sys_id} -->|"{_m(e.relationship)}"| {external_ids[e.name]}'
        for e in config.externals
    ]
    return "\n".join(lines) + "\n"


def _component_owner(config: C4Config) -> dict[str, str]:
    """Map each component name to the display name of its owning container.

    A component is owned by the container its ``container`` field names, or the
    first declared container when that field is empty (see
    :func:`_components_for_container`).

    Args:
        config: The model skeleton.

    Returns:
        Component name → owning container name for every declared component.
    """
    owner: dict[str, str] = {}
    for idx, container in enumerate(config.containers):
        for component in _components_for_container(config, container, idx):
            owner[component.name] = container.name
    return owner


def _derive_container_edges(
    config: C4Config, edges: set[tuple[str, str]], *, include_derived: bool = True
) -> set[tuple[str, str]]:
    """Collapse component-level edges to cross-container pairs.

    Maps each component relationship to its endpoints' owning containers,
    keeping only pairs that cross a container boundary. Declared edges always
    contribute; derived import edges contribute only when *include_derived* is
    True. This is the Container view's relationship summary — never the union
    of component edges (#116).

    Args:
        config: The model skeleton.
        edges: Derived component-to-component import relationships.
        include_derived: Whether to fold the derived import edges into the
            summary; declared edges are always folded in.

    Returns:
        ``(source container, destination container)`` pairs, cross-container
        only (same-container pairs dropped).
    """
    owner = _component_owner(config)
    component_edges = {(r.source, r.destination) for r in config.relationships}
    if include_derived:
        component_edges |= set(edges)
    pairs: set[tuple[str, str]] = set()
    for src, dst in component_edges:
        src_owner, dst_owner = owner.get(src), owner.get(dst)
        if src_owner and dst_owner and src_owner != dst_owner:
            pairs.add((src_owner, dst_owner))
    return pairs


def _container_level_maps(
    config: C4Config,
    *,
    sys_id: str,
    person_ids: dict[str, str],
    external_ids: dict[str, str],
    container_ids: dict[str, str],
) -> _IdMaps:
    """Build id maps that resolve every endpoint to its Container-view node.

    The Container view has no component nodes, so a component endpoint must
    anchor to its owning container. This is expressed by mapping each component
    name to its owning container's node id under ``component_ids`` — letting
    :func:`_resolve_endpoint` route components to containers transparently
    while containers, externals, persons, and the system map to themselves.

    Args:
        config: The model skeleton.
        sys_id: The system boundary's node id.
        person_ids: Person name → node id.
        external_ids: External-system name → node id.
        container_ids: Container name → node id.

    Returns:
        An :class:`_IdMaps` whose ``component_ids`` point at owning containers.
    """
    owner = _component_owner(config)
    component_to_container = {
        name: container_ids[cont]
        for name, cont in owner.items()
        if cont in container_ids
    }
    return _IdMaps(
        sys_id, person_ids, external_ids, container_ids, component_to_container
    )


def _container_view_declared(
    config: C4Config, ids: _IdMaps
) -> tuple[list[str], set[str]]:
    """Render Container-view declared edges and the externals they target.

    One pass over ``config.relationships`` yields both outputs the Container
    view needs, so the relationship list is traversed once rather than twice.

    Each endpoint maps to the node representing it here: a component to its
    owning container, a container/external/person/system to itself. Pure
    component→component pairs are skipped — those already summarize into the
    Container view's ``uses`` arrows (:func:`_derive_container_edges`); their
    same-container case would self-loop and their cross-container case would
    double the summary. Self-loops are dropped. This surfaces the
    container↔container, container/component↔external, and actor↔container/
    component edges the summary cannot express. The second element drives
    per-view suppression of the generic ``system -> external`` arrow: only
    externals that receive a specific edge *here* are suppressed, so an external
    whose specific edge lives in another view keeps its radial arrow.

    Args:
        config: The model skeleton.
        ids: Container-view id maps from :func:`_container_level_maps`.

    Returns:
        A tuple of (Mermaid edge lines in relationship-declaration order,
        display names of externals that are the resolved destination of a
        rendered edge).
    """
    components = {c.name for c in config.components}
    externals = {e.name for e in config.externals}
    lines: list[str] = []
    external_targets: set[str] = set()
    for r in config.relationships:
        if r.source in components and r.destination in components:
            continue
        src = _resolve_endpoint(r.source, ids, config.system)
        dst = _resolve_endpoint(r.destination, ids, config.system)
        if not (src and dst and src != dst):
            continue
        lines.append(f'    {src} -->|"{_m(r.description)}"| {dst}')
        if r.destination in externals:
            external_targets.add(r.destination)
    return lines, external_targets


def _render_mermaid_containers(
    config: C4Config, container_edges: set[tuple[str, str]]
) -> str:
    """Render the Container view: containers inside the system boundary.

    Shows only the system's containers (plus persons, externals, and their
    relationships) — no component-level boxes or edges. Actor edges anchor at
    container granularity: a person targeting a component routes to that
    component's owning container, a person targeting a container routes there,
    else the system boundary. The system relates out to each external.
    Cross-container component summaries arrive in *container_edges*; richer
    declared edges (container↔container, container/component↔external,
    actor↔container/component) are rendered here at container granularity
    (#116).

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
    maps = _container_level_maps(
        config,
        sys_id=sys_id,
        person_ids=person_ids,
        external_ids=external_ids,
        container_ids=container_ids,
    )
    lines = [f"graph {config.direction}"]
    lines += _actors_subgraph(config, person_ids, alloc)
    lines.append(f'    subgraph {sys_id}["{_m(config.system)}"]')
    lines += [
        f'        {container_ids[c.name]}["'
        f'{_mermaid_box(c.name, c.technology, c.description, markdown=True)}"]'
        for c in config.containers
    ]
    lines.append("    end")
    # Externals as FLAT nodes (no subgraph), exactly as the System Context view
    # emits them: wrapping them in a cluster gave dagre a third sibling subgraph
    # to mis-rank, tangling the inter-cluster container→external edges. Flat
    # nodes let dagre place them cleanly on the flow's far side; declared after
    # the system so they rank to the right (LR) / below (TB).
    lines += [
        _external_node_line(external_ids[e.name], e, markdown=True)
        for e in config.externals
    ]
    lines += [
        f'    {person_ids[p.name]} -->|"{_m(p.uses)}"| '
        f"{_person_node(p.container, maps, fallback=sys_id)}"
        for p in config.persons
    ]
    # Suppress the generic arrow only for externals that get a specific edge in
    # THIS view; keep it for externals targeted only in other views (or not at
    # all) so they retain an incoming edge here.
    declared_edges, declared_targets = _container_view_declared(config, maps)
    lines += [
        f'    {sys_id} -->|"{_m(e.relationship)}"| {external_ids[e.name]}'
        for e in config.externals
        if e.name not in declared_targets
    ]
    lines += [
        f'    {container_ids[src]} -->|"uses"| {container_ids[dst]}'
        for src, dst in sorted(container_edges)
    ]
    lines += declared_edges
    return "\n".join(lines) + "\n"


def _component_view_peripherals(
    config: C4Config,
    names: set[str],
    component_ids: dict[str, str],
    alloc: _IdAllocator,
) -> tuple[list[str], list[str]]:
    """Render external/person peripherals + edges for one container's view.

    For a declared relationship with one endpoint a component owned by this
    container and the other an external system or a person, the peripheral is
    drawn as a node here and connected — the publish/consume-to-external-store
    data flow and the actor→component flow that belong in this view. Pairs
    where the other endpoint is a component in another container are excluded
    (they summarize to the Container view). Peripheral nodes are allocated from
    the view's own *alloc* and rendered once even when referenced repeatedly.

    Args:
        config: The model skeleton.
        names: Display names of the components owned by this container.
        component_ids: Component name → node id for this view.
        alloc: The view's id allocator, extended in place with peripheral ids.

    Returns:
        Tuple of (peripheral node-declaration lines, edge lines), each in
        relationship-declaration order and deduplicated by node.
    """
    externals = {e.name: e for e in config.externals}
    persons = {p.name: p for p in config.persons}
    node_lines: list[str] = []
    edge_lines: list[str] = []
    peripheral_ids: dict[str, str] = {}

    def _peripheral(name: str) -> str | None:
        """Return the node id for external/person *name*, allocating once.

        Args:
            name: The external or person name to allocate.

        Returns:
            The allocated node id, or None if name matches neither.
        """
        if name in peripheral_ids:
            return peripheral_ids[name]
        if name in externals:
            pid = alloc.allocate(name, "ext")
            node_lines.append(_external_node_line(pid, externals[name], markdown=True))
        elif name in persons:
            pid = alloc.allocate(name, "person")
            person = persons[name]
            box = _mermaid_box(person.name, "Person", person.description, markdown=True)
            node_lines.append(f'    {pid}(["{box}"])')
        else:
            return None
        peripheral_ids[name] = pid
        return pid

    for r in config.relationships:
        if r.source in names and r.destination not in names:
            other = _peripheral(r.destination)
            if other is not None:
                edge_lines.append(
                    f'    {component_ids[r.source]} -->|"{_m(r.description)}"| {other}'
                )
        elif r.destination in names and r.source not in names:
            other = _peripheral(r.source)
            if other is not None:
                edge_lines.append(
                    f'    {other} -->|"{_m(r.description)}"| '
                    f"{component_ids[r.destination]}"
                )
    return node_lines, edge_lines


def _render_mermaid_components_for(
    config: C4Config,
    container: Container,
    idx: int,
    edges: set[tuple[str, str]],
    *,
    include_derived: bool = True,
) -> str:
    """Render one container's Component view: its components and their edges.

    Shows the components owned by *container* and the relationships whose
    **both** endpoints sit in it — declared relationships first, then derived
    imports when *include_derived* is True. A declared relationship between one
    of these components and an external system or a person is also drawn, with
    that external/person rendered as a peripheral node (the publish/consume-to-
    external and actor→component flows); cross-container component→component
    pairs are excluded — they summarize to the Container view (#116).

    Args:
        config: The model skeleton.
        container: The container whose components are rendered.
        idx: The container's index in ``config.containers``.
        edges: Derived component-to-component import relationships.
        include_derived: Whether to render the derived import edges; declared
            relationships always render.

    Returns:
        Deterministic Mermaid source ending in a trailing newline.
    """
    components = _components_for_container(config, container, idx)
    names = {c.name for c in components}
    alloc = _IdAllocator()
    component_ids = {c.name: alloc.allocate(c.name, "component") for c in components}
    container_id = alloc.allocate(container.name, "container")
    lines = [
        f"graph {config.direction}",
        f'    subgraph {container_id}["{_m(container.name)}"]',
    ]

    def _component_line(c: Component) -> str:
        box_label = _mermaid_box(
            c.name, c.technology, _component_description(c), markdown=True
        )
        return f'        {component_ids[c.name]}["{box_label}"]'

    lines += [_component_line(c) for c in components]
    lines.append("    end")
    peripheral_nodes, peripheral_edges = _component_view_peripherals(
        config, names, component_ids, alloc
    )
    lines += peripheral_nodes
    declared = {(r.source, r.destination) for r in config.relationships}
    # Both endpoints are components of THIS container, so the id is a direct
    # dict hit — no _resolve_endpoint/_declared_edges indirection needed here.
    # `names` and `component_ids` are co-built from the same component list, so
    # the membership test and the lookup cannot diverge.
    lines += [
        f'    {component_ids[r.source]} -->|"{_m(r.description)}"| '
        f"{component_ids[r.destination]}"
        for r in config.relationships
        if r.source in names and r.destination in names
    ]
    lines += peripheral_edges
    if include_derived:
        lines += [
            f'    {component_ids[src]} -->|"imports"| {component_ids[dst]}'
            for src, dst in sorted(edges)
            if src in names and dst in names and (src, dst) not in declared
        ]
    return "\n".join(lines) + "\n"


def _mermaid_init_options(render: RenderConfig, *, layout_var: str) -> str:
    """Build the ``mermaid.initialize(...)`` options object for the HTML view.

    Single source of truth for the ``[tool.forge.c4.render]`` key → Mermaid-scope
    mapping (root / ``flowchart`` / ``elk`` / ``themeVariables``). Always-emitted
    keys reproduce forge's shipped look plus the wrap fix (``securityLevel``,
    ``theme``, ``useMaxWidth:false``, ``wrappingWidth``, ``markdownAutoWrap``, and
    the ELK layout tuning); pass-through keys are emitted only when set, leaving
    Mermaid's own default in place otherwise. ``themeVariables`` apply only under
    ``theme:"base"`` (Mermaid ignores them otherwise).

    Args:
        render: The resolved render config.
        layout_var: Name of the in-page JS variable holding the resolved layout
            (a variable reference, not a string literal, so the dagre fallback
            the page computes at runtime wins).

    Returns:
        A JS object literal (braces included) ready to drop into
        ``mermaid.initialize(...)``.
    """
    flowchart: dict[str, object] = {
        "useMaxWidth": False,
        "wrappingWidth": render.wrapping_width,
        **{
            key: value
            for key, value in (
                ("nodeSpacing", render.node_spacing),
                ("rankSpacing", render.rank_spacing),
                ("padding", render.padding),
                ("diagramPadding", render.diagram_padding),
            )
            if value is not None
        },
    }
    elk: dict[str, object] = {
        "nodePlacementStrategy": render.node_placement_strategy,
        "forceNodeModelOrder": render.force_node_model_order,
        "mergeEdges": render.merge_edges,
    }
    if render.consider_model_order:
        elk["considerModelOrder"] = render.consider_model_order
    if render.cycle_breaking_strategy:
        elk["cycleBreakingStrategy"] = render.cycle_breaking_strategy
    root: dict[str, object] = {
        "startOnLoad": False,
        "securityLevel": "loose",
        "theme": render.theme,
        "markdownAutoWrap": True,
        "flowchart": flowchart,
        "elk": elk,
    }
    if render.html_labels is not None:
        root["htmlLabels"] = render.html_labels
    if render.font_family:
        root["fontFamily"] = render.font_family
    if render.font_size is not None:
        root["fontSize"] = render.font_size
    if render.custom_css:
        root["themeCSS"] = render.custom_css
    if render.theme == "base" and render.theme_colors:
        root["themeVariables"] = render.theme_colors
    inner = json.dumps(root)[1:-1]
    return f'{{{inner}, "layout": {layout_var}}}'


def _pdf_page_geometry(render: RenderConfig) -> tuple[int, int, float, int, int]:
    """Resolve the print page box + printable pixel area from the PDF config.

    Validates ``pdf_page_size`` / ``pdf_orientation`` / ``pdf_margin`` (unknown
    size → A4, unknown orientation → landscape) and converts the printable area
    to CSS px at the 96-dpi print reference, reserving room for the per-page
    title. The px area feeds the in-page fit-to-page scale (the browser maps the
    ``@page`` mm box to those px when printing).

    Args:
        render: The resolved render config.

    Returns:
        ``(page_w_mm, page_h_mm, margin_mm, printable_w_px, printable_h_px)``.
    """
    short, long_ = _PAGE_SIZES_MM.get(
        render.pdf_page_size.upper(), _PAGE_SIZES_MM["A4"]
    )
    if render.pdf_orientation == "portrait":
        page_w, page_h = short, long_
    else:
        page_w, page_h = long_, short
    margin = render.pdf_margin if render.pdf_margin >= 0 else 10
    printable_w_px = round((page_w - 2 * margin) * _PX_PER_MM)
    printable_h_px = round((page_h - 2 * margin) * _PX_PER_MM) - _PDF_TITLE_RESERVE_PX
    return page_w, page_h, margin, max(printable_w_px, 1), max(printable_h_px, 1)


def _print_page_css(render: RenderConfig) -> str:
    """Build the ``@page`` + ``@media print`` rules for the PDF layout.

    Lays every view out one-per-page at the configured size/orientation/margin.
    In ``contain`` fit each diagram is scaled by the JS-measured
    ``--c4-print-scale`` (set per SVG so the whole diagram fits width AND
    height); in ``width`` fit the SVG is capped to the page width only.

    Args:
        render: The resolved render config.

    Returns:
        A CSS fragment (single-brace literal) for the page's ``<style>``.
    """
    page_w, page_h, margin, _pw, _ph = _pdf_page_geometry(render)
    if render.pdf_fit == "width":
        diagram_rule = (
            "    .diagram-scroll svg { max-width: 100% !important;\n"
            "      width: auto !important; height: auto !important; }"
        )
    else:
        # `zoom` (not `transform: scale`) so the layout box reflows to the scaled
        # size — otherwise the unscaled box keeps its full height and the page
        # break lands mid-pane, pushing the next view's title onto this page.
        diagram_rule = (
            "    .diagram-scroll svg {\n"
            "      zoom: var(--c4-print-scale, 1); max-width: none !important;\n"
            "      width: auto !important; height: auto !important; }"
        )
    msg = (
        "Print / PDF: drop the interactive chrome, show every view "
        "(Mermaid renders all panes before the tab script hides them), "
        "one per page, scaled to fit."
    )
    return f"""  /* {msg} */
  @page {{ size: {page_w}mm {page_h}mm; margin: {margin}mm; }}
  @media print {{
    body {{ margin: 0; }}
    .tabbar, h1, p.desc {{ display: none; }}
    .views.ready .pane,
    .views.ready .pane.active {{ display: block; }}
    .pane {{ margin: 0; page-break-after: always; break-after: page; }}
    .pane:last-child {{ page-break-after: auto; break-after: auto; }}
    .view-title {{ display: block; margin: 0 0 0.5rem; font-size: 1rem; }}
    .diagram-scroll {{ overflow: visible; max-height: none; border: none;
      padding: 0; }}
{diagram_rule}
    svg.c4-focus-mode g.node, svg.c4-focus-mode path.flowchart-link,
    svg.c4-focus-mode g.edgeLabel {{ opacity: 1 !important; }}
  }}"""


def _html_interaction_css() -> str:
    """Return the inline CSS for the hover-highlight focus state.

    While a node is hovered (``c4-focus-mode``) every node, edge path, and edge
    *label* dims except those marked ``c4-on`` — so only the hovered node's
    connections, their neighbours, and *their* relationship text stay readable.

    Returns:
        CSS rules (no ``<style>`` wrapper) for the ``c4-focus-mode`` state.
    """
    return """
  .diagram-scroll svg g.node, .diagram-scroll svg path.flowchart-link,
  .diagram-scroll svg g.edgeLabel { transition: opacity .15s ease; }
  svg.c4-focus-mode g.node { opacity: 0.18; }
  svg.c4-focus-mode path.flowchart-link { opacity: 0.08; }
  svg.c4-focus-mode g.edgeLabel { opacity: 0.1; }
  svg.c4-focus-mode g.node.c4-on { opacity: 1; }
  svg.c4-focus-mode path.flowchart-link.c4-on { opacity: 1; }
  svg.c4-focus-mode g.edgeLabel.c4-on { opacity: 1; }"""


def _html_interaction_script() -> str:
    """Return the inline JS wiring hover-highlight + click-to-open-tab interactivity.

    Operates on Mermaid's post-``run()`` SVG DOM. Edge incidence is read from the
    ELK-rendered edge ids (``L_<source>_<target>_<n>_<m>``) resolved against the
    set of ``g.node`` logical ids — node slugs contain underscores, so each id is
    split at every boundary and accepted only when both halves are known nodes
    (class-based ``LS-``/``LE-`` hooks are not emitted under the ELK layout).
    Hovering a node reveals it, its incident edges, those edges' relationship
    labels, and their neighbours while dimming everything else (edge labels carry
    no id, so they map to edges by document order). Clicking a node that has a
    drill-down view switches to that tab (``window.c4Tabs`` →
    ``window.c4ShowPane``); other nodes have no click action. Every selector is
    guarded, so a missing node/edge degrades to a no-op.

    Returns:
        A JS snippet defining ``window.c4WireView(svg)`` for embedding in a
        classic ``<script>``.
    """
    return r"""
window.c4WireView = (function () {
  "use strict";
  function logicalId(node) {
    return (node.id || "").replace(/^flowchart-/, "").replace(/-\d+$/, "");
  }
  function edgeEnds(edge, nodeSet) {
    // id is L_<source>_<target>_<counter...>; node ids contain underscores, so
    // try every split and accept the first where both halves are known nodes.
    var parts = (edge.id || "").replace(/^L_/, "").split("_");
    for (var i = 1; i < parts.length; i++) {
      var src = parts.slice(0, i).join("_");
      if (!nodeSet[src]) { continue; }
      for (var j = i + 1; j <= parts.length; j++) {
        var tgt = parts.slice(i, j).join("_");
        if (nodeSet[tgt]) { return [src, tgt]; }
      }
    }
    return null;
  }
  function tabFor(node) {
    var tabs = window.c4Tabs || [];
    var text = (node.textContent || "").replace(/\s+/g, " ").trim();
    for (var i = 0; i < tabs.length; i++) {
      if (text.indexOf(tabs[i].name) === 0) { return tabs[i].pane; }
    }
    return null;
  }
  return function wire(svg) {
    if (!svg) { return; }
    var nodes = svg.querySelectorAll("g.node");
    var edges = svg.querySelectorAll("path.flowchart-link");
    if (!nodes.length) { return; }
    // Edge labels carry no id; Mermaid emits them in the same order as the
    // edge paths, so the i-th label belongs to the i-th edge.
    var labels = svg.querySelectorAll("g.edgeLabels g.edgeLabel");
    var nodeSet = {};
    nodes.forEach(function (g) { nodeSet[logicalId(g)] = true; });
    function focus(id) {
      svg.classList.add("c4-focus-mode");
      var keepNodes = {}; keepNodes[id] = true;
      edges.forEach(function (edge, i) {
        var ends = edgeEnds(edge, nodeSet);
        var on = !!ends && (ends[0] === id || ends[1] === id);
        edge.classList.toggle("c4-on", on);
        if (labels[i]) { labels[i].classList.toggle("c4-on", on); }
        if (on) { keepNodes[ends[0]] = true; keepNodes[ends[1]] = true; }
      });
      nodes.forEach(function (g) {
        g.classList.toggle("c4-on", keepNodes[logicalId(g)] === true);
      });
    }
    function clear() {
      svg.classList.remove("c4-focus-mode");
      nodes.forEach(function (g) { g.classList.remove("c4-on"); });
      edges.forEach(function (edge) { edge.classList.remove("c4-on"); });
      labels.forEach(function (label) { label.classList.remove("c4-on"); });
    }
    nodes.forEach(function (g) {
      var id = logicalId(g);
      g.addEventListener("mouseenter", function () { focus(id); });
      g.addEventListener("mouseleave", function () { clear(); });
      var target = tabFor(g);
      if (target !== null) {
        g.style.cursor = "pointer";
        g.addEventListener("click", function (ev) {
          ev.stopPropagation();
          if (typeof window.c4ShowPane === "function") { window.c4ShowPane(target); }
        });
      }
    });
    // Fit-to-page: uniformly scale the diagram so it fits the printable area in
    // both dimensions. Measured here (pane still visible) and applied only under
    // @media print via the --c4-print-scale custom property.
    if (window.c4Print && window.c4Print.fit === "contain") {
      var rect = svg.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        var scale = Math.min(
          window.c4Print.w / rect.width,
          window.c4Print.h / rect.height
        );
        svg.style.setProperty("--c4-print-scale", String(scale));
      }
    }
  };
})();
"""


def render_html(config: C4Config, views: list[tuple[str, str]]) -> str:
    """Wrap the C4 views in a self-contained, offline, tabbed HTML page.

    Each emitted view (System Context, Containers, one per container's
    Components) becomes its own navigable tab mirroring the DSL views, so a
    reader zooms in deliberately instead of facing one flattened diagram.
    References the vendored ``mermaid.min.js`` sidecar by relative path, so it
    renders with no network.

    Mermaid renders every pane while it is still visible, then the tab script
    hides the inactive ones — rendering a Mermaid block inside a
    ``display:none`` element would otherwise produce a zero-size diagram.

    The Mermaid setup is driven by ``[tool.forge.c4.render]``
    (:func:`_mermaid_init_options`); its defaults wrap box labels, fixing the
    node-label overflow. Hover-highlight and click-to-open-tab interactivity
    (:func:`_html_interaction_script`) is wired onto each pane's SVG after
    render.

    Args:
        config: The model skeleton (page title/description + render config).
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
        f'<h2 class="view-title">{html.escape(label)}</h2>\n'
        f'<div class="diagram-scroll"><pre class="mermaid">\n'
        f"{html.escape(text)}</pre></div>\n  </div>"
        for i, (label, text) in enumerate(views)
    )
    init_options = _mermaid_init_options(config.render, layout_var="c4layout")
    requested_layout = json.dumps(config.render.layout)
    interaction_css = _html_interaction_css()
    interaction_script = _html_interaction_script()
    print_css = _print_page_css(config.render)
    _w, _h, _m, print_w_px, print_h_px = _pdf_page_geometry(config.render)
    print_config = json.dumps(
        {"w": print_w_px, "h": print_h_px, "fit": config.render.pdf_fit}
    )
    # Click-to-open-tab map: each container's Component view, keyed by the
    # container name its node label starts with. Longest name first so a name
    # that prefixes another (e.g. "forge" vs "forge-scripts") never mis-routes.
    suffix = " Components"
    tab_targets = sorted(
        (
            {"name": label[: -len(suffix)], "pane": i}
            for i, (label, _t) in enumerate(views)
            if label.endswith(suffix)
        ),
        key=lambda t: len(t["name"]),
        reverse=True,
    )
    tab_config = json.dumps(tab_targets)
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
  .view-title {{ display: none; }}
  .diagram-scroll {{ overflow: auto; max-height: 82vh;
    border: 1px solid #eee; padding: 0.5rem; }}
  .diagram-scroll svg {{ max-width: none !important; height: auto; }}
{interaction_css}
{print_css}
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
<script src="{MERMAID_ELK_JS_NAME}"></script>
<script>{interaction_script}</script>
<script>
// securityLevel 'loose' lets the markdown box labels (and the ELK layout
// loader) run; safe here because the model is generated locally from the
// repo's own config. The requested layout (default "elk") attempts the
// vendored ELK loader — a classic-script global, so it works from file:// —
// which untangles the Container view's cross-cluster edges that dagre
// mis-ranks; if the loader is missing or errors, it falls back to dagre.
var requestedLayout = {requested_layout};
var c4layout = requestedLayout;
if (requestedLayout.indexOf("elk") === 0) {{
  c4layout = "dagre";
  try {{
    if (window.elkLayouts) {{
      mermaid.registerLayoutLoaders(window.elkLayouts.default || window.elkLayouts);
      c4layout = requestedLayout;
    }}
  }} catch (err) {{
    console.warn("c4: ELK layout unavailable — falling back to dagre:", err);
  }}
}}
console.log("c4: layout engine =", c4layout);
// Printable page area (px @96dpi) + fit mode, consumed by c4WireView to set each
// SVG's --c4-print-scale so the diagram fits its page when printed to PDF.
window.c4Print = {print_config};
// Container name -> Component-view pane index, for click-to-open-tab.
window.c4Tabs = {tab_config};
mermaid.initialize({init_options});
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
  window.c4ShowPane = show;  // let a node click open its drill-down tab
  // Wire + measure every pane's SVG while all panes are still visible — a
  // display:none pane reports a zero box, which would break the print scale.
  document.querySelectorAll(".views .pane svg").forEach(function (svg) {{
    try {{ if (window.c4WireView) {{ window.c4WireView(svg); }} }}
    catch (err) {{ console.warn("c4: interactivity wiring failed:", err); }}
  }});
  views.classList.add("ready");
  show(0);
}});
</script>
</body>
</html>
"""


def _copy_vendored_mermaid(dest_dir: Path) -> None:
    """Write the vendored Mermaid + ELK-layout bundles next to an emitted HTML.

    Both are classic-script globals copied as sidecars so the HTML renders
    fully offline from ``file://`` — Mermaid for the diagrams and the
    pre-bundled ELK layout loader the Container view uses (with a dagre
    fallback baked into the page if the loader is absent).

    Args:
        dest_dir: Directory the HTML was written to; the sidecar JS files land
            here as ``mermaid.min.js`` and ``mermaid-layout-elk.iife.min.js``.
    """
    for name in (MERMAID_JS_NAME, MERMAID_ELK_JS_NAME):
        src = resources.files("forge").joinpath(f"data/{name}")
        (dest_dir / name).write_bytes(src.read_bytes())


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
    root: Path, config: C4Config, edges: set[tuple[str, str]], output: str | None
) -> int:
    """Print or write the canonical Mermaid source.

    Args:
        root: Repository root directory (bounds a file write).
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
        _safe_out_path(root, output).write_text(mermaid)
    return 0


def _build_views(
    config: C4Config, edges: set[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Build the ``(tab label, Mermaid source)`` pairs for the per-view artifacts.

    Single source of truth for the view set the HTML and PDF formats both render:
    System Context, Containers, then one Component view per container, honouring
    each view's edge-source mode.

    Args:
        config: The model skeleton.
        edges: Derived component edges.

    Returns:
        Ordered ``(label, Mermaid source)`` pairs.
    """
    container_mode = config.container_edges or config.edges
    component_mode = config.component_edges or config.edges
    container_edges = _derive_container_edges(
        config, edges, include_derived=_includes_derived(container_mode)
    )
    views = [
        ("System Context", _render_mermaid_system_context(config)),
        ("Containers", _render_mermaid_containers(config, container_edges)),
    ]
    views += [
        (
            f"{container.name} Components",
            _render_mermaid_components_for(
                config,
                container,
                idx,
                edges,
                include_derived=_includes_derived(component_mode),
            ),
        )
        for idx, container in enumerate(config.containers)
    ]
    return views


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
    content = render_html(config, _build_views(config, edges))
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


def _find_headless_browser() -> str | None:
    """Locate an installed Chromium-family browser for headless PDF printing.

    Resolution order: the ``FORGE_C4_BROWSER`` override, then the known macOS
    app-bundle paths, then PATH command names (Linux/Windows). Any of Chrome,
    Chromium, Edge, or Brave works — they share the ``--headless
    --print-to-pdf`` interface.

    Returns:
        An executable path, or ``None`` when no supported browser is found.
    """
    override = os.environ.get(_BROWSER_ENV)
    if override:
        ok = Path(override).is_file() and os.access(override, os.X_OK)
        return override if ok else None
    for path in _BROWSER_APP_PATHS:
        if Path(path).exists():
            return path
    for command in _BROWSER_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    return None


def _print_html_to_pdf(browser: str, html_path: Path, pdf_path: Path) -> None:
    """Drive *browser* headlessly to print *html_path* to *pdf_path*.

    Mermaid renders the diagrams client-side, so the browser engine — not forge
    — produces the vector PDF; the ``@media print`` rules in the page lay every
    view out one-per-page. Flags keep the run offline and non-interactive (no
    sandbox prompt, no background networking, no first-run UI).

    Args:
        browser: Path to the Chromium-family executable.
        html_path: The offline HTML to render (loaded over ``file://``).
        pdf_path: Destination PDF path.

    Raises:
        subprocess.CalledProcessError: If the browser exits non-zero.
        subprocess.TimeoutExpired: If the print exceeds the hard timeout.
    """
    cmd = [
        browser,
        "--headless",
        # --no-sandbox: the Chromium sandbox needs a writable user-data dir /
        # namespaces forge can't assume in CI containers, and the only input is
        # forge's own locally generated file:// page — no untrusted web content.
        "--no-sandbox",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-extensions",
        "--no-pdf-header-footer",
        f"--virtual-time-budget={_PDF_VIRTUAL_TIME_BUDGET_MS}",
        f"--print-to-pdf={os.fspath(pdf_path)}",
        html_path.as_uri(),
    ]
    with progress_logger("c4: headless print-to-PDF"):
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=_PDF_TIMEOUT_S,
        )


def _emit_pdf(
    root: Path, config: C4Config, edges: set[tuple[str, str]], args: argparse.Namespace
) -> int:
    """Render the C4 views to a vector PDF via a headless browser.

    Writes the same offline HTML the ``html`` format emits (plus the vendored
    Mermaid + ELK sidecars) into a temp dir, then prints it to PDF with a
    detected Chromium-family browser. The PDF is a binary, non-deterministic
    artifact, so ``--check`` has nothing to verify — it is a no-op (never writes)
    rather than silently regenerating.

    Args:
        root: Repository root directory.
        config: The model skeleton.
        edges: Derived component edges.
        args: Parsed CLI args (``check``, ``output``).

    Returns:
        ``0`` on a successful write (or a skipped ``--check``); ``1`` when no
        browser is found or the headless print fails.
    """
    if args.check:
        logger.info(
            "PDF output has no drift check (binary, non-deterministic); "
            "--check skips it. Regenerate with `%s --format pdf`.",
            REGEN_CMD,
        )
        return 0
    browser = _find_headless_browser()
    if browser is None:
        logger.error(
            "No headless-capable browser found for PDF export. Install Chrome / "
            "Chromium / Edge / Brave, set %s=/path/to/browser, or open the "
            "`--format html` page and Print → Save as PDF.",
            _BROWSER_ENV,
        )
        return 1
    out_relpath = args.output or DEFAULT_PDF_OUTPUT
    out_path = _safe_out_path(root, out_relpath)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = render_html(config, _build_views(config, edges))
    with tempfile.TemporaryDirectory(prefix="forge-c4-") as workdir:
        work = Path(workdir)
        html_path = work / "architecture.html"
        html_path.write_text(content)
        _copy_vendored_mermaid(work)
        try:
            _print_html_to_pdf(browser, html_path, out_path)
        except subprocess.TimeoutExpired:
            logger.exception("PDF render timed out after %ss.", _PDF_TIMEOUT_S)
            return 1
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode(errors="replace").strip()
            logger.exception("Headless browser failed to render the PDF: %s", detail)
            return 1
    logger.info("Wrote %s (via %s).", out_relpath, Path(browser).name)
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
        return _emit_mermaid(root, config, edges, args.output)
    if args.format == "html":
        return _emit_html(root, config, edges, args)
    if args.format == "pdf":
        return _emit_pdf(root, config, edges, args)
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
            "a self-contained offline HTML view, a vector PDF, or raw Mermaid."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("dsl", "html", "pdf", "mermaid"),
        default="dsl",
        help=(
            "Output: 'dsl' (Structurizr + README block, default), "
            "'html' (offline view), 'pdf' (vector PDF via a headless browser), "
            "or 'mermaid' (raw Mermaid to stdout)."
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
