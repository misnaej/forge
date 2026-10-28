"""forge-intake — one committed file per sighting of a problem not yet filed.

FOUNDATION §1 records a problem that does not yet justify an issue where
it was found, and files it once it recurs; §12 routes lessons through an
intake before they reach the rules. This module is that intake. Each
sighting is one markdown file, so recurrence is counted from files rather
than remembered, and two branches never edit the same file.

Two kinds, by directory under ``[tool.forge.intake].dir`` (default
``docs/intake``):

- ``lessons/`` — a trap in this repo's own process. Recurring lessons are
  promoted into the repo's rules or filed in its own tracker.
- ``findings/`` — a forge-shipped process misbehaving. Written anonymised
  so it can be filed upstream unchanged (in forge itself, "upstream" is
  forge's own tracker).

An entry is ``# <title>``, then ``- key: value`` fields (``surface``,
``release``, ``seen``, ``branch``; optional ``sibling`` and, for migrated
lessons, ``occurrences``), then free sections. A finding also needs a
``## Evidence`` section citing a ``file:line``. Files named ``README.md``
or starting with ``_`` are not entries. The format reference is the
``README.md`` that ``init`` writes.

Subcommands:

- ``init`` — create the directories, README and findings template, and
  move a legacy ``docs/lessons.md`` into one file per lesson.
- ``new lesson|finding <slug> --title T [--surface S]`` — write a stamped
  entry (forge release, UTC date, branch).
- ``list`` — every entry.
- ``group [--min N]`` — entries that look like the same problem, with the
  occurrence header a filed issue carries.
- ``check`` — validate entries; blocking pre-commit step ``intake_check``.
- ``retire <files> --issue <url>`` — delete filed entries and record them
  in ``_LEDGER.md``.

A standalone CLI rather than a pre-commit-only step: ``new``, ``group``
and ``retire`` are used by people and skills outside any hook
(``docs/step-invocation.md`` criterion 1).

Exit codes:
    0  success (``check``: no problems)
    1  ``check`` found problems, or a subcommand refused its input
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from forge.changelog_fragments import SLUG_PATTERN
from forge.config import read_tool_forge_section
from forge.git_utils import (
    configure_cli_logging,
    emit,
    repo_root,
    resolve_current_branch,
)
from forge.version_surfaces import pip_version


configure_cli_logging()
logger = logging.getLogger(__name__)

DEFAULT_DIR = "docs/intake"
KIND_DIRS = {"lesson": "lessons", "finding": "findings"}
README_NAME = "README.md"
TEMPLATE_NAME = "_TEMPLATE.md"
LEDGER_NAME = "_LEDGER.md"
LEGACY_LESSONS_FILE = "docs/lessons.md"
# A lesson moved from the legacy file carries this branch and its old
# count; the count stands in for sightings the intake never saw.
MIGRATED_BRANCH = "migrated"
PROMOTION_AT = 2
GROUP_THRESHOLD = 0.5

_SLUG_RE = re.compile(rf"^{SLUG_PATTERN}$")
_FIELD_RE = re.compile(r"^- (?P<key>[a-z][a-z -]*?):\s*(?P<value>.*?)\s*$")
_FILE_LINE_RE = re.compile(r"[\w./-]+\.\w+:\d+")
_ISSUE_URL_RE = re.compile(r"^https://\S+$")
_WORD_RE = re.compile(r"[a-z0-9]+")
# Words shorter than this carry no meaning for grouping; a plural "s" is
# stripped only from words longer than the stem minimum.
_MIN_WORD_LEN = 3
_STEM_MIN_LEN = 4
_STOPWORDS = frozenset(
    {"the", "and", "for", "not", "but", "its", "with", "from", "when", "into"}
)

# Secret-shaped strings: refused in every entry, since all are committed.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,})"),
    ),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
# Machine-specific paths: refused in findings, which leave the repo.
_PRIVATE_PATH_RE = re.compile(r"(?:/home|/Users)/[^/\s]+/")

_README = """\
# Intake

