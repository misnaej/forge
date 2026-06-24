"""forge-audit-deps: module dependency analysis.

Computes per-module metrics in the style of Robert C. Martin's Clean
Architecture (Ch. 14):

    * ``Ca`` — afferent couplings (number of modules depending on us)
    * ``Ce`` — efferent couplings (number of modules we depend on)
    * ``I``  — instability = ``Ce / (Ca + Ce)`` ∈ [0, 1]
    * ``A``  — abstractness = abstract_classes / total_classes ∈ [0, 1]
    * ``D``  — distance from main sequence = ``|A + I - 1|``

Findings emitted:

    * CRITICAL — cyclic dependencies (ADP violation), detected via Tarjan SCC
    * MEDIUM   — modules far from the main sequence (D above threshold)
    * HIGH     — declared-rule violations from optional ``tach`` integration

Optional ``tach`` integration: when ``tach.toml`` exists and the ``tach``
CLI is on PATH, ``tach check`` is run and its violations merged into the
findings as HIGH-severity entries.

Every run also renders the internal module dependency graph as a readable
plain-text tree to ``code_health/audit_deps_tree.log`` (sibling of
``audit_deps.log``). Pass ``--tree`` to additionally print that tree to
stdout for direct human inspection.
"""

from __future__ import annotations

import ast
import logging
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from forge.audit import common
from forge.audit.common import (
    CODE_HEALTH_DIR,
    Finding,
    Scope,
    Severity,
    exit_code_for,
    iter_files,
    make_audit_parser,
    relpath,
    resolve_roots,
    write_log,
)
from forge.git_utils import configure_cli_logging, repo_root


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


DEFAULT_DISTANCE_THRESHOLD = 0.7
MIN_CYCLE_SIZE = 2
TACH_VIOLATION_PREVIEW = 200


@dataclass(frozen=True)
class ModuleNode:
    """One Python module after parsing.

    Attributes:
        name: Dotted module name relative to the package root.
        path: Repo-relative POSIX path to the source file.
        abstract_classes: Count of classes marked abstract.
        total_classes: Total class definitions in the module.
    """

    name: str
    path: str
    abstract_classes: int
    total_classes: int


@dataclass(frozen=True)
class DepsConfig:
    """Tunable knobs for the dependency-analysis pipeline.

    Attributes:
        distance_threshold: Report modules with ``D`` above this value.
        output: Optional log-path override.
        print_tree: When ``True``, also print the rendered dependency tree to
            stdout via the module logger.
    """

    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD
    output: Path | None = None
    print_tree: bool = False


def _resolve_module_name(path: Path, package_roots: list[Path]) -> str | None:
    """Translate a ``.py`` path to a dotted module name.

    Args:
        path: Absolute path to a Python source file.
        package_roots: Candidate ancestor directories (``src``, ``lib``, …).

    Returns:
        Dotted module name (``"forge.audit.dup"``) or ``None`` if the path
        is not under any known root.
    """
    for root in package_roots:
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            return None
        return ".".join(parts)
    return None


def _extract_imports(tree: ast.Module, current_module: str) -> set[str]:
    """Return the set of fully-qualified import-candidate targets.

    Relative imports are resolved against ``current_module``. For
    ``from X import Y`` we emit BOTH ``X`` and ``X.Y`` as candidates —
    at parse time we cannot know whether ``Y`` is a submodule (becomes
    an edge to ``X.Y``) or an attribute of ``X`` (edge to ``X``).
    Downstream, ``_closest_known`` picks the deepest match present in
    the graph, so attribute imports collapse to ``X`` and submodule
    imports resolve to ``X.Y``.

    Args:
        tree: Parsed module.
        current_module: Dotted name of the importing module (for relative
            import resolution).

    Returns:
        Set of dotted target candidates.
    """
    targets: set[str] = set()
    parts = current_module.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            base = ".".join(parts[: len(parts) - level]) if level else ""
            base_module = (
                (f"{base}.{node.module}" if base else node.module)
                if node.module
                else base
            )
            if not base_module:
                continue
            targets.add(base_module)
            for alias in node.names:
                if alias.name != "*":
                    targets.add(f"{base_module}.{alias.name}")
    return targets


