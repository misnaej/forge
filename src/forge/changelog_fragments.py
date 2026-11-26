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
to remove. The rest of the body is ``- `` bullets, copied verbatim.

Direction of truth is one-way: fragments → assembler → CHANGELOG +
version. In fragment mode nothing may read ``CHANGELOG.md`` as a
version or bump signal — the changelog is an OUTPUT of release, written
by :func:`assemble_changelog`'s single writer. The version itself is
assembler-owned too, and tag-aware: pending fragments already inside
a ``v*`` tag's tree were released by that tag (a per-merge tag in a
manifest-less repo) and assemble under its heading; only fragments no tag
holds mint a new version — ``latest v* tag + max(bump level over the
unreleased fragments)`` (:func:`plan_assembly`), computed at release, never
carried per-PR. When every pending fragment is tagged, nothing is minted:
the assembly backfills the per-tag headings and syncs the manifest to the
latest tag.

Usage:

- ``forge-changelog auto-tag`` — tag-per-merge CI seam: cut and push an
  annotated tag from the fragments merged since the last tag (tag-tree
  membership marks consumption); never touches the base branch.
- ``forge-changelog release-pr`` — unattended counterpart of
  ``release``: branch ``chore/assemble-vX.Y.Z``, stage, commit, push,
  and open the assembly PR with in-body gate evidence; idempotent and
  race-tolerant; merging stays human.
- ``forge-changelog release`` — plan the assembly (tag-aware), assemble
  ``CHANGELOG.md`` with a heading per already-tagged group plus any
  minted heading, and
  stage everything (never commits). Merge the resulting PR; the tag job
  cuts the tag at that merge when the plan minted one.
- ``forge-changelog next-version`` — read-only print of the computed
  next version and its bump level.
- ``forge-changelog assemble --version vX.Y.Z`` — collate every pending
  fragment into ``CHANGELOG.md`` under an explicit version and stage
  the fragment deletions (never commits).
- ``forge-changelog check`` — validate pending fragments (the same gate
  the ``changelog_version`` pre-commit step runs in fragment mode).
- ``forge-changelog restrand`` — shared-heading repos only: mechanically
  move entries stranded under a released heading to the next open
  ``## vX.Y.Z`` slot (self-skips in fragment mode, where entries cannot
  strand); stages ``CHANGELOG.md``, never commits.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from forge.changelog import restrand_changelog, top_release_heading
from forge.config import is_fragments_mode, load_config, read_tool_forge_section
from forge.git_utils import (
    configure_cli_logging,
    create_annotated_tag,
    create_commit,
    diff_names_z,
    emit,
    fetch_tags_best_effort,
    find_open_pr_by_head_prefix,
    git_z_names,
    latest_v_tag,
    merge_base_with_head,
    next_version,
    push_branch,
    push_tag,
    repo_root,
    require_cli,
    resolve_pr_base_ref,
    run_gate_evidence,
    run_git,
    tag_commit_date,
    tag_on_remote,
    v_tags,
)
from forge.text_budget import BUDGET_RULE, length_verdict


if TYPE_CHECKING:
    from collections.abc import Callable


configure_cli_logging()
logger = logging.getLogger(__name__)


FRAGMENTS_DIR = Path("changelog.d")

# Fields per ``for-each-ref`` tag line: direct object, peeled object (annotated
# tags only), then the short name. An unpeeled line carries two.
_TAG_LINE_PEELED_FIELDS = 3
_TAG_LINE_DIRECT_FIELDS = 2

# Fragment type token → release-entry group heading, in assembly order.
# dict order IS the rendered group order — deliberate and stable.
# `upgrade` renders first: it is the lane consumers are told to read
# before anything else, and the heading `forge-upgrade` extracts.
FRAGMENT_TYPES: dict[str, str] = {
    "upgrade": "⚠️ Upgrade notes",
    "added": "Features",
    "changed": "Changes",
    "fixed": "Fixes",
    "removed": "Removed",
    "docs": "Docs",
}

# The only values the bump front-matter may carry — semver LEVELS, never
# numbers. Ordered weakest-first; comparisons use the index.
FRAGMENT_LEVELS = ("patch", "minor", "major")

# A plain filename slug: no dots, so a following type token parses
# unambiguously. Shared with the intake's entry names.
SLUG_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]*"

# `<slug>.<type>.md`.
_FILENAME_RE = re.compile(rf"^(?P<slug>{SLUG_PATTERN})\.(?P<type>[a-z]+)\.md$")

# First body line: `bump: <level>`.
_BUMP_RE = re.compile(r"^bump:\s*(?P<level>[a-z]+)\s*$")

# A version-shaped token (vX.Y.Z or X.Y.Z) — forbidden anywhere in a
# fragment, filename or body: the level-only rule is gate-enforced, not
# conventional.
_VERSION_SHAPED_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b")

# An embedded `## ` heading would splice fake structure into the
# assembled changelog — rejected outright.
# Any markdown heading, not just `## `: the assembler writes the group
# headings, and a `### …` line in a body could forge a group — notably the
# `### ⚠️ Upgrade notes` lane forge-upgrade extracts — or end one early.
_EMBEDDED_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)


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