One file per sighting of a problem not yet filed (FOUNDATION §1, §12).
Write entries with `forge-intake new`; never edit one to bump a count —
a second sighting is a second file.

- `lessons/` — a trap in this repo's own process.
- `findings/` — a forge-shipped process misbehaving. Anonymised: no
  private names, hosts or paths; `forge-intake check` refuses them.

Entry: `# <title>`, then `- key: value` fields (`surface`, `release`,
`seen`, `branch`; `sibling: <slug>` links another entry by hand), then
sections. A finding needs `## Evidence` citing a `file:line`.

`forge-intake group` shows recurrences; `/report-to-forge` files a
finding group and `forge-intake retire` records it in `_LEDGER.md`.
"""

_FINDING_TEMPLATE = """\
# <one-line symptom>

- surface: <forge CLI, agent, hook, skill or step>
- release: forge <version>
- seen: <YYYY-MM-DD>
- branch: <branch>

## Evidence

<file:line in the installed package, and the exact output quoted>

## What it costs

## What would fix it
"""


_LESSON_TEMPLATE = """\
# <one-line symptom>

- seen: <YYYY-MM-DD>
- branch: <branch>

## What happened

## What it costs

## Fix, if known
"""


@dataclass
class Entry:
    """One intake file.

    Attributes:
        path: The file.
        kind: ``"lesson"`` or ``"finding"``.
        title: The ``# `` heading, without the marker.
        fields: The ``- key: value`` lines after the title.
        lines: Every line of the file, for line-numbered checks.
    """

    path: Path
    kind: str
    title: str
    fields: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        """The file name without ``.md``."""
        return self.path.stem

    @property
    def branch(self) -> str:
        """The branch the sighting was recorded on, ``unknown`` if unset."""
        return self.fields.get("branch") or "unknown"

    @property
    def occurrences(self) -> int:
        """Sightings this file stands for: its migrated count, else one."""
        raw = self.fields.get("occurrences", "")
        return int(raw) if raw.isdigit() and int(raw) > 0 else 1


def _inside(root: Path, raw: str) -> Path | None:
    """Return *raw* resolved under *root*, or ``None`` if it escapes.

    Args:
        root: The directory the path must stay inside.
        raw: A relative path from configuration or the command line.

    Returns:
        The joined path, or ``None`` when *raw* is absolute or resolves
        outside *root* (a cloned repo must not aim a write elsewhere).
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        return None
    return root / candidate


def configured_dir(root: Path) -> Path:
    """Return the intake directory: ``[tool.forge.intake].dir`` or the default.

    Args:
        root: Repository whose ``pyproject.toml`` is read.

    Returns:
        The configured directory when it is a relative path inside *root*,
        else :data:`DEFAULT_DIR` (an invalid value is logged).
    """
    raw = read_tool_forge_section(root, "intake").get("dir")
    if not isinstance(raw, str) or not raw.strip():
        return root / DEFAULT_DIR
    path = _inside(root, raw)
    if path is None:
        logger.warning(
            "forge-intake: [tool.forge.intake].dir %r must be a relative path "
            "inside the repo; using %s",
            raw,
            DEFAULT_DIR,
        )
        return root / DEFAULT_DIR
    return path


def configured_redact(root: Path) -> tuple[str, ...]:
    """Return ``[tool.forge.intake].redact``: tokens findings must not contain.

    Args:
        root: Repository whose ``pyproject.toml`` is read.

    Returns:
        The non-empty string tokens; the built-in secret patterns apply
        whatever this holds.
    """
    raw = read_tool_forge_section(root, "intake").get("redact", [])
    if not isinstance(raw, list):
        logger.warning("forge-intake: [tool.forge.intake].redact must be a list")
        return ()
    return tuple(t for t in raw if isinstance(t, str) and t.strip())


