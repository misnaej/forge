"""verify-forge-repo-structure — verify REPO_STRUCTURE.md matches the actual tree.

``REPO_STRUCTURE.md`` is the map agents read before anything else, so the
check holds it to what it can prove rather than to what it mentions. It
reads every bullet of the form ``- <name>.<ext>: ...`` (a file) or
``- <name>/: ...`` (a folder), resolved against the folder of the heading
or numbered item it sits under, and enforces five rules:

- **Listed but missing** — every listed file or folder exists.
- **On disk but unlisted** — every folder that has its own heading
  (``## Name (`dir/`)`` or ``1. **Name (`dir/`)**``) lists all of its
  direct children that git tracks or would track, except hidden entries,
  ``__init__.py``, ``conftest.py`` and ``IGNORE_PATTERNS`` names. A heading
  carrying ``<!-- summary -->`` opts its folder out. A subfolder named only
  as a bullet is a single entry; its own contents are not checked. The
  main top-level entries in ``MUST_DOCUMENT`` must be mentioned too.
- **Stale names** — every backticked path in a description (anything with
  a known file extension or a trailing ``/``) exists, resolved against the
  folder it is described under, then the repo root. Globs, CLI names,
  config keys and paths under ignored roots (``code_health/``, ``.plan/``)
  are skipped.
- **Opening** — between the title and the first section, the only text
  allowed is ``CANONICAL_OPENING``, word for word, so the map's own claim
  about this check can never drift from what the check does.
- **Sections without a folder path** hold only file/folder bullets (names
  resolved against the repo root) and numbered grouping labels — no prose.

The disk side comes from git (tracked plus untracked-but-not-ignored
files), so ``.gitignore`` is respected and a clean checkout and a
working clone agree.

Usage:

    # Check REPO_STRUCTURE.md against the repo
    verify-forge-repo-structure

    # Also show every folder and bullet the parser read
    verify-forge-repo-structure --verbose

Exit Codes:
    0: REPO_STRUCTURE.md is in sync with the repository.
    1: Drift detected, or REPO_STRUCTURE.md is missing.

Integration:
    Called by ``forge-precommit`` as the ``repo_structure_check`` step;
    its output is written to ``code_health/repo_structure_check.log``.
"""

from __future__ import annotations

import argparse
import logging
import posixpath
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from forge.audit.common import sanitize_log_text
from forge.git_utils import (
    capturing_to_step_log,
    configure_cli_logging,
    path_escapes_repo,
    repo_root,
    run_git,
)


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

# The only text allowed between the map's title and its first section. It
# states exactly what this module verifies and nothing more — the map is
# the one place an agent reads the claim, so the claim is owned here.
CANONICAL_OPENING = (
    "Checked on every commit by `verify-forge-repo-structure`: every listed "
    "file and folder exists; every backticked file path in a description "
    "(a known file type, or ending in `/`) exists; and every folder with its "
    "own heading lists all its files and subfolders that git does not "
    "ignore, except hidden ones, `__init__.py`, `conftest.py` and build or "
    "cache output (`build`, `dist`, `tmp`, `code_health`, `*.egg-info`, "
    "compiled and editor-backup files), unless the heading is marked "
    "`<!-- summary -->`."
)

# Heading suffix opting a folder section out of the full-listing rule.
SUMMARY_MARKER = "<!-- summary -->"

# Names never required in a full listing.
EXEMPT_NAMES = frozenset({"__init__.py", "conftest.py"})

# Patterns to always ignore — as listing entries and as path roots.
IGNORE_PATTERNS = (
    r"^\.git$",
    r"^\.plan$",
    r"^\.cache$",
    r"^\.ruff_cache$",
    r"^\.pytest_cache$",
    r"^\.mypy_cache$",
    r"^__pycache__$",
    r"^.*\.egg-info$",
    r"^build$",
    r"^dist$",
    r"^tmp$",
    r"^code_health$",
    r"^.*\.pyc$",
    r"^.*\.pyo$",
    r"^.*\.swp$",
    r"^.*~$",
)