@dataclass(frozen=True)
class AssemblyPlan:
    """What one assembly run writes: per-tag backfill plus an optional mint.

    Attributes:
        tagged: ``(tag, fragments)`` groups in ascending tag order — each
            fragment already sits in that tag's tree, so the tag released
            it and the assembly writes the heading it was owed.
        untagged: Fragments no tag holds — the unreleased set.
        version: Bare release version: the latest tag bumped by
            ``untagged``'s strongest level, or the latest tag itself when
            ``untagged`` is empty (the manifest syncs, nothing is minted).
        level: The bump level behind a minted version; ``None`` when
            nothing is minted.
    """

    tagged: list[tuple[str, list[Fragment]]]
    untagged: list[Fragment]
    version: str
    level: str | None

    @property
    def fragments(self) -> list[Fragment]:
        """Every fragment the assembly consumes, tagged groups first."""
        return [f for _tag, group in self.tagged for f in group] + self.untagged


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
) -> tuple[str, str, int, list[str]]:
    """Validate bump line and extract level, body and the body's file line.

    Args:
        path: Fragment file path.
        lines: Split text lines.

    Returns:
        ``(level, body, body_line, errors)`` — ``body_line`` is the 1-based
        line of the file the body starts on, so an error about a body
        offset can name a line the author can jump to; both strings are
        empty when errors exist.
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
    rest = lines[1:]
    leading_blanks = next((i for i, raw in enumerate(rest) if raw.strip()), len(rest))
    body = "\n".join(rest).strip()
    if not body:
        errors.append(f"{name}: empty entry body")
    return level, body, 2 + leading_blanks, errors


def _check_no_versions_or_headings(name: str, body: str, body_line: int) -> list[str]:
    """Check for version-shaped strings and embedded headings.

    Args:
        name: Fragment filename (for error messages).
        body: The fragment body text.
        body_line: 1-based file line the body starts on, so a reported
            line points into the file rather than into the body.

    Returns:
        List of errors found (empty if all checks pass).
    """
    errors: list[str] = []
    found = _VERSION_SHAPED_RE.search(body)
    if found:
        # Quote the match and say that a *past* release in prose counts: the
        # rule reads as "do not name the version you are releasing", so a
        # sentence like "renamed in v1.2.3" looks allowed and is not. The
        # line is counted from the file, not the body: the body starts after
        # the bump line and any blank lines following it, so a body-relative
        # number sends the author to the wrong line.
        line = body_line + body.count("\n", 0, found.start())
        errors.append(
            f"{name}: line {line}: {found.group()!r} is version-shaped — a "
            "fragment may not name any version, a past release in prose "
            'included (write "when agents were renamed" instead). The '
            "assembler is the only writer of version numbers."
        )
    if _EMBEDDED_HEADING_RE.search(body):
        errors.append(
            f"{name}: embedded markdown heading in body — not allowed; the "
            "assembler writes every heading"
        )
    return errors


def fragment_bullets(body: str) -> list[str]:
    """Return the body's bullets, one string each, continuations folded in.

    The one bullet grammar every reader of a fragment shares: a ``- ``
    line opens a bullet and an indented line continues the one above it,
    so the length advisory and the coverage check cannot disagree about
    what a bullet is.

    A continuation is folded into its bullet rather than dropped, so a
    bullet wrapped across lines is one item with its whole text: the
    numbering a refusal prints, and the word count the advisory reads,
    both describe what the author wrote.

    Args:
        body: The fragment body, as validated.

    Returns:
        Each bullet's text without its ``- `` marker, in document order,
        with any continuation lines joined by single spaces.
    """
    bullets: list[str] = []
    for line in body.splitlines():
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif bullets and line.startswith((" ", "\t")) and line.strip():
            bullets[-1] = f"{bullets[-1]} {line.strip()}"
    return bullets


def _check_bullets(name: str, body: str) -> list[str]:
    """Check the body starts with a ``- `` bullet and every line continues one.

    The assembler copies bodies verbatim under the group heading, so a
    line that is neither a bullet nor an indented continuation renders as
    loose prose between entries, and an indented first line as a code block.

    Args:
        name: Fragment filename (for error messages).
        body: The fragment body text (stripped, so its first line is not blank).

    Returns:
        One error naming the first offending line and how many there are,
        or none.
    """
    lines = body.splitlines()
    bad = [
        line
        for i, line in enumerate(lines)
        if line.strip()
        and not line.startswith("- ")
        and (i == 0 or not line.startswith((" ", "\t")))
    ]
    if not bad:
        return []
    return [
        (
            f"{name}: {len(bad)} body line(s) not a '- ' bullet or an indented "
            f"continuation, first: {bad[0][:60]!r} — prefix each entry with '- '"
        )
    ]


#: Words a changelog bullet aims at. Deliberately small: the entry says
#: what changed, and the reader decides from it whether a release affects
#: them. Everything beyond that — why, how, what it supersedes — belongs
#: in the commit, the PR or the code, each of which the reader can reach
#: and none of which they are made to read first. Not a line budget and
#: not scaled to the diff: a one-line fix and a rewrite each earn one
#: bullet per thing that changed, and the bullet is short either way.
#:
#: A ceiling, never a length to write toward: over it the entry is
#: advised, and only past :data:`BULLET_REFUSAL_WORDS` is it refused.
BULLET_WORD_CEILING: Final[int] = 12

#: The count past which a bullet is refused rather than advised — twice
#: the ceiling, where a bullet has stopped being an entry and become
#: prose. Stated outright rather than taken as a percentage of the
#: ceiling, because :data:`~forge.text_budget.REFUSAL_PERCENT` of twelve
#: words is two, and refusing an entry two words over buys nothing.
BULLET_REFUSAL_WORDS: Final[int] = 2 * BULLET_WORD_CEILING

#: Longest bullet excerpt quoted in the advisory, in characters.
_NOTE_QUOTE_MAX: Final[int] = 80


def _bullet_verdicts(body: str) -> list[tuple[str, int, str]]:
    """Return every bullet with its word count and its length verdict.

    One loop behind both :func:`advised_bullets` and
    :func:`refused_bullets`, so the advised set and the refused set
    cannot disagree about where a bullet sits.

    One bullet per thing that changed is the rule the author applies;
    this measures only the part a program can see — that each bullet is
    as short as it can be. A count of bullets is deliberately not
    checked: how many things changed is not derivable from a diff, and
    guessing it from one would nag the entries that are right.

    Args:
        body: The fragment body, as validated.

    Returns:
        ``(bullet, word count, verdict)`` per bullet, in document order.
        The bullet carries its ``- `` marker, and a wrapped bullet
        counts whole.
    """
    verdicts: list[tuple[str, int, str]] = []
    for bullet in fragment_bullets(body):
        words = len(bullet.split())
        verdict = length_verdict(
            words, BULLET_WORD_CEILING, refuse_over=BULLET_REFUSAL_WORDS
        )
        verdicts.append((f"- {bullet}", words, verdict))
    return verdicts


def advised_bullets(body: str) -> list[tuple[str, int]]:
    """Return the bullets over the ceiling that are still accepted.

    Args:
        body: The fragment body, as validated.

    Returns:
        ``(bullet, word count)`` for each bullet over
        :data:`BULLET_WORD_CEILING` and at or under
        :data:`BULLET_REFUSAL_WORDS`, in document order.
    """
    return [
        (bullet, words)
        for bullet, words, verdict in _bullet_verdicts(body)
        if verdict == "warn"
    ]


def refused_bullets(body: str) -> list[tuple[str, int]]:
    """Return the bullets long enough to refuse the commit.

    Args:
        body: The fragment body, as validated.

    Returns:
        ``(bullet, word count)`` for each bullet past
        :data:`BULLET_REFUSAL_WORDS`, in document order.
    """
    return [
        (bullet, words)
        for bullet, words, verdict in _bullet_verdicts(body)
        if verdict == "refuse"
    ]


def _worst_quoted(bullets: list[tuple[str, int]]) -> tuple[str, int]:
    """Return the longest bullet, quotable, with its word count.

    Args:
        bullets: ``(bullet, word count)`` pairs, never empty.

    Returns:
        The longest bullet capped at :data:`_NOTE_QUOTE_MAX` characters,
        and its word count. Capped because the message naming it is read
        in a terminal, where a 111-word quote buries its own advice.
    """
    worst, words = max(bullets, key=lambda pair: pair[1])
    if len(worst) > _NOTE_QUOTE_MAX:
        worst = worst[:_NOTE_QUOTE_MAX] + "…"
    return worst, words


def verbose_entry_note(body: str) -> str | None:
    """Return an advisory when a fragment's bullets are longer than needed.

    Advisory while the bullet is merely over the ceiling: what it buys is
    the question being asked while the entry is still cheap to cut. Past
    :data:`BULLET_REFUSAL_WORDS` the gate refuses instead, and that
    refusal names the bullet, so this line says nothing about one.

    Args:
        body: The fragment body, as validated.

    Returns:
        One advisory line, or ``None`` when no bullet sits over the
        ceiling without being refused.
    """
    advised = advised_bullets(body)
    if not advised:
        return None

    worst, words = _worst_quoted(advised)
    return (
        f"ADVISORY: {len(advised)} bullet(s) run long, worst at {words} words "
        f"against a ceiling of {BULLET_WORD_CEILING}. {BUDGET_RULE} The why "
        f"belongs in the commit, not here. Longest: {worst}. Past "
        f"{BULLET_REFUSAL_WORDS} words a bullet is refused, not advised."
    )


def overlong_bullet_refusal(body: str) -> str | None:
    """Return the refusal for a fragment carrying a bullet of prose.

    The checkable half of "say what changed and stop": a bullet past
    twice its ceiling has stopped being an entry, whatever it says. The
    cut is always available, so nothing here needs a bypass.

    Args:
        body: The fragment body, as validated.

    Returns:
        One refusal line, or ``None`` when every bullet is within
        :data:`BULLET_REFUSAL_WORDS`.
    """
    refused = refused_bullets(body)
    if not refused:
        return None

    worst, words = _worst_quoted(refused)
    return (
        f"{len(refused)} bullet(s) past {BULLET_REFUSAL_WORDS} words, worst "
        f"at {words} — that is prose, not an entry. Cut to the "
        f"{BULLET_WORD_CEILING}-word ceiling: say what changed and stop, and "
        f"leave the why to the commit, the PR or the code, each of which the "
        f"reader can reach. Longest: {worst}"
    )


def _check_major_carries_upgrade_notes(name: str, ftype: str, level: str) -> list[str]:
    """Require a ``major`` fragment to be the ``upgrade`` lane itself.

    A major release is the one a consumer cannot take without acting, and
    ``forge-upgrade`` reads exactly one lane to tell them what to do.
    Nothing else can carry it: the gate admits **one** fragment per PR,
    so a sibling ``<slug>.upgrade.md`` is not an option the author has —
    the single fragment is either the upgrade note or there is none.

    Args:
        name: Fragment filename (for error messages).
        ftype: The parsed type token.
        level: The parsed bump level.

    Returns:
        One error when a major fragment sits in another lane, else none.
    """
    if level != "major" or ftype == "upgrade":
        return []
    return [
        (
            f"{name}: 'bump: major' requires type 'upgrade', not '{ftype}' — a "
            f"major is what a consumer must act on, and `forge-upgrade` reads "
            f"the upgrade lane to tell them how. A PR carries one fragment, so "
            f"rename this to <slug>.upgrade.md and write the action in it; if "
            f"nothing is owed on upgrade, the bump is not major."
        )
    ]


def validate_fragment(path: Path) -> tuple[Fragment | None, list[str]]:
    """Parse *path* into a :class:`Fragment`, collecting every violation.

    The gate contract: filename must match ``<slug>.<type>.md`` with a
    known type; the body's first line must be ``bump: <level>`` with a
    known level; a ``major`` level must carry type ``upgrade``; no
    version-shaped string may appear in the filename or
    body (level-only rule); no embedded ``## `` heading may appear in
    the body (assembly-structure injection); every body line is a ``- ``
    bullet or an indented continuation.

    Args:
        path: Fragment file to validate.

    Returns:
        ``(fragment, errors)`` — ``fragment`` is ``None`` whenever
        ``errors`` is non-empty.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"{path.name}: unreadable ({exc})"]
    return _validate_fragment_text(path, text)


