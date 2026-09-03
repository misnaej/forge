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
by :func:`assemble_changelog`'s single writer. The version itself is
assembler-owned too: the next release is always ``latest v* tag +
max(bump level over pending fragments)``
(:func:`next_version_from_fragments`), computed at release, never
carried per-PR.

Usage:

- ``forge-changelog auto-tag`` — tag-per-merge CI seam: cut and push an
  annotated tag from the fragments merged since the last tag (tag-tree
  membership marks consumption); never touches the base branch.
- ``forge-changelog release-pr`` — unattended counterpart of
  ``release``: branch ``chore/assemble-vX.Y.Z``, stage, commit, push,
  and open the assembly PR with in-body gate evidence; idempotent and
  race-tolerant; merging stays human.
- ``forge-changelog release`` — compute the next version from the
  latest tag + pending fragments, assemble ``CHANGELOG.md`` under it,
  write ``.claude-plugin/plugin.json`` to it (when a manifest exists —
  the manifest's single writer), and stage everything (never commits).
  Merge the resulting PR; tag-on-merge cuts the tag.
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
from typing import TYPE_CHECKING

from forge.changelog import restrand_changelog, top_release_heading
from forge.config import is_fragments_mode, load_config, read_tool_forge_section
from forge.git_utils import (
    configure_cli_logging,
    create_annotated_tag,
    create_commit,
    emit,
    find_open_pr_by_head_prefix,
    latest_v_tag,
    merge_base_with_head,
    next_version,
    render_plugin_version,
    repo_root,
    require_cli,
    resolve_base_branch_ref,
    run_gate_evidence,
    run_git,
)


if TYPE_CHECKING:
    from collections.abc import Callable


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
    fragment mode), the fragment-mode branch of
    ``verify-forge-plugin-version``, and the ``check`` subcommand all
    consume this, so the gate cannot drift between them.

    Args:
        root: Repository root directory.

    Returns:
        Every validation error across pending fragments; empty when all
        are valid or none exist.
    """
    return _collect_valid_fragments(root)[1]


def branch_added_fragments(root: Path) -> list[str]:
    """Return the fragment paths this branch adds and the base does not have.

    Enforcement seam for the one-fragment-per-PR convention. A fragment
    counts as the branch's own only when BOTH hold:

    1. it is added relative to the merge-base with the configured base
       branch (``--diff-filter=A`` against the working tree, so staged
       additions count before they are committed), and
    2. it is absent from the base branch's tip tree — tree membership
       in the base means the fragment belongs to another (already
       landed) PR, the same membership idea the auto-tagger uses with
       tag trees.

    Condition 2 is what keeps the count correct mid-merge: a conflicted
    base merge runs pre-commit while ``HEAD`` is still the pre-merge
    commit, so every fragment the merge brings in from the base looks
    "added since the fork" — but each is in the base tip's tree, so
    none is counted as this branch's.

    Args:
        root: Repository root directory.

    Returns:
        Repo-relative paths of branch-added fragments, sorted; empty
        when the base ref cannot be resolved (fresh clone, no remote) —
        the count check degrades open rather than failing on repos
        where the fork point is unknowable.
    """
    cfg_base = load_config(root).base_branch
    base_ref = resolve_base_branch_ref(root, cfg_base)
    if base_ref is None:
        return []
    fork = merge_base_with_head(root, cfg_base)
    if not fork:
        return []
    raw = run_git(
        "diff",
        "--name-only",
        "--diff-filter=A",
        fork,
        "--",
        str(FRAGMENTS_DIR),
        cwd=root,
        check=False,
    )
    in_base = set(
        run_git(
            "ls-tree",
            "-r",
            "--name-only",
            base_ref,
            "--",
            str(FRAGMENTS_DIR),
            cwd=root,
            check=False,
        ).splitlines()
    )
    return sorted(
        line
        for line in raw.splitlines()
        if line.endswith(".md") and line not in in_base
    )


def next_version_from_fragments(root: Path, latest_tag: str) -> tuple[str, str] | None:
    """Compute the next release version from pending fragments.

    The assembler-owned version computation: the next release is always
    *latest_tag* bumped by the strongest level declared across pending
    fragments, so version numbers exist nowhere but the release commit
    — no per-PR slot to collide on.

    Args:
        root: Repository root directory.
        latest_tag: Latest ``v*`` release tag (the bump baseline).

    Returns:
        ``(bare_version, level)`` — e.g. ``("1.3.0", "minor")`` — or
        ``None`` when no fragments are pending.

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
    level = max_level(fragments)
    return next_version(latest_tag, level).removeprefix("v"), level


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
    """Print the computed next release version: latest tag + max pending level.

    Args:
        root: Repository root directory.

    Returns:
        ``0`` with ``vX.Y.Z (level)`` printed; ``2`` when there is no
        ``v*`` tag, no pending fragment, or an invalid fragment (the
        message says which).
    """
    rc, computed = _computed_next_version(root, "next-version", none_pending_rc=2)
    if computed is None:
        return rc if rc is not None else 2
    bare_version, level = computed
    emit(f"v{bare_version} ({level})")
    return 0


