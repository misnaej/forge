"""Enforce the rolling-next and declared-version manifest invariants.

Standalone phase CLI for the ``plugin_version`` step in the forge
pre-commit sequence. Two invariants, both required for a healthy exit:

1. **Rolling-next**: ``.claude-plugin/plugin.json["version"]`` > latest
   git tag. The manifest version always names the next release about to
   be tagged, so consumers pinning by tag never receive a stale
   manifest.
2. **Declared-version documented**: whatever version the manifest
   declares has a matching ``## vX.Y.Z`` heading in ``CHANGELOG.md``, so
   the identity Claude Code keys its plugin cache on is always one
   consumers can read about (see :func:`_declared_version_documented`).

Both invariants are skipped when:
- ``.claude-plugin/plugin.json`` does not exist (consumer repo without
  a plugin manifest) — nothing declares a version, so neither rule has
  a subject.

Only the rolling-next invariant is skipped — declared-version
documentation is still checked — when:
- The repo has no git tags yet (pre-release repo): there is no tag to
  be newer than, but a declared version still has to be one consumers
  can read about. A repo with no ``CHANGELOG.md`` passes anyway.
- ``HEAD``'s release fingerprint (tree content minus ``CHANGELOG.md``)
  reproduces any published ``v*`` release tag — so a staged
  ``release/vX.Y.Z`` branch promoting an older minor still passes even
  when its ``plugin.json`` sits below the global-max tag, and even when it
  finalizes the curated ``@main`` CHANGELOG entry.

Fragment mode (``[tool.forge.changelog].mode = "fragments"``): the
manifest parks at or behind the latest tag between assembly PRs — the
bump lives in ``changelog.d/`` fragments, tag-per-merge (``forge-changelog
auto-tag``) advances tags past the manifest, and ``forge-changelog
release`` is the single writer that re-syncs ``plugin.json``.
``plugin.json <= tag`` is therefore healthy, provided every pending
fragment passes the gate (the next version must stay derivable);
below-tag stays an error only in shared-heading mode.

``forge-precommit`` shells out to this CLI; agents may invoke it
standalone to refresh just ``plugin_version.log``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from forge.changelog import changelog_lacks_entry
from forge.changelog_fragments import check_pending, discover_fragments
from forge.config import is_fragments_mode
from forge.git_utils import (
    capturing_to_step_log,
    configure_cli_logging,
    latest_v_tag,
    parse_semver,
    read_local_plugin_version,
    release_tree_fingerprint,
    run_git,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


# Module-internal alias for the canonical forge.git_utils.parse_semver,
# used by main() and referenced by this module's tests.
_parse_semver = parse_semver


def _is_release_commit(repo_root: Path) -> bool:
    """Return True when ``HEAD``'s release fingerprint matches ANY published ``v*`` tag.

    Compares the **release fingerprint** (tree content minus
    ``CHANGELOG.md``, see :func:`forge.git_utils.release_tree_fingerprint`)
    of ``HEAD`` against every ``v*`` tag — not commit SHAs. Fingerprint
    equality means the working file-state reproduces an already-tagged
    release, so the rolling-next rule ("bump plugin.json past the latest
    tag") must NOT fire. CHANGELOG.md is excluded because a promotion's
    ``release/vX.Y.Z`` branch finalizes the curated ``@main`` CHANGELOG
    entry (release-process.md §5), diverging that one file from the tagged
    ``dev`` release while remaining the same release; any *other* file
    difference still makes HEAD a non-release commit that must bump.

    Checking **every** tag — not only the latest — is load-bearing for
    the staged ``dev → main`` promotion (see the ``promote`` skill).
    When ``main`` is two or more minors behind, a ``release/vX.Y.Z``
    branch carries an *older* minor's tree, so its ``plugin.json`` sits
    legitimately **below** the global-max tag; it is still a real release
    commit and must pass the guard. Narrowing to a single (global-max)
    tag breaks staged catch-up: a release branch for an older minor
    reproduces that minor's tree, which never equals the latest tag's
    tree. **Do not narrow this back to a single tag** — the
    ``test_main_skips_when_head_reproduces_older_tag`` test locks it.

    Cases that correctly skip:

    1. The literal release commit (HEAD == a tag commit) — same tree.
    2. A staged ``release/vX.Y.Z`` promotion branch reproducing an older
       tag's tree (``plugin.json`` below the global-max tag).
    3. A ``-s ours`` merge / empty commit / net-zero revert — tree
       unchanged from a tagged release.

    Args:
        repo_root: Git repo root.

    Returns:
        ``True`` when ``HEAD``'s release fingerprint equals that of some
        ``v*`` tag; ``False`` when HEAD's tree resolves emptily or matches
        none.
    """
    head_fp = release_tree_fingerprint(repo_root, "HEAD")
    if head_fp is None:
        return False
    tags = run_git("tag", "--list", "v*", cwd=repo_root, check=False).split()
    return any(release_tree_fingerprint(repo_root, tag) == head_fp for tag in tags)


def main() -> int:
    """Enforce the rolling-next and declared-version manifest invariants.

    Returns:
        ``0`` on success or when skipped — including fragment mode
        parked at the latest tag with valid pending fragments (see
        :func:`_not_ahead_verdict`). ``1`` when ``plugin.json["version"]``
        is below the latest semver-style tag, at the tag outside a
        healthy fragment-mode parked state, when either version string
        is unparseable, or when the declared version has no matching
        heading in ``CHANGELOG.md`` (see
        :func:`_declared_version_documented`).
    """
    argparse.ArgumentParser(
        prog="verify-forge-plugin-version",
        description=(
            "Assert .claude-plugin/plugin.json['version'] is strictly "
            "greater than the latest git tag. Writes "
            "code_health/plugin_version.log."
        ),
    ).parse_args()

    repo_root = Path.cwd()
    with capturing_to_step_log(repo_root, "plugin_version"):
        plugin = repo_root / ".claude-plugin" / "plugin.json"
        if not plugin.is_file():
            logger.info("(no .claude-plugin/plugin.json — skipped)")
            return 0

        # Global semver-max ``v*`` tag, NOT ancestry-scoped ``git
        # describe`` — the guard and the auto-tagger (forge-next-prep)
        # must resolve "latest release" the same way, or they disagree in
        # the dual-track case (a release tagged on main is absent from
        # dev's history). See forge.git_utils.latest_v_tag.
        latest_tag = latest_v_tag(repo_root)
        plugin_version_str = read_local_plugin_version(repo_root)
        if latest_tag is None:
            # No tag to be newer than, so rolling-next has nothing to
            # say — but a declared version still owes its heading.
            logger.info("(no git tags yet — rolling-next skipped)")
            return _declared_version_documented(repo_root, plugin_version_str)

        tag_ver = _parse_semver(latest_tag)
        plugin_ver = _parse_semver(plugin_version_str) if plugin_version_str else None
        if tag_ver is None or plugin_ver is None:
            logger.error(
                "plugin_version: cannot compare. latest tag=%r, plugin.json version=%r",
                latest_tag,
                plugin_version_str,
            )
            return 1
        # An ahead manifest is the release window (shared-heading mode's
        # common dev commit; fragments mode's release PR). In fragments
        # mode an ahead manifest with NEW pending fragments is a stale
        # release computation — a bump merged after `release` ran, and
        # tagging now would ship that change under the wrong version.
        if plugin_ver > tag_ver:
            if is_fragments_mode(repo_root) and discover_fragments(repo_root):
                logger.error(
                    "fragment mode: plugin.json %s is ahead of tag %s but "
                    "pending changelog.d/ fragments exist — the release "
                    "computation is stale (a bump merged after "
                    "`forge-changelog release` ran). Sync the base and "
                    "recompute the release before committing.",
                    plugin_ver,
                    latest_tag,
                )
                return 1
            logger.info(
                "plugin.json %s > latest tag %s (%s)", plugin_ver, latest_tag, tag_ver
            )
            rc = 0
        else:
            rc = _not_ahead_verdict(repo_root, plugin_ver, tag_ver, latest_tag)
        # Applied to every healthy exit, not to one branch: the
        # declared version is what consumers adopt, so it must be a
        # version they can read about.
        return rc or _declared_version_documented(repo_root, plugin_version_str)


def _declared_version_documented(repo_root: Path, version: str | None) -> int:
    """Refuse a declared plugin version that ``CHANGELOG.md`` never mentions.

    The manifest version is not bookkeeping: Claude Code keys its plugin
    cache on it, so it is the identity consumers actually adopt. A
    version nobody can read about is one they cannot evaluate before
    running it, and hand-editing the manifest is the way that happens —
    the assembler writes the version and its heading together, so its
    output always satisfies this.

    Scope, deliberately: this asks whether the declared version is
    *real*, never whether it is *current*. A manifest parked at an old
    version keeps its old heading and passes — staleness is the
    scheduled assembly's job, not this gate's.

    Args:
        repo_root: Git repo root.
        version: Version the manifest declares, or ``None`` when it
            declares none.

    Returns:
        ``0`` when the declared version has a heading, when no
        ``CHANGELOG.md`` exists, or when no version is declared;
        ``1`` when the heading is missing.
    """
    changelog = repo_root / "CHANGELOG.md"
    if not changelog.is_file() or not version:
        return 0
    tag = f"v{version}"
    if changelog_lacks_entry(changelog.read_text(encoding="utf-8"), tag):
        logger.error(
            "plugin_version: plugin.json declares %s but CHANGELOG.md has no "
            "`## %s` heading. Consumers adopt the declared version — it must "
            "be one they can read about. The assembler writes both together, "
            "so a mismatch means the manifest was edited by hand: revert it "
            "and let `forge-changelog release` write the version.",
            version,
            tag,
        )
        return 1
    logger.info("declared version %s is documented in CHANGELOG.md", tag)
    return 0


def _not_ahead_verdict(
    repo_root: Path,
    plugin_ver: tuple[int, int, int],
    tag_ver: tuple[int, int, int],
    latest_tag: str,
) -> int:
    """Resolve the verdict when ``plugin.json`` is NOT ahead of the latest tag.

    Args:
        repo_root: Git repo root.
        plugin_ver: Parsed ``plugin.json`` version.
        tag_ver: Parsed latest-tag version.
        latest_tag: Latest ``v*`` tag name (for messages).

    Returns:
        ``0`` when the state is healthy (release commit, or fragment
        mode parked at the tag with valid pending fragments); ``1``
        otherwise.
    """
    # Parked check first: in fragments mode EVERY ordinary commit sits at
    # or behind the tag (auto-tag advances tags per merge while the
    # manifest waits for the next assembly PR), so the fingerprint
    # fallback (per-tag `git ls-tree` over the whole tag set) must not
    # run on the common path. A release/assembly commit has zero pending
    # fragments and passes the parked check anyway.
    if is_fragments_mode(repo_root) and plugin_ver <= tag_ver:
        return _fragment_parked_verdict(repo_root, latest_tag)
    if _is_release_commit(repo_root):
        logger.info("(HEAD reproduces a published v* release tag — skipped)")
        return 0
    logger.error(
        "plugin.json version %s must be strictly greater than the latest "
        "tag %s (%s). Bump .claude-plugin/plugin.json before the next "
        "commit.",
        plugin_ver,
        latest_tag,
        tag_ver,
    )
    return 1


def _fragment_parked_verdict(repo_root: Path, latest_tag: str) -> int:
    """Gate the fragment-mode resting state: manifest at or behind the tag.

    Tag-per-merge advances tags on every fragment-carrying merge while
    the manifest syncs only at assembly PRs, so lagging the tag is the
    normal state — healthy while the next version stays derivable:
    every pending fragment must pass the gate. Zero pending fragments is
    also healthy (the just-assembled resting state).

    Args:
        repo_root: Git repo root.
        latest_tag: Latest ``v*`` tag name (for messages).

    Returns:
        ``0`` when every pending fragment is valid; ``1`` when any
        fragment fails validation (each error logged).
    """
    errors = check_pending(repo_root)
    if errors:
        for err in errors:
            logger.error("plugin_version: invalid fragment — %s", err)
        return 1
    logger.info(
        "fragment mode: manifest parked at/behind latest tag %s; %d pending "
        "fragment(s) carry the bump",
        latest_tag,
        len(discover_fragments(repo_root)),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