def _validate_fragment_text(path: Path, text: str) -> tuple[Fragment | None, list[str]]:
    """Apply :func:`validate_fragment`'s contract to *text* as *path*'s content.

    Args:
        path: Fragment path the text belongs to (its name is validated).
        text: Fragment contents — from disk, or a staged blob.

    Returns:
        ``(fragment, errors)`` — ``fragment`` is ``None`` whenever
        ``errors`` is non-empty.
    """
    name = path.name
    slug, ftype, filename_errors = _parse_and_validate_filename(name)
    lines = text.splitlines()
    level, body, body_line, bump_errors = _parse_bump_line_and_body(path, lines)
    content_errors = _check_no_versions_or_headings(name, body, body_line)
    if body:
        content_errors += _check_bullets(name, body)
    # Only once both halves parsed: a filename or bump error already told
    # the author what to fix, and a cross-check over an unparsed token
    # would name a lane that is not what they wrote.
    if not filename_errors and not bump_errors:
        content_errors += _check_major_carries_upgrade_notes(name, ftype, level)
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


def _collect_valid_fragments(root: Path) -> tuple[list[Fragment], list[str]]:
    """Parse every pending fragment, splitting valid ones from errors.

    Args:
        root: Repository root directory.

    Returns:
        ``(fragments, errors)`` — parsed fragments in discovery order,
        plus every validation error across the pending set.
    """
    fragments: list[Fragment] = []
    errors: list[str] = []
    for path in discover_fragments(root):
        fragment, frag_errors = validate_fragment(path)
        if fragment is not None:
            fragments.append(fragment)
        errors.extend(frag_errors)
    return fragments, errors


def check_pending(root: Path) -> list[str]:
    """Validate every pending fragment under *root*.

    The shared gate seam: the ``changelog_version`` pre-commit step (in
    fragment mode) and the ``check`` subcommand both consume this, so
    the gate cannot drift between them.

    Args:
        root: Repository root directory.

    Returns:
        Every validation error across pending fragments; empty when all
        are valid or none exist.
    """
    return _collect_valid_fragments(root)[1]


def check_staged(root: Path, paths: list[str]) -> list[str]:
    """Validate the staged (index) copy of each fragment in *paths*.

    :func:`check_pending` reads the disk, but a commit carries the index:
    a placeholder staged and then rewritten on disk would pass a disk-only
    check while the commit ships the placeholder. Paths outside
    ``changelog.d/*.md`` and paths absent from the index (a staged
    deletion) are skipped — there is no blob to validate.

    Args:
        root: Repository root directory.
        paths: Repo-relative candidate fragment paths.

    Returns:
        Every validation error found in the staged copies.
    """
    errors: list[str] = []
    for rel in paths:
        rel_path = Path(rel)
        if rel_path.parent != FRAGMENTS_DIR or rel_path.suffix != ".md":
            continue
        try:
            text = run_git("show", f":{rel}", cwd=root, log_errors=False)
        except subprocess.CalledProcessError:
            continue
        errors.extend(_validate_fragment_text(rel_path, text)[1])
    return errors


def staged_fragment(root: Path, rel: str) -> tuple[Fragment | None, list[str]]:
    """Return the staged copy of the fragment at *rel*, parsed.

    A commit carries the index, not the disk: a bullet staged long and
    then shortened on disk would pass a disk-only check while the commit
    ships the long one — the same drift :func:`check_staged` guards for
    structural errors.

    Args:
        root: Repository root directory.
        rel: Repo-relative fragment path.

    Returns:
        The parsed fragment and its errors. ``(None, [])`` when the path
        has no blob in the index, which is not an error: during an
        ordinary pre-commit run nothing is staged yet.
    """
    try:
        text = run_git("show", f":{rel}", cwd=root, log_errors=False)
    except subprocess.CalledProcessError:
        return None, []
    return _validate_fragment_text(Path(rel), text)


def branch_added_fragments(root: Path, *, base_ref: str | None = None) -> list[str]:
    """Return the fragment paths this branch adds and the base does not have.

    Enforcement seam for the one-fragment-per-PR convention. A fragment
    counts as the branch's own only when BOTH hold:

    1. it is added relative to the merge-base with the PR's base — the
       branch the open PR actually targets when one exists (a stacked
       PR's parent), else the configured base branch
       (:func:`forge.git_utils.resolve_pr_base_ref`) — with
       ``--diff-filter=A`` against the working tree, so staged additions
       count before they are committed, and
    2. it is absent from that base's tip tree — tree membership in the
       base means the fragment belongs to another (already landed or
       parent) PR, the same membership idea the auto-tagger uses with
       tag trees. (Assumes a path collision denotes the same logical
       fragment: a same-named-but-different fragment independently
       landed on base excludes the branch's own — distinctive slugs
       are the namespace.)

    Condition 2 is what keeps the count correct mid-merge: a conflicted
    base merge runs pre-commit while ``HEAD`` is still the pre-merge
    commit, so every fragment the merge brings in from the base looks
    "added since the fork" — but each is in the base tip's tree, so
    none is counted as this branch's.

    Args:
        root: Repository root directory.
        base_ref: Pre-resolved PR base ref; ``None`` resolves via
            :func:`forge.git_utils.resolve_pr_base_ref`. Callers that
            also report the resolved base pass it here so the gh
            lookup runs once.

    Returns:
        Repo-relative paths of branch-added fragments, sorted; empty
        when the base ref cannot be resolved (fresh clone, no remote) —
        the count check degrades open rather than failing on repos
        where the fork point is unknowable.
    """
    if base_ref is None:
        base_ref = resolve_pr_base_ref(root, load_config(root).base_branch)
    if base_ref is None:
        return []
    fork = run_git("merge-base", base_ref, "HEAD", cwd=root, check=False)
    if not fork:
        return []
    added = diff_names_z(root, "--diff-filter=A", fork, "--", str(FRAGMENTS_DIR))
    # ls-tree failure must degrade OPEN (skip the count), not to an
    # empty membership set — an empty set would silently reproduce the
    # very over-count this exclusion exists to prevent.
    try:
        in_base = set(
            git_z_names(
                root,
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                base_ref,
                "--",
                str(FRAGMENTS_DIR),
                check=True,
            )
        )
    except subprocess.CalledProcessError:
        return []
    return sorted(
        name for name in added if name.endswith(".md") and name not in in_base
    )


