"""Parity between the config forge reads and the config it declares.

``forge_config.CONFIG_KEYS`` is the single declared enumeration of the
``[tool.forge.*]`` keys forge reads. An AST scan of ``src/forge`` finds the
reads that actually happen, so a key read but undeclared (invisible to
``forge-config`` and the config doc) fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge.forge_config import CONFIG_KEYS


if TYPE_CHECKING:
    from collections.abc import Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]

_SECTION_READERS = {"_forge_step_config", "read_tool_forge_section"}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef)

# Reads that cannot be declared as a key, as (section, key-or-"*") with the
# section relative to [tool.forge] ("" = the top level). Every entry says why.
_DYNAMIC_READS: dict[tuple[str, str], str] = {
    ("c4", "system"): (
        "C4 model data, not a setting: the presence of an inline `system` "
        "table decides whether [tool.forge.c4] itself is the model"
    ),
}

# Declared keys whose read the scan cannot see, as (section, key). Every
# entry says why. A key read through a dict handed to a helper or a parser
# is invisible to a per-literal scan.
_READ_ELSEWHERE: dict[tuple[str, str], str] = {
    ("changelog", "exempt_paths"): (
        "read by a helper given the section dict: "
        "_cfg_str_list(step_cfg, 'exempt_paths', [])"
    ),
    ("changelog", "require_paths"): (
        "read by a helper given the section dict: "
        "_cfg_str_list(step_cfg, 'require_paths', [])"
    ),
    ("docstring_coverage", "badge"): (
        "read from the whole parsed pyproject dict passed as a parameter "
        "(_badge_enabled(data))"
    ),
    ("pip_audit", "max_age_hours"): (
        "read via a section dict held in a parameter (cfg.get('max_age_hours'))"
    ),
}

# Section-reader calls whose section is not a literal, as (file, call text).
# The wrapper forwards its caller's section name, so each real read is
# scanned at the caller, where the literal is.
_NONLITERAL_SECTIONS: dict[tuple[str, str], str] = {
    ("precommit.py", "config.read_tool_forge_section(repo_root, step)"): (
        "_forge_step_config forwards its `step` parameter; callers pass literals"
    ),
}


def _call_name(call: ast.Call) -> str:
    """Return the bare name of the function a call invokes.

    Args:
        call: The call node to inspect.

    Returns:
        The called name, or ``""`` when it is neither a name nor an attribute.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _str_arg(call: ast.Call, index: int) -> str | None:
    """Return the positional argument at an index when it is a string literal.

    Args:
        call: The call node to inspect.
        index: Zero-based position of the positional argument.

    Returns:
        The string value, or None when absent or not a string literal.
    """
    if len(call.args) > index:
        arg = call.args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _section_of_call(call: ast.Call) -> str | None:
    """Return the ``[tool.forge]``-relative section a call reads, if literal.

    Args:
        call: The call node to inspect.

    Returns:
        The section name, or None when the call is not a section reader or
        its section is not a literal.
    """
    name = _call_name(call)
    if name in _SECTION_READERS:
        if len(call.args) == 1 and name == "read_tool_forge_section":
            return ""
        return _str_arg(call, 1)
    return None


def _const_key(node: ast.expr) -> str | None:
    """Return a node's value when it is a string literal.

    Args:
        node: The expression node to inspect.

    Returns:
        The string value, or None when the node is not a string literal.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _walk_own(scope: ast.AST) -> Iterator[ast.AST]:
    """Walk a scope without entering nested functions (they are own scopes).

    Args:
        scope: The module or function node to walk.

    Yields:
        Each node belonging directly to the scope.
    """
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, _SCOPES):
            pending.extend(ast.iter_child_nodes(node))


def _section_aliases(own: list[ast.AST]) -> dict[str, str]:
    """Map each name assigned a section-reader call to the section it holds.

    Args:
        own: Nodes belonging directly to one scope.

    Returns:
        Mapping of variable name to the section its reader call returns.
    """
    aliases: dict[str, str] = {}
    for node in own:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.targets[0], ast.Name)
        ):
            section = _section_of_call(node.value)
            if section is not None:
                aliases[node.targets[0].id] = section
    return aliases


def _key_lookups(node: ast.AST) -> list[tuple[str | None, ast.AST]]:
    """Return (key, looked-up object) pairs for ``.get``, ``[]`` and ``in``.

    Args:
        node: The node to inspect for key lookups.

    Returns:
        One pair per lookup; the key is None when it is not a string literal.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
    ):
        return [(_const_key(node.args[0]), node.func.value)]
    if isinstance(node, ast.Subscript):
        return [(_const_key(node.slice), node.value)]
    if isinstance(node, ast.Compare):
        # Chained comparisons carry one (op, left, right) per link.
        lefts = [node.left, *node.comparators[:-1]]
        return [
            (_const_key(left), right)
            for op, left, right in zip(node.ops, lefts, node.comparators, strict=True)
            if isinstance(op, (ast.In, ast.NotIn))
        ]
    return []


