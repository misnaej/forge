"""forge-changelog — changelog fragments: validation, discovery, assembly.

Fragment mode (``[tool.forge.changelog].mode = "fragments"``) replaces the
shared ``## vX.Y.Z`` heading — the merge-conflict hotspot every parallel
PR edits — with one unique file per PR under ``changelog.d/``:

    changelog.d/<slug>.<type>.md

``<type>`` is one of :data:`FRAGMENT_TYPES` and maps to the release
entry's group heading. The file body opens with a ``bump:`` front-matter
line naming the semver LEVEL only (:data:`FRAGMENT_LEVELS`) — a concrete
version number anywhere in a fragment is INVALID by design: versions are
computed once, by the assembler, from the latest tag; a version inside
fragments would recreate the exact next-slot collision fragments exist
to remove. The rest of the body is the entry's markdown, verbatim.

Direction of truth is one-way: fragments → assembler → CHANGELOG +
version. In fragment mode nothing may read ``CHANGELOG.md`` as a
version or bump signal — the changelog is an OUTPUT of release, written
by :func:`assemble_changelog`'s single writer (the dual-track promotion
commit, or ``forge-changelog assemble`` in a single-track release flow).

Usage:

- ``forge-changelog assemble --version vX.Y.Z`` — collate every pending
  fragment into ``CHANGELOG.md`` under a new heading and stage the
  fragment deletions (never commits).
- ``forge-changelog check`` — validate pending fragments (the same gate
  the ``changelog_version`` pre-commit step runs in fragment mode).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forge.changelog import top_release_heading
from forge.git_utils import configure_cli_logging, emit, repo_root, run_git


configure_cli_logging()
logger = logging.getLogger(__name__)


FRAGMENTS_DIR = Path("changelog.d")

# Fragment type token → release-entry group heading, in assembly order.
# dict order IS the rendered group order — deliberate and stable.
FRAGMENT_TYPES: dict[str, str] = {
    "added": "Features",
    "changed": "Changes",
    "fixed": "Fixes",
    "removed": "Removed",
    "docs": "Docs",
}

# The only values the bump front-matter may carry — semver LEVELS, never
# numbers. Ordered weakest-first; comparisons use the index.
FRAGMENT_LEVELS = ("patch", "minor", "major")

# `<slug>.<type>.md` — slug is a plain filename fragment (no dots, so the
# type token parses unambiguously).
_FILENAME_RE = re.compile(
    r"^(?P<slug>[A-Za-z0-9][A-Za-z0-9_-]*)\.(?P<type>[a-z]+)\.md$"
)

# First body line: `bump: <level>`.
_BUMP_RE = re.compile(r"^bump:\s*(?P<level>[a-z]+)\s*$")

# A version-shaped token (vX.Y.Z or X.Y.Z) — forbidden anywhere in a
# fragment, filename or body: the level-only rule is gate-enforced, not
# conventional.
_VERSION_SHAPED_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b")

# An embedded `## ` heading would splice fake structure into the
# assembled changelog — rejected outright.
_EMBEDDED_HEADING_RE = re.compile(r"^##\s", re.MULTILINE)


@dataclass(frozen=True)
class Fragment:
    """One parsed pending changelog fragment.

    Attributes:
        path: The fragment file location under ``changelog.d/``.
        slug: The filename's identifying stem.
        type: The fragment type token (a :data:`FRAGMENT_TYPES` key).
        level: The declared bump level (a :data:`FRAGMENT_LEVELS` member).
        body: The entry markdown, front-matter stripped, whitespace-trimmed.
    """

    path: Path
    slug: str
    type: str
    level: str
    body: str


def _parse_and_validate_filename(name: str) -> tuple[str, str, list[str]]:
    """Validate filename and extract slug and type.

    Args:
        name: Fragment filename.

    Returns:
        ``(slug, type, errors)`` — both strings set to empty when errors exist.
    """
    errors: list[str] = []
    slug = ""
    ftype = ""
    match = _FILENAME_RE.match(name)
    if not match:
        errors.append(f"{name}: filename must be <slug>.<type>.md")
    else:
        slug, ftype = match.group("slug"), match.group("type")
        if ftype not in FRAGMENT_TYPES:
            errors.append(
                f"{name}: unknown type '{ftype}' (allowed: {', '.join(FRAGMENT_TYPES)})"
            )
    if _VERSION_SHAPED_RE.search(name):
        errors.append(f"{name}: version-shaped string in filename — levels only")
    return slug, ftype, errors


def _parse_bump_line_and_body(
    path: Path, lines: list[str]
) -> tuple[str, str, list[str]]:
    """Validate bump line and extract level and body.

    Args:
        path: Fragment file path.
        lines: Split text lines.

    Returns:
        ``(level, body, errors)`` — both strings set to empty when errors exist.
    """
    errors: list[str] = []
    name = path.name
    level = ""
    if not lines or not (bump := _BUMP_RE.match(lines[0])):
        errors.append(f"{name}: first line must be 'bump: patch|minor|major'")
    else:
        level = bump.group("level")
        if level not in FRAGMENT_LEVELS:
            errors.append(
                f"{name}: unknown level '{level}' "
                f"(allowed: {', '.join(FRAGMENT_LEVELS)})"
            )
    body = "\n".join(lines[1:]).strip()
    if not body:
        errors.append(f"{name}: empty entry body")
    return level, body, errors


def _check_no_versions_or_headings(name: str, body: str) -> list[str]:
    """Check for version-shaped strings and embedded headings.

    Args:
        name: Fragment filename (for error messages).
        body: The fragment body text.

    Returns:
        List of errors found (empty if all checks pass).
    """
    errors: list[str] = []
    if _VERSION_SHAPED_RE.search(body):
        errors.append(
            f"{name}: version-shaped string in body — the assembler is the "
            "only writer of version numbers"
        )
    if _EMBEDDED_HEADING_RE.search(body):
        errors.append(f"{name}: embedded '## ' heading in body — not allowed")
    return errors


def validate_fragment(path: Path) -> tuple[Fragment | None, list[str]]:
    """Parse *path* into a :class:`Fragment`, collecting every violation.

    The gate contract: filename must match ``<slug>.<type>.md`` with a
    known type; the body's first line must be ``bump: <level>`` with a
    known level; no version-shaped string may appear in the filename or
    body (level-only rule); no embedded ``## `` heading may appear in
    the body (assembly-structure injection).

    Args:
        path: Fragment file to validate.

    Returns:
        ``(fragment, errors)`` — ``fragment`` is ``None`` whenever
        ``errors`` is non-empty.
    """
    name = path.name
    slug, ftype, filename_errors = _parse_and_validate_filename(name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"{name}: unreadable ({exc})"]
    lines = text.splitlines()
    level, body, bump_errors = _parse_bump_line_and_body(path, lines)
    content_errors = _check_no_versions_or_headings(name, body)
    errors = filename_errors + bump_errors + content_errors
    if errors:
        return None, errors
    return Fragment(path=path, slug=slug, type=ftype, level=level, body=body), []


def discover_fragments(root: Path) -> list[Path]:
    """Return pending fragment files under ``changelog.d/``, filename-sorted.

    Sorting makes assembly output deterministic regardless of filesystem
    order — tests and PR diffs stay stable.

    Args:
        root: Repository root directory.

    Returns:
        Sorted fragment paths; empty when the directory is absent.
    """
    directory = root / FRAGMENTS_DIR
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.md") if p.is_file())


def max_level(fragments: list[Fragment]) -> str:
    """Return the strongest bump level among *fragments*.

    Args:
        fragments: Validated fragments (non-empty).

    Returns:
        The highest :data:`FRAGMENT_LEVELS` member present.
    """
    return max((f.level for f in fragments), key=FRAGMENT_LEVELS.index)


def assemble_changelog(
    text: str, fragments: list[Fragment], version: str, *, date: str = ""
) -> str:
    """Insert a new release heading built from *fragments* into *text*.

    Single-writer by contract: this runs once per release (the promotion
    commit, or a single-track release flow). Entries group by fragment
    type in :data:`FRAGMENT_TYPES` order; within a group, fragments keep
    their filename-sorted order. Idempotence guard: a heading for
    *version* already present in *text* is a hard error, never a second
    insertion or a silent merge.

    Args:
        text: Current ``CHANGELOG.md`` contents.
        fragments: Validated fragments to collate (non-empty).
        version: The release version (``vX.Y.Z``).
        date: Optional ``YYYY-MM-DD`` heading suffix (defaults to today).

    Returns:
        The updated changelog text.

    Raises:
        ValueError: If *version*'s heading already exists in *text*, or
            *fragments* is empty.
    """
    if not fragments:
        msg = "no fragments to assemble"
        raise ValueError(msg)
    if re.search(rf"(?m)^##\s+{re.escape(version)}\b", text):
        msg = f"heading for {version} already exists — refusing a second assembly"
        raise ValueError(msg)
    stamp = date or datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"## {version} — {stamp}", ""]
    for ftype, group in FRAGMENT_TYPES.items():
        group_frags = [f for f in fragments if f.type == ftype]
        if not group_frags:
            continue
        lines.append(f"### {group}")
        lines.extend(f.body for f in group_frags)
        lines.append("")
    entry = "\n".join(lines).rstrip() + "\n"
    anchor = top_release_heading(text)
    if anchor is None:
        # No release heading yet — append after the prose preamble.
        return text.rstrip() + "\n\n" + entry
    anchor_pos = text.index(f"## {anchor}")
    return text[:anchor_pos] + entry + "\n" + text[anchor_pos:]


def check_pending(root: Path) -> list[str]:
    """Validate every pending fragment under *root*.

    The shared gate seam: the ``changelog_version`` pre-commit step (in
    fragment mode) and the ``check`` subcommand both consume this, so
    the gate cannot drift between the two.

    Args:
        root: Repository root directory.

    Returns:
        Every validation error across pending fragments; empty when all
        are valid or none exist.
    """
    errors: list[str] = []
    for path in discover_fragments(root):
        _, frag_errors = validate_fragment(path)
        errors.extend(frag_errors)
    return errors


def _cmd_check(root: Path) -> int:
    """Report on pending fragments; gate on validity.

    Args:
        root: Repository root directory.

    Returns:
        ``0`` when all pending fragments are valid (or none exist);
        ``2`` when any fragment fails validation.
    """
    errors = check_pending(root)
    if errors:
        for err in errors:
            emit(f"changelog.d: INVALID — {err}")
        return 2
    count = len(discover_fragments(root))
    emit(
        f"changelog.d: {count} pending fragment(s), all valid."
        if count
        else "changelog.d: no pending fragments."
    )
    return 0


def _cmd_assemble(root: Path, version: str, date: str, *, delete: bool) -> int:
    """Collate pending fragments into ``CHANGELOG.md`` under *version*.

    Args:
        root: Repository root directory.
        version: Release version for the new heading (``vX.Y.Z``).
        date: Optional heading date override.
        delete: Stage fragment deletions via ``git rm`` (never commits).

    Returns:
        ``0`` on success; ``2`` on validation/assembly failure.
    """
    paths = discover_fragments(root)
    if not paths:
        emit("changelog.d: nothing to assemble.")
        return 2
    fragments: list[Fragment] = []
    for path in paths:
        fragment, errors = validate_fragment(path)
        if errors:
            for err in errors:
                emit(f"changelog.d: INVALID — {err}")
            return 2
        fragments.append(fragment)  # type: ignore[arg-type]
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8") if changelog.is_file() else ""
    try:
        updated = assemble_changelog(text, fragments, version, date=date)
    except ValueError as exc:
        emit(f"changelog.d: {exc}")
        return 2
    changelog.write_text(updated, encoding="utf-8")
    emit(
        f"Assembled {len(fragments)} fragment(s) into CHANGELOG.md "
        f"under {version} (max level: {max_level(fragments)})."
    )
    if delete:
        run_git("add", "CHANGELOG.md", cwd=root)
        for fragment in fragments:
            run_git("rm", "-q", str(fragment.path.relative_to(root)), cwd=root)
        emit("Staged CHANGELOG.md and fragment deletions — commit is yours.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the ``forge-changelog`` CLI.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        ``0`` on success; ``2`` on validation or assembly failure.
    """
    parser = argparse.ArgumentParser(
        prog="forge-changelog",
        description="Changelog fragments: validate pending entries, assemble "
        "them into CHANGELOG.md at release (single writer).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate pending changelog.d/ fragments")
    asm = sub.add_parser(
        "assemble", help="collate fragments into CHANGELOG.md under a version"
    )
    asm.add_argument("--version", required=True, help="release version (vX.Y.Z)")
    asm.add_argument("--date", default="", help="heading date (default: today, UTC)")
    asm.add_argument(
        "--delete",
        action="store_true",
        help="stage fragment deletions with git rm (never commits)",
    )
    args = parser.parse_args(argv)
    root = repo_root()
    if args.command == "check":
        return _cmd_check(root)
    return _cmd_assemble(root, args.version, args.date, delete=args.delete)


if __name__ == "__main__":
    sys.exit(main())