def _fragments_in_tag_tree(root: Path, tag: str, *, check: bool = False) -> set[str]:
    """Return the repo-relative fragment paths present in *tag*'s tree.

    Args:
        root: Repository root directory.
        tag: An existing tag name.
        check: Raise when git fails instead of reading as an empty tree.
            Callers that turn this listing into a statement about where a
            fragment shipped pass ``True``: an unreadable tree is not the
            same as a tag holding nothing, and reporting it as such puts a
            false sentence in front of a reader.

    Returns:
        Paths under ``changelog.d/`` as ``git ls-tree`` prints them; empty
        when the tag holds none — or, with *check* off, when it could not
        be read.
    """
    return set(
        git_z_names(
            root,
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            tag,
            "--",
            str(FRAGMENTS_DIR),
            check=check,
        )
    )


@dataclass(frozen=True)
class TagGap:
    """A default-branch commit that brought fragments no tag names.

    Attributes:
        commit: The full commit SHA.
        subject: The commit's subject line.
        fragments: Repo-relative fragment paths the commit added.
        folded_into: The earliest tag whose tree holds those fragments, or
            ``None`` when no tag counts them yet. A fold is still a gap:
            the commit shipped without a release of its own, and the next
            tag's notes carry two merges' worth of change.
    """

    commit: str
    subject: str
    fragments: tuple[str, ...]
    folded_into: str | None


def _tagged_commits(root: Path) -> dict[str, str]:
    """Map each ``v*`` tag's commit SHA to the tag name.

    Annotated tags are peeled (``*objectname``), so a tag object's own SHA
    never stands in for the commit it names.

    Args:
        root: Repository root directory.

    Returns:
        Commit SHA to tag name; empty when no ``v*`` tag exists.
    """
    raw = run_git(
        "for-each-ref",
        "--format=%(objectname) %(*objectname) %(refname:short)",
        "refs/tags/v*",
        cwd=root,
        # A repo with no tags lists nothing and exits 0, so only a broken
        # listing raises — and that must raise rather than read as "no tag
        # anywhere", which would report every commit as a gap.
        check=True,
    )
    tagged: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == _TAG_LINE_PEELED_FIELDS:
            direct, peeled, name = parts
            tagged[peeled or direct] = name
        elif len(parts) == _TAG_LINE_DIRECT_FIELDS:
            tagged[parts[0]] = parts[1]
    return tagged


def tag_gaps(root: Path, *, since: str | None = None) -> list[TagGap]:
    """Find commits that added fragments but carry no tag of their own.

    The tag-per-merge invariant says every merge bringing fragments gets a
    release tag. A merge can miss one for reasons the tagging step never
    sees — its run never fired, CI went red, the runner died, or the remote
    refused the tag push — so this reads git alone and never consults why.
    The next merge's ``auto-tag`` then counts the orphaned fragments into
    its own tag, which repairs the version but loses the per-merge release
    and silently doubles up the notes.

    First-parent only: a squash-merging repo records each merge as one
    commit on the default branch, so a merge that added a fragment is a
    first-parent commit whose diff adds a path under ``changelog.d/``.

    Args:
        root: Repository root directory.
        since: Revision to start after, exclusive. ``None`` examines
            ``HEAD`` alone — the shape for a tagging job checking the
            merge it just handled.

    Returns:
        One :class:`TagGap` per offending commit, newest first; empty when
        every fragment-bearing commit in the window carries its own tag.
    """
    # `-1` is an option, so it has to precede `--end-of-options`: after it
    # git reads it as a revision and refuses the whole command. `check` is
    # on because that refusal exits non-zero with no output, which a
    # tolerant call would hand back as "no gaps" — a broken probe must not
    # read as a clean one. An empty window exits 0, so it still returns [].
    limit = [] if since else ["-1"]
    raw = run_git(
        "log",
        "--first-parent",
        "--diff-filter=A",
        "--name-only",
        "-z",
        "--format=%x1e%H%x1f%s",
        *limit,
        "--end-of-options",
        f"{since}..HEAD" if since else "HEAD",
        "--",
        str(FRAGMENTS_DIR),
        cwd=root,
        check=True,
    )
    tagged = _tagged_commits(root)
    gaps: list[TagGap] = []
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        header, _, names = record.partition("\x00")
        commit, _, subject = header.partition("\x1f")
        # Git writes the format line, then a NUL, then a newline before the
        # first name: the names need stripping, not just filtering, or each
        # carries that newline into the tag-tree comparison.
        added = tuple(sorted(n.strip() for n in names.split("\x00") if n.strip()))
        if not added or commit in tagged:
            continue
        gaps.append(
            TagGap(
                commit=commit,
                subject=subject.strip(),
                fragments=added,
                folded_into=_earliest_tag_holding(root, added),
            )
        )
    return gaps


def _earliest_tag_holding(root: Path, fragments: tuple[str, ...]) -> str | None:
    """Return the oldest ``v*`` tag whose tree holds any of *fragments*.

    A fragment stays in the tree until an assembly PR deletes it, so it is
    present in every tag cut after the one that counted it. The oldest
    holder is therefore the tag that shipped it; :func:`v_tags` lists
    newest first, hence the reversal.

    Args:
        root: Repository root directory.
        fragments: Repo-relative fragment paths.

    Returns:
        The oldest tag whose tree holds one, or ``None`` when no tag does —
        meaning the fragments are still waiting for any release.
    """
    wanted = set(fragments)
    for tag in reversed(v_tags(root)):
        if wanted & _fragments_in_tag_tree(root, tag, check=True):
            return tag
    return None


def _partition_by_release_tag(
    root: Path, fragments: list[Fragment]
) -> tuple[list[tuple[str, list[Fragment]]], list[Fragment]]:
    """Group *fragments* by the earliest tag whose tree holds each one.

    Walks the tags newest-first, one ``ls-tree`` each, and stops at the
    first tag holding none of the pending fragments: a fragment only
    ever enters a tree and stays until an assembly deletes it, so every
    older tag holds none either. The last tag seen holding a fragment
    is therefore its earliest — the release that shipped it.

    Args:
        root: Repository root directory.
        fragments: Validated pending fragments.

    Returns:
        ``(tagged, untagged)`` — ``tagged`` as ``(tag, fragments)`` in
        ascending tag order, each group filename-sorted; ``untagged``
        the fragments no tag holds, filename-sorted.
    """
    by_path = {str(f.path.relative_to(root)): f for f in fragments}
    owner: dict[str, str] = {}
    tags_seen: list[str] = []
    for tag in v_tags(root):
        members = _fragments_in_tag_tree(root, tag) & by_path.keys()
        if not members:
            break
        tags_seen.append(tag)
        for rel in members:
            owner[rel] = tag
    tagged = [
        (
            tag,
            sorted(
                (by_path[r] for r, t in owner.items() if t == tag), key=lambda f: f.path
            ),
        )
        for tag in reversed(tags_seen)
        if any(t == tag for t in owner.values())
    ]
    untagged = sorted(
        (f for rel, f in by_path.items() if rel not in owner), key=lambda f: f.path
    )
    return tagged, untagged


def plan_assembly(root: Path, latest_tag: str) -> AssemblyPlan | None:
    """Plan the assembly: tag-aware version and per-tag fragment groups.

    The assembler-owned version computation. Fragments already released
    by a tag (tag-per-merge) never bump again — they were counted when
    that tag was cut — so the minted version, if any, comes from the
    unreleased fragments alone, and an all-released backlog mints
    nothing.

    Args:
        root: Repository root directory.
        latest_tag: Latest ``v*`` release tag (the bump baseline).

    Returns:
        The plan, or ``None`` when no fragments are pending.

    Raises:
        ValueError: When any pending fragment is invalid; the message
            lists every validation error.
    """
    fragments, errors = _collect_valid_fragments(root)
    if errors:
        msg = "invalid pending fragment(s):\n" + "\n".join(errors)
        raise ValueError(msg)
    if not fragments:
        return None
    tagged, untagged = _partition_by_release_tag(root, fragments)
    if untagged:
        level = max_level(untagged)
        version = next_version(latest_tag, level).removeprefix("v")
    else:
        level = None
        version = latest_tag.removeprefix("v")
    return AssemblyPlan(tagged=tagged, untagged=untagged, version=version, level=level)