def _whole_section_reads(
    own: list[ast.AST],
    consumed: set[ast.AST],
    used_aliases: set[str],
) -> set[tuple[str, str]]:
    """Return ``(section, "*")`` for section reads whose keys are never looked up.

    A call bound to an alias that some key lookup used is already accounted
    for; any other unconsumed call reads the section whole.

    Args:
        own: Nodes belonging directly to one scope.
        consumed: Section-reader calls already used by a key lookup.
        used_aliases: Alias names that some key lookup used.

    Returns:
        The whole-section reads as ``(section, "*")`` pairs.
    """
    parents = {
        child: parent for parent in own for child in ast.iter_child_nodes(parent)
    }
    reads: set[tuple[str, str]] = set()
    for node in own:
        if not isinstance(node, ast.Call) or node in consumed:
            continue
        section = _section_of_call(node)
        if section is None:
            continue
        parent = parents.get(node)
        target = parent.targets[0] if isinstance(parent, ast.Assign) else None
        if not (isinstance(target, ast.Name) and target.id in used_aliases):
            reads.add((section, "*"))
    return reads


def _scope_reads(scope: ast.AST) -> set[tuple[str, str]]:
    """Collect the (section, key) reads inside one function or module scope.

    Args:
        scope: The module or function node to scan.

    Returns:
        The set of ``(section, key)`` reads made in the scope.
    """
    own = list(_walk_own(scope))
    aliases = _section_aliases(own)

    def source(node: ast.AST) -> str | None:
        if isinstance(node, ast.Call):
            return _section_of_call(node)
        if isinstance(node, ast.Name):
            return aliases.get(node.id)
        return None

    reads: set[tuple[str, str]] = set()
    used_aliases: set[str] = set()
    consumed: set[ast.AST] = set()
    for node in own:
        for key, src in _key_lookups(node):
            section = source(src) if key is not None else None
            if section is None:
                continue
            reads.add((section, key))
            consumed.add(src)
            if isinstance(src, ast.Name):
                used_aliases.add(src.id)
    return reads | _whole_section_reads(own, consumed, used_aliases)


def _call_reads(tree: ast.AST) -> set[tuple[str, str]]:
    """Return the reads made by helper calls that take the tool name as an argument.

    Args:
        tree: The parsed module to scan.

    Returns:
        The set of ``(section, key)`` reads implied by those helper calls.
    """
    reads: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "resolve_tool_roots":
            tool = _str_arg(node, 1)
            if tool is not None:
                reads.add((tool, "paths"))
        elif name == "resolve_test_naming_roots":
            reads.add(("test_naming_check", "paths"))
    return reads


def _scan_source(source: str) -> set[tuple[str, str]]:
    """Return every literal config read in source text.

    Args:
        source: Python source code to parse and scan.

    Returns:
        The set of ``(section, key)`` reads found.
    """
    tree = ast.parse(source)
    reads = _call_reads(tree)
    scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, _SCOPES))]
    for scope in scopes:
        reads |= _scope_reads(scope)
    return reads


def _scan_tree(root: Path) -> Iterator[tuple[Path, set[tuple[str, str]]]]:
    """Yield each Python file under a directory with the config reads it makes.

    Args:
        root: Directory searched recursively for ``*.py`` files.

    Yields:
        ``(path, reads)`` pairs in sorted path order.
    """
    for path in sorted(root.rglob("*.py")):
        yield path, _scan_source(path.read_text(encoding="utf-8"))


