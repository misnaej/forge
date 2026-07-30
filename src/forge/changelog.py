"""Changelog-parsing primitives shared by forge's release tooling.

Single source of truth for recognizing ``## vX.Y.Z`` release headings in
a ``CHANGELOG.md``. Consumed by ``forge-next-prep --promotion-status``
(missing-entry advisory), ``verify-forge-changelog-history``
(dropped-entry guard), ``forge-release`` (pre-tag CHANGELOG gate),
``forge-upgrade`` (``**Action:**`` marker extraction), and
``forge-precommit``'s ``changelog_updated`` step (the
:func:`wants_no_version` opt-out) — and public API for consumer repos
composing their own release flow. Mostly pure string parsing; the
no-version opt-out section at the bottom is the one git/config-backed
part (it reads the branch, commit log, and ``[tool.forge]``).
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from forge.config import load_config
from forge.git_utils import (
    parse_semver,
    resolve_base_branch_ref,
    resolve_current_branch,
    run_git,
)


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


_HEADING_RE = re.compile(r"^##\s+(v\d+\.\d+\.\d+)\b", re.MULTILINE)

# Any level-2 heading, version-shaped or not — the changelog_version
# pre-commit step validates every ``## `` heading against the release
# recognizer, so a stray ``## Unreleased`` or ``## v1.2`` is flagged
# rather than silently ignored.
_ANY_HEADING_RE = re.compile(r"^##\s+(\S.*?)\s*$", re.MULTILINE)

# New-file hunk header of a unified diff: ``@@ -a,b +c,d @@`` → ``c``.
_HUNK_NEW_START_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# An action-required marker line: an optional list bullet, then the
# ``**Action:**`` marker, then the action text. The marker convention is
# documented in ``docs/consumer-release.md`` "Changelog convention".
_ACTION_RE = re.compile(r"^\s*(?:[-*]\s+)?\*\*Action:\*\*\s*(?P<text>\S.*?)\s*$")


def release_headings(text: str) -> set[str]:
    """Return the set of ``## v<semver>`` release headings in *text*.

    The one heading recognizer for the whole package: a level-2 heading
    whose first token is a ``v``-prefixed semver triple, with or without
    a trailing suffix (``## v1.6.0 — 2026-07-01`` and bare ``## v1.6.0``
    both count).

    Args:
        text: CHANGELOG markdown body.

    Returns:
        Each ``vX.Y.Z`` named in a level-2 release heading; empty when none.
    """
    return set(_HEADING_RE.findall(text))


def changelog_lacks_entry(changelog_text: str, tag: str) -> bool:
    """Return ``True`` when *changelog_text* has no ``## <tag>`` heading.

    Drives the promotion advisory in ``forge-next-prep`` and the hard
    pre-tag gate in ``forge-release``; see ``docs/release-process.md`` §5.

    Args:
        changelog_text: Full ``CHANGELOG.md`` contents.
        tag: Release tag to look for, e.g. ``"v1.6.0"``.

    Returns:
        ``True`` when no heading for *tag* is present.
    """
    return tag not in release_headings(changelog_text)


def _recognized_version(token: str) -> str | None:
    """Return the ``vX.Y.Z`` a heading *token* names, or ``None``.

    Routes recognition through :func:`release_headings` so validity here
    can never drift from the tag gate in ``forge-release`` — one
    recognizer for the whole package (a v-less ``0.2.0`` or truncated
    ``v1.2`` is invalid in both places for the same reason).

    Args:
        token: The text after ``## `` on a heading line.

    Returns:
        The recognized ``vX.Y.Z`` triple, or ``None`` when *token* is not
        a release heading.
    """
    found = release_headings(f"## {token}")
    return next(iter(found)) if found else None


def top_release_heading(text: str) -> str | None:
    """Return the first (topmost) recognized ``vX.Y.Z`` release heading.

    The single-track convention's *declared* version
    (``docs/consumer-release.md`` "Changelog convention"): the top
    heading names the release being prepared, so it is the version
    ``forge-release --from-changelog`` cuts. Non-version ``##`` headings
    above it (which ``changelog_version_findings`` flags) are skipped
    rather than treated as the top.

    Args:
        text: Full ``CHANGELOG.md`` contents.

    Returns:
        The topmost recognized ``vX.Y.Z``, or ``None`` when no release
        heading exists.
    """
    for token in _ANY_HEADING_RE.findall(text):
        version = _recognized_version(token)
        if version is not None:
            return version
    return None


def changelog_version_findings(text: str, latest_tag: str | None) -> list[str]:
    """Validate *text*'s release headings against each other and *latest_tag*.

    The single-track declared-version invariant (the CHANGELOG top
    heading names the release being prepared — ``docs/consumer-release.md``
    "Changelog convention"): every ``## `` heading is a recognized
    version, headings strictly decrease, the latest tag has an entry, and
    the top heading never falls behind the latest tag. Equality of top
    heading and latest tag is valid — that is the normal state right
    after a release is cut, until the next PR opens the next heading.

    Args:
        text: Full ``CHANGELOG.md`` contents.
        latest_tag: Latest ``v*`` tag (e.g. ``"v1.2.3"``), or ``None``
            when the repo has no release tags yet (tag-dependent checks
            are skipped).

    Returns:
        Human-readable findings; empty when the changelog is consistent.
    """
    findings: list[str] = []
    versions: list[str] = []
    for token in _ANY_HEADING_RE.findall(text):
        version = _recognized_version(token)
        if version is None:
            findings.append(
                f"CHANGELOG heading `## {token}` is not a valid vX.Y.Z version."
            )
        else:
            versions.append(version)
    if not versions:
        if not findings:
            findings.append("CHANGELOG.md has no `## vX.Y.Z` heading.")
        return findings
    parsed = [parse_semver(v) for v in versions]
    for i in range(1, len(versions)):
        newer, older = parsed[i - 1], parsed[i]
        if newer is not None and older is not None and newer <= older:
            findings.append(
                f"CHANGELOG headings are not strictly decreasing: "
                f"{versions[i - 1]} above {versions[i]}."
            )
    if latest_tag is not None:
        if changelog_lacks_entry(text, latest_tag):
            findings.append(
                f"Latest git tag {latest_tag} has no `## {latest_tag}` "
                "heading in CHANGELOG.md."
            )
        top = parse_semver(versions[0])
        tag = parse_semver(latest_tag)
        if top is not None and tag is not None and top < tag:
            findings.append(
                f"CHANGELOG top version {versions[0]} is behind the "
                f"latest tag {latest_tag}."
            )
    return findings


def action_items(text: str) -> list[tuple[str, str]]:
    """Return ``(version, action)`` pairs for every ``**Action:**`` line.

    The marker convention (``docs/consumer-release.md`` "Changelog
    convention"): a release entry flags each change a consumer must act
    on — adopt a new capability, react to a contract change — with a
    line whose content starts ``**Action:** <what to do>``. This
    extractor attributes each marker line to its governing ``## vX.Y.Z``
    heading; marker lines above the first release heading are ignored.
    Forward-only by construction: entries without markers yield nothing.

    Args:
        text: Full ``CHANGELOG.md`` contents.

    Returns:
        ``(version, action_text)`` pairs in file order (newest release
        first for a conventionally ordered changelog); empty when no
        marker lines exist.
    """
    governing = _governing_versions(text)
    items: list[tuple[str, str]] = []
    for index, line in enumerate(text.splitlines()):
        match = _ACTION_RE.match(line)
        if match is None:
            continue
        version = governing[index]
        if version is not None:
            items.append((version, match.group("text")))
    return items


def _governing_versions(text: str) -> list[str | None]:
    """Map each line in *text* to its governing release version heading.

    Scans the text sequentially, updating the "current" heading as new
    ``## `` lines are encountered.

    Args:
        text: Full CHANGELOG.md contents.

    Returns:
        A list where index *i* holds the governing version for line *i*.
    """
    governing: list[str | None] = []
    current: str | None = None
    for line in text.splitlines():
        match = _ANY_HEADING_RE.match(line)
        if match:
            current = _recognized_version(match.group(1))
        governing.append(current)
    return governing


def _iter_added_lines(diff_text: str) -> Iterator[tuple[int, str]]:
    """Yield (line_number, content) pairs for each addition in a unified diff.

    Parses unified-diff format, tracking hunk headers to maintain accurate
    line numbers in the new file, and yields only the content of lines
    starting with ``+`` (excluding ``+++`` file headers and ``-`` deletions).

    Args:
        diff_text: A unified diff in the standard format (output of
            ``git diff``, etc.).

    Yields:
        Tuples of (1-indexed line number in the new file, the added content
        after the ``+`` prefix).
    """
    new_lineno = 0
    for line in diff_text.splitlines():
        hunk = _HUNK_NEW_START_RE.match(line)
        if hunk:
            new_lineno = int(hunk.group(1)) - 1
            continue
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("-"):
            continue
        # ``\ No newline at end of file`` markers consume no new-file line;
        # counting one would shift attribution for the rest of the hunk.
        if line.startswith("\\"):
            continue
        new_lineno += 1
        if line.startswith("+"):
            yield (new_lineno, line[1:])


def _stranded_from_added(
    governing: list[str | None],
    added_lines: Iterator[tuple[int, str]],
    tag: tuple[int, int, int] | None,
) -> list[str]:
    """Detect released headings that received new content in a diff.

    Iterates over added lines, looks up each line's governing version from
    the governing list, and accumulates distinct versions that are at or
    below *tag* (released before or at the tagging point). Skips blank
    lines and lines that are themselves headings (only non-heading content
    counts as "stranded" — a new heading under a released heading is normal).

    Args:
        governing: Output of :func:`_governing_versions` — a parallel list
            mapping line numbers to governing versions.
        added_lines: Iterator of (line_no, content) from
            :func:`_iter_added_lines`.
        tag: The parsed semver of the latest release tag, or ``None``.

    Returns:
        Distinct stranded versions in file order; empty when none.
    """
    if tag is None:
        return []
    stranded: list[str] = []
    for lineno, added in added_lines:
        if not added.strip() or _ANY_HEADING_RE.match(added):
            continue
        index = lineno - 1
        version = governing[index] if 0 <= index < len(governing) else None
        if version is None:
            continue
        parsed = parse_semver(version)
        if parsed is not None and parsed <= tag and version not in stranded:
            stranded.append(version)
    return stranded


# --- no-version opt-out -----------------------------------------------------

# Truthy values for the opt-out env vars; a leftover `=0` / `=false`
# does NOT opt out.
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})

# `NO_VERSION` is the version-aware name; `SKIP_CHANGELOG_CHECK` is the
# original changelog-gate spelling, kept for back-compat. These opt-out
# vars are LOCAL-ONLY — absent in CI — which is why the branch-token
# (with its `GITHUB_HEAD_REF` fallback) and commit-tag signals below
# exist.
_NO_VERSION_ENV_VARS = ("NO_VERSION", "SKIP_CHANGELOG_CHECK")

# `no-version` as a whole delimited token in a branch name, bounded by
# `/`, `-`, or the ends: matches `chore/tidy-no-version` and
# `no-version/ci-fix`, NOT `fix/no-versioning`.
_NO_VERSION_BRANCH_RE = re.compile(r"(?:^|[/-])no-version(?:$|[/-])", re.IGNORECASE)

_NO_VERSION_COMMIT_MARKER = "[no-version]"


def _env_no_version() -> str | None:
    """Return the name of the first truthy opt-out env var, or ``None``."""
    for name in _NO_VERSION_ENV_VARS:
        value = os.environ.get(name)
        if value is not None and value.strip().lower() in _TRUTHY_ENV:
            return name
    return None


def wants_no_version(repo_root: Path) -> str | None:
    """Return the fired no-version signal, or ``None`` when none is set.

    The CI-durable opt-out for "this change doesn't deserve a version":
    three signals, any one suffices —

    1. **env** — ``NO_VERSION`` / ``SKIP_CHANGELOG_CHECK`` truthy
       (local-only; absent in CI).
    2. **branch token** — ``no-version`` as a delimited token in the
       current branch name. ``git branch --show-current`` wins when
       non-empty; on a detached HEAD (a CI ``pull_request`` checkout of
       ``refs/pull/N/merge``) it is empty, so the PR source branch is
       read from ``GITHUB_HEAD_REF`` instead — the branch token is
       CI-durable only through that fallback.
    3. **commit tag** — ``[no-version]`` (case-insensitive) in any
       commit message over ``<base>..HEAD``, base resolved via
       :func:`forge.git_utils.resolve_base_branch_ref` (origin-first,
       local fallback) from ``[tool.forge].base_branch`` — CI checks
       out a detached ``refs/pull/N/merge`` with no local base branch,
       but the fetch creates ``origin/<base>``, so detection stays
       durable there.

    Args:
        repo_root: Git repo root.

    Returns:
        A short human-readable description of the signal that fired
        (truthy — usable directly in a skip message), or ``None``.
    """
    env_name = _env_no_version()
    if env_name is not None:
        return f"{env_name} env var set"
    # The branch/config reads below may repeat work a caller already did
    # (step_changelog_updated reads both) — accepted: they are cheap
    # once-per-commit calls, and threading them in would complicate the
    # single-arg public signature consumers import.
    resolved = resolve_current_branch(repo_root)
    if resolved is not None:
        branch, source = resolved
        if _NO_VERSION_BRANCH_RE.search(branch):
            origin = " (GITHUB_HEAD_REF)" if source == "GITHUB_HEAD_REF" else ""
            return f"`no-version` token in branch name {branch!r}{origin}"
    base_ref = resolve_base_branch_ref(repo_root, load_config(repo_root).base_branch)
    if base_ref is None:
        return None
    log = run_git("log", f"{base_ref}..HEAD", "--format=%B", cwd=repo_root, check=False)
    if _NO_VERSION_COMMIT_MARKER in log.lower():
        return f"{_NO_VERSION_COMMIT_MARKER} tag in a commit message ({base_ref}..HEAD)"
    return None


def stranded_added_versions(
    text: str, diff_text: str, latest_tag: str | None
) -> list[str]:
    """Return released heading versions that *diff_text* adds entries under.

    The stranded-entries race: a release tag is cut while a PR is open,
    so the PR's bullets sit under a now-released ``## vX.Y.Z`` heading —
    no merge conflict signals it, and the global top-vs-tag check passes
    on equality. This maps each ``+`` line of a unified diff of
    ``CHANGELOG.md`` to its governing heading in *text* and reports the
    headings at or below *latest_tag* that received non-heading content.

    Args:
        text: Full current ``CHANGELOG.md`` contents (the diff's new side).
        diff_text: Unified diff of ``CHANGELOG.md`` against the base
            branch (new side must match *text*).
        latest_tag: Latest ``v*`` tag, or ``None`` (no tags → nothing can
            be stranded; returns empty).

    Returns:
        Distinct stranded versions in file order; empty when none.
    """
    tag = parse_semver(latest_tag) if latest_tag is not None else None
    if tag is None:
        return []
    governing = _governing_versions(text)
    return _stranded_from_added(governing, _iter_added_lines(diff_text), tag)