# Top-level items that MUST be mentioned in REPO_STRUCTURE.md when present.
MUST_DOCUMENT = frozenset(
    {
        # Directories
        "src",
        "tests",
        "agents",
        "skills",
        "claude-hooks",
        "docs",
        "dev",
        ".claude-plugin",
        ".githooks",
        ".github",
        # Files
        "CLAUDE.md",
        "FOUNDATION.md",
        "README.md",
        "REPO_STRUCTURE.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "pyproject.toml",
        "ruff.toml",
    },
)

# A backticked token counts as a path only with one of these extensions
# (or a trailing ``/``): dotted Python names like ``audit.deps`` or
# ``forge.run_context`` must not be mistaken for files.
PATH_EXTENSIONS = frozenset(
    {
        "cfg",
        "csv",
        "dsl",
        "html",
        "ini",
        "js",
        "json",
        "jsonl",
        "lock",
        "log",
        "md",
        "pdf",
        "png",
        "py",
        "sh",
        "svg",
        "toml",
        "txt",
        "yaml",
        "yml",
    },
)

# Characters that mark a backticked token as a glob, placeholder, CLI
# invocation or config key rather than a concrete path.
_NON_PATH_CHARS = frozenset("*?[]{}<>$=:")


@dataclass(frozen=True)
class Folder:
    """A folder declared by a heading or numbered item.

    Attributes:
        path: Repo-relative folder path (no trailing slash).
        strict: Whether its full listing is required.
        line: 1-based line number in the map.
    """

    path: str
    strict: bool
    line: int


@dataclass(frozen=True)
class Bullet:
    """A ``- <name>:`` bullet naming one file or folder.

    Attributes:
        path: Repo-relative path the bullet names (no trailing slash).
        line: 1-based line number in the map.
    """

    path: str
    line: int


@dataclass(frozen=True)
class NamedPath:
    """A backticked path inside a description or paragraph.

    Attributes:
        token: The backticked text as written.
        bases: Folders to resolve against, in order (``""`` is the root).
        line: 1-based line number in the map.
    """

    token: str
    bases: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class RepoMap:
    """Everything the checks need from a parsed ``REPO_STRUCTURE.md``.

    Attributes:
        has_title: Whether a ``# `` title line exists.
        opening: Whitespace-normalised text before the first section.
        folders: Folders declared by headings and numbered items.
        bullets: File and folder bullets.
        names: Backticked paths found in descriptions and paragraphs.
        prose_lines: Lines of prose in sections without a folder path.
    """

    has_title: bool
    opening: str
    folders: tuple[Folder, ...]
    bullets: tuple[Bullet, ...]
    names: tuple[NamedPath, ...]
    prose_lines: tuple[int, ...]


@dataclass(frozen=True)
class Listing:
    """The repository's files as git sees them.

    Attributes:
        files: Repo-relative paths of tracked and untracked-unignored files.
        dirs: Every folder containing at least one of those files.
    """

    files: frozenset[str]
    dirs: frozenset[str]

    @classmethod
    def from_files(cls, files: set[str] | list[str]) -> Listing:
        """Build a listing, deriving the folder set from the file paths.

        Args:
            files: Repo-relative file paths.

        Returns:
            The listing.
        """
        dirs = {parent for path in files for parent in _parent_dirs(path)}
        return cls(files=frozenset(files), dirs=frozenset(dirs))

    def exists(self, path: str) -> bool:
        """Check whether a repo-relative path is a known file or folder.

        Args:
            path: Repo-relative path (no trailing slash).

        Returns:
            True when the path is a listed file or a folder holding one.
        """
        return path in self.files or path in self.dirs

    def children(self, folder: str) -> set[str]:
        """Return a folder's direct children that a full listing must name.

        Args:
            folder: Repo-relative folder path (``""`` for the root).

        Returns:
            Repo-relative paths of the non-exempt direct children.
        """
        prefix = f"{folder}/" if folder else ""
        children: set[str] = set()
        for path in self.files:
            if not path.startswith(prefix):
                continue
            name = path[len(prefix) :].split("/", 1)[0]
            if _is_exempt(name):
                continue
            children.add(f"{prefix}{name}")
        return children