def _cmd_release(root: Path, date: str) -> int:
    """Prepare the release commit: assemble + manifest write, all staged.

    Computes the version once (:func:`next_version_from_fragments`),
    assembles ``CHANGELOG.md`` under it with fragment deletions staged,
    and — when the repo ships a plugin manifest — writes
    ``.claude-plugin/plugin.json`` to the computed version and stages
    it. Never commits: the caller branches, commits, and opens the
    release PR; tag-on-merge cuts the tag. Manifest-less (tag-versioned)
    repos skip the manifest write and use the printed version for their
    own tag flow.

    Args:
        root: Repository root directory.
        date: Optional heading date override.

    Returns:
        ``0`` on success; ``2`` when there is no ``v*`` tag, no pending
        fragment, an invalid fragment, or the assembly/manifest write
        fails.
    """
    rc, computed = _computed_next_version(root, "release", none_pending_rc=2)
    if computed is None:
        return rc if rc is not None else 2
    bare_version, _level = computed
    outcome = _stage_release(root, date, bare_version)
    if outcome != 0:
        return outcome
    emit(
        f"Release v{bare_version} prepared — commit, PR, merge; "
        "tag-on-merge cuts the tag."
    )
    return 0


def _stage_release(root: Path, date: str, bare_version: str) -> int:
    """Assemble + manifest-write the release for *bare_version*, all staged.

    The shared body of ``release`` and ``release-pr``: the caller has
    already computed (and thereby validated) the version via
    :func:`next_version_from_fragments`; this stages the outcome and
    never commits.

    Args:
        root: Repository root directory.
        date: Optional heading date override.
        bare_version: The computed release version (no ``v`` prefix).

    Returns:
        ``0`` on success; ``2`` when the manifest render or assembly
        fails (tree left untouched on a manifest refusal).
    """
    # Compute-then-write (rebump's invariant, mirrored): render the
    # manifest FIRST — it is pure and carries the fail-closed
    # validation — so a manifest refusal leaves the tree untouched
    # instead of stranding a half-release (heading written, fragments
    # already deleted, manifest stale).
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest_text: str | None = None
    if manifest.is_file():
        try:
            manifest_text = render_plugin_version(
                manifest.read_text(encoding="utf-8"), bare_version
            )
        except ValueError as exc:
            emit(f"release: {exc}")
            return 2
    # next_version_from_fragments already proved every fragment valid,
    # so this re-collection cannot surface errors — it only recovers the
    # Fragment list the tuple result does not carry.
    fragments, _errors = _collect_valid_fragments(root)
    outcome = _assemble_and_stage(
        root, fragments, f"v{bare_version}", date, delete=True
    )
    if outcome != 0:
        return outcome
    if manifest_text is not None:
        manifest.write_text(manifest_text, encoding="utf-8")
        run_git("add", ".claude-plugin/plugin.json", cwd=root)
        emit(f"Staged .claude-plugin/plugin.json at {bare_version}.")
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
    in_tag = set(
        run_git(
            "ls-tree",
            "-r",
            "--name-only",
            tag,
            "--",
            str(FRAGMENTS_DIR),
            cwd=root,
            check=False,
        ).splitlines()
    )
    return [p for p in pending if str(p.relative_to(root)) not in in_tag]