def parse_entry(path: Path, kind: str) -> Entry:
    """Read one intake file.

    Args:
        path: The file.
        kind: ``"lesson"`` or ``"finding"``, from its directory.

    Returns:
        The entry; an empty title when the first line is not ``# ``.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else ""
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        match = _FIELD_RE.match(line)
        if match is None:
            break
        fields[match["key"]] = match["value"]
    return Entry(path=path, kind=kind, title=title, fields=fields, lines=lines)


def discover(intake_dir: Path) -> list[Entry]:
    """Return every entry under *intake_dir*, lessons first, by name.

    Args:
        intake_dir: The intake directory.

    Returns:
        Parsed entries; README, ``_``-prefixed files and non-``.md`` files
        are skipped, and a missing directory yields none.
    """
    entries: list[Entry] = []
    for kind, sub in KIND_DIRS.items():
        folder = intake_dir / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            if path.name == README_NAME or path.name.startswith("_"):
                continue
            entries.append(parse_entry(path, kind))
    return entries


def _stem(word: str) -> str:
    """Return *word* without a plural ending, so "bullets" matches "bullet".

    Args:
        word: Lowercase word to stem.

    Returns:
        The word with a plural ending removed, or unchanged.
    """
    if word.endswith(("shes", "ches", "xes", "sses")):
        return word[:-2]
    if len(word) > _STEM_MIN_LEN and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(entry: Entry) -> set[str]:
    """Return the comparison words of *entry*'s slug and title.

    Args:
        entry: Entry to extract words from.

    Returns:
        Stemmed words, minus stopwords and short words.
    """
    text = f"{entry.slug} {entry.title}".lower().replace("_", " ")
    return {
        _stem(w)
        for w in _WORD_RE.findall(text)
        if len(w) >= _MIN_WORD_LEN and w not in _STOPWORDS
    }


def _similar(a: Entry, b: Entry) -> bool:
    """Return whether *a* and *b* look like one problem (word overlap).

    Args:
        a: First entry.
        b: Second entry.

    Returns:
        True when the word-set overlap ratio reaches ``GROUP_THRESHOLD``.
    """
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= GROUP_THRESHOLD


def _linked(a: Entry, b: Entry) -> bool:
    """Return whether either entry names the other in a ``sibling`` field.

    Args:
        a: First entry.
        b: Second entry.

    Returns:
        True when either entry's ``sibling`` field lists the other's slug.
    """

    def names(e: Entry) -> set[str]:
        return {s.strip() for s in e.fields.get("sibling", "").split(",") if s.strip()}

    return b.slug in names(a) or a.slug in names(b)


def group_entries(entries: list[Entry]) -> list[list[Entry]]:
    """Cluster entries of one kind that look like the same problem.

    Word overlap catches most repeats; a ``sibling`` field fixes the ones
    it misses. Grouping is advisory — a wrong group costs a line of
    output, never a failure.

    Args:
        entries: Entries to cluster.

    Returns:
        Groups (each in input order), largest occurrence count first.
    """
    parent = list(range(len(entries)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(entries):
        for j in range(i + 1, len(entries)):
            b = entries[j]
            if a.kind == b.kind and (_linked(a, b) or _similar(a, b)):
                parent[find(j)] = find(i)
    clusters: dict[int, list[Entry]] = {}
    for i, entry in enumerate(entries):
        clusters.setdefault(find(i), []).append(entry)
    return sorted(clusters.values(), key=lambda g: -occurrence_count(g))


def occurrence_count(group: list[Entry]) -> int:
    """Return the sightings *group* stands for.

    Two files from one branch are one sighting; a migrated lesson adds
    the count it carried over.

    Args:
        group: Entries judged to be one problem.

    Returns:
        Distinct recording branches plus migrated counts.
    """
    branches = {e.branch for e in group if e.branch != MIGRATED_BRANCH}
    return len(branches) + sum(
        e.occurrences for e in group if e.branch == MIGRATED_BRANCH
    )


def occurrence_header(group: list[Entry]) -> str:
    """Return the header a filed issue carries for *group*.

    Counts only: branch names can be private, so they are never listed.

    Args:
        group: Entries judged to be one problem.

    Returns:
        ``Occurrences: N, from M different branches``, with the share
        recorded before the intake named when there is one.
    """
    branches = len({e.branch for e in group if e.branch != MIGRATED_BRANCH})
    carried = sum(e.occurrences for e in group if e.branch == MIGRATED_BRANCH)
    parts = []
    if branches:
        parts.append(f"from {branches} different branch{'es' if branches > 1 else ''}")
    if carried:
        parts.append(f"{carried} recorded before the intake")
    return f"Occurrences: {occurrence_count(group)}, " + ", ".join(parts)


def _scan(entry: Entry, redact: tuple[str, ...]) -> list[str]:
    """Return redaction problems in *entry*, as ``<line>: <what>``.

    Args:
        entry: Entry whose lines are scanned.
        redact: Configured tokens that must not appear in findings.

    Returns:
        One ``<line>: <what>`` string per problem; empty when clean.
    """
    problems: list[str] = []
    lowered = tuple(t.lower() for t in redact)
    for number, line in enumerate(entry.lines, start=1):
        problems.extend(
            f"{number}: {label}-shaped string"
            for label, pattern in _SECRET_PATTERNS
            if pattern.search(line)
        )
        if entry.kind != "finding":
            continue
        if _PRIVATE_PATH_RE.search(line):
            problems.append(f"{number}: machine-specific path")
        low = line.lower()
        problems.extend(
            f"{number}: redacted token {token!r}" for token in lowered if token in low
        )
    return problems


def _shape_problems(entry: Entry) -> list[str]:
    """Return missing-field and missing-evidence problems in *entry*.

    Args:
        entry: Entry to validate.

    Returns:
        Human-readable problem strings; empty when the shape is valid.
    """
    problems: list[str] = []
    if not _SLUG_RE.match(entry.slug):
        problems.append(f"file name must match {SLUG_PATTERN}.md")
    if not entry.title:
        problems.append("first line must be '# <title>'")
    required = ("seen",) if entry.kind == "lesson" else ("surface", "release", "seen")
    problems.extend(
        f"missing '- {key}:' field" for key in required if not entry.fields.get(key)
    )
    if entry.kind == "finding" and not _has_evidence(entry):
        problems.append("'## Evidence' must cite a file:line")
    return problems


def _has_evidence(entry: Entry) -> bool:
    """Return whether *entry*'s ``## Evidence`` section cites a file:line.

    Args:
        entry: Entry to inspect.

    Returns:
        True when a line in the Evidence section matches a file:line citation.
    """
    in_section = False
    for line in entry.lines:
        if line.startswith("## "):
            in_section = line.strip().lower() == "## evidence"
        elif in_section and _FILE_LINE_RE.search(line):
            return True
    return False


def check(intake_dir: Path, redact: tuple[str, ...]) -> list[str]:
    """Validate every entry under *intake_dir*.

    Every entry is committed, so secret-shaped strings are refused in all
    of them. Findings leave the repo, so they also need their filing
    fields and evidence, and must carry no machine path or *redact* token.

    Args:
        intake_dir: The intake directory.
        redact: Tokens findings must not contain (case-insensitive).

    Returns:
        ``<path>:<line>: <problem>`` strings (``<path>: <problem>`` for
        file-level ones), empty when every entry passes.
    """
    problems: list[str] = []
    for entry in discover(intake_dir):
        problems.extend(f"{entry.path}: {p}" for p in _shape_problems(entry))
        problems.extend(f"{entry.path}:{p}" for p in _scan(entry, redact))
    return problems


def _unique_path(folder: Path, slug: str) -> Path:
    """Return ``<slug>.md`` in *folder*, suffixed ``-2``, ``-3`` … if taken.

    Args:
        folder: Directory the entry file goes in.
        slug: Base file name without extension.

    Returns:
        A path that does not exist yet.
    """
    path = folder / f"{slug}.md"
    n = 2
    while path.exists():
        path = folder / f"{slug}-{n}.md"
        n += 1
    return path


def new_entry(intake_dir: Path, kind: str, slug: str, title: str, surface: str) -> Path:
    """Write a new stamped entry and return its path.

    Args:
        intake_dir: The intake directory.
        kind: ``"lesson"`` or ``"finding"``.
        slug: File name stem; must match :data:`SLUG_PATTERN`.
        title: One-line symptom.
        surface: The forge surface involved (required for findings).

    Returns:
        The written file's path.

    Raises:
        ValueError: If *slug* is not a plain slug, or a finding has no
            surface.
    """
    if not _SLUG_RE.match(slug):
        msg = f"slug {slug!r} must match {SLUG_PATTERN}"
        raise ValueError(msg)
    if kind == "finding" and not surface:
        msg = "a finding needs --surface"
        raise ValueError(msg)
    folder = intake_dir / KIND_DIRS[kind]
    folder.mkdir(parents=True, exist_ok=True)
    branch = resolve_current_branch(repo_root())
    fields = [
        f"- surface: {surface}" if surface else None,
        f"- release: forge {pip_version() or 'unknown'}",
        f"- seen: {_dt.datetime.now(tz=_dt.UTC).date().isoformat()}",
        f"- branch: {branch[0] if branch else 'unknown'}",
    ]
    sections = (
        ["## Evidence", "", "## What it costs", "", "## What would fix it"]
        if kind == "finding"
        else ["## What happened", "", "## What it costs", "", "## Fix, if known"]
    )
    body = [f"# {title}", "", *[f for f in fields if f], "", *sections]
    path = _unique_path(folder, slug)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _write_once(folder: Path, slug: str, text: str) -> None:
    """Write *text* as ``<slug>.md`` unless an earlier run already did.

    A file holding exactly *text* is that earlier write and is kept; a
    file holding anything else is a different entry, so the next free
    ``-N`` suffix is used — two titles sharing a slug both survive.

    Args:
        folder: Directory to write into.
        slug: Base file name without extension.
        text: Full file content.
    """
    path, n = folder / f"{slug}.md", 2
    while path.exists():
        if path.read_text(encoding="utf-8") == text:
            return
        path, n = folder / f"{slug}-{n}.md", n + 1
    path.write_text(text, encoding="utf-8")


def _slugify(title: str) -> str:
    """Return a deterministic slug for a migrated lesson title.

    Args:
        title: Lesson title.

    Returns:
        Lowercase hyphen-joined words, at most 60 characters.
    """
    return "-".join(_WORD_RE.findall(title.lower()))[:60].strip("-")


def _legacy_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split a legacy lessons file into ``(title, body lines)`` per ``## ``.

    Args:
        text: Content of the legacy lessons file.

    Returns:
        One ``(title, body lines)`` pair per non-empty ``## `` heading.
    """
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return [(t, body) for t, body in sections if t]