@dataclass(frozen=True)
class Findings:
    """Result of checking a map against the repository.

    Attributes:
        missing: Listed paths absent from the repository.
        unlisted: Paths present in a fully-listed folder but not listed.
        stale_names: Backticked description paths that do not resolve.
        violations: Opening, prose and containment violations.
        strict_folders: Folders whose full listing was checked.
    """

    missing: tuple[str, ...]
    unlisted: tuple[str, ...]
    stale_names: tuple[str, ...]
    violations: tuple[str, ...]
    strict_folders: tuple[str, ...]

    @property
    def in_sync(self) -> bool:
        """Whether every rule passed."""
        return not (
            self.missing or self.unlisted or self.stale_names or self.violations
        )


def should_ignore(name: str) -> bool:
    """Check whether a path segment matches an ignore pattern.

    Args:
        name: The file or directory name to check.

    Returns:
        True if the name matches any ignore pattern.
    """
    return any(re.match(pattern, name) for pattern in IGNORE_PATTERNS)


def _is_exempt(name: str) -> bool:
    """Whether a direct child is exempt from full listings.

    Args:
        name: The entry name to check.

    Returns:
        True if the entry is exempt.
    """
    return name.startswith(".") or name in EXEMPT_NAMES or should_ignore(name)


def _parent_dirs(path: str) -> list[str]:
    """Return every folder enclosing a repo-relative file path.

    Args:
        path: The repo-relative path.

    Returns:
        List of parent folder paths.
    """
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _under_ignored_root(path: str) -> bool:
    """Whether a repo-relative path lies under an ignored root.

    Args:
        path: The repo-relative path.

    Returns:
        True if the path is under an ignored root.
    """
    return bool(path) and should_ignore(path.split("/", 1)[0])


def _normalize(path: str) -> str:
    """Normalise a doc-supplied path to repo-relative form without slashes.

    Args:
        path: The path to normalize.

    Returns:
        The normalized path.
    """
    normalized = posixpath.normpath(path.strip().rstrip("/"))
    return "" if normalized == "." else normalized


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

_TITLE = re.compile(r"^#\s")
_HEADING = re.compile(r"^#{2,6}\s")
_HEADING_PATH = re.compile(r"\(`([^`)]+)`\)")
_NUMBERED = re.compile(r"^\d+\.\s+\*\*([^*]+)\*\*(.*)$")
_BULLET = re.compile(
    r"^(?P<indent>\s*)-\s+"
    r"(?P<name>[A-Za-z0-9_.][A-Za-z0-9_./\-]*?)(?P<slash>/?):(?:\s+(?P<desc>.*))?$",
)
_BACKTICK = re.compile(r"`([^`\s]+)`")