def _cmd_tag_gaps(root: Path, since: str | None) -> int:
    """Report default-branch commits whose fragments got no tag of their own.

    Reads git only, so it reports the lost release whatever caused it —
    including the causes the tagging step cannot observe, which is why it
    is not folded into ``auto-tag``'s own result. Tags are refreshed first:
    a CI checkout may start without them, and a local-only view would call
    a tagged commit untagged.

    Args:
        root: Repository root directory.
        since: Revision to start after, exclusive, or ``None`` for ``HEAD``
            alone.

    Returns:
        ``0`` when every fragment-bearing commit in the window carries its
        own tag; ``2`` when any does not.
    """
    for note in fetch_tags_best_effort(root):
        emit(f"tag-gaps: {note}")
    gaps = tag_gaps(root, since=since)
    window = f"{since}..HEAD" if since else "HEAD"
    if not gaps:
        emit(f"tag-gaps: every fragment-bearing commit in {window} has its own tag.")
        return 0
    for gap in gaps:
        fold = (
            f"its fragments ship under {gap.folded_into}"
            if gap.folded_into
            else "its fragments are in no tag yet"
        )
        emit(
            f"tag-gaps: {gap.commit[:7]} brought "
            f"{len(gap.fragments)} fragment(s) but no tag names it — "
            f"{fold}. {gap.subject}"
        )
    emit(
        f"tag-gaps: {len(gaps)} commit(s) in {window} carry no release tag "
        "of their own. Where a later tag holds their fragments the version "
        "is intact, but those merges have no release to name and the next "
        "tag's notes cover more than one merge. On a branch the window also "
        "spans commits that have not merged yet, which no tag names because "
        "none is due."
    )
    return 2


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


def _assemble_and_stage(
    root: Path, fragments: list[Fragment], version: str, date: str, *, delete: bool
) -> int:
    """Assemble *fragments* into ``CHANGELOG.md`` under *version*; maybe stage.

    The shared assembly core behind ``assemble`` (explicit version) and
    ``release`` (computed version).

    Args:
        root: Repository root directory.
        fragments: Validated fragments to collate (non-empty).
        version: Release version for the new heading (``vX.Y.Z``).
        date: Optional heading date override.
        delete: Stage fragment deletions via ``git rm`` (never commits).

    Returns:
        ``0`` on success; ``2`` on assembly failure.
    """
    try:
        updated = assemble_changelog(
            _read_changelog(root), fragments, version, date=date
        )
    except ValueError as exc:
        emit(f"changelog.d: {exc}")
        return 2
    emit(
        f"Assembled {len(fragments)} fragment(s) into CHANGELOG.md "
        f"under {version} (max level: {max_level(fragments)})."
    )
    _write_assembly(root, updated, fragments, delete=delete)
    return 0


def _read_changelog(root: Path) -> str:
    """Return ``CHANGELOG.md``'s text, or ``""`` when the file does not exist.

    Args:
        root: Repository root directory.

    Returns:
        The contents of ``CHANGELOG.md``, or an empty string if the file does
        not exist.
    """
    changelog = root / "CHANGELOG.md"
    return changelog.read_text(encoding="utf-8") if changelog.is_file() else ""


def _write_assembly(
    root: Path, text: str, fragments: list[Fragment], *, delete: bool
) -> None:
    """Write the assembled changelog and, when asked, stage it with the deletions.

    Args:
        root: Repository root directory.
        text: The full assembled ``CHANGELOG.md`` contents.
        fragments: The consumed fragments (deleted when *delete*).
        delete: Stage ``CHANGELOG.md`` and ``git rm`` the fragments (never commits).
    """
    (root / "CHANGELOG.md").write_text(text, encoding="utf-8")
    if delete:
        run_git("add", "CHANGELOG.md", cwd=root)
        for fragment in fragments:
            run_git("rm", "-q", str(fragment.path.relative_to(root)), cwd=root)
        emit("Staged CHANGELOG.md and fragment deletions — commit is yours.")


def _render_assembly(root: Path, text: str, plan: AssemblyPlan, date: str) -> str:
    """Insert every heading the plan owes into *text*, newest ending on top.

    Tagged groups go in ascending order, each dated when its tag was
    cut; the minted heading (if any) goes last, so it lands above them.

    Args:
        root: Repository root directory.
        text: Current ``CHANGELOG.md`` contents.
        plan: The assembly plan.
        date: Optional date override for the minted heading only.

    Returns:
        The updated changelog text.

    Raises:
        ValueError: From :func:`assemble_changelog` (a heading already
            exists, or a group is empty).
    """
    for tag, group in plan.tagged:
        text = assemble_changelog(text, group, tag, date=tag_commit_date(root, tag))
        emit(f"Assembled {len(group)} fragment(s) under existing tag {tag}.")
    if plan.untagged:
        version = f"v{plan.version}"
        text = assemble_changelog(text, plan.untagged, version, date=date)
        emit(
            f"Assembled {len(plan.untagged)} unreleased fragment(s) under "
            f"{version} (level: {plan.level})."
        )
    return text


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
    fragments, errors = _collect_valid_fragments(root)
    if errors:
        for err in errors:
            emit(f"changelog.d: INVALID — {err}")
        return 2
    if not fragments:
        emit("changelog.d: nothing to assemble.")
        return 2
    return _assemble_and_stage(root, fragments, version, date, delete=delete)


def _cmd_next_version(root: Path) -> int:
    """Print the computed next release version, tag-aware.

    Args:
        root: Repository root directory.

    Returns:
        ``0`` with ``vX.Y.Z (level)`` printed — or, when every pending
        fragment is already released by a tag, ``vX.Y.Z (already tagged
        — N fragment(s) across M tag(s); nothing to mint)``; ``2`` when
        there is no ``v*`` tag, no pending fragment, or an invalid
        fragment (the message says which).
    """
    rc, plan = _computed_next_version(root, "next-version", none_pending_rc=2)
    if plan is None:
        return rc if rc is not None else 2
    emit(_describe_plan(plan))
    return 0


def _describe_plan(plan: AssemblyPlan) -> str:
    """Return the one-line ``next-version`` verdict for the assembly plan.

    Args:
        plan: The assembly plan to describe.

    Returns:
        A one-line summary of the plan's version and scope.
    """
    n_tagged = sum(len(group) for _tag, group in plan.tagged)
    if plan.level is None:
        return (
            f"v{plan.version} (already tagged — {n_tagged} fragment(s) across "
            f"{len(plan.tagged)} tag(s); nothing to mint)"
        )
    suffix = (
        f" + {n_tagged} fragment(s) already under {len(plan.tagged)} tag(s)"
        if n_tagged
        else ""
    )
    return f"v{plan.version} ({plan.level}){suffix}"


