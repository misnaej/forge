"""Reverse test→source import graph and depth expansion.

Builds the static import graph of the repo (every module → the internal
modules it imports, via :mod:`forge.import_graph`), then answers the
smart-test question: *which test modules reach a changed source module,
and in how many import hops?* A test that imports a changed module
directly is **depth 0**; one import-level removed is **depth 1**; and so
on. Changed test files always run at depth 0 regardless of their imports.

The walk is conservative — it errs toward selecting an extra test rather
than skipping one that a change could affect (a deliberate guarantee).
Pure module-graph reachability; no runtime instrumentation.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from forge.config import resolve_tool_roots
from forge.import_graph import extract_import_targets, resolve_module_name


if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


@dataclass(frozen=True)
class SelectionPlan:
    """The tests smart-test would run, grouped by the depth they enter at.

    Attributes:
        newly_at_depth: ``{depth: [test relpaths]}`` — tests that first
            become reachable at exactly that depth (a test reachable at
            depth 0 is not repeated at depth 1).
        changed_tests: Test files that were themselves modified; these run
            at depth 0 regardless of imports.
        max_depth: Highest depth the plan was computed for (0, 1, or 2).
    """

    newly_at_depth: dict[int, list[str]]
    changed_tests: list[str]
    max_depth: int

    def tests_up_to(self, depth: int) -> list[str]:
        """Return the sorted unique test relpaths selected at *depth* or below.

        Args:
            depth: Inclusive upper depth bound.

        Returns:
            Sorted test paths: every test newly reachable at depths
            ``0..depth`` plus the directly-changed test files.
        """
        selected = set(self.changed_tests)
        for d, tests in self.newly_at_depth.items():
            if d <= depth:
                selected.update(tests)
        return sorted(selected)


def _roots(repo_root: Path) -> tuple[list[Path], list[Path]]:
    """Return ``(source_dir_paths, test_dir_paths)`` as absolute paths.

    Args:
        repo_root: Git repo root.

    Returns:
        Source roots and test roots, resolved from ``[tool.forge]`` layout
        (``resolve_tool_roots``) to absolute existing directories.
    """
    source = [repo_root / d for d in resolve_tool_roots(repo_root, "smart_test")]
    both = [
        repo_root / d
        for d in resolve_tool_roots(repo_root, "smart_test", include_tests=True)
    ]
    tests = [d for d in both if d not in source]
    return source, tests


def _iter_py(roots: Iterable[Path]) -> Iterable[Path]:
    """Yield every ``.py`` file under *roots*.

    Args:
        roots: Directories to walk.

    Yields:
        Each Python file path found under any root.
    """
    for root in roots:
        yield from root.rglob("*.py")


def _closest_known(target: str, modules: set[str]) -> str | None:
    """Resolve an import *target* to the deepest known module that covers it.

    ``from pkg.mod import name`` yields candidates ``pkg.mod.name`` and
    ``pkg.mod``; this walks the dotted name from longest to shortest and
    returns the first that names a real module in the graph, so an
    attribute import collapses to its module and a submodule import
    resolves to the submodule.

    Args:
        target: A dotted import candidate.
        modules: The set of known internal module names.

    Returns:
        The matching module name, or ``None`` when *target* is external.
    """
    parts = target.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in modules:
            return candidate
    return None


def _dotted(node: ast.expr) -> str | None:
    """Return the dotted name of an attribute/name chain, or ``None``.

    ``patch`` → ``"patch"``; ``mock.patch.object`` → ``"mock.patch.object"``.
    Returns ``None`` for any callee that is not a plain name/attribute chain
    (e.g. a subscript or call result).

    Args:
        node: The ``func`` expression of an :class:`ast.Call`.

    Returns:
        The dotted callee name, or ``None``.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _string_literals(args: list[ast.expr]) -> list[str]:
    """Return the string-constant values among *args*, in order.

    Args:
        args: Positional argument expressions of a call.

    Returns:
        Each ``str`` constant arg; non-string args are skipped.
    """
    return [
        a.value
        for a in args
        if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]


def _classify_patch_call(node: ast.Call) -> str | None:
    """Classify a call node as a patch variant: ``"patch"``, ``"dict"``, or ``None``.

    Returns ``None`` if the node is not a patch call or is ``patch.object``.

    Args:
        node: An AST Call node.

    Returns:
        Patch kind (``"patch"`` or ``"dict"``) or ``None`` if not a patch call.
    """
    callee = _dotted(node.func)
    if callee is None:
        return None
    segments = callee.split(".")
    tail = ".".join(segments[-2:])
    if tail in {"patch.object", "patch.dict"}:
        kind = tail.split(".")[-1]
    elif segments[-1] == "patch":
        kind = "patch"
    else:
        return None
    return None if kind == "object" else kind


def _collect_sys_modules_targets(node: ast.Call, targets: set[str]) -> None:
    """Extract module names from a ``patch.dict("sys.modules", {…})`` call.

    Yields the dict literal keys (module names being injected).

    Args:
        node: A Call node confirmed to be ``patch.dict("sys.modules", {...})``.
        targets: Set to update with the extracted module names.
    """
    for arg in node.args[1:]:
        if isinstance(arg, ast.Dict):
            targets.update(
                k.value
                for k in arg.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )


def _patch_targets(tree: ast.Module) -> set[str]:
    """Return the dotted module-attribute targets of ``mock.patch`` calls.

    Walks every :class:`ast.Call` (decorators included — ``ast.walk`` visits
    them) and collects the string-literal target of the ``patch`` family:
    ``patch("pkg.mod.attr")`` / ``@patch(...)`` / ``patch.dict(...)``,
    tolerating ``mock.``/``mocker.`` prefixes. ``patch.object(obj, "attr")``
    is skipped — ``obj`` is reached through its own import. The special
    ``patch.dict("sys.modules", {…})`` form yields the injected module names
    (its dict keys). Targets are returned raw (``"pkg.mod.attr"``); the
    caller reduces them to importable modules via :func:`_closest_known`,
    exactly as import targets are reduced.

    Args:
        tree: Parsed test module.

    Returns:
        Raw dotted patch targets; empty when the module patches nothing.
    """
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _classify_patch_call(node)
        if kind is None:
            continue
        strings = _string_literals(node.args)
        if not strings:
            continue
        if kind == "dict" and strings[0] == "sys.modules":
            _collect_sys_modules_targets(node, targets)
            continue
        targets.add(strings[0])
    return targets


@dataclass
class _Graph:
    """The internal import graph plus the name↔path mapping.

    Attributes:
        imports: ``{module: set(internal modules it imports)}``.
        path_of: ``{module: repo-relative path}``.
        test_modules: Subset of module names that are test files.
    """

    imports: dict[str, set[str]] = field(default_factory=dict)
    path_of: dict[str, str] = field(default_factory=dict)
    test_modules: set[str] = field(default_factory=set)


def build_graph(repo_root: Path, *, follow_mock_patches: bool = False) -> _Graph:
    """Parse the repo into an internal import graph.

    Source roots resolve to dotted names rooted at the source dir
    (``src/forge/x.py`` → ``forge.x``); test files resolve rooted at the
    repo so they namespace distinctly (``tests/test_x.py`` →
    ``tests.test_x``) while their ``from forge.x import …`` edges still
    point at the source module. Only edges to known internal modules are
    kept; external imports are dropped.

    Args:
        repo_root: Git repo root.
        follow_mock_patches: When ``True``, a test file's ``mock.patch``
            string targets are added as edges alongside its (opt-in)
            imports — ``patch("pkg.mod.attr")`` becomes a dep on ``pkg.mod``
            even with no import statement. Off by default.

    Returns:
        The populated :class:`_Graph`.
    """
    source_roots, test_roots = _roots(repo_root)
    # Source roots first so src files win their dotted name; repo_root last
    # as the catch-all that names test files (which live outside src).
    package_roots = [*source_roots, repo_root]

    parsed: dict[str, tuple[str, set[str]]] = {}
    test_modules: set[str] = set()
    for path in _iter_py([*source_roots, *test_roots]):
        name = resolve_module_name(path, package_roots)
        if not name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = path.relative_to(repo_root).as_posix()
        targets = extract_import_targets(tree, name)
        if any(path.is_relative_to(tr) for tr in test_roots):
            test_modules.add(name)
            if follow_mock_patches:
                targets = targets | _patch_targets(tree)
        parsed[name] = (rel, targets)

    known = set(parsed)
    graph = _Graph(test_modules=test_modules)
    for name, (rel, targets) in parsed.items():
        graph.path_of[name] = rel
        resolved = {m for t in targets if (m := _closest_known(t, known)) and m != name}
        graph.imports[name] = resolved
    return graph


def select_tests(
    repo_root: Path,
    changed_files: set[str],
    max_depth: int,
    *,
    follow_mock_patches: bool = False,
) -> SelectionPlan:
    """Compute the depth-layered test selection for a change set.

    Reverse-BFS from the changed source modules over import edges: modules
    that import a changed module are one hop out (depth 0 for tests),
    their importers are two hops (depth 1), and so on up to *max_depth*.
    Directly-changed test files are collected separately and always run at
    depth 0.

    Args:
        repo_root: Git repo root.
        changed_files: Repo-relative ``.py`` paths that changed.
        max_depth: Highest depth to expand (0, 1, or 2).
        follow_mock_patches: Treat ``mock.patch`` string targets as edges
            (opt-in); forwarded to :func:`build_graph`.

    Returns:
        A :class:`SelectionPlan` describing the selection.
    """
    graph = build_graph(repo_root, follow_mock_patches=follow_mock_patches)
    module_of = {rel: name for name, rel in graph.path_of.items()}

    changed_modules = {module_of[f] for f in changed_files if f in module_of}
    changed_tests = sorted(
        graph.path_of[m] for m in changed_modules if m in graph.test_modules
    )

    importers: dict[str, set[str]] = {}
    for module, deps in graph.imports.items():
        for dep in deps:
            importers.setdefault(dep, set()).add(module)

    newly_at_depth: dict[int, list[str]] = {}
    seen = set(changed_modules)
    frontier = set(changed_modules)
    for depth in range(max_depth + 1):
        nxt = {imp for m in frontier for imp in importers.get(m, set())} - seen
        if not nxt:
            break
        tests_here = sorted(graph.path_of[m] for m in nxt if m in graph.test_modules)
        if tests_here:
            newly_at_depth[depth] = tests_here
        seen |= nxt
        frontier = nxt

    return SelectionPlan(
        newly_at_depth=newly_at_depth,
        changed_tests=changed_tests,
        max_depth=max_depth,
    )


def render_plan(plan: SelectionPlan, depth: int) -> str:
    """Render a parseable ``--show-files`` plan for *depth*.

    Args:
        plan: The computed selection.
        depth: Depth tier to render the cumulative selection for.

    Returns:
        A text block headed ``📋 Tests covering changed code`` with one
        ``  - <path>`` line per selected test, or a no-tests notice.
    """
    tests = plan.tests_up_to(depth)
    header = f"📋 Tests covering changed code (depth {depth})"
    if not tests:
        return f"{header}\n  (none — no tests reach the changed files)"
    lines = "\n".join(f"  - {t}" for t in tests)
    return f"{header}\n{lines}"