class _MapParser:
    """Line-by-line state machine turning map markdown into a ``RepoMap``."""

    def __init__(self) -> None:
        """Start in the opening, before any section."""
        self.has_title = False
        self.in_opening = True
        self.opening: list[str] = []
        self.section: str | None = None
        self.item: str | None = None
        self.stack: list[tuple[int, str]] = []
        self.after_bullet = False
        self.folders: list[Folder] = []
        self.bullets: list[Bullet] = []
        self.names: list[NamedPath] = []
        self.prose: list[int] = []

    @property
    def folder(self) -> str | None:
        """The folder the current line is described under, if any."""
        return self.item if self.item is not None else self.section

    def feed(self, lineno: int, line: str) -> None:
        """Consume one markdown line.

        Args:
            lineno: 1-based line number.
            line: The line, without its newline.
        """
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            return
        if _TITLE.match(line):
            self.has_title = True
            return
        if _HEADING.match(line):
            self._heading(lineno, line)
            return
        if self.in_opening:
            self.opening.append(stripped)
            return
        numbered = _NUMBERED.match(line)
        if numbered:
            self._numbered(lineno, line, numbered)
            return
        bullet = _BULLET.match(line)
        if bullet:
            self._bullet(lineno, bullet)
            return
        self._text(lineno, line)

    def _declare(self, lineno: int, line: str) -> str | None:
        """Record the folder a heading or numbered item names, if any.

        Args:
            lineno: The line number.
            line: The line content.

        Returns:
            The folder path if declared, None otherwise.
        """
        match = _HEADING_PATH.search(line)
        if not match:
            return None
        path = _normalize(match.group(1))
        self.folders.append(Folder(path, SUMMARY_MARKER not in line, lineno))
        return path

    def _heading(self, lineno: int, line: str) -> None:
        """Start a new section.

        Args:
            lineno: The line number.
            line: The line content.
        """
        self.in_opening = False
        self.section = self._declare(lineno, line)
        self.item = None
        self.stack = []
        self.after_bullet = False

    def _numbered(self, lineno: int, line: str, match: re.Match[str]) -> None:
        """Start a numbered item — a folder or a plain grouping label.

        Args:
            lineno: The line number.
            line: The line content.
            match: The regex match object for the numbered pattern.
        """
        self.item = self._declare(lineno, line)
        self.stack = []
        self.after_bullet = False
        self._scan(lineno, match.group(2), self.folder)

    def _bullet(self, lineno: int, match: re.Match[str]) -> None:
        """Record a file or folder bullet, tracking indentation for nesting.

        Args:
            lineno: The line number.
            match: The regex match object for the bullet pattern.
        """
        indent = len(match.group("indent"))
        while self.stack and self.stack[-1][0] >= indent:
            self.stack.pop()
        base = self.stack[-1][1] if self.stack else (self.folder or "")
        path = _normalize(posixpath.join(base, match.group("name")))
        self.bullets.append(Bullet(path, lineno))
        if match.group("slash"):
            self.stack.append((indent, path))
            base = path  # a folder's description names its own contents first
        self.after_bullet = True
        self._scan(lineno, match.group("desc") or "", base)

    def _text(self, lineno: int, line: str) -> None:
        """Handle a continuation line or a paragraph.

        Args:
            lineno: The line number.
            line: The line content.
        """
        continuation = self.after_bullet and line[:1].isspace()
        if not continuation:
            self.after_bullet = False
            if self.folder is None:
                self.prose.append(lineno)
        base = self.stack[-1][1] if continuation and self.stack else self.folder
        self._scan(lineno, line, base)

    def _scan(self, lineno: int, text: str, base: str | None) -> None:
        """Collect backticked paths from description text.

        Args:
            lineno: The line number.
            text: The text to scan for paths.
            base: The base path for relative resolution.
        """
        candidates = (base, self.folder, "")
        bases = tuple(dict.fromkeys(b for b in candidates if b is not None))
        self.names.extend(
            NamedPath(token, bases, lineno)
            for token in _BACKTICK.findall(text)
            if _looks_like_path(token)
        )

    def result(self) -> RepoMap:
        """Return the parsed map.

        Returns:
            The immutable parse result.
        """
        return RepoMap(
            has_title=self.has_title,
            opening=" ".join(" ".join(self.opening).split()),
            folders=tuple(self.folders),
            bullets=tuple(self.bullets),
            names=tuple(self.names),
            prose_lines=tuple(self.prose),
        )