def _nonliteral_section_calls(source: str) -> list[tuple[int, str]]:
    """Return (line, call text) of section-reader calls with a non-literal section.

    Args:
        source: Python source code to parse and scan.

    Returns:
        Sorted ``(line number, unparsed call)`` pairs.
    """
    return sorted(
        (node.lineno, ast.unparse(node))
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and _call_name(node) in _SECTION_READERS
        and _section_of_call(node) is None
    )


def _stale_entries(
    allowlist: dict[tuple[str, str], str],
    seen: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Return allowlist entries that match nothing seen.

    Args:
        allowlist: Mapping of ``(section, key)`` to the reason it is allowed.
        seen: The ``(section, key)`` reads actually found in source.

    Returns:
        Sorted allowlist keys absent from ``seen``.
    """
    return sorted(set(allowlist) - seen)


def _declared_reads() -> set[tuple[str, str]]:
    """CONFIG_KEYS as (section, leaf), section relative to [tool.forge]."""
    return {
        ("", k.path[2]) if len(k.path) == 3 else (k.path[2], k.path[3])
        for k in CONFIG_KEYS
        if k.path[:2] == ("tool", "forge") and len(k.path) > 2
    }


def _is_declared(section: str, key: str) -> bool:
    """Return whether CONFIG_KEYS declares a key under a section.

    Args:
        section: Section relative to ``[tool.forge]``; ``""`` means top level.
        key: Key name, or ``*`` for any key in the section.

    Returns:
        True when CONFIG_KEYS declares the key (or any key for ``*``).
    """
    paths = {k.path for k in CONFIG_KEYS}
    if key == "*":
        wanted = section or None
        return any(len(p) > 3 and (wanted is None or p[2] == wanted) for p in paths)
    if not section:
        return ("tool", "forge", key) in paths or any(
            len(p) > 3 and p[2] == key for p in paths
        )
    return ("tool", "forge", section, key) in paths


@pytest.fixture
def source_with_all_read_patterns() -> str:
    """Source holding one read of every pattern the scanner understands."""
    return (
        "def f(root):\n"
        "    resolve_tool_roots(root, 'ruff')\n"
        "    config.resolve_test_naming_roots(root)\n"
        "    _forge_step_config(root, 'doctest').get('blocking')\n"
        "    read_tool_forge_section(root, 'pr')['docs_only_globs']\n"
        "    if 'mode' in read_tool_forge_section(root, 'changelog'):\n"
        "        pass\n"
        "    alias = _forge_step_config(root, 'smart_test')\n"
        "    alias.get('cadence_mode')\n"
        "    read_tool_forge_section(root).get('base_branch')\n"
        "    read_tool_forge_section(root, 'c4')\n"
        "    read_tool_forge_section(root)\n"
    )


@pytest.fixture
def source_with_structural_read_patterns() -> str:
    """Source reading through scopes, ``not in``, chained compares and an alias."""
    return (
        "def outer(root):\n"
        "    def inner():\n"
        "        return _forge_step_config(root, 'nested').get('deep')\n"
        "    return inner\n"
        "class Holder:\n"
        "    def method(self, root):\n"
        "        return read_tool_forge_section(root, 'cls')['meth']\n"
        "def negated(root):\n"
        "    if 'absent' not in read_tool_forge_section(root, 'neg'):\n"
        "        pass\n"
        "def chained(root):\n"
        "    cfg = read_tool_forge_section(root, 'chain')\n"
        "    return 1 < 2 and 'a' == 'a' in cfg\n"
        "def chained_second_link(root):\n"
        "    cfg = read_tool_forge_section(root, 'link')\n"
        "    return 'x' == 'x' not in cfg\n"
        "def unused_alias(root):\n"
        "    cfg = read_tool_forge_section(root, 'whole')\n"
        "    return cfg\n"
    )


def test_scanner_handles_scopes_negation_and_unused_alias(
    source_with_structural_read_patterns: str,
) -> None:
    """Nested/method scopes, ``not in``, later compare links and aliases all read."""
    assert _scan_source(source_with_structural_read_patterns) == {
        ("nested", "deep"),
        ("cls", "meth"),
        ("neg", "absent"),
        ("chain", "a"),
        ("link", "x"),
        ("whole", "*"),
    }


def test_nonliteral_section_calls_are_reported_with_their_line() -> None:
    """A section reader fed a variable is reported, a literal one is not."""
    source = (
        "read_tool_forge_section(root, 'ok')\n"
        "read_tool_forge_section(root)\n"
        "_forge_step_config(root, name)\n"
    )
    assert _nonliteral_section_calls(source) == [
        (3, "_forge_step_config(root, name)"),
    ]


def test_stale_entries_names_allowlist_keys_matching_no_read() -> None:
    """An allowlist entry absent from the observed reads is reported as stale."""
    allowlist = {("a", "x"): "why", ("b", "y"): "why"}
    assert _stale_entries(allowlist, {("a", "x"), ("c", "z")}) == [("b", "y")]
    assert _stale_entries(allowlist, set(allowlist)) == []


def test_scanner_finds_known_reads(
    source_with_all_read_patterns: str,
) -> None:
    """The scanner yields exactly the literal reads each pattern implies."""
    assert _scan_source(source_with_all_read_patterns) == {
        ("ruff", "paths"),
        ("test_naming_check", "paths"),
        ("doctest", "blocking"),
        ("pr", "docs_only_globs"),
        ("changelog", "mode"),
        ("smart_test", "cadence_mode"),
        ("", "base_branch"),
        ("c4", "*"),
        ("", "*"),
    }


def test_every_forge_config_read_is_declared() -> None:
    """Every literal ``[tool.forge]`` read in src/forge is in CONFIG_KEYS.

    Allowlisted reads (``_DYNAMIC_READS``) are the only exemption, and a
    stale allowlist entry fails so the list cannot outlive its reason.
    """
    seen: dict[tuple[str, str], list[str]] = {}
    for path, reads in _scan_tree(REPO_ROOT / "src" / "forge"):
        for read in reads:
            seen.setdefault(read, []).append(path.name)
    assert seen, "scanner found no config reads at all"

    undeclared = {
        read: files
        for read, files in seen.items()
        if not _is_declared(*read) and read not in _DYNAMIC_READS
    }
    assert not undeclared, f"read but absent from CONFIG_KEYS: {undeclared}"

    stale = _stale_entries(_DYNAMIC_READS, set(seen))
    assert not stale, f"_DYNAMIC_READS entries matching no read: {stale}"


def test_every_declared_key_is_read_somewhere() -> None:
    """Every CONFIG_KEYS entry is read in src/forge, or allowlisted with a reason.

    A ``*`` read of a key's section counts as reading it; the allowlist
    (``_READ_ELSEWHERE``) covers reads the scan cannot see, and a stale
    entry fails so it cannot outlive its reason.
    """
    seen: set[tuple[str, str]] = set()
    for _path, reads in _scan_tree(REPO_ROOT / "src" / "forge"):
        seen |= reads

    unread = sorted(
        (section, key)
        for section, key in _declared_reads()
        if (section, key) not in seen
        and (section, "*") not in seen
        and (section, key) not in _READ_ELSEWHERE
    )
    assert not unread, f"declared in CONFIG_KEYS but read nowhere: {unread}"

    stale = _stale_entries(_READ_ELSEWHERE, _declared_reads())
    assert not stale, f"_READ_ELSEWHERE entries not in CONFIG_KEYS: {stale}"
    redundant = sorted(
        entry for entry in _READ_ELSEWHERE if entry in seen or (entry[0], "*") in seen
    )
    assert not redundant, f"_READ_ELSEWHERE entries the scan now sees: {redundant}"


def test_section_readers_always_get_a_literal_section() -> None:
    """No section-reader call in src/forge takes a non-literal section.

    The scan can only see literal sections, so a variable one would hide
    reads from both parity checks. Allowlisted calls
    (``_NONLITERAL_SECTIONS``) forward a caller's literal.
    """
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted((REPO_ROOT / "src" / "forge").rglob("*.py")):
        for line, text in _nonliteral_section_calls(path.read_text(encoding="utf-8")):
            found.setdefault((path.name, text), []).append(line)

    unlisted = {
        f"{name}:{line}": text
        for (name, text), lines in found.items()
        if (name, text) not in _NONLITERAL_SECTIONS
        for line in lines
    }
    assert not unlisted, f"non-literal config section: {unlisted}"

    stale = _stale_entries(_NONLITERAL_SECTIONS, set(found))
    assert not stale, f"_NONLITERAL_SECTIONS entries matching no call: {stale}"
