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
    from pathlib import Path


_HEADING_RE = re.compile(r"^##\s+(v\d+\.\d+\.\d+)\b", re.MULTILINE)

# Any level-2 heading, version-shaped or not — the changelog_version
# pre-commit step validates every ``## `` heading against the release
# recognizer, so a stray ``## Unreleased`` or ``## v1.2`` is flagged
# rather than silently ignored.
_ANY_HEADING_RE = re.compile(r"^##\s+(\S.*?)\s*$", re.MULTILINE)

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

# The two CI-durable no-version spellings are package-public (shared by
# every forge module that emits or reads the signal — not part of the
# consumer import-surface table in docs/consumer-release.md): writers
# (e.g. forge-resync branding its branch/commit) import these constants
# instead of re-spelling the literals, so reader and writer can never
# drift.
NO_VERSION_BRANCH_TOKEN = "no-version"  # noqa: S105

NO_VERSION_COMMIT_MARKER = "[no-version]"

# The branch token as a whole delimited token, bounded by `/`, `-`, or
# the ends: matches `chore/tidy-no-version` and `no-version/ci-fix`,
# NOT `fix/no-versioning`. Derived from the public constant — one
# spelling.
_NO_VERSION_BRANCH_RE = re.compile(
    rf"(?:^|[/-]){re.escape(NO_VERSION_BRANCH_TOKEN)}(?:$|[/-])", re.IGNORECASE
)


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
    if NO_VERSION_COMMIT_MARKER in log.lower():
        return f"{NO_VERSION_COMMIT_MARKER} tag in a commit message ({base_ref}..HEAD)"
    return None


def _section_content(text: str) -> dict[str, set[str]]:
    """Map each release version to its normalized non-heading content lines.

    Args:
        text: Full ``CHANGELOG.md`` contents.

    Returns:
        Stripped, non-blank, non-heading lines per governing version.
    """
    sections: dict[str, set[str]] = {}
    for line, version in zip(text.splitlines(), _governing_versions(text), strict=True):
        if version is None:
            continue
        stripped = line.strip()
        if not stripped or _ANY_HEADING_RE.match(line):
            continue
        sections.setdefault(version, set()).add(stripped)
    return sections


def stranded_added_versions(
    old_text: str, new_text: str, latest_tag: str | None
) -> list[str]:
    """Return released versions whose sections gained content vs *old_text*.

    The stranded-entries race: a release tag is cut while a PR is open,
    so the PR's bullets sit under a now-released ``## vX.Y.Z`` heading —
    no merge conflict signals it, and the global top-vs-tag check passes
    on equality. Detection compares each side's heading→content
    **membership** rather than attributing raw diff ``+`` lines to the
    textually-preceding heading: git renders a valid restrand (a new
    heading inserted above byte-identical entries) as a heading rename
    plus a re-insert of the old heading lower down, which line
    attribution false-flags but membership comparison sees as a strict
    shrink. Accepted biases of set membership: a line merely reworded
    under a released heading still counts as a gain (false positive —
    cheap re-run), and a newly added bullet whose text is byte-identical
    to one already in the same released section collapses into it and
    goes undetected (narrow false negative, traded for not flagging
    reorders and restrands).

    Args:
        old_text: ``CHANGELOG.md`` contents at the comparison point — the
            merge base (pre-commit step) or the released tag
            (``forge-release``).
        new_text: Full current ``CHANGELOG.md`` contents.
        latest_tag: Latest ``v*`` tag, or ``None`` (no tags → nothing can
            be stranded; returns empty).

    Returns:
        Distinct stranded versions in file order; empty when none.
    """
    tag = parse_semver(latest_tag) if latest_tag is not None else None
    if tag is None:
        return []
    old_sections = _section_content(old_text)
    new_sections = _section_content(new_text)
    stranded: list[str] = []
    for version in _governing_versions(new_text):
        if version is None or version in stranded:
            continue
        parsed = parse_semver(version)
        if parsed is None or parsed > tag:
            continue
        if new_sections.get(version, set()) - old_sections.get(version, set()):
            stranded.append(version)
    return stranded


def released_deleted_versions(
    old_text: str, new_text: str, latest_tag: str | None
) -> list[str]:
    """Return released versions whose sections lost content vs *old_text*.

    The inverse blind spot of :func:`stranded_added_versions`: only
    additions under a released heading were rejected, so an edit that
    deletes lines from — or removes outright — a section at or below the
    latest tag passed the gate silently, erasing a shipped entry (#363).
    Membership comparison carries the same accepted biases as the added
    direction, mirrored: a reworded line counts as a loss (cheap false
    positive), and deleting one of two byte-identical bullets goes
    undetected.

    Args:
        old_text: ``CHANGELOG.md`` contents at the comparison point (the
            merge base).
        new_text: Full current ``CHANGELOG.md`` contents.
        latest_tag: Latest ``v*`` tag, or ``None`` (no tags → nothing is
            released; returns empty).

    Returns:
        Distinct released versions with removed content, in *old_text*
        file order; empty when none.
    """
    tag = parse_semver(latest_tag) if latest_tag is not None else None
    if tag is None:
        return []
    old_sections = _section_content(old_text)
    new_sections = _section_content(new_text)
    shrunk: list[str] = []
    for version in _governing_versions(old_text):
        if version is None or version in shrunk:
            continue
        parsed = parse_semver(version)
        if parsed is None or parsed > tag:
            continue
        if old_sections.get(version, set()) - new_sections.get(version, set()):
            shrunk.append(version)
    return shrunk