def _looks_like_path(token: str) -> bool:
    """Whether a backticked token names a concrete repository path.

    Args:
        token: The token to check.

    Returns:
        True if the token appears to be a repository path.
    """
    if token.startswith(("-", "http")) or _NON_PATH_CHARS & set(token):
        return False
    if token.endswith("/"):
        return bool(token.strip("/"))
    stem, dot, ext = posixpath.basename(token).rpartition(".")
    return bool(stem and dot) and ext in PATH_EXTENSIONS


def parse_map(content: str) -> RepoMap:
    """Parse ``REPO_STRUCTURE.md`` text.

    Sibling folder bullets never nest: a bullet only resolves under a
    folder bullet that is indented less than it.

    Args:
        content: The markdown content.

    Returns:
        The parsed map.
    """
    parser = _MapParser()
    for lineno, line in enumerate(content.splitlines(), start=1):
        parser.feed(lineno, line)
    return parser.result()


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------


def git_listing(root: Path) -> Listing:
    """List the repository's tracked and untracked-unignored files.

    Args:
        root: Repository root.

    Returns:
        The listing.
    """
    # NUL-separated output: plain `git ls-files` C-quotes any name holding
    # a control or (by default) non-ASCII character, and a quoted name no
    # longer starts with its folder — such a file would escape the
    # "on disk but unlisted" rule entirely.
    out = run_git(
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        cwd=root,
        check=False,
        log_errors=False,
    )
    files = sorted({name for name in out.split("\0") if name})
    return Listing.from_files(files)


def _checkable(root: Path, path: str) -> bool:
    """Whether a doc-supplied path is inside the repo and not ignored.

    Args:
        root: The repository root.
        path: The path to check.

    Returns:
        True if the path is checkable.
    """
    if _under_ignored_root(path):
        return False
    return not path_escapes_repo(root, path or ".")


def find_missing(repo_map: RepoMap, listing: Listing, root: Path) -> list[str]:
    """Find listed files and declared folders absent from the repository.

    Args:
        repo_map: Parsed map.
        listing: Repository listing.
        root: Repository root (for the containment guard).

    Returns:
        ``"<path>  (line N)"`` entries, sorted.
    """
    entries = [(b.path, b.line) for b in repo_map.bullets]
    entries += [(f.path, f.line) for f in repo_map.folders if f.path]
    return sorted(
        f"{path}  (line {line})"
        for path, line in entries
        if _checkable(root, path) and not listing.exists(path)
    )


def strict_folders(repo_map: RepoMap, listing: Listing, root: Path) -> list[str]:
    """Return the folders whose full listing is enforced.

    Args:
        repo_map: Parsed map.
        listing: Repository listing.
        root: Repository root (for the containment guard).

    Returns:
        Sorted, deduplicated folder paths.
    """
    return sorted(
        {
            f.path
            for f in repo_map.folders
            if f.strict
            and _checkable(root, f.path)
            and (not f.path or listing.exists(f.path))
        },
    )


def find_unlisted(repo_map: RepoMap, listing: Listing, folders: list[str]) -> list[str]:
    """Find entries of fully-listed folders that the map does not name.

    A folder declared by its own heading or numbered item counts as listed
    in its parent. Missing ``MUST_DOCUMENT`` top-level entries are
    reported too.

    Args:
        repo_map: Parsed map.
        listing: Repository listing.
        folders: Folders whose full listing is enforced.

    Returns:
        Unlisted repo-relative paths, sorted.
    """
    listed = {b.path for b in repo_map.bullets} | {f.path for f in repo_map.folders}
    unlisted = {child for folder in folders for child in listing.children(folder)}
    unlisted -= listed
    top_level = {path.split("/", 1)[0] for path in listing.files}
    unlisted |= {
        item for item in top_level & MUST_DOCUMENT if not path_is_covered(item, listed)
    }
    return sorted(unlisted)