def _migrated_text(title: str, body: list[str]) -> str:
    """Render one legacy lesson section as an intake entry.

    Args:
        title: Section title.
        body: Section body lines.

    Returns:
        Markdown text of the migrated intake entry.
    """
    fields = {"occurrences": "1", "first seen": "unknown"}
    prose: list[str] = []
    for line in body:
        match = _FIELD_RE.match(line)
        if match is not None and not prose:
            fields[match["key"]] = match["value"]
        elif line.strip() or prose:
            prose.append(line)
    head = [
        f"# {title}",
        "",
        f"- seen: {fields['first seen']}",
        f"- branch: {MIGRATED_BRANCH}",
        f"- occurrences: {fields['occurrences']}",
    ]
    if fields.get("where it applies"):
        head.append(f"- where it applies: {fields['where it applies']}")
    return "\n".join([*head, "", *prose]).rstrip() + "\n"


def migrate_legacy_lessons(root: Path, intake_dir: Path) -> str | None:
    """Move ``docs/lessons.md`` into one lesson file per entry.

    Idempotent and crash-safe: each slug is derived from its title, an
    existing file is kept, and the legacy file is deleted only after
    every entry exists. A file with no parseable entry is left alone.

    Args:
        root: Repository root.
        intake_dir: The intake directory.

    Returns:
        A one-line report of what happened, or ``None`` when there was no
        legacy file.
    """
    legacy = root / LEGACY_LESSONS_FILE
    if legacy.is_symlink() or not legacy.is_file():
        return None
    sections = _legacy_sections(legacy.read_text(encoding="utf-8"))
    if not sections:
        return f"{LEGACY_LESSONS_FILE} has no '## ' entries — left in place"
    folder = intake_dir / KIND_DIRS["lesson"]
    folder.mkdir(parents=True, exist_ok=True)
    for index, (title, body) in enumerate(sections, start=1):
        _write_once(
            folder, _slugify(title) or f"lesson-{index}", _migrated_text(title, body)
        )
    legacy.unlink()
    return f"moved {len(sections)} lesson(s) from {LEGACY_LESSONS_FILE} into {folder}"