def _cmd_release(root: Path, date: str) -> int:
    """Prepare the release commit: assemble the changelog, all staged.

    Computes the plan once (:func:`plan_assembly`) and assembles
    ``CHANGELOG.md`` — one heading per already-cut tag, plus a minted
    heading for fragments no tag has reached yet — with the fragment
    deletions staged. Prose only: the version lives in the tags, so
    there is nothing else to write and nothing to keep in step.

    Never commits: the caller branches, commits and opens the assembly
    PR. The tags these entries describe were cut on the merges that
    brought the fragments, so no tag follows from this.

    Args:
        root: Repository root directory.
        date: Optional heading date override.

    Returns:
        ``0`` on success; ``2`` when there is no ``v*`` tag, no pending
        fragment, an invalid fragment, or the assembly fails.
    """
    rc, plan = _computed_next_version(root, "release", none_pending_rc=2)
    if plan is None:
        return rc if rc is not None else 2
    outcome = _stage_release(root, date, plan)
    if outcome != 0:
        return outcome
    if plan.level is None:
        emit(
            "Assembly under existing tags prepared — commit, PR, merge; "
            "the tags already exist."
        )
    else:
        emit(
            f"Assembly for v{plan.version} prepared — commit, PR, merge; "
            "the tag for it is cut on that merge."
        )
    emit(
        f"Commit it on `{assembly_branch(plan.version)}` — "
        "`release-pr` and the scheduled job recognise an open assembly "
        "only by that branch prefix, so an assembly committed elsewhere "
        "gets a second one opened alongside it."
    )
    emit(
        "These fragments are now staged for deletion and CHANGELOG.md is "
        "already rewritten, so `release-pr` reports nothing pending until "
        "this lands. Abandoning it means restoring the tree, which "
        "discards that work — ask first, and never reset."
    )
    return 0


def _stage_release(root: Path, date: str, plan: AssemblyPlan) -> int:
    """Assemble the release per *plan*, staged.

    The shared body of ``release`` and ``release-pr``: the caller has
    already computed (and thereby validated) the plan via
    :func:`plan_assembly`; this stages the outcome and never commits.

    Assembly writes prose only. The version itself lives in the ``v*``
    tags and nowhere else, so there is no second copy for this to keep
    in step — a tag is cut per merge from the fragments that merge
    brought, and the assembly files each fragment under the tag that
    already shipped it.

    Args:
        root: Repository root directory.
        date: Optional heading date override (minted heading only).
        plan: The assembly plan.

    Returns:
        ``0`` on success; ``2`` when the assembly fails, leaving the tree
        untouched.
    """
    try:
        updated = _render_assembly(root, _read_changelog(root), plan, date)
    except ValueError as exc:
        emit(f"changelog.d: {exc}")
        return 2
    _write_assembly(root, updated, plan.fragments, delete=True)
    return 0


def fragments_new_since_tag(root: Path, tag: str | None) -> list[Path]:
    """Return pending fragments absent from *tag*'s tree — the newly merged.

    Tag-tree membership is the consumption marker in the tag-per-merge
    model: a fragment already present in the latest tag's tree was
    counted by that tag's bump and must not bump again. ``None`` (no
    tags yet) treats every pending fragment as new.

    Args:
        root: Repository root directory.
        tag: Latest ``v*`` tag, or ``None``.

    Returns:
        Pending fragment paths not in *tag*'s tree, filename-sorted.
    """
    pending = discover_fragments(root)
    if tag is None:
        return pending
    in_tag = _fragments_in_tag_tree(root, tag)
    return [p for p in pending if str(p.relative_to(root)) not in in_tag]


def _tag_exists(root: Path, version: str) -> bool:
    """Check if a tag exists in the local repository.

    Local refs only — a tag that exists only on the remote reads as
    absent until it is fetched.

    Args:
        root: Repository root directory.
        version: The tag to check (e.g., "v1.2.3").

    Returns:
        ``True`` if the tag exists locally, ``False`` otherwise.
    """
    return bool(
        run_git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/{version}",
            cwd=root,
            check=False,
        )
    )


def _validate_auto_tag_fragments(
    new_paths: list[Path],
) -> tuple[list[Fragment], list[str]]:
    """Validate fragments and separate valid ones from errors.

    Args:
        new_paths: List of fragment file paths to validate.

    Returns:
        A tuple of (valid_fragments, error_messages). The error_messages
        list contains all validation errors found; if non-empty, the
        fragments should not be used.
    """
    fragments: list[Fragment] = []
    errors: list[str] = []
    for path in new_paths:
        fragment, frag_errors = validate_fragment(path)
        if fragment is not None:
            fragments.append(fragment)
        else:
            errors.extend(frag_errors)
    return fragments, errors


def _create_and_push_tag(root: Path, version: str, level: str, n_fragments: int) -> int:
    """Create and push an annotated tag, handling concurrent-runner races.

    This function checks whether the tag already exists locally before
    creating it, and after a push failure, checks remotely to distinguish
    a lost race (another runner's push arrived first) from a genuine push
    failure. Two race checkpoints exist because two runners may both pass
    the "new fragments" gate and reach tag-creation before either pushes.
    The push goes through :func:`~forge.git_utils.push_tag`, so it is
    bounded and never prompts: this runs unattended in CI, where a
    credential prompt would hang the job instead of failing it.

    A refusal under the remote's workflow-permission rule
    (:attr:`~forge.git_utils.PushResult.workflow_scope_refused`) is
    reported with its cause and remedy rather than as a generic push
    failure. It still returns ``2``: the merge has no tag of its own, a
    lost release until something cuts one, and a warning would leave that
    looking handled.

    Args:
        root: Repository root directory.
        version: The version string to tag (e.g., "v1.2.3").
        level: The bump level that produced this version (e.g., "minor").
        n_fragments: Count of fragments contributing to this tag (for logging).

    Returns:
        ``0`` if the tag was created and pushed, or if another runner won
        the race; ``2`` if the push failed and no concurrent winner was
        detected.
    """
    if _tag_exists(root, version):
        emit(f"auto-tag: {version} already exists — another runner won.")
        return 0
    create_annotated_tag(root, version)
    push = push_tag(root, version)
    if not push.ok:
        # Local _tag_exists is useless here — this function just created
        # that ref; only the remote can attest a concurrent winner. An
        # unanswered probe (None) cannot attest one, so it falls through
        # to the failure report, as an unreadable listing always did.
        if tag_on_remote(root, version):
            emit(f"auto-tag: {version} appeared remotely — another runner won.")
            return 0
        if push.workflow_scope_refused:
            emit(
                f"auto-tag: the remote refused {version} under the "
                "workflow-permission rule. The commit being tagged reaches a "
                "different .github/workflows/ state than the remote's branch "
                "tips hold — most often because a later merge changed a "
                "workflow file — and this credential cannot carry one. "
                f"No tag now names this commit: the next auto-tag that "
                f"succeeds counts these fragments, or `forge-release` cuts "
                f"{version} by hand. Only a pushing credential carrying the "
                f"workflows scope removes the refusal — scoped to this "
                f"repository, since it is the narrowest that works; no "
                f"permission on the default token can. Git said: "
                f"{push.stderr}"
            )
            return 2
        emit(
            f"auto-tag: pushing {version} FAILED (exit {push.returncode}) and "
            f"no concurrent winner explains it: {push.stderr}"
        )
        return 2
    emit(
        f"auto-tag: cut {version} ({level}) from {n_fragments} newly "
        "merged fragment(s)."
    )
    return 0


def _cmd_auto_tag(root: Path) -> int:
    """Tag the current commit from its newly merged fragments (CI seam).

    The tag-per-merge mechanism: read the last tag, read the levels of
    the fragments merged since it (tag-tree membership — see
    :func:`fragments_new_since_tag`), bump, tag, push the tag. Tag refs
    sit outside branch rulesets, so no commit to the base branch is
    needed; fragment files persist until an assembly PR collates the
    changelog. Never silent: every path emits what happened or why
    nothing did.

    Args:
        root: Repository root directory.

    Returns:
        ``0`` tagged, already-tagged (another runner won), or nothing
        new to tag; ``2`` on invalid fragments or a push failure that no
        concurrent winner explains; ``3`` when ``[tool.forge.release]``
        does not opt in (``auto = "merge"``) but new fragments are
        pending — the caller surfaces this as a loud warning.
    """
    if not is_fragments_mode(root):
        emit("auto-tag: not a fragments-mode repo — nothing to do.")
        return 0
    latest = latest_v_tag(root)
    new_paths = fragments_new_since_tag(root, latest)
    if not new_paths:
        emit(
            f"auto-tag: no new fragments since {latest or '(no tag)'} — nothing to tag."
        )
        return 0
    if read_tool_forge_section(root, "release").get("auto") != "merge":
        emit(
            f"auto-tag: {len(new_paths)} new fragment(s) pending but "
            '[tool.forge.release].auto != "merge" — no tag will be cut '
            "until `forge-changelog release` runs or auto-tagging is "
            "enabled."
        )
        return 3
    fragments, errors = _validate_auto_tag_fragments(new_paths)
    if errors:
        for err in errors:
            emit(f"auto-tag: INVALID — {err}")
        return 2
    level = max_level(fragments)
    version = next_version(latest, level)
    return _create_and_push_tag(root, version, level, len(fragments))