def find_stale_names(repo_map: RepoMap, listing: Listing) -> list[str]:
    """Find backticked description paths that resolve nowhere.

    Args:
        repo_map: Parsed map.
        listing: Repository listing.

    Returns:
        ``"`<token>`  (line N)"`` entries, sorted.
    """
    stale: set[str] = set()
    for name in repo_map.names:
        candidates = [
            _normalize(posixpath.join(base, name.token)) for base in name.bases
        ]
        if any(_under_ignored_root(c) or c.startswith("..") for c in candidates):
            continue
        if not any(listing.exists(c) for c in candidates):
            stale.add(f"`{name.token}`  (line {name.line})")
    return sorted(stale)


def find_violations(repo_map: RepoMap, root: Path) -> list[str]:
    """Find opening, prose and containment violations.

    Args:
        repo_map: Parsed map.
        root: Repository root (for the containment guard).

    Returns:
        Human-readable violations.
    """
    violations: list[str] = []
    if not repo_map.has_title:
        violations.append("no `# ` title line")
    if repo_map.opening != CANONICAL_OPENING:
        violations.append("the opening is not the canonical sentence (see HOW TO FIX)")
    violations.extend(
        f"line {line}: prose in a section without a folder path "
        "(only file/folder bullets allowed)"
        for line in repo_map.prose_lines
    )
    paths = [(f.path, f.line) for f in repo_map.folders]
    paths += [(b.path, b.line) for b in repo_map.bullets]
    violations.extend(
        f"line {line}: path escapes the repository: {path}"
        for path, line in paths
        if path_escapes_repo(root, path or ".")
    )
    return violations


def path_is_covered(path: str, documented_paths: set[str]) -> bool:
    """Check whether a path is covered by the documented paths.

    A path is covered if it appears directly in *documented_paths* or if
    any documented path is a child of it (e.g. ``src`` is covered when
    ``src/forge`` is documented).

    Args:
        path: The path to check.
        documented_paths: Set of paths documented in REPO_STRUCTURE.md.

    Returns:
        True if the path is covered by documentation.
    """
    if path in documented_paths:
        return True
    return any(doc.startswith(path + "/") for doc in documented_paths)


def check_map(repo_map: RepoMap, listing: Listing, root: Path) -> Findings:
    """Run every rule against a parsed map.

    Args:
        repo_map: Parsed map.
        listing: Repository listing.
        root: Repository root (for the containment guard).

    Returns:
        The findings.
    """
    folders = strict_folders(repo_map, listing, root)
    return Findings(
        missing=tuple(find_missing(repo_map, listing, root)),
        unlisted=tuple(find_unlisted(repo_map, listing, folders)),
        stale_names=tuple(find_stale_names(repo_map, listing)),
        violations=tuple(find_violations(repo_map, root)),
        strict_folders=tuple(folders),
    )


def verify_structure(root: Path, *, verbose: bool = False) -> Findings:
    """Verify REPO_STRUCTURE.md against the repository.

    Args:
        root: Repository root directory.
        verbose: Whether to log every folder and bullet the parser read.

    Returns:
        The findings.

    Raises:
        FileNotFoundError: If ``REPO_STRUCTURE.md`` does not exist.
    """
    repo_structure_path = root / "REPO_STRUCTURE.md"
    if not repo_structure_path.exists():
        msg = "REPO_STRUCTURE.md not found"
        raise FileNotFoundError(msg)

    repo_map = parse_map(repo_structure_path.read_text())
    if verbose:
        _log_parsed(repo_map)
    return check_map(repo_map, git_listing(root), root)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _log_parsed(repo_map: RepoMap) -> None:
    """Log every folder and bullet the parser read.

    Args:
        repo_map: The parsed repository map.
    """
    logger.info("Folders declared by headings:")
    for folder in repo_map.folders:
        mode = "full listing" if folder.strict else "summary"
        logger.info("  %s/  (%s, line %d)", folder.path, mode, folder.line)
    logger.info("Bullets:")
    for bullet in repo_map.bullets:
        logger.info("  %s  (line %d)", bullet.path, bullet.line)
    logger.info("")