def init(root: Path, intake_dir: Path) -> list[str]:
    """Create the intake layout and migrate the legacy lessons file.

    Args:
        root: Repository root.
        intake_dir: The intake directory.

    Returns:
        One line per change made; empty when everything already existed.
    """
    done: list[str] = []
    for sub in KIND_DIRS.values():
        folder = intake_dir / sub
        if not folder.is_dir():
            folder.mkdir(parents=True)
            done.append(f"created {folder}")
    # A template in each directory also keeps it committable while empty.
    for path, text in (
        (intake_dir / README_NAME, _README),
        (intake_dir / KIND_DIRS["lesson"] / TEMPLATE_NAME, _LESSON_TEMPLATE),
        (intake_dir / KIND_DIRS["finding"] / TEMPLATE_NAME, _FINDING_TEMPLATE),
    ):
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            done.append(f"wrote {path}")
    if (moved := migrate_legacy_lessons(root, intake_dir)) is not None:
        done.append(moved)
    return done


def retire(intake_dir: Path, paths: list[Path], issue_url: str) -> str:
    """Delete filed entries and record them in ``_LEDGER.md``.

    Args:
        intake_dir: The intake directory.
        paths: The entries the issue was filed from.
        issue_url: The filed issue.

    Returns:
        The ledger row written.

    Raises:
        ValueError: If the URL is not a single-line https URL, or a path
            is not an entry file inside *intake_dir*.
    """
    if not _ISSUE_URL_RE.match(issue_url):
        msg = f"--issue must be an https URL, got {issue_url!r}"
        raise ValueError(msg)
    base = intake_dir.resolve()
    entries: list[Entry] = []
    for path in paths:
        resolved = path.resolve()
        kind = next(
            (k for k, d in KIND_DIRS.items() if resolved.parent == base / d), None
        )
        if path.is_symlink() or not path.is_file() or kind is None:
            msg = f"{path} is not an entry under {intake_dir}"
            raise ValueError(msg)
        entries.append(parse_entry(path, kind))
    if not entries:
        msg = "retire needs at least one entry"
        raise ValueError(msg)
    title = entries[0].title.replace("|", r"\|")
    today = _dt.datetime.now(tz=_dt.UTC).date().isoformat()
    files = len(entries)
    row = (
        f"| {today} | {title} | {issue_url} | {occurrence_header(entries)} | {files} |"
    )
    ledger = intake_dir / LEDGER_NAME
    if not ledger.exists():
        ledger.write_text(
            "# Intake ledger\n\nEntries filed as issues, then retired.\n\n"
            "| Retired | Title | Issue | Occurrences | Files |\n"
            "|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")
    for entry in entries:
        entry.path.unlink()
    return row