def _tag_exists(root: Path, version: str) -> bool:
    """Check if a tag already exists in the repository.

    Args:
        root: Repository root directory.
        version: The tag to check (e.g., "v1.2.3").

    Returns:
        ``True`` if the tag exists locally or remotely, ``False`` otherwise.
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
    push = subprocess.run(
        ["git", "push", "origin", version],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if push.returncode != 0:
        # Local _tag_exists is useless here — this function just created
        # that ref; only the remote can attest a concurrent winner.
        remote_tag = run_git(
            "ls-remote",
            "--tags",
            "origin",
            f"refs/tags/{version}",
            cwd=root,
            check=False,
        )
        if remote_tag.strip():
            emit(f"auto-tag: {version} appeared remotely — another runner won.")
            return 0
        emit(
            f"auto-tag: pushing {version} FAILED and no concurrent winner "
            f"explains it: {push.stderr.strip()}"
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
    changelog and syncs the manifest. Never silent: every path emits
    what happened or why nothing did.

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

# The assembly PR is opened by unattended automation, so the PR body —
# not a cron log nobody reads — carries what the merger must know.
_ASSEMBLY_PR_BODY = """\
Scheduled changelog assembly ({version}): collates the pending
`changelog.d/` fragments into `CHANGELOG.md` under **{version}** and
syncs `.claude-plugin/plugin.json` (when present).

**Merging this PR is the release act for the changelog**: the fragments
are consumed. {tagging} Merge stays a human decision.

Opened by `forge-changelog release-pr` (assemble-release workflow).
"""

# The tagging sentence must match the repo's version source: a manifest
# repo's tag-release workflow tags the merge (`forge-next-prep --tag`
# sees the manifest ahead); a manifest-less repo has nothing ahead and
# `auto-tag` no-ops on the merge (the fragments were just deleted), so
# the tag is a post-merge step the body must name — not claim happened.
_TAGGING_WITH_MANIFEST = (
    "`forge-next-prep --tag` in the tag-release workflow tags the merge "
    "(the manifest is ahead)."
)
_TAGGING_MANIFEST_LESS = (
    "No plugin manifest: cut the release tag after merging — "
    "`forge-release --from-changelog` reads the heading this PR writes "
    "(auto-tag cannot cover it; the fragments are deleted by this very "
    "assembly)."
)


def _assembly_pr_body(root: Path, version: str) -> str:
    """Render the assembly PR body with the repo-correct tagging sentence.

    Args:
        root: Repository root directory.
        version: Release version tag name (``vX.Y.Z``).

    Returns:
        The formatted PR body.
    """
    manifest = (root / ".claude-plugin" / "plugin.json").is_file()
    tagging = _TAGGING_WITH_MANIFEST if manifest else _TAGGING_MANIFEST_LESS
    return _ASSEMBLY_PR_BODY.format(version=version, tagging=tagging)


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
    gates = "changelog_version,plugin_version"
    return run_gate_evidence(
        root,
        gates,
        pass_headline=(
            f"✅ **Versioning gates pass on the assembled tree** "
            f"(`forge-precommit --only {gates}`)."
        ),
        fail_headline=(
            "⚠️ **Versioning gates FAILED on the assembled tree — do not "
            "merge without a full review.**"
        ),
        section_title="Assembly verification",
    )


def _computed_next_version(
    root: Path, cmd: str, *, none_pending_rc: int
) -> tuple[int | None, tuple[str, str] | None]:
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
        ``(exit_code, None)`` when the guard decides, or
        ``(None, (bare_version, level))`` to proceed.
    """
    latest = latest_v_tag(root)
    if latest is None:
        emit(f"{cmd}: no v* tag — no release baseline to bump from.")
        return 2, None
    try:
        computed = next_version_from_fragments(root, latest)
    except ValueError as exc:
        emit(f"{cmd}: {exc}")
        return 2, None
    if computed is None:
        emit(f"{cmd}: no pending fragments — nothing to release.")
        return none_pending_rc, None
    return None, computed


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
    rc, computed = _computed_next_version(root, "release-pr", none_pending_rc=0)
    if computed is None:
        return rc if rc is not None else 2
    existing = find_open_pr_by_head_prefix(root, ASSEMBLY_BRANCH_PREFIX)
    if existing:
        emit(f"release-pr: assembly PR already open — {existing}")
        return 0
    bare_version, _level = computed
    version = f"v{bare_version}"
    return _publish_assembly_pr(root, version, bare_version, date=date, draft=draft)