ASSEMBLY_BRANCH_PREFIX = "chore/assemble-"


def assembly_branch(version: str) -> str:
    """Return the branch name an assembly for *version* belongs on.

    The one place the name is built, because two callers depend on it
    agreeing: :func:`_cmd_release_pr` creates it, and
    :func:`find_open_pr_by_head_prefix` recognises an open assembly PR by
    that prefix alone. An assembly committed onto any other branch is
    invisible to that check, so the scheduled job opens a second one.

    Args:
        version: Release version, with or without its leading ``v``.

    Returns:
        The branch name, e.g. ``chore/assemble-v1.2.3``.
    """
    tag = version if version.startswith("v") else f"v{version}"
    return f"{ASSEMBLY_BRANCH_PREFIX}{tag}"


# The PR body carries what the merger must know about the release it
# cuts — whoever opened it, the body is the record that outlives the run.
_ASSEMBLY_PR_BODY = """\
Changelog assembly ({version}): collates the pending
`changelog.d/` fragments into `CHANGELOG.md` {headings}.

**No tag is cut by this PR.** A release tag is cut on every merge, from
the fragments that merge brought, so the tags these entries describe
already exist — this writes the prose for them. Merging consumes the
fragments, and stays a human decision.

Opened by `forge-changelog release-pr`.
"""


def _assembly_pr_body(plan: AssemblyPlan) -> str:
    """Render the assembly PR body.

    Args:
        plan: The assembly plan.

    Returns:
        The formatted PR body.
    """
    version = f"v{plan.version}"
    tags = ", ".join(tag for tag, _group in plan.tagged)
    if plan.level is None:
        headings = f"under their release tags ({tags})"
    else:
        headings = f"under **{version}**"
        if tags:
            headings += f" (plus backfilled headings for {tags})"
    return _ASSEMBLY_PR_BODY.format(version=version, headings=headings)


def _gate_evidence(root: Path) -> tuple[bool, str]:
    """Run the versioning gates and format PR-body evidence.

    ``release-pr`` may open its PR with a token whose events trigger no
    CI (GitHub's anti-recursion rule for ``GITHUB_TOKEN``), so the PR
    body itself must carry verification evidence. Formatting and the
    failure-never-blocks contract live in
    :func:`forge.git_utils.run_gate_evidence`.

    Args:
        root: Repo root passed to the gate subprocess as cwd.

    Returns:
        ``(passed, evidence_block)`` per ``run_gate_evidence``.
    """
    gates = "changelog_version"
    return run_gate_evidence(
        root,
        gates,
        success_headline=(
            f"✅ **Versioning gates pass on the assembled tree** "
            f"(`forge-precommit --only {gates}`)."
        ),
        failure_headline=(
            "⚠️ **Versioning gates FAILED on the assembled tree — do not "
            "merge without a full review.**"
        ),
        section_title="Assembly verification",
    )


def _computed_next_version(
    root: Path, cmd: str, *, none_pending_rc: int
) -> tuple[int | None, AssemblyPlan | None]:
    """Run the shared version-computation guard for a subcommand.

    The identical guard trio of ``release``, ``release-pr``, and
    ``next-version``: no baseline tag and an invalid fragment are hard
    failures; "nothing pending" is quiet or loud per caller.

    Args:
        root: Repository root directory.
        cmd: Subcommand name for message prefixes.
        none_pending_rc: Exit code for the nothing-pending case (``0``
            quiet for the unattended ``release-pr``; ``2`` for the
            explicit ``release`` / ``next-version`` invocations).

    Returns:
        ``(exit_code, None)`` when the guard decides, or ``(None, plan)``
        to proceed.
    """
    latest = latest_v_tag(root)
    if latest is None:
        emit(f"{cmd}: no v* tag — no release baseline to bump from.")
        return 2, None
    try:
        plan = plan_assembly(root, latest)
    except ValueError as exc:
        emit(f"{cmd}: {exc}")
        return 2, None
    if plan is None:
        emit(f"{cmd}: no pending fragments — nothing to release.")
        return none_pending_rc, None
    return None, plan


def _gh_preflight() -> int | None:
    """Refuse loudly up front when ``gh`` is missing or unauthenticated.

    Runs BEFORE any mutation: an unattended cron run must never strand
    a pushed branch on a late ``gh`` failure.

    Returns:
        ``2`` when ``gh`` is unauthenticated; ``None`` when ready. A
        missing ``gh`` never returns — ``require_cli`` aborts with
        ``SystemExit(2)``.
    """
    require_cli("gh", caller="forge-changelog", hint="install GitHub CLI (gh)")
    auth = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, check=False
    )
    if auth.returncode != 0:
        emit(f"release-pr: gh is not authenticated:\n{auth.stderr.strip()}")
        return 2
    return None


def _cmd_release_pr(root: Path, date: str, *, draft: bool) -> int:
    """Open the assembly PR for the pending fragments (scheduled CI seam).

    The unattended counterpart of ``release``: guard, branch
    ``chore/assemble-vX.Y.Z``, stage the assembly via the shared
    :func:`_stage_release`, commit, push, and open the PR with in-body
    gate evidence. Merging stays human. Idempotent and race-tolerant:
    an already-open assembly PR — found up front or surfacing when the
    push/create loses a race — defers with exit ``0``.

    Args:
        root: Repository root directory.
        date: Optional heading date override.
        draft: Open the PR as a draft.

    Returns:
        ``0`` — PR opened, an assembly PR already exists, or nothing is
        pending (the quiet weekly case); ``2`` on a guard failure (no
        tag, invalid fragment, unauthenticated gh, git/gh failure no
        race explains); a missing ``gh`` aborts via ``SystemExit(2)``.
    """
    if not is_fragments_mode(root):
        emit("release-pr: not a fragments-mode repo — nothing to do.")
        return 0
    preflight = _gh_preflight()
    if preflight is not None:
        return preflight
    rc, plan = _computed_next_version(root, "release-pr", none_pending_rc=0)
    if plan is None:
        return rc if rc is not None else 2
    existing = find_open_pr_by_head_prefix(root, ASSEMBLY_BRANCH_PREFIX)
    if existing:
        emit(f"release-pr: assembly PR already open — {existing}")
        return 0
    return _publish_assembly_pr(root, plan, date=date, draft=draft)


def _stage_and_commit_assembly(root: Path, date: str, plan: AssemblyPlan) -> int:
    """Stage and commit the assembly changelog and manifest.

    Args:
        root: Repository root directory.
        date: Optional heading date override.
        plan: The assembly plan (its version names the commit).

    Returns:
        ``0`` on success; ``2`` on staging failure.
    """
    outcome = _stage_release(root, date, plan)
    if outcome != 0:
        return outcome
    create_commit(root, f"chore(release): assemble v{plan.version}")
    return 0


def _publish_assembly_pr(
    root: Path,
    plan: AssemblyPlan,
    *,
    date: str = "",
    draft: bool = False,
) -> int:
    """Branch, stage, commit, push the assembly and open its PR.

    Args:
        root: Repository root directory.
        plan: The assembly plan (its version names the branch and titles).
        date: Optional heading date override (defaults to today).
        draft: Open the PR as a draft.

    Returns:
        ``0`` — PR opened, or another runner's assembly PR explains the
        push/create failure; ``2`` on a guard or push failure no race
        explains.
    """
    base_branch = load_config(root).base_branch
    version = f"v{plan.version}"
    branch = assembly_branch(version)
    start_branch = run_git("branch", "--show-current", cwd=root, check=False)
    run_git("switch", "-c", branch, cwd=root)
    try:
        outcome = _stage_and_commit_assembly(root, date, plan)
        if outcome != 0:
            return outcome
        return _push_and_open_pr(root, branch, plan, base_branch, draft=draft)
    finally:
        if start_branch:
            run_git("switch", start_branch, cwd=root, check=False)