def _cmd_list(intake_dir: Path) -> int:
    """Print every entry.

    Args:
        intake_dir: Intake directory to list.

    Returns:
        Exit code, always 0.
    """
    for entry in discover(intake_dir):
        emit(f"{entry.kind:8} {entry.path} | {entry.title} | branch {entry.branch}")
    return 0


def _cmd_group(intake_dir: Path, minimum: int) -> int:
    """Print groups whose occurrence count reaches *minimum*.

    Args:
        intake_dir: Intake directory to group.
        minimum: Smallest occurrence count to print.

    Returns:
        Exit code, always 0.
    """
    for group in group_entries(discover(intake_dir)):
        if occurrence_count(group) < minimum:
            continue
        emit(f"{occurrence_header(group)} — {group[0].kind} — {group[0].title}")
        for entry in group:
            emit(f"  {entry.path}")
    return 0


def _cmd_check(root: Path, intake_dir: Path) -> int:
    """Print check problems; exit 1 when there are any.

    Args:
        root: Repository root, used to read the redact configuration.
        intake_dir: Intake directory to check.

    Returns:
        1 when problems were found, else 0.
    """
    problems = check(intake_dir, configured_redact(root))
    for problem in problems:
        emit(problem)
    if problems:
        emit(f"forge-intake check: {len(problems)} problem(s)")
        return 1
    emit(f"forge-intake check: {len(discover(intake_dir))} entries OK")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``forge-intake`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="forge-intake",
        description="Record, check, group and retire sightings of problems.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the intake layout; migrate docs/lessons.md")
    new = sub.add_parser("new", help="write a stamped entry")
    new.add_argument("kind", choices=sorted(KIND_DIRS))
    new.add_argument("slug")
    new.add_argument("--title", required=True)
    new.add_argument("--surface", default="")
    sub.add_parser("list", help="list every entry")
    grp = sub.add_parser("group", help="show likely recurrences")
    grp.add_argument("--min", type=int, default=PROMOTION_AT, dest="minimum")
    sub.add_parser("check", help="validate entries (pre-commit step intake_check)")
    ret = sub.add_parser(
        "retire", help="delete filed entries; record them in the ledger"
    )
    ret.add_argument("paths", nargs="+", type=Path)
    ret.add_argument("--issue", required=True)
    return parser


def _dispatch_write(args: argparse.Namespace, root: Path, intake_dir: Path) -> None:
    """Run the subcommands that write files; may raise ``ValueError``.

    Args:
        args: Parsed command line; ``command`` is ``init``, ``new`` or ``retire``.
        root: Repository root.
        intake_dir: The intake directory.
    """
    if args.command == "init":
        for line in init(root, intake_dir) or ["intake already set up"]:
            emit(line)
    elif args.command == "new":
        emit(str(new_entry(intake_dir, args.kind, args.slug, args.title, args.surface)))
    else:
        emit(retire(intake_dir, args.paths, args.issue))


def main(argv: list[str] | None = None) -> int:
    """Run ``forge-intake``.

    Args:
        argv: Argument vector; ``None`` reads ``sys.argv``.

    Returns:
        The subcommand's exit code.
    """
    args = _build_parser().parse_args(argv)
    root = repo_root()
    intake_dir = configured_dir(root)
    if args.command == "group":
        return _cmd_group(intake_dir, args.minimum)
    if args.command == "check":
        return _cmd_check(root, intake_dir)
    if args.command == "list":
        return _cmd_list(intake_dir)
    try:
        _dispatch_write(args, root, intake_dir)
    except ValueError as exc:
        sys.stderr.write(f"forge-intake: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