def _stage_and_commit_assembly(
    root: Path, date: str, bare_version: str, version: str
) -> int:
    """Stage and commit the assembly changelog and manifest.

    Args:
        root: Repository root directory.
        date: Optional heading date override.
        bare_version: The computed release version (no ``v`` prefix).
        version: Release version tag name (``vX.Y.Z``) for the commit message.

    Returns:
        ``0`` on success; ``2`` on staging failure.
    """
    outcome = _stage_release(root, date, bare_version)
    if outcome != 0:
        return outcome
    create_commit(root, f"chore(release): assemble {version}")
    return 0


def _publish_assembly_pr(
    root: Path,
    version: str,
    bare_version: str,
    *,
    date: str = "",
    draft: bool = False,
) -> int:
    """Branch, stage, commit, push the assembly and open its PR.

    Args:
        root: Repository root directory.
        version: Release version tag name (``vX.Y.Z``) for the branch and titles.
        bare_version: The computed release version (no ``v`` prefix).
        date: Optional heading date override (defaults to today).
        draft: Open the PR as a draft.

    Returns:
        ``0`` — PR opened, or another runner's assembly PR explains the
        push/create failure; ``2`` on a guard or push failure no race
        explains.
    """
    base_branch = load_config(root).base_branch
    branch = f"{ASSEMBLY_BRANCH_PREFIX}{version}"
    start_branch = run_git("branch", "--show-current", cwd=root, check=False)
    run_git("switch", "-c", branch, cwd=root)
    try:
        outcome = _stage_and_commit_assembly(root, date, bare_version, version)
        if outcome != 0:
            return outcome
        return _push_and_open_pr(root, branch, version, base_branch, draft=draft)
    finally:
        if start_branch:
            run_git("switch", start_branch, cwd=root, check=False)


def _push_and_open_pr(
    root: Path, branch: str, version: str, base_branch: str, *, draft: bool
) -> int:
    """Push the assembly branch and open its PR, deferring to race winners.

    Args:
        root: Repository root directory.
        branch: The assembly branch (already committed, checked out).
        version: Release version tag name (``vX.Y.Z``) for titles.
        base_branch: PR base — the repo's ``[tool.forge].base_branch``.
        draft: Open the PR as a draft.

    Returns:
        ``0`` — PR opened, or another runner's assembly PR explains the
        push/create failure; ``2`` on a failure no race explains.
    """
    _passed, evidence = _gate_evidence(root)
    push = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    if push.returncode != 0:
        racing = find_open_pr_by_head_prefix(root, ASSEMBLY_BRANCH_PREFIX)
        if racing:
            emit(f"release-pr: lost the race — assembly PR open at {racing}")
            return 0
        emit(f"release-pr: push FAILED:\n{push.stderr.strip()}")
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
            f"chore(release): assemble {version}",
            "--body",
            _assembly_pr_body(root, version) + "\n" + evidence,
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
        help="print the computed next release version "
        "(latest v* tag + max pending bump level)",
    )
    rel = sub.add_parser(
        "release",
        help="assemble CHANGELOG.md under the computed next version, write "
        "plugin.json to it (when present), stage everything — never commits",
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
        "(tag-per-merge CI seam; pushes the tag only)",
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
        "release-pr": lambda: _cmd_release_pr(root, args.date, draft=args.draft),
        "restrand": lambda: _cmd_restrand(root, args.bump),
        "assemble": lambda: _cmd_assemble(
            root, args.version, args.date, delete=args.delete
        ),
    }
    return handlers[args.command]()


if __name__ == "__main__":
    sys.exit(main())