def _push_and_open_pr(
    root: Path, branch: str, plan: AssemblyPlan, base_branch: str, *, draft: bool
) -> int:
    """Push the assembly branch and open its PR, deferring to race winners.

    Args:
        root: Repository root directory.
        branch: The assembly branch (already committed, checked out).
        plan: The assembly plan (its version names the title; its groups the body).
        base_branch: PR base — the repo's ``[tool.forge].base_branch``.
        draft: Open the PR as a draft.

    Returns:
        ``0`` — PR opened, or another runner's assembly PR explains the
        push/create failure; ``2`` on a failure no race explains.
    """
    _passed, evidence = _gate_evidence(root)
    push = push_branch(root, branch, set_upstream=True)
    if not push.ok:
        racing = find_open_pr_by_head_prefix(root, ASSEMBLY_BRANCH_PREFIX)
        if racing:
            emit(f"release-pr: lost the race — assembly PR open at {racing}")
            return 0
        emit(f"release-pr: push FAILED:\n{push.stderr}")
        return 2
    create = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            *(["--draft"] if draft else []),
            "--base",
            base_branch,
            "--head",
            branch,
            "--title",
            f"chore(release): assemble v{plan.version}",
            "--body",
            _assembly_pr_body(plan) + "\n" + evidence,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    if create.returncode != 0:
        racing = find_open_pr_by_head_prefix(root, ASSEMBLY_BRANCH_PREFIX)
        if racing:
            emit(f"release-pr: assembly PR already open — {racing}")
            return 0
        emit(
            f"release-pr: gh pr create FAILED (branch {branch} pushed — "
            f"open the PR manually):\n{create.stderr.strip()}"
        )
        return 2
    emit(f"release-pr: opened {create.stdout.strip()}")
    return 0


def _restrand_old_text(root: Path) -> str | None:
    """Return the comparison-point ``CHANGELOG.md`` for the restrand.

    Prefers the merge base with the configured base branch (the same
    reference the ``changelog_version`` pre-commit step diffs against);
    falls back to the latest tag's copy (``forge-release``'s reference)
    when no base resolves.

    Args:
        root: Repository root directory.

    Returns:
        The old changelog text, or ``None`` when neither reference
        yields one.
    """
    base = merge_base_with_head(root, load_config(root).base_branch)
    refs = [base] if base else []
    latest = latest_v_tag(root)
    if latest:
        refs.append(latest)
    for ref in refs:
        text = run_git("show", f"{ref}:CHANGELOG.md", cwd=root, check=False)
        if text:
            return text
    return None


def _restrand_preflight(root: Path) -> int | tuple[Path, str, str, str]:
    """Resolve restrand preconditions or the early-exit code for a missing one.

    Args:
        root: Repository root directory.

    Returns:
        The early-exit code (``0`` or ``2``, with message already emitted)
        when the restrand cannot proceed; otherwise a tuple of
        ``(changelog_path, new_text, old_text, latest_tag)``.
    """
    if is_fragments_mode(root):
        emit("restrand: fragments mode — entries cannot strand; nothing to do.")
        return 0
    changelog = root / "CHANGELOG.md"
    if not changelog.is_file():
        emit("restrand: no CHANGELOG.md — nothing to do.")
        return 0
    latest = latest_v_tag(root)
    if latest is None:
        emit("restrand: no v* tag — nothing can be stranded.")
        return 0
    old_text = _restrand_old_text(root)
    if old_text is None:
        emit("restrand: no comparison point (merge base or tagged CHANGELOG.md).")
        return 2
    new_text = changelog.read_text(encoding="utf-8")
    return changelog, new_text, old_text, latest


def _cmd_restrand(root: Path, bump: str) -> int:
    """Repair stranded changelog entries mechanically; stage the result.

    The shared-heading counterpart to fragments mode's immunity: entries
    that landed under a now-released heading (a release tag cut while
    the branch was open) move to the next open slot via
    :func:`forge.changelog.restrand_changelog`. Self-skips in fragments
    mode (nothing can strand there). Stages ``CHANGELOG.md``; never
    commits.

    Args:
        root: Repository root directory.
        bump: Slot class for the restranded entries
            (``patch``/``minor``/``major``).

    Returns:
        ``0`` on success or nothing-to-do; ``2`` when prerequisites are
        missing or the repair does not verify.
    """
    preflight = _restrand_preflight(root)
    if isinstance(preflight, int):
        return preflight
    changelog, new_text, old_text, latest = preflight

    try:
        repaired = restrand_changelog(old_text, new_text, latest, bump)
    except ValueError as exc:
        emit(f"restrand: {exc}")
        return 2
    if repaired == new_text:
        emit("restrand: nothing stranded.")
        return 0
    changelog.write_text(repaired, encoding="utf-8")
    run_git("add", "CHANGELOG.md", cwd=root)
    emit(
        f"Restranded entries under the next {bump} slot above {latest}; "
        "staged CHANGELOG.md — commit is yours."
    )
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
        "them into CHANGELOG.md at release (single writer). Shared-heading "
        "repos: `restrand` repairs stranded entries mechanically.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate pending changelog.d/ fragments")
    sub.add_parser(
        "next-version",
        help="print the computed next release version — latest v* tag + "
        "max level over the UNRELEASED fragments; fragments already in a "
        "tag's tree assemble under that tag and never bump again",
    )
    rel = sub.add_parser(
        "release",
        help="assemble CHANGELOG.md under the computed next version and "
        "stage everything — never commits; consumes the pending fragments, "
        "so prefer `release-pr` unless you mean to commit it yourself",
    )
    rel.add_argument("--date", default="", help="heading date (default: today, UTC)")
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
    sub.add_parser(
        "auto-tag",
        help="tag HEAD from fragments merged since the last tag "
        "(tag-per-merge CI seam; pushes the tag only; never tags a repo "
        "with a plugin manifest, which is tagged at the assembly merge)",
    )
    gaps = sub.add_parser(
        "tag-gaps",
        help="report commits that brought changelog fragments but carry no "
        "release tag of their own (reads git only, so it catches a lost "
        "tag whatever stopped it)",
    )
    gaps.add_argument(
        "--since",
        default="",
        help="revision to start after, exclusive (default: HEAD alone)",
    )
    relpr = sub.add_parser(
        "release-pr",
        help="branch, stage, commit and open the assembly PR for the "
        "pending fragments (scheduled CI seam; merge stays human)",
    )
    relpr.add_argument("--date", default="", help="heading date (default: today, UTC)")
    relpr.add_argument("--draft", action="store_true", help="open the PR as a draft")
    restrand = sub.add_parser(
        "restrand",
        help="move entries stranded under released headings to the next slot "
        "(shared-heading mode; stages, never commits)",
    )
    restrand.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        default="patch",
        help="slot class for the restranded entries (default: patch)",
    )
    args = parser.parse_args(argv)
    root = repo_root()
    handlers: dict[str, Callable[[], int]] = {
        "check": lambda: _cmd_check(root),
        "next-version": lambda: _cmd_next_version(root),
        "release": lambda: _cmd_release(root, args.date),
        "auto-tag": lambda: _cmd_auto_tag(root),
        "tag-gaps": lambda: _cmd_tag_gaps(root, args.since or None),
        "release-pr": lambda: _cmd_release_pr(root, args.date, draft=args.draft),
        "restrand": lambda: _cmd_restrand(root, args.bump),
        "assemble": lambda: _cmd_assemble(
            root, args.version, args.date, delete=args.delete
        ),
    }
    return handlers[args.command]()


if __name__ == "__main__":
    sys.exit(main())
