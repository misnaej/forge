"""forge-audit-layering: enforce layer-composition contracts.

Layer admission rules usually live as prose in architecture docs, and
prose rots silently. This audit makes the one rule prose cannot enforce
mechanical: a **positive composition clause**. Each configured layer may
declare ``composes_all_of`` — the layers its members must be *built on* —
and every direct child of the layer must reach each of those layers
through its transitive internal-import closure. A child that hand-rolls
its own copy instead of composing the shared layer (the failure mode
import-permission tools cannot express — they only forbid, never
require) becomes a finding.

Granularity is load-bearing: the rule is evaluated per **direct child**
of the layer's package (the next dotted segment), over the child's whole
transitive closure — not per module, which drowns the signal in leaf
noise. The layer package's own ``__init__`` module is the layer surface,
not a child, and is never evaluated.

Configuration (``pyproject.toml``)::

    [tool.forge.layering]
    require_all_classified = true  # optional coverage gate (see below)
    unclassified_allow = ["scripts"]  # deliberate, visible opt-outs

    [[tool.forge.layering.layer]]
    name = "domain"
    packages = ["myproj.models", "myproj.rules"]  # multi-package layer

    [[tool.forge.layering.layer]]
    name = "pipelines"
    package = "myproj.pipelines"
    composes_all_of = ["domain"]
    exempt = ["legacy_import_job"]  # honest, visible exemptions

Each layer names its modules with exactly one of ``package`` (a single
dotted prefix) or ``packages`` (several). ``package`` predates
``packages`` and stays supported — it is shipped consumer config, so
collapsing to one list-typed key (the ``forge-gen-c4`` shape) would
break existing ``pyproject.toml`` files for no capability gain.

``require_all_classified`` (default off) adds a **coverage gate**: every
top-level source package must be classified — reached by some layer's
prefix, or named in ``unclassified_allow`` — else a blocking finding
fires. This is the safety half of multi-package layering: during a
package-dissolve refactor, a package promoted to a new top-level prefix
but not added to any layer would otherwise drop out of the gate
*silently*, on exactly the code just moved. Unlike ``composes_all_of``
violations, an unclassified-package finding is **not** baseline-scoped:
it blocks every commit while the flag is on, not just commits touching
that package.

Severity model: a pre-existing violation is ``LOW`` (reported, never
blocking — the diff is the baseline, so adopting the audit costs zero
day-one noise); a violation in a child containing an **added or renamed**
module (vs the configured base branch) is ``HIGH`` and exits non-zero —
the gate fires on exactly the moment placement is decided.

Relationship to ``forge-gen-c4``: both group modules by dotted prefix
over the same :func:`forge.audit.deps.build_module_graph` seam — C4
*describes* the grouping, this audit *enforces* a contract over it.

Findings are written to ``code_health/audit_layering.log`` in the
standard format.
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from forge.audit.common import (
    Finding,
    Scope,
    Severity,
    exit_code_for,
    make_audit_parser,
    resolve_roots,
    under_module_prefix,
    write_log,
)
from forge.audit.deps import build_module_graph
from forge.config import (
    load_config,
    read_tool_forge_section,
    resolve_tool_roots,
    select_diff_files,
)
from forge.git_utils import added_or_moved_files, configure_cli_logging, repo_root


if TYPE_CHECKING:
    from pathlib import Path

    from forge.audit.deps import ModuleNode


configure_cli_logging()
logger = logging.getLogger(__name__)


_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.REVIEW: 4,
}


@dataclass(frozen=True)
class LayerSpec:
    """One configured layer contract.

    Attributes:
        name: Layer name, referenced by other layers' ``composes_all_of``.
        packages: Dotted package prefixes owning the layer's modules
            (one entry when configured via ``package``).
        composes_all_of: Names of layers every direct child must reach in
            its transitive internal-import closure.
        exempt: Direct-child names excluded from evaluation (rendered as
            informational findings so exemptions stay visible). Matched
            by bare child name, so one entry covers a same-named child
            under every prefix of a multi-package layer.
    """

    name: str
    packages: tuple[str, ...]
    composes_all_of: tuple[str, ...] = ()
    exempt: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayeringConfig:
    """Tunable knobs for the layering audit.

    Attributes:
        output: Optional log-path override.
    """

    output: Path | None = None


def _parse_one_layer(entry: dict[str, object], index: int) -> LayerSpec | str:
    """Parse one ``[[tool.forge.layering.layer]]`` table.

    Every value is type-checked and rejected with an explicit error —
    never coerced: ``package = ["a", "b"]`` used to stringify to a
    prefix matching nothing, a silent misconfiguration.

    Args:
        entry: The raw layer table.
        index: 1-based table position, used in error messages.

    Returns:
        The parsed spec, or an error message when the table is malformed.
    """
    name = entry.get("name")
    if not isinstance(name, str):
        return f"layer #{index}: 'name' must be a string"
    if ("package" in entry) == ("packages" in entry):
        return (
            f"layer #{index}: needs exactly one of 'package' (string) "
            f"or 'packages' (array of strings)"
        )
    if "package" in entry:
        package = entry["package"]
        if not isinstance(package, str):
            return (
                f"layer #{index}: 'package' must be a string — use "
                f"'packages' for a multi-package layer"
            )
        packages: tuple[str, ...] = (package,)
    else:
        raw_packages = entry["packages"]
        if (
            not isinstance(raw_packages, list)
            or not raw_packages
            or not all(isinstance(p, str) for p in raw_packages)
        ):
            return f"layer #{index}: 'packages' must be a non-empty array of strings"
        packages = tuple(raw_packages)
    composes = entry.get("composes_all_of", [])
    exempt = entry.get("exempt", [])
    if not isinstance(composes, list) or not isinstance(exempt, list):
        return (
            f"layer #{index}: 'composes_all_of' and 'exempt' must be "
            f"arrays of layer/child names"
        )
    return LayerSpec(
        name=name,
        packages=packages,
        composes_all_of=tuple(composes),
        exempt=tuple(exempt),
    )


def parse_layers(raw: dict[str, object]) -> tuple[list[LayerSpec], list[str]]:
    """Parse ``[tool.forge.layering]`` into layer specs.

    Args:
        raw: The ``[tool.forge.layering]`` table (may be empty).

    Returns:
        Tuple of (valid layer specs, config-error messages). Errors cover
        malformed tables, duplicate layer names (first definition wins),
        and ``composes_all_of`` references to undefined layer names —
        surfaced as findings, never silently dropped.
    """
    entries = raw.get("layer", [])
    specs: list[LayerSpec] = []
    errors: list[str] = []
    if not isinstance(entries, list):
        return [], ["[tool.forge.layering].layer must be an array of tables"]
    seen: dict[str, int] = {}
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            errors.append(f"layer #{i}: must be a table")
            continue
        parsed = _parse_one_layer(entry, i)
        if isinstance(parsed, str):
            errors.append(parsed)
            continue
        if parsed.name in seen:
            errors.append(
                f"layer #{i}: duplicate layer name '{parsed.name}' "
                f"(first definition, layer #{seen[parsed.name]}, wins)",
            )
            continue
        seen[parsed.name] = i
        specs.append(parsed)
    known = {s.name for s in specs}
    errors.extend(
        f"layer '{s.name}': composes_all_of names undefined layer '{target}'"
        for s in specs
        for target in s.composes_all_of
        if target not in known
    )
    return specs, errors


def _direct_children(
    layer: LayerSpec,
    modules: dict[str, ModuleNode],
) -> dict[tuple[str, str], set[str]]:
    """Group a layer's modules by direct child under each package prefix.

    Keyed by ``(prefix, child)`` so same-named children under different
    prefixes of a multi-package layer stay distinct. A module equal to a
    prefix itself (the package surface) is not a child and is excluded.

    Args:
        layer: The layer being evaluated.
        modules: All discovered modules keyed by dotted name.

    Returns:
        Mapping of (package prefix, child name) to the set of module
        names the child contains.
    """
    children: dict[tuple[str, str], set[str]] = defaultdict(set)
    for prefix in layer.packages:
        for name in modules:
            if not under_module_prefix(name, prefix) or name == prefix:
                continue
            child = name[len(prefix) + 1 :].split(".", 1)[0]
            children[prefix, child].add(name)
    return children


def _closure(graph: dict[str, set[str]], start: set[str]) -> set[str]:
    """Return the transitive import closure of ``start`` (inclusive).

    Args:
        graph: Internal adjacency map (module → imported modules).
        start: Seed module names.

    Returns:
        Every module reachable from the seeds, seeds included.
    """
    seen = set(start)
    stack = list(start)
    while stack:
        for target in graph.get(stack.pop(), ()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


def _child_finding_anchor(
    mods: set[str],
    modules: dict[str, ModuleNode],
) -> tuple[str, int]:
    """Pick a stable file anchor for a child-level finding.

    Args:
        mods: The child's module names.
        modules: All discovered modules.

    Returns:
        (repo-relative path of the child's first module, line 1).
    """
    first = min(mods)
    return modules[first].path, 1


def evaluate(
    layers: list[LayerSpec],
    modules: dict[str, ModuleNode],
    graph: dict[str, set[str]],
    *,
    escalate_paths: set[str],
) -> list[Finding]:
    """Evaluate every layer contract over the module graph.

    Args:
        layers: Parsed layer specs.
        modules: Discovered modules keyed by dotted name.
        graph: Internal adjacency map.
        escalate_paths: Repo-relative paths of added/renamed files — a
            violating child containing one is ``HIGH`` (blocking) instead
            of ``LOW`` (pre-existing baseline).

    Returns:
        Severity-ordered findings (composition misses + visible exemptions).
    """
    layer_modules = {
        spec.name: {
            m for m in modules if any(under_module_prefix(m, p) for p in spec.packages)
        }
        for spec in layers
    }
    findings: list[Finding] = []
    for spec in layers:
        if not spec.composes_all_of:
            continue
        for (_prefix, child), mods in sorted(_direct_children(spec, modules).items()):
            path, line = _child_finding_anchor(mods, modules)
            if child in spec.exempt:
                findings.append(
                    Finding(
                        audit="layering",
                        severity=Severity.REVIEW,
                        path=path,
                        line=line,
                        message=(
                            f"layer '{spec.name}' child '{child}' is exempt "
                            f"from composes_all_of (visible exemption)"
                        ),
                    ),
                )
                continue
            closure = _closure(graph, mods)
            added_here = any(modules[m].path in escalate_paths for m in mods)
            for target in spec.composes_all_of:
                if not layer_modules.get(target):
                    # Undefined name or zero-module layer: both are
                    # config-error findings, never per-child misses — an
                    # emptied layer (mid-restructure) must not fail
                    # every child of every composing layer.
                    continue
                if closure & layer_modules[target]:
                    continue
                severity = Severity.HIGH if added_here else Severity.LOW
                findings.append(
                    Finding(
                        audit="layering",
                        severity=severity,
                        path=path,
                        line=line,
                        message=(
                            f"layer '{spec.name}' child '{child}' does not "
                            f"compose layer '{target}' anywhere in its "
                            f"import closure (composes_all_of)"
                            + (" [added/moved module]" if added_here else "")
                        ),
                    ),
                )
    findings.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.path, f.line))
    return findings


def _parse_coverage_config(
    raw: dict[str, object],
    *,
    has_layers: bool,
) -> tuple[bool, tuple[str, ...], list[str]]:
    """Parse the coverage-gate keys of ``[tool.forge.layering]``.

    Same never-coerce discipline as :func:`_parse_one_layer`: a wrong
    type is a named config error, and requiring classification with no
    layers configured is an error too — nothing could classify anything,
    and a silent no-op is the exact failure the gate exists to prevent.

    Args:
        raw: The ``[tool.forge.layering]`` table (may be empty).
        has_layers: Whether at least one layer spec parsed successfully.

    Returns:
        Tuple of (require_all_classified, allow entries, error messages).
    """
    errors: list[str] = []
    require = raw.get("require_all_classified", False)
    if not isinstance(require, bool):
        errors.append(
            "[tool.forge.layering].require_all_classified must be a boolean",
        )
        require = False
    allow_raw = raw.get("unclassified_allow", [])
    if not isinstance(allow_raw, list) or not all(
        isinstance(a, str) for a in allow_raw
    ):
        errors.append(
            "[tool.forge.layering].unclassified_allow must be an array of strings",
        )
        allow_raw = []
    if require and not has_layers:
        errors.append(
            "require_all_classified = true needs at least one "
            "[[tool.forge.layering.layer]] — with no layers, nothing "
            "could classify any package",
        )
        require = False
    return require, tuple(allow_raw), errors


def _top_level_packages(modules: dict[str, ModuleNode]) -> dict[str, set[str]]:
    """Group modules by top-level package (first dotted segment).

    Args:
        modules: Discovered modules keyed by dotted name.

    Returns:
        Mapping of top-level package name to its module names.
    """
    tops: dict[str, set[str]] = defaultdict(set)
    for name in modules:
        tops[name.split(".", 1)[0]].add(name)
    return tops


def _coverage_findings(
    layers: list[LayerSpec],
    modules: dict[str, ModuleNode],
    *,
    allow: tuple[str, ...],
) -> tuple[list[Finding], int]:
    """Flag top-level packages no layer classifies (``require_all_classified``).

    A top-level package counts as classified when any layer prefix
    shares its first dotted segment — a single-root layout (every prefix
    under ``myproj``) never flags its own root, while a package promoted
    to a new top-level prefix with no layer reference is caught the
    moment it appears. Allowed packages and stale allow entries surface
    as REVIEW findings so opt-outs stay visible.

    Args:
        layers: Parsed layer specs.
        modules: Discovered modules keyed by dotted name.
        allow: ``unclassified_allow`` entries (deliberate opt-outs).

    Returns:
        Tuple of (findings, count of blocking unclassified packages).
    """
    tops = _top_level_packages(modules)
    classified = {p.split(".", 1)[0] for spec in layers for p in spec.packages}
    findings: list[Finding] = []
    n_unclassified = 0
    for top, mods in sorted(tops.items()):
        if top in classified:
            continue
        path, line = _child_finding_anchor(mods, modules)
        if top in allow:
            findings.append(
                Finding(
                    audit="layering",
                    severity=Severity.REVIEW,
                    path=path,
                    line=line,
                    message=(
                        f"top-level package '{top}' is deliberately "
                        f"unclassified (unclassified_allow — visible exemption)"
                    ),
                ),
            )
            continue
        n_unclassified += 1
        findings.append(
            Finding(
                audit="layering",
                severity=Severity.HIGH,
                path=path,
                line=line,
                message=(
                    f"top-level package '{top}' is not classified by any "
                    f"layer (require_all_classified) — add it to a layer's "
                    f"package(s) or to unclassified_allow"
                ),
            ),
        )
    for entry in allow:
        if entry not in tops:
            note = "matches no discovered top-level package (stale — remove it)"
        elif entry in classified:
            note = "names an already-classified package (redundant — remove it)"
        else:
            continue  # consumed above as a visible exemption
        findings.append(
            Finding(
                audit="layering",
                severity=Severity.REVIEW,
                path="pyproject.toml",
                line=1,
                message=f"unclassified_allow entry '{entry}' {note}",
            ),
        )
    return findings, n_unclassified


def _summary(
    n_layers: int,
    n_children: int,
    findings: list[Finding],
    *,
    n_config_errors: int = 0,
    n_unclassified: int = 0,
) -> str:
    """Render the one-paragraph audit summary.

    Args:
        n_layers: Number of configured layers.
        n_children: Number of evaluated direct children.
        findings: Final findings list (config errors included).
        n_config_errors: HIGH findings that are config errors, not
            added/moved-module violations — counted separately so the
            summary does not misattribute them.
        n_unclassified: HIGH findings from the ``require_all_classified``
            coverage gate — same misattribution concern.

    Returns:
        One-line summary for the log header.
    """
    n_block = (
        sum(1 for f in findings if f.severity is Severity.HIGH)
        - n_config_errors
        - n_unclassified
    )
    n_base = sum(1 for f in findings if f.severity is Severity.LOW)
    errors_clause = f" {n_config_errors} config error(s)." if n_config_errors else ""
    unclassified_clause = (
        f" {n_unclassified} unclassified package(s)." if n_unclassified else ""
    )
    return (
        f"Evaluated {n_children} direct child(ren) across {n_layers} "
        f"layer(s). {n_block} blocking violation(s) on added/moved modules, "
        f"{n_base} pre-existing (baseline, non-blocking)."
        f"{unclassified_clause}{errors_clause}"
    )


def run(scope: Scope, roots: list[Path], config: LayeringConfig) -> int:
    """Execute the layering audit.

    The module graph is always built over the full tree (a layer contract
    is a whole-tree property); ``CHANGED`` scope filters *findings* to
    children containing a modified file, mirroring the prior-art filter
    semantics of ``forge-audit-dup``.

    Args:
        scope: ``FULL`` or ``CHANGED``.
        roots: Package-root directories to scan.
        config: Tunable knobs.

    Returns:
        Process exit code (0 = clean or baseline-only, 1 = blocking
        violation or config error).
    """
    root = repo_root()
    raw = read_tool_forge_section(root, "layering")
    layers, errors = parse_layers(raw)
    # Coverage-config errors merge in BEFORE the early return: requiring
    # classification with nothing configured must fail loudly, not exit 0
    # as "nothing to enforce" — that silent no-op is the gate's whole
    # reason to exist.
    require_classified, allow, coverage_errors = _parse_coverage_config(
        raw,
        has_layers=bool(layers),
    )
    errors.extend(coverage_errors)
    if not layers and not errors:
        write_log(
            "layering",
            [],
            "No [tool.forge.layering] layers configured — nothing to enforce.",
            output=config.output,
        )
        return 0

    modules, graph = build_module_graph(Scope.FULL, roots)
    # A layer matching zero modules is one loud config error, not N child
    # misses (evaluate() skips empty targets): a typo'd prefix or an
    # emptied layer mid-restructure gets a single actionable finding.
    errors.extend(
        f"layer '{spec.name}' matches no modules under "
        + ", ".join(f"'{p}'" for p in spec.packages)
        for spec in layers
        if not any(under_module_prefix(m, p) for m in modules for p in spec.packages)
    )
    escalate = set(
        added_or_moved_files(
            repo_root=root,
            base_branch=load_config(root).base_branch,
        ),
    )
    findings = evaluate(layers, modules, graph, escalate_paths=escalate)
    n_unclassified = 0
    if require_classified:
        coverage, n_unclassified = _coverage_findings(layers, modules, allow=allow)
        findings = sorted(
            findings + coverage,
            key=lambda f: (_SEVERITY_ORDER[f.severity], f.path, f.line),
        )

    if scope is Scope.CHANGED:
        # Keep a finding when ANY module of its child was touched (child
        # membership, not just the anchor path — mirrors dup's
        # _touches_changed semantics); HIGH findings always survive.
        changed = set(select_diff_files(root)) | escalate
        touched_anchors: set[str] = set()
        for spec in layers:
            for mods in _direct_children(spec, modules).values():
                if {modules[m].path for m in mods} & changed:
                    touched_anchors.add(_child_finding_anchor(mods, modules)[0])
        if require_classified:
            # REVIEW coverage findings survive when their package was
            # touched (HIGH ones survive unconditionally below).
            for mods in _top_level_packages(modules).values():
                if {modules[m].path for m in mods} & changed:
                    touched_anchors.add(_child_finding_anchor(mods, modules)[0])
        findings = [
            f
            for f in findings
            if f.path in touched_anchors or f.severity is Severity.HIGH
        ]

    findings = [
        Finding(
            audit="layering",
            severity=Severity.HIGH,
            path="pyproject.toml",
            line=1,
            message=msg,
        )
        for msg in errors
    ] + findings

    n_children = sum(
        len(_direct_children(spec, modules)) for spec in layers if spec.composes_all_of
    )
    write_log(
        "layering",
        findings,
        _summary(
            len(layers),
            n_children,
            findings,
            n_config_errors=len(errors),
            n_unclassified=n_unclassified,
        ),
        output=config.output,
    )
    return exit_code_for(findings)


def main() -> int:
    """CLI entry point for ``forge-audit-layering``.

    Returns:
        Process exit code.
    """
    parser = make_audit_parser(
        prog="forge-audit-layering",
        description=(
            "Enforce layer-composition contracts: every direct child of a "
            "configured layer must compose the layers named in its "
            "composes_all_of clause. Blocking only for added/moved modules."
        ),
    )
    args = parser.parse_args()
    root = repo_root()
    # Layer contracts are source-tree properties: the generic audit
    # DEFAULT_ROOTS include test dirs, and a test package mirroring a
    # source namespace would be evaluated as a layer child (spurious
    # findings). Route through the shared source-only resolution
    # ([tool.forge.layering].paths → source_dirs → auto-detect) instead;
    # explicit --roots stays the highest override.
    if args.roots:
        roots = resolve_roots(args.roots)
    else:
        roots = [(root / r).resolve() for r in resolve_tool_roots(root, "layering")]
    return run(
        Scope(args.scope),
        roots,
        LayeringConfig(output=args.output),
    )


if __name__ == "__main__":
    sys.exit(main())
