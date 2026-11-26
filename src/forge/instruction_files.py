"""Instruction-document text: packaged foundation, ``@``-includes, AGENTS.md.

Vibe renders ``AGENTS.md`` verbatim into the system prompt — it has no
``@include`` directive, unlike Claude Code's ``CLAUDE.md``. So forge
resolves ``@path`` directive lines at generation time, recursively, and
writes one flattened ``AGENTS.md``. Markdown links (``[text](path)``) are
deliberately left as lazy pointers: both harnesses treat them as
read-on-demand references, and inlining every linked doc would multiply
the instruction context for no semantic gain.

This module is the single home for: the packaged ``FOUNDATION.md`` text,
the shipped-source resolver, the include-line regex, recursive include
resolution, and the AGENTS.md flatten/drift computation. Leaf module —
stdlib only.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Final


#: What a generated agent or skill is called, on every harness.
AGENT_NAME_PREFIX: Final = "forge-"

#: The template beside the shipped agents: it documents how to author an
#: agent rather than defining a spawnable one, so every converter skips
#: it and nothing shipped may cite it (its run-time half lives in
#: ``forge-docs/agent-contracts.md``, which consumers receive).
AGENT_TEMPLATE: Final = "_TEMPLATE.md"


# A whole-line @-directive: optional whitespace, `@`, then a path token
# that must look like a file reference (contains `/` or ends in `.md`) so
# prose mentions like `@team` never match.
INCLUDE_LINE_RE = re.compile(r"^\s*@(\S*(?:/\S*|\.md))\s*\n?", re.MULTILINE)

FOUNDATION_RESOURCE = "data/FOUNDATION.md"

#: Repo-relative forge source paths and their packaged copies under
#: ``src/forge/data/`` (symlinks into the repo root at build time, real
#: files inside the wheel). Consumer repos have none of the repo-root
#: copies, so the packaged ones are what they install from.
SHIPPED_SOURCES: Final = {
    "claude-hooks": "data/claude-hooks",
    "agents": "data/agents",
    "skills": "data/skills",
}


def shipped_path(rel: str) -> Path:
    """Resolve a forge-shipped source path — always from the package.

    **A repo's own directory of the same name is never used**, which is
    why this takes no repo argument at all. Letting it win is unsafe
    now that the guard scripts travel this way: a
    repo carrying its own ``claude-hooks/block_force_push.sh`` would have
    that copy installed and armed in place of forge's, and a consumer
    with an ordinary ``skills/`` directory of their own would see it
    published as forge's and pruned against forge's roster.

    Nothing is lost by dropping it. Forge's own ``agents/``, ``skills/``
    and ``claude-hooks/`` are symlinks into ``src/forge/data/``, so the
    package path reaches the same files in this repo; a consumer who
    wants their own agents writes them unprefixed beside forge's, which
    the ownership rules already allow. Wheels extract to real files, so
    the resource path is a plain filesystem path.

    Args:
        rel: Repo-relative path; must be a key of :data:`SHIPPED_SOURCES`.

    Returns:
        The packaged path.

    Raises:
        KeyError: When *rel* is not a shipped source.
    """
    return Path(str(resources.files("forge").joinpath(SHIPPED_SOURCES[rel])))


def foundation_text() -> str:
    """Return the bundled FOUNDATION.md text shipped with the pip package.

    Returns:
        The full content of ``src/forge/data/FOUNDATION.md`` as a string.
    """
    return resources.files("forge").joinpath(FOUNDATION_RESOURCE).read_text()


def resolve_includes(
    text: str, base_dir: Path, *, _seen: frozenset[Path] = frozenset()
) -> str:
    """Replace whole-line ``@path`` directives with the referenced file's content.

    Resolution is recursive: included files may themselves contain
    directives, resolved against the including file's directory. Markdown
    links are left untouched. A missing target or an include cycle is a
    hard error — a silently dropped rule is worse than a loud failure.

    Args:
        text: The document text to resolve.
        base_dir: Directory that relative ``@path`` targets resolve against.
        _seen: Internal recursion guard: absolute paths already being expanded.

    Returns:
        The text with all directives replaced by their file contents.

    Raises:
        FileNotFoundError: If a directive target does not exist.
        ValueError: If the includes form a cycle.
    """

    def _expand(match: re.Match[str]) -> str:
        target = (base_dir / match.group(1)).resolve()
        if target in _seen:
            msg = f"@-include cycle: {target}"
            raise ValueError(msg)
        if not target.is_file():
            msg = f"@-include target not found: {target}"
            raise FileNotFoundError(msg)
        inner = target.read_text(encoding="utf-8")
        return resolve_includes(inner, target.parent, _seen=_seen | {target})

    return INCLUDE_LINE_RE.sub(_expand, text)


def flatten_agents_md(repo_root: Path) -> str | None:
    """Compose the flattened AGENTS.md for a checkout.

    The composition is the repo's ``FOUNDATION.md`` followed by a
    separator and its ``CLAUDE.md``, with every ``@path`` directive
    resolved recursively. Returns ``None`` when either input file is
    absent (callers fall back to presence-only checks).

    Args:
        repo_root: Repository root containing FOUNDATION.md and CLAUDE.md.

    Returns:
        The flattened AGENTS.md content, or None when inputs are missing.
    """
    foundation_path = repo_root / "FOUNDATION.md"
    claude_md_path = repo_root / "CLAUDE.md"
    if not (foundation_path.is_file() and claude_md_path.is_file()):
        return None
    foundation = resolve_includes(
        foundation_path.read_text(encoding="utf-8"), repo_root
    )
    # The foundation is prepended below, so a @FOUNDATION.md directive in
    # CLAUDE.md is dropped rather than inlined (would duplicate it); every
    # other @-include is resolved in place.
    foundation_abs = foundation_path.resolve()

    def _expand_claude(match: re.Match[str]) -> str:
        target = (repo_root / match.group(1)).resolve()
        if target == foundation_abs:
            return ""
        return resolve_includes(target.read_text(encoding="utf-8"), target.parent)

    claude = INCLUDE_LINE_RE.sub(
        _expand_claude, claude_md_path.read_text(encoding="utf-8")
    )
    return f"{foundation.rstrip()}\n\n---\n\n{claude}"


def generated_name(canonical: str) -> str:
    """Return the name forge publishes a shipped agent or skill under.

    Args:
        canonical: The shipped stem, e.g. ``design-checker``.

    Returns:
        The prefixed name, e.g. ``forge-design-checker``.
    """
    return f"{AGENT_NAME_PREFIX}{canonical}"


def renamed_frontmatter(text: str, canonical: str) -> str:
    """Rewrite a definition's ``name:`` field to the published name.

    The harness resolves an agent or skill by the ``name`` in its
    frontmatter, while the file is found by its path. Publishing under a
    prefixed file name without rewriting the field would leave the two
    disagreeing — the file present, the name it claims unreachable — so
    both move together or neither does.

    Only the first ``name:`` line inside the opening frontmatter block is
    touched; a later one in the body is prose.

    Args:
        text: The shipped definition, frontmatter first.
        canonical: The shipped stem the field is expected to carry.

    Returns:
        The content with the field renamed, unchanged when it is absent
        or does not hold *canonical* (a definition naming something else
        is left for the doc checks to flag rather than silently edited).
    """
    marker = f"\nname: {canonical}\n"
    if not text.startswith("---") or marker not in text:
        return text
    head, sep, tail = text.partition("\n---\n")
    if not sep:
        return text
    # The separator consumes the newline that ends the last frontmatter
    # line, so a `name:` sitting last has no trailing newline left in
    # *head* for the marker to match. Rejoin it for the replacement:
    # matching only mid-block would silently skip exactly that case, and
    # publish a definition whose name disagrees with its own path.
    renamed = (head + "\n").replace(marker, f"\nname: {generated_name(canonical)}\n", 1)
    return renamed.removesuffix("\n") + sep + tail
