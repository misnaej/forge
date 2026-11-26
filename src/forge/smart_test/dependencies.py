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
import difflib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from forge.config import read_pytest_ignores, resolve_tool_roots
from forge.import_graph import (
    ancestor_edges,
    closest_known,
    extract_import_targets,
    resolve_module_name,
    resolve_package_module_name,
)
from forge.smart_test.git_helpers import file_at_ref, visible_paths


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path


logger = logging.getLogger(__name__)


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
        total_tests: How many test files the whole suite holds, so a
            caller can say what fraction of it a tier runs. No default:
            a zero would read as "the suite is empty" and make every
            selection look like 0% of it.
    """

    newly_at_depth: dict[int, list[str]]
    changed_tests: list[str]
    max_depth: int
    total_tests: int

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


def _is_test_file(path: Path) -> bool:
    """Return whether pytest would collect *path* as a test module.

    Args:
        path: The file path to check.

    Returns:
        True if pytest would collect this path as a test module.
    """
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def _unselectable_test(repo_root: Path) -> Callable[[str], bool]:
    """Build the predicate for "this test file must not be selected".

    Two exclusions, both about files that exist on disk but are not part
    of the suite: those git ignores (a scratch copy, anything under an
    ignored directory) and those pytest's own config excludes from
    collection. Forge has to apply pytest's rules itself because it
    passes explicit file paths, which pytest collects regardless of its
    directory-level ignore settings.

    Only files that would be *collected as tests* are ever put through
    this. Conftests and helper modules keep their place in the import
    graph even when ignored: dropping an edge there would make a real
    test unreachable, which under-selects — the one direction smart-test
    must never fail in.

    Args:
        repo_root: Git repo root.

    Returns:
        A predicate over repo-relative POSIX test paths.
    """
    visible = visible_paths(repo_root)
    ignores = read_pytest_ignores(repo_root)

    def unselectable(relpath: str) -> bool:
        """Return whether *relpath* must be left out of the selection.

        Args:
            relpath: Repo-relative POSIX path of a test file.

        Returns:
            True when git or pytest's config excludes it.
        """
        # `visible is None` means git could not answer, which is "no
        # filter" — never "nothing is visible" (see `visible_paths`).
        if visible is not None and relpath not in visible:
            return True
        return ignores.ignores(relpath)

    return unselectable


def all_test_files(
    repo_root: Path,
    unselectable: Callable[[str], bool] | None = None,
) -> set[str]:
    """Return every repo-relative test file under the configured test roots.

    The full tier's explicit file list — lifecycle deselection subtracts
    from this set, so it must match what a bare pytest run would collect
    (``test_*.py`` / ``*_test.py`` under the test roots), minus the files
    git ignores or pytest's own config excludes (:func:`_unselectable_test`).

    Args:
        repo_root: Git repo root.
        unselectable: The exclusion predicate to reuse. Built here when
            omitted; passed in by a caller that already has one, since
            building it costs a git query and a config read.

    Returns:
        Repo-relative test-file paths.
    """
    _, tests = _roots(repo_root)
    if unselectable is None:
        unselectable = _unselectable_test(repo_root)
    found: set[str] = set()
    for path in _iter_py(tests):
        if _is_test_file(path):
            relpath = path.relative_to(repo_root).as_posix()
            if not unselectable(relpath):
                found.add(relpath)
    return found


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
    caller reduces them to importable modules via
    :func:`forge.import_graph.closest_known`,
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


def _arg_names(args: ast.arguments) -> set[str]:
    """Return every parameter name of *args* — fixtures inject by name.

    Args:
        args: The ``arguments`` node of a function definition.

    Returns:
        All positional, keyword-only, ``*args`` and ``**kwargs`` names.
    """
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return names


def _call_fixture_ref(node: ast.Call) -> tuple[str | None, bool]:
    """Classify one call node for fixture-name references.

    Args:
        node: An AST Call node.

    Returns:
        ``(fixture name or None, is pytest_generate_tests call)``.
        ``getfixturevalue`` and ``parametrize`` take the fixture name as
        their first string argument.
    """
    callee = _dotted(node.func)
    if callee is None:
        return None, False
    if callee.endswith("pytest_generate_tests"):
        return None, True
    if callee.endswith(("getfixturevalue", "parametrize")):
        strings = _string_literals(node.args)
        return (strings[0] if strings else None), False
    return None, False


def _fixture_refs(tree: ast.Module) -> tuple[set[str], bool]:
    """Return the fixture names *tree* references, plus its generate-tests flag.

    A fixture is requested by being a function parameter (pytest injects
    it by name) or dynamically via ``request.getfixturevalue("name")``.
    Both are plain name references the selector can match; anything
    fancier — ``pytest_generate_tests`` — cannot be matched by name, so
    the module is flagged instead and its conftest edges stay wide.
    Collecting every parameter name over-approximates (``tmp_path``,
    ``self``), which is the safe direction for a selector.

    Args:
        tree: Parsed module.

    Returns:
        ``(referenced fixture names, uses pytest_generate_tests)``.
    """
    names: set[str] = set()
    generate_tests = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name == "pytest_generate_tests":
                generate_tests = True
            names.update(_arg_names(node.args))
        elif isinstance(node, ast.Call):
            ref, dynamic = _call_fixture_ref(node)
            generate_tests = generate_tests or dynamic
            if ref is not None:
                names.add(ref)
    return names, generate_tests


@dataclass
class _Graph:
    """The internal import graph plus the name↔path mapping.

    Attributes:
        imports: ``{module: set(internal modules it imports)}``.
        path_of: ``{module: repo-relative path}``.
        test_modules: Subset of module names that are test files.
        fixture_refs: ``{module: fixture names it references}`` — function
            parameter names plus ``getfixturevalue("name")`` literals.
        generate_tests: Modules calling ``pytest_generate_tests``; their
            fixture use is parametrized dynamically and cannot be matched
            by name, so conftest edges toward them stay wide.
    """

    imports: dict[str, set[str]] = field(default_factory=dict)
    path_of: dict[str, str] = field(default_factory=dict)
    test_modules: set[str] = field(default_factory=set)
    fixture_refs: dict[str, set[str]] = field(default_factory=dict)
    generate_tests: set[str] = field(default_factory=set)


def _parse_sources(
    repo_root: Path,
    source_roots: list[Path],
    test_roots: list[Path],
    *,
    follow_mock_patches: bool = False,
) -> tuple[dict[str, tuple[str, set[str]]], set[str], dict[str, set[str]], set[str]]:
    """Parse all source and test files into an import target map.

    Args:
        repo_root: Git repo root.
        source_roots: List of source root paths.
        test_roots: List of test root paths.
        follow_mock_patches: Whether to follow mock.patch targets.

    Returns:
        ``(parsed, test_modules, fixture_refs, generate_tests)``: module
        name → ``(rel_path, targets)``, the names of collectable test
        modules, module → referenced fixture names, and the modules that
        call ``pytest_generate_tests``. Files that fail to parse stay in
        ``parsed`` with empty targets (and out of ``fixture_refs``) so a
        changed one is still reachable — wide, never silently dropped.
    """
    parsed: dict[str, tuple[str, set[str]]] = {}
    test_modules: set[str] = set()
    fixture_refs: dict[str, set[str]] = {}
    generate_tests: set[str] = set()
    for path in _iter_py([*source_roots, *test_roots]):
        is_test = any(path.is_relative_to(tr) for tr in test_roots)
        # Source files are named by their real import root (package-walk) so
        # the dotted name matches what importers use even when the configured
        # scan dir is not the sys.path root. Test files stay repo-rooted
        # (tests.test_x) so they namespace distinctly from the source tree.
        name = (
            resolve_module_name(path, [repo_root])
            if is_test
            else resolve_package_module_name(path, repo_root)
        )
        if not name:
            continue
        rel = path.relative_to(repo_root).as_posix()
        try:
            tree: ast.Module | None = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        except (SyntaxError, UnicodeDecodeError):
            # An unparseable file still enters the graph with no edges: a
            # changed conftest that no longer parses must keep its subtree
            # selected (wide), and a changed test file must still run —
            # dropping it would under-select, the one direction the
            # selector must never fail in. Its fixture refs stay unknown
            # (absent from fixture_refs), which the narrowing reads as wide.
            tree = None
        if tree is not None:
            targets = extract_import_targets(tree, name, include_type_checking=True)
            refs, dynamic = _fixture_refs(tree)
            fixture_refs[name] = refs
            if dynamic:
                generate_tests.add(name)
            if is_test and follow_mock_patches:
                targets = targets | _patch_targets(tree)
        else:
            targets = set()
        if is_test and _is_test_file(path):
            # Only files pytest collects are tests; a conftest or a helper
            # under a test root is a dependency, never a selected test.
            test_modules.add(name)
        parsed[name] = (rel, targets)
    return parsed, test_modules, fixture_refs, generate_tests


def build_graph(
    repo_root: Path,
    *,
    follow_mock_patches: bool = False,
    include_ancestor_edges: bool = False,
) -> _Graph:
    """Parse the repo into an internal import graph.

    Source files are named by climbing the ``__init__.py`` chain from
    the file's parent to find the real import root (e.g.
    ``src/forge/x.py`` → ``forge.x`` when ``src`` has no
    ``__init__.py``); test files resolve rooted at the repo so they
    namespace distinctly (``tests/test_x.py`` → ``tests.test_x``) while
    their ``from forge.x import …`` edges still point at the source
    module. Only edges to known internal modules are kept; external
    imports are dropped. Every test module also gains edges to the
    ``conftest.py`` files pytest loads for it (see :func:`_conftest_edges`).

    Args:
        repo_root: Git repo root.
        follow_mock_patches: When ``True``, a test file's ``mock.patch``
            string targets are added as edges alongside its (opt-in)
            imports — ``patch("pkg.mod.attr")`` becomes a dep on ``pkg.mod``
            even with no import statement. Off by default.
        include_ancestor_edges: When ``True``, every known module gains
            edges to its known ancestor packages
            (:func:`forge.import_graph.ancestor_edges`) — importing
            ``a.b.c`` executes the ancestor ``__init__`` files, so a
            package ``__init__`` edit reaches its descendants' tests in
            the reverse walk. Off by default (design-time consumers model
            declared imports only).

    Returns:
        The populated :class:`_Graph`.
    """
    source_roots, test_roots = _roots(repo_root)
    parsed, test_modules, fixture_refs, generate_tests = _parse_sources(
        repo_root, source_roots, test_roots, follow_mock_patches=follow_mock_patches
    )

    known = set(parsed)
    graph = _Graph(
        test_modules=test_modules,
        fixture_refs=fixture_refs,
        generate_tests=generate_tests,
    )
    for name, (rel, targets) in parsed.items():
        graph.path_of[name] = rel
        resolved = {m for t in targets if (m := closest_known(t, known)) and m != name}
        graph.imports[name] = resolved
    for name, conftests in _conftest_edges(graph).items():
        graph.imports[name] |= conftests
    if include_ancestor_edges:
        for name, ancestors in ancestor_edges(known).items():
            graph.imports[name] |= ancestors
    return graph


def unscanned_conftests(repo_root: Path, changed: set[str]) -> set[str]:
    """Return changed ``conftest.py`` files outside every test root.

    A conftest under a test root reaches its tests through the graph
    (see :func:`_conftest_edges`); one elsewhere — typically at the repo
    root, applying to the whole suite — never enters the graph, so the
    only safe answer is the full suite.

    Args:
        repo_root: Git repo root.
        changed: Repo-relative changed paths.

    Returns:
        The changed conftest paths no test root contains.
    """
    _, test_roots = _roots(repo_root)
    return {
        rel
        for rel in changed
        if rel.rpartition("/")[2] == "conftest.py"
        and not any((repo_root / rel).is_relative_to(tr) for tr in test_roots)
    }


def _conftest_edges(graph: _Graph) -> dict[str, set[str]]:
    """Map each test module to the conftests pytest loads for it.

    pytest runs every test under the ``conftest.py`` files of its folder
    and each ancestor folder, without any import statement naming them,
    so the edge has to be modelled: without it a conftest edit — often an
    autouse fixture every test runs under — reaches no test at all.

    Args:
        graph: The graph with ``path_of`` and ``test_modules`` populated.

    Returns:
        Test module → the conftest modules that apply to it.
    """
    conftest_in: dict[str, str] = {}
    for name, rel in graph.path_of.items():
        folder, _, filename = rel.rpartition("/")
        if filename == "conftest.py":
            conftest_in[folder] = name
    edges: dict[str, set[str]] = {}
    for name in graph.test_modules:
        folder = graph.path_of[name].rpartition("/")[0]
        found: set[str] = set()
        while True:
            if (conftest := conftest_in.get(folder)) is not None:
                found.add(conftest)
            if not folder:
                break
            folder = folder.rpartition("/")[0]
        if found:
            edges[name] = found
    return edges


def _is_fixture_decorator(dec: ast.expr) -> bool:
    """Return whether *dec* is a ``pytest.fixture`` decorator, called or not.

    Args:
        dec: A decorator expression.

    Returns:
        True for ``@fixture``, ``@pytest.fixture`` and their called forms.
    """
    target = dec.func if isinstance(dec, ast.Call) else dec
    dotted = _dotted(target)
    return dotted is not None and (dotted == "fixture" or dotted.endswith(".fixture"))


def _fixture_is_autouse(dec: ast.expr) -> bool:
    """Return whether a fixture decorator call sets ``autouse=True``.

    Args:
        dec: A decorator expression already known to be a fixture decorator.

    Returns:
        True when the decorator is a call with ``autouse=True``.
    """
    if not isinstance(dec, ast.Call):
        return False
    return any(
        kw.arg == "autouse"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in dec.keywords
    )


def _changed_lines(old_src: str, new_src: str) -> tuple[set[int], set[int]]:
    """Return the 1-based line numbers that differ between two sources.

    Args:
        old_src: The base version of a file.
        new_src: The working-tree version.

    Returns:
        ``(changed old lines, changed new lines)``; deletions only touch
        the old side, insertions only the new side.
    """
    matcher = difflib.SequenceMatcher(
        None, old_src.splitlines(), new_src.splitlines(), autojunk=False
    )
    old_changed: set[int] = set()
    new_changed: set[int] = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_changed.update(range(i1 + 1, i2 + 1))
        new_changed.update(range(j1 + 1, j2 + 1))
    return old_changed, new_changed


def _classify_conftest_change(
    repo_root: Path, base_ref: str, rel: str
) -> set[str] | None:
    """Classify what changed inside one conftest: narrow fixture names, or ``None``.

    ``None`` means escalate (the blanket subtree edge stays): an autouse
    fixture, any non-fixture top-level change (module-level code, imports,
    classes), a decorator that itself changed, an unparseable or deleted
    file, or no old version at *base_ref* (a brand-new conftest). The
    narrow result is the set of fixture names whose definitions changed —
    old and new names both on a rename; a change touching no fixture (a
    comment) yields an empty set, because no test can be affected by it.

    Args:
        repo_root: Git repo root.
        base_ref: Ref holding the old version of the conftest.
        rel: Repo-relative POSIX path of the conftest.

    Returns:
        The changed non-autouse fixture names, or ``None`` for wide.
    """
    old_src = file_at_ref(repo_root, base_ref, rel)
    if old_src is None:
        return None
    try:
        new_src = (repo_root / rel).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        old_tree = ast.parse(old_src)
        new_tree = ast.parse(new_src)
    except (SyntaxError, UnicodeDecodeError):
        return None
    old_changed, new_changed = _changed_lines(old_src, new_src)
    names: set[str] = set()
    if not _fold_conftest_side(old_tree, old_changed, names):
        return None
    if not _fold_conftest_side(new_tree, new_changed, names):
        return None
    return names


def _fold_conftest_side(tree: ast.Module, changed: set[int], names: set[str]) -> bool:
    """Fold one version's changed top-level nodes into *names*.

    Args:
        tree: The AST of one version of the conftest.
        changed: That version's changed line numbers.
        names: Set updated with each changed non-autouse fixture's name.

    Returns:
        False when a changed node is anything but an unchanged-decorator
        non-autouse fixture (i.e. the change is wide).
    """
    for node in tree.body:
        # A decorated function's span starts at its first decorator:
        # decorator lines are part of the change surface even though
        # they sit above ``node.lineno``.
        decorators = getattr(node, "decorator_list", [])
        lo = min([node.lineno, *(d.lineno for d in decorators)])
        hi = node.end_lineno or node.lineno
        if not changed.intersection(range(lo, hi + 1)):
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            _is_fixture_decorator(d) for d in node.decorator_list
        ):
            decorator_lines = {
                line
                for d in node.decorator_list
                for line in range(d.lineno, (d.end_lineno or d.lineno) + 1)
            }
            if changed & decorator_lines:
                return False
            if any(_fixture_is_autouse(d) for d in node.decorator_list):
                return False
            names.add(node.name)
        else:
            return False
    return True


def _conftest_fixture_changes(
    repo_root: Path, base_ref: str, changed: set[str], graph: _Graph
) -> dict[str, set[str] | None]:
    """Map each changed in-graph conftest module to its narrow fixture names.

    Args:
        repo_root: Git repo root.
        base_ref: Ref holding the old versions.
        changed: Repo-relative changed paths.
        graph: The import graph (for module↔path mapping).

    Returns:
        Conftest module → changed non-autouse fixture names, or ``None``
        when the change must stay wide.
    """
    module_of = {rel: name for name, rel in graph.path_of.items()}
    return {
        name: _classify_conftest_change(repo_root, base_ref, rel)
        for rel in changed
        if (name := module_of.get(rel)) is not None
        and rel.rpartition("/")[2] == "conftest.py"
    }


def _narrow_conftest_edges(
    repo_root: Path,
    base_ref: str,
    changed_files: set[str],
    graph: _Graph,
    importers: dict[str, set[str]],
) -> None:
    """Restrict changed conftests' reverse edges to fixture-referencing tests.

    A changed conftest whose diff is confined to non-autouse fixtures
    reaches only the subtree tests that reference those fixture names;
    anything uncertain (wide classification, ``pytest_generate_tests`` in
    a candidate test, an unparsed test module) keeps the blanket edge.
    Mutates *importers* in place.

    Args:
        repo_root: Git repo root.
        base_ref: Ref holding the pre-change versions.
        changed_files: Repo-relative changed paths.
        graph: The import graph.
        importers: Reverse edge map, mutated in place.
    """
    for conftest, names in _conftest_fixture_changes(
        repo_root, base_ref, changed_files, graph
    ).items():
        if names is None or conftest not in importers:
            continue
        subtree = importers[conftest]
        # pytest_generate_tests parametrizes by name dynamically; a narrow
        # edge cannot see those references, so stay wide.
        if any(t in graph.generate_tests for t in subtree if t in graph.test_modules):
            continue
        importers[conftest] = {
            m
            for m in subtree
            # An unparsed module's refs are unknown — keep it (wide).
            if m not in graph.test_modules
            or m not in graph.fixture_refs
            or graph.fixture_refs[m] & names
        }


def select_tests(
    repo_root: Path,
    changed_files: set[str],
    max_depth: int,
    *,
    follow_mock_patches: bool = False,
    base_ref: str | None = None,
) -> SelectionPlan:
    """Compute the depth-layered test selection for a change set.

    Reverse-BFS from the changed source modules over import edges: modules
    that import a changed module are one hop out (depth 0 for tests),
    their importers are two hops (depth 1), and so on up to *max_depth*.
    Directly-changed test files are collected separately and always run at
    depth 0.

    A changed conftest normally reaches its whole subtree (see
    :func:`_conftest_edges`), but when *base_ref* is given and the diff
    shows only non-autouse fixtures changed inside it
    (:func:`_conftest_fixture_changes`), the edge narrows to the subtree
    tests that reference those fixture names. Anything uncertain stays
    wide — the depth-tier guarantee errs toward over-selection only.

    Args:
        repo_root: Git repo root.
        changed_files: Repo-relative ``.py`` paths that changed.
        max_depth: Highest depth to expand (0, 1, or 2).
        follow_mock_patches: Treat ``mock.patch`` string targets as edges
            (opt-in); forwarded to :func:`build_graph`.
        base_ref: Ref holding the pre-change versions; enables conftest
            fixture narrowing. ``None`` keeps the blanket conftest edges.

    Returns:
        A :class:`SelectionPlan` describing the selection.
    """
    graph = build_graph(
        repo_root,
        follow_mock_patches=follow_mock_patches,
        include_ancestor_edges=True,
    )
    module_of = {rel: name for name, rel in graph.path_of.items()}

    # The import graph deliberately keeps ignored files (an ignored
    # conftest still carries real edges), so every path that leaves this
    # function as a *selected test* is filtered here. Filtering only
    # `all_test_files` would let a git-ignored or pytest-ignored test
    # back in through the graph.
    unselectable = _unselectable_test(repo_root)

    def selected_tests(modules: Iterable[str]) -> list[str]:
        """Return the selectable test paths among *modules*.

        Args:
            modules: Dotted module names from the graph.

        Returns:
            Sorted repo-relative paths of those that are test modules and
            are not excluded by git or pytest's config.
        """
        paths = (graph.path_of[m] for m in modules if m in graph.test_modules)
        return sorted(path for path in paths if not unselectable(path))

    changed_modules = {module_of[f] for f in changed_files if f in module_of}
    changed_tests = selected_tests(changed_modules)

    importers: dict[str, set[str]] = {}
    for module, deps in graph.imports.items():
        for dep in deps:
            importers.setdefault(dep, set()).add(module)

    if base_ref is not None:
        _narrow_conftest_edges(repo_root, base_ref, changed_files, graph, importers)

    # Self-check: a changed *source* module that nothing imports is the
    # fingerprint of a source-dir/import-root mismatch — the dotted name we
    # resolved does not match the name importers reference, so its reverse
    # edge never connects and the tier would select zero tests silently. Warn
    # loudly instead. A genuinely un-imported module (e.g. a CLI entry point)
    # trips this too; a spurious warning beats a silent false green. A package
    # __init__ with known descendants does not trip it: the ancestor edges
    # give it incoming edges, so it selects through its descendants instead.
    for module in changed_modules:
        if module not in graph.test_modules and not importers.get(module):
            logger.warning(
                "resolved '%s' for %s; no importer references '%s' — "
                "likely a source-dir/import-root mismatch",
                module,
                graph.path_of[module],
                module,
            )

    newly_at_depth: dict[int, list[str]] = {}
    seen = set(changed_modules)
    frontier = set(changed_modules)
    for depth in range(max_depth + 1):
        nxt = {imp for m in frontier for imp in importers.get(m, set())} - seen
        if not nxt:
            break
        tests_here = selected_tests(nxt)
        if tests_here:
            newly_at_depth[depth] = tests_here
        seen |= nxt
        frontier = nxt

    return SelectionPlan(
        newly_at_depth=newly_at_depth,
        changed_tests=changed_tests,
        max_depth=max_depth,
        total_tests=len(all_test_files(repo_root, unselectable)),
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
