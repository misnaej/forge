"""Shared AST import-graph primitives.

The two pure, audit-agnostic building blocks for static import analysis:
turning a ``.py`` path into a dotted module name, and extracting the set
of import targets from a parsed module. Both are derived purely from the
syntax tree — no runtime instrumentation, no import execution.

They live here rather than inside their consumers because both
``forge.audit.deps`` (module-coupling graph for architecture metrics) and
``forge.smart_test`` (reverse test→source reachability graph) build on the
same primitive — "what does this module import?". Sharing it keeps that
primitive a single source of truth (FOUNDATION §12) instead of copied.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet
    from pathlib import Path


def closest_known(target: str, modules: AbstractSet[str]) -> str | None:
    """Resolve an import *target* to the deepest known module that covers it.

    The inverse consumer of :func:`extract_import_targets`'s ``(X, X.Y)``
    dual-emit: ``from pkg.mod import name`` yields candidates ``pkg.mod.name``
    and ``pkg.mod``; this walks the dotted name from longest to shortest and
    returns the first that names a real module in *modules*. So an attribute
    import collapses to its module (``pkg.mod``) and a submodule import
    resolves to the submodule (``pkg.mod.name``).

    Args:
        target: A dotted import candidate.
        modules: The set of known internal module names.

    Returns:
        The matching module name, or ``None`` when *target* is external
        (no prefix of it names a known module).
    """
    parts = target.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in modules:
            return candidate
    return None


def ancestor_edges(modules: AbstractSet[str]) -> dict[str, set[str]]:
    """Map each known module to its known ancestor packages.

    Importing ``a.b.c`` executes ``a/__init__.py`` and ``a/b/__init__.py``
    at runtime, so every module implicitly depends on its ancestor
    packages even when no import statement names them. A statically-built
    graph that omits these edges leaves a package ``__init__`` with
    submodule-reaching consumers (``from a.b.c import X``) with zero
    incoming edges — the false-negative test selection this helper
    exists to prevent. Opt-in per consumer: design-time graphs (deps
    audit, C4) deliberately model declared imports only and do not call
    this.

    Args:
        modules: The set of known internal module names.

    Returns:
        ``{module: {known ancestors}}``; modules with no known ancestor
        are omitted.
    """
    edges: dict[str, set[str]] = {}
    for module in modules:
        parts = module.split(".")
        ancestors = {
            candidate
            for end in range(1, len(parts))
            if (candidate := ".".join(parts[:end])) in modules
        }
        if ancestors:
            edges[module] = ancestors
    return edges


def _rel_to_dotted(rel: Path) -> str | None:
    """Convert a root-relative ``.py`` path to a dotted module name.

    ``pkg/mod.py`` → ``pkg.mod``; ``pkg/__init__.py`` → ``pkg`` (the package
    is named by its dir, not its ``__init__``); a bare ``__init__.py`` at the
    root → ``None``.

    Args:
        rel: Path relative to a ``sys.path`` root, suffix included.

    Returns:
        Dotted module name, or ``None`` when nothing remains after dropping
        an ``__init__`` leaf.
    """
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def resolve_module_name(path: Path, package_roots: list[Path]) -> str | None:
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
        return _rel_to_dotted(rel)
    return None


def resolve_package_module_name(path: Path, repo_root: Path) -> str | None:
    """Name a source file by its real import root, derived from package layout.

    Unlike :func:`resolve_module_name` — which strips a *configured* scan-dir
    prefix — this climbs the ``__init__.py`` chain to find the actual
    ``sys.path`` root, so the emitted dotted name matches what importers use
    regardless of how the scan dir was configured. The root is the first
    ancestor that is **not** a package (has no ``__init__.py``); the walk is
    floored at *repo_root* so it never escapes the repo.

    This resolves the source-dir/import-root split that misnames modules for
    two layouts a plain scan-dir strip gets wrong:

    - a ``source_dirs`` entry that is itself a package (``libs/`` with an
      ``__init__.py`` → ``libs.thing.core``, not ``thing.core``), and
    - a ``source_dirs`` entry holding a nested ``*/src`` root
      (``projects/APP/src/pkg/runner.py`` → ``pkg.runner``, not
      ``APP.src.pkg.runner``).

    A plain ``src/pkg/mod.py`` (``src`` has no ``__init__.py``) still resolves
    to ``pkg.mod`` — identical to the scan-dir strip, so no behavior change.

    Args:
        path: Absolute path to a Python source file.
        repo_root: Git repo root; the highest the walk may climb.

    Returns:
        Dotted module name, or ``None`` if *path* is not under *repo_root*.
    """
    root = path.parent
    while root != repo_root and (root / "__init__.py").exists():
        root = root.parent
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return _rel_to_dotted(rel)


def _is_type_checking_test(test: ast.expr) -> bool:
    """Return whether an ``if`` test is the ``TYPE_CHECKING`` guard.

    Matches the bare name (``if TYPE_CHECKING:``) and the attribute form
    (``if typing.TYPE_CHECKING:``).

    Args:
        test: The ``ast.If`` node's test expression.

    Returns:
        True for either recognized guard spelling.
    """
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def extract_import_targets(
    tree: ast.Module,
    current_module: str,
    *,
    include_type_checking: bool = False,
) -> set[str]:
    """Return the set of fully-qualified import-candidate targets.

    Relative imports are resolved against ``current_module``. For
    ``from X import Y`` we emit BOTH ``X`` and ``X.Y`` as candidates —
    at parse time we cannot know whether ``Y`` is a submodule (an edge to
    ``X.Y``) or an attribute of ``X`` (an edge to ``X``). A consumer that
    cares picks the deepest target present in its own graph, so attribute
    imports collapse to ``X`` and submodule imports resolve to ``X.Y``.

    ``if TYPE_CHECKING:`` bodies are skipped by default: those imports
    never execute, so counting them invents runtime edges — a false
    cycle in the deps audit, or a ``composes_all_of`` clause silently
    satisfied by an import that never runs. Callers wanting design-time
    edges (architecture diagrams, conservative test selection) opt in.

    Args:
        tree: Parsed module.
        current_module: Dotted name of the importing module (for relative
            import resolution).
        include_type_checking: Also record imports inside
            ``if TYPE_CHECKING:`` bodies (the ``else`` branch is always
            walked — it runs at runtime).

    Returns:
        Set of dotted target candidates.
    """
    targets: set[str] = set()
    parts = current_module.split(".")
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if (
            not include_type_checking
            and isinstance(node, ast.If)
            and _is_type_checking_test(node.test)
        ):
            stack.extend(node.orelse)
            continue
        stack.extend(ast.iter_child_nodes(node))
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