def _closest_known(target: str, modules: dict[str, ModuleNode]) -> str | None:
    """Walk up the dotted name until a known module is found.

    Args:
        target: Raw import target (may be deeper than the known graph).
        modules: All discovered module nodes keyed by dotted name.

    Returns:
        Closest known ancestor dotted name, or ``None`` if external.
    """
    parts = target.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in modules:
            return candidate
        parts.pop()
    return None


def _abstractness(tree: ast.Module) -> tuple[int, int]:
    """Count abstract vs total class definitions in a module.

    A class is "abstract" if it inherits from ``ABC`` / ``abc.ABC`` /
    ``ABCMeta`` / ``abc.ABCMeta`` or holds any ``@abstractmethod`` method.

    Args:
        tree: Parsed module.

    Returns:
        Pair ``(abstract_class_count, total_class_count)``.
    """
    abstract = 0
    total = 0
    abstract_bases = {"ABC", "abc.ABC", "ABCMeta", "abc.ABCMeta"}
    abstract_decorators = {"abstractmethod", "abc.abstractmethod"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        total += 1
        bases = {ast.unparse(b) for b in node.bases}
        if bases & abstract_bases:
            abstract += 1
            continue
        has_abstract_method = any(
            isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(ast.unparse(d) in abstract_decorators for d in m.decorator_list)
            for m in node.body
        )
        if has_abstract_method:
            abstract += 1
    return abstract, total


@dataclass
class _TarjanState:
    """Mutable scratch space shared across Tarjan recursion frames.

    Attributes:
        counter: Monotonic DFS discovery index, incremented on each node visit.
        stack: DFS path stack.
        on_stack: Membership flag for each node.
        index: DFS discovery index per node.
        lowlink: Lowest reachable index per node.
        sccs: Completed strongly-connected components.
    """

    counter: int = 0
    stack: list[str] = field(default_factory=list)
    on_stack: dict[str, bool] = field(default_factory=dict)
    index: dict[str, int] = field(default_factory=dict)
    lowlink: dict[str, int] = field(default_factory=dict)
    sccs: list[list[str]] = field(default_factory=list)


def _pop_scc(state: _TarjanState, root: str) -> None:
    """Pop nodes off the DFS stack down to ``root``, forming one SCC.

    Args:
        state: Shared Tarjan scratch.
        root: Node that opened the current SCC.
    """
    component: list[str] = []
    while True:
        w = state.stack.pop()
        state.on_stack[w] = False
        component.append(w)
        if w == root:
            break
    state.sccs.append(component)


def _strongconnect(node: str, graph: dict[str, set[str]], state: _TarjanState) -> None:
    """Tarjan inner step rooted at ``node``.

    Args:
        node: Current DFS node.
        graph: Full adjacency map.
        state: Shared Tarjan scratch.
    """
    state.index[node] = state.counter
    state.lowlink[node] = state.counter
    state.counter += 1
    state.stack.append(node)
    state.on_stack[node] = True
    for succ in graph.get(node, set()):
        if succ not in graph:
            continue
        if succ not in state.index:
            _strongconnect(succ, graph, state)
            state.lowlink[node] = min(state.lowlink[node], state.lowlink[succ])
        elif state.on_stack.get(succ, False):
            state.lowlink[node] = min(state.lowlink[node], state.index[succ])
    if state.lowlink[node] == state.index[node]:
        _pop_scc(state, node)


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Compute strongly-connected components via Tarjan's algorithm.

    Note:
        Large dependency graphs can exceed Python's default recursion
        limit. ``main()`` raises the limit before calling this helper;
        programmatic callers should do the same.

    Args:
        graph: Adjacency map (node → set of successors).

    Returns:
        List of SCCs. Each SCC is a list of node names. Single-node SCCs
        are included.
    """
    state = _TarjanState()
    for node in graph:
        if node not in state.index:
            _strongconnect(node, graph, state)
    return state.sccs


def _compute_couplings(
    graph: dict[str, set[str]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Compute afferent and efferent coupling counts.

    Args:
        graph: Internal dependency graph (only known modules).

    Returns:
        Pair ``(Ca, Ce)`` keyed by module name.
    """
    ca: dict[str, int] = defaultdict(int)
    ce: dict[str, int] = defaultdict(int)
    for src, targets in graph.items():
        ce[src] = len(targets)
        for t in targets:
            ca[t] += 1
    return dict(ca), dict(ce)


def _instability(ca: int, ce: int) -> float:
    """Compute the Martin instability metric.

    Args:
        ca: Afferent coupling count.
        ce: Efferent coupling count.

    Returns:
        ``Ce / (Ca + Ce)``, or ``0.0`` when both counts are zero.
    """
    total = ca + ce
    return ce / total if total else 0.0


def _build_cycle_findings(
    sccs: list[list[str]],
    modules: dict[str, ModuleNode],
) -> list[Finding]:
    """Render multi-node SCCs as CRITICAL ADP-violation findings.

    Args:
        sccs: Strongly-connected components from Tarjan.
        modules: Discovered module nodes keyed by dotted name.

    Returns:
        One finding per cycle.
    """
    findings: list[Finding] = []
    for scc in sccs:
        if len(scc) < MIN_CYCLE_SIZE:
            continue
        ordered = sorted(scc)
        head = modules[ordered[0]]
        evidence = tuple(f"in cycle: {modules[n].path} ({n})" for n in ordered[1:])
        findings.append(
            Finding(
                audit="deps",
                severity=Severity.CRITICAL,
                path=head.path,
                line=1,
                message=f"cyclic dependency: {len(scc)} modules ({', '.join(ordered)})",
                evidence=evidence,
            ),
        )
    return findings


def _build_distance_findings(
    modules: dict[str, ModuleNode],
    ca: dict[str, int],
    ce: dict[str, int],
    *,
    threshold: float,
) -> list[Finding]:
    """Render main-sequence-distance violations as LOW findings.

    Distance from Martin's main sequence is an architectural observation,
    not a defect: utility modules with ``Ce=0`` (no outgoing
    dependencies) are inherently concrete + stable and live at ``D=1.0``
    by construction. Reporting them at ``MEDIUM`` (a blocking severity
    via :func:`exit_code_for`) is a category error — the metric is
    informational. Cycles remain ``CRITICAL`` because they are genuine
    defects.

    Args:
        modules: All discovered module nodes.
        ca: Afferent coupling per module.
        ce: Efferent coupling per module.
        threshold: Report modules with ``D`` above this value.

    Returns:
        One finding per qualifying module.
    """
    findings: list[Finding] = []
    for name, node in modules.items():
        ca_v = ca.get(name, 0)
        ce_v = ce.get(name, 0)
        if ca_v + ce_v == 0:
            continue
        i_v = _instability(ca_v, ce_v)
        a_v = node.abstract_classes / node.total_classes if node.total_classes else 0.0
        d_v = abs(a_v + i_v - 1.0)
        if d_v <= threshold:
            continue
        findings.append(
            Finding(
                audit="deps",
                severity=Severity.LOW,
                path=node.path,
                line=1,
                message=(
                    f"D={d_v:.2f} far from main sequence (I={i_v:.2f}, A={a_v:.2f})"
                ),
                evidence=(
                    f"Ca={ca_v} Ce={ce_v} "
                    f"abstract={node.abstract_classes}/{node.total_classes}",
                ),
            ),
        )
    return findings


def _run_tach() -> list[Finding]:
    """Run optional ``tach check`` and translate violations to findings.

    No-op when ``tach.toml`` is absent or the ``tach`` CLI is not on PATH.

    Returns:
        One HIGH-severity finding per tach violation line.
    """
    if not (repo_root() / "tach.toml").exists():
        return []
    if not shutil.which("tach"):
        logger.info("tach.toml present but `tach` not on PATH — skipping integration")
        return []
    try:
        proc = subprocess.run(
            ["tach", "check"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(repo_root()),
        )
    except OSError as exc:
        logger.warning("tach invocation failed: %s", exc)
        return []
    if proc.returncode == 0:
        return []
    findings: list[Finding] = []
    for raw in (proc.stdout + "\n" + proc.stderr).splitlines():
        line = raw.strip()
        if not line:
            continue
        findings.append(
            Finding(
                audit="deps",
                severity=Severity.HIGH,
                path="tach.toml",
                line=0,
                message=f"tach: {line[:TACH_VIOLATION_PREVIEW]}",
            ),
        )
    return findings


def _scan_module(
    path: Path,
    package_roots: list[Path],
) -> tuple[str, ModuleNode, set[str]] | None:
    """Parse a single file into (name, node, raw-imports).

    Args:
        path: Absolute path to a Python source file.
        package_roots: Roots used to compute dotted names.

    Returns:
        Tuple of name, ``ModuleNode``, and the set of raw import targets.
        ``None`` on parse failure or when the path is outside all roots.
    """
    name = _resolve_module_name(path, package_roots)
    if not name:
        return None
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        logger.debug("skipping %s: %s", path, exc)
        return None
    abstract_count, total_count = _abstractness(tree)
    node = ModuleNode(
        name=name,
        path=relpath(path),
        abstract_classes=abstract_count,
        total_classes=total_count,
    )
    return name, node, _extract_imports(tree, name)


def _build_internal_graph(
    modules: dict[str, ModuleNode],
    raw_imports: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Project raw imports onto the known-module graph.

    Args:
        modules: Discovered modules keyed by dotted name.
        raw_imports: Raw imports per source module.

    Returns:
        Adjacency map keyed by module → set of target module names.
    """
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for src, targets in raw_imports.items():
        for target in targets:
            actual = _closest_known(target, modules)
            if actual and actual != src:
                graph[src].add(actual)
    return graph


TREE_LOG_NAME = "audit_deps_tree.log"
_TREE_BRANCH = "├─"
_TREE_LAST = "└─"
_CYCLE_TAG = " [cycle]"


def render_dependency_tree(
    graph: dict[str, set[str]],
    sccs: list[list[str]],
) -> str:
    """Render the internal dependency graph as a readable plain-text tree.

    Modules are listed in sorted order; each module's internal dependencies
    are listed (sorted) beneath it, indented with tree connectors. Modules
    with no internal dependencies render as bare leaves. Any module that
    participates in an import cycle (member of an SCC of size ``>= 2``) is
    annotated with a ``[cycle]`` tag wherever it appears.

    Args:
        graph: Internal dependency graph (module → set of imported modules),
            as built by :func:`run`.
        sccs: Strongly-connected components from :func:`_tarjan_scc`, used to
            identify cycle members.

    Returns:
        Multi-line string ending with a trailing newline. Deterministic for
        a given graph and SCC set.
    """
    cyclic: set[str] = {
        node for scc in sccs if len(scc) >= MIN_CYCLE_SIZE for node in scc
    }

    def _label(module: str) -> str:
        """Return the display label for ``module`` with an optional cycle tag.

        Args:
            module: Dotted module name.

        Returns:
            The module name, suffixed with ``[cycle]`` if it is a cycle member.
        """
        return f"{module}{_CYCLE_TAG}" if module in cyclic else module

    lines: list[str] = []
    for module in sorted(graph):
        lines.append(_label(module))
        deps = sorted(graph[module])
        for idx, dep in enumerate(deps):
            connector = _TREE_LAST if idx == len(deps) - 1 else _TREE_BRANCH
            lines.append(f"{connector} {_label(dep)}")
    return "\n".join(lines) + "\n"


def _write_tree_log(tree: str, *, output: Path | None) -> Path:
    """Write the rendered dependency tree to ``code_health/audit_deps_tree.log``.

    The repo-root / ``code_health`` resolution mirrors
    :func:`forge.audit.common.write_log` so the tree log lands beside
    ``audit_deps.log``.

    Args:
        tree: Rendered tree text from :func:`render_dependency_tree`.
        output: Optional override for the primary ``audit_deps.log`` path. When
            given, the tree log is placed in the same directory; otherwise it
            defaults to ``code_health/audit_deps_tree.log``.

    Returns:
        Path to the written tree log.
    """
    if output is not None:
        log_dir = output.parent
    else:
        log_dir = common.repo_root() / CODE_HEALTH_DIR
    log_path = log_dir / TREE_LOG_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    header = f"# forge-audit-deps dependency tree\n# generated: {timestamp}\n\n"
    log_path.write_text(header + tree, encoding="utf-8")
    logger.info("wrote %s", log_path)
    return log_path


def build_module_graph(
    scope: Scope,
    roots: list[Path],
) -> tuple[dict[str, ModuleNode], dict[str, set[str]]]:
    """Scan source roots into a module map + internal import graph.

    The shared seam behind both ``forge-audit-deps`` and ``forge-gen-c4``:
    walk the source files under *roots*, parse each into a
    :class:`ModuleNode`, and project raw imports onto the known-module set
    so only intra-codebase edges survive (external imports are dropped).

    Args:
        scope: ``FULL`` or ``CHANGED`` file selection.
        roots: Package-root directories to scan.

    Returns:
        Tuple of (modules keyed by dotted name, adjacency map keyed by
        module → set of imported internal module names).
    """
    package_roots = list(roots)
    modules: dict[str, ModuleNode] = {}
    raw_imports: dict[str, set[str]] = {}
    for path in iter_files(scope, roots):
        result = _scan_module(path, package_roots)
        if result is None:
            continue
        name, node, imports = result
        modules[name] = node
        raw_imports[name] = imports
    return modules, _build_internal_graph(modules, raw_imports)


def run(scope: Scope, roots: list[Path], config: DepsConfig) -> int:
    """Execute the full dependency-analysis pipeline.

    Always writes the findings log to ``code_health/audit_deps.log`` and the
    rendered dependency tree to ``code_health/audit_deps_tree.log``.

    Args:
        scope: ``FULL`` or ``CHANGED``.
        roots: Directories to walk for ``FULL`` scope.
        config: Tunable knobs (distance threshold, log path, tree-print flag).

    Returns:
        Process exit code (0 = clean or only LOW findings, 1 otherwise).
    """
    modules, graph = build_module_graph(scope, roots)
    ca, ce = _compute_couplings(graph)
    sccs = _tarjan_scc(graph)

    tree = render_dependency_tree(graph, sccs)
    _write_tree_log(tree, output=config.output)
    if config.print_tree:
        logger.info("dependency tree:\n%s", tree)

    findings: list[Finding] = []
    findings.extend(_build_cycle_findings(sccs, modules))
    findings.extend(
        _build_distance_findings(modules, ca, ce, threshold=config.distance_threshold),
    )
    findings.extend(_run_tach())

    n_cycles = sum(1 for s in sccs if len(s) >= MIN_CYCLE_SIZE)
    summary = (
        f"Scanned {len(modules)} modules. "
        f"Found {n_cycles} cycle(s); "
        f"{len(findings) - n_cycles} other finding(s)."
    )
    write_log("deps", findings, summary, output=config.output)
    return exit_code_for(findings)


def main() -> int:
    """CLI entry point for ``forge-audit-deps``.

    Raises the interpreter recursion limit before running so Tarjan's SCC
    traversal can handle large dependency graphs without hitting the
    default 1000-frame cap.

    Returns:
        Process exit code.
    """
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))
    parser = make_audit_parser(
        prog="forge-audit-deps",
        description=(
            "Module dependency analysis (cycles + Martin I/A/D metrics). "
            "Also renders a readable dependency tree to "
            "code_health/audit_deps_tree.log."
        ),
    )
    parser.add_argument(
        "--distance-threshold",
        type=float,
        default=DEFAULT_DISTANCE_THRESHOLD,
        help=(
            "Report modules with main-sequence distance above this value "
            f"(default: {DEFAULT_DISTANCE_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--tree",
        action="store_true",
        help=(
            "Print the rendered dependency tree to stdout. The tree is always "
            "written to code_health/audit_deps_tree.log regardless of this flag."
        ),
    )
    args = parser.parse_args()
    scope = Scope(args.scope)
    roots = resolve_roots(args.roots)
    config = DepsConfig(
        distance_threshold=args.distance_threshold,
        output=args.output,
        print_tree=args.tree,
    )
    return run(scope, roots, config)


if __name__ == "__main__":
    sys.exit(main())