def _log_group(title: str, marker: str, items: tuple[str, ...]) -> None:
    """Log one finding group when it is non-empty.

    Args:
        title: The group title.
        marker: The marker to prefix each item.
        items: The items to log.
    """
    if not items:
        return
    logger.warning("%s:", title)
    logger.warning("-" * 50)
    # Items carry names from the map and from git (filenames may hold
    # newlines or escape codes); this log is evidence agents read, so a
    # name must never be able to forge a line of it.
    for item in items:
        logger.warning("  %s %s", marker, sanitize_log_text(item))
    logger.warning("")


def _log_summary(findings: Findings) -> None:
    """Log every count, on a pass as well as on drift.

    Args:
        findings: The findings summary to log.
    """
    logger.info("  - Listed but missing: %d", len(findings.missing))
    logger.info("  - On disk but unlisted: %d", len(findings.unlisted))
    logger.info("  - Stale names in descriptions: %d", len(findings.stale_names))
    logger.info("  - Opening/prose violations: %d", len(findings.violations))
    logger.info(
        "  - Folders checked in full: %d (%s)",
        len(findings.strict_folders),
        ", ".join(f"{f}/" for f in findings.strict_folders) or "none",
    )


def _log_fix_instructions(findings: Findings) -> None:
    """Log instructions for resolving the detected drift.

    Args:
        findings: The findings to provide fix instructions for.
    """
    logger.info("HOW TO FIX (edit REPO_STRUCTURE.md):")
    logger.info("-" * 50)
    if findings.missing:
        logger.info("Listed but missing: remove or correct those bullets.")
    if findings.unlisted:
        logger.info(
            "On disk but unlisted: add a `- <name>: <one-line description>` "
            "bullet under the folder's heading, or mark the heading %s if "
            "the section describes the folder in prose.",
            SUMMARY_MARKER,
        )
    if findings.stale_names:
        logger.info("Stale names: correct or remove the backticked paths.")
    if findings.violations:
        logger.info(
            "Opening: the only text between the title and the first section "
            "must be exactly this sentence:",
        )
        logger.info("")
        logger.info("%s", CANONICAL_OPENING)
        logger.info("")
        logger.info(
            "Sections without a folder path may hold only file/folder bullets "
            "and numbered grouping labels.",
        )


def main() -> int:
    """Verify REPO_STRUCTURE.md is in sync with the repository tree.

    Returns:
        Exit code: ``0`` when in sync, ``1`` when drift is detected or
        ``REPO_STRUCTURE.md`` is missing.
    """
    parser = argparse.ArgumentParser(
        prog="verify-forge-repo-structure",
        description="Verify REPO_STRUCTURE.md is in sync with actual structure.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show every folder and bullet the parser read.",
    )
    args = parser.parse_args()

    root = repo_root()

    with capturing_to_step_log(root, "repo_structure_check"):
        logger.info("=" * 70)
        logger.info("REPO_STRUCTURE.md VERIFICATION")
        logger.info("=" * 70)
        logger.info("")

        try:
            findings = verify_structure(root, verbose=args.verbose)
        except FileNotFoundError:
            logger.exception("REPO_STRUCTURE.md not found")
            return 1

        _log_group("LISTED BUT MISSING", "-", findings.missing)
        _log_group("ON DISK BUT UNLISTED", "+", findings.unlisted)
        _log_group("STALE NAMES IN DESCRIPTIONS", "-", findings.stale_names)
        _log_group("OPENING/PROSE VIOLATIONS", "!", findings.violations)

        logger.info("=" * 70)
        if findings.in_sync:
            logger.info("RESULT: REPO_STRUCTURE.md is in sync")
        else:
            logger.warning("RESULT: DRIFT DETECTED")
        _log_summary(findings)
        logger.info("=" * 70)
        if findings.in_sync:
            return 0
        logger.info("")
        _log_fix_instructions(findings)
        return 1


if __name__ == "__main__":
    sys.exit(main())
