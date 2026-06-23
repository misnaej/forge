"""Enforce that ``.claude-plugin/plugin.json["version"]`` > latest git tag.

Standalone phase CLI for the ``plugin_version`` step in the forge
pre-commit sequence. Implements the rolling-next invariant: the manifest
version always names the next release about to be tagged, so consumers
pinning by tag never receive a stale manifest.

Skipped when:
- ``.claude-plugin/plugin.json`` does not exist (consumer repo without
  a plugin manifest).
- The repo has no git tags yet (pre-release repo).
- ``HEAD``'s tree reproduces any published ``v*`` release tag — so a
  staged ``release/vX.Y.Z`` branch promoting an older minor still passes
  even when its ``plugin.json`` sits below the global-max tag.

``forge-precommit`` shells out to this CLI; agents may invoke it
standalone to refresh just ``plugin_version.log``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from forge.git_utils import (
    capturing_to_step_log,
    configure_cli_logging,
    get_tree_sha,
    latest_v_tag,
    parse_semver,
    read_local_plugin_version,
    run_git,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


# Re-exported alias preserved for backwards compatibility with internal
# imports (e.g. forge.next_prep) — the canonical implementation lives in
# forge.git_utils.parse_semver.
_parse_semver = parse_semver


def _is_release_commit(repo_root: Path) -> bool:
    """Return True when ``HEAD``'s tree reproduces ANY published ``v*`` tag.

    Compares the git **tree** SHA of ``HEAD`` against the tree of every
    ``v*`` tag — not commit SHAs. Tree equality means the working
    file-state reproduces an already-tagged release, so the rolling-next
    rule ("bump plugin.json past the latest tag") must NOT fire.

    Checking **every** tag — not only the latest — is load-bearing for
    the staged ``dev → main`` promotion (see the ``promote`` skill).
    When ``main`` is two or more minors behind, a ``release/vX.Y.Z``
    branch carries an *older* minor's tree, so its ``plugin.json`` sits
    legitimately **below** the global-max tag; it is still a real release
    commit and must pass the guard. A prior version compared HEAD only
    against the *latest* tag, which made promoting any minor below the
    global-max impossible — the release branch's tree never equals the
    latest tag's tree (regression from the #43 ancestry→global tag
    switch). **Do not narrow this back to a single tag** — the
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
        ``True`` when ``HEAD``'s tree SHA equals the tree of some ``v*``
        tag; ``False`` when HEAD's tree resolves emptily or matches none.
    """
    head_tree = get_tree_sha(repo_root, "HEAD")
    if head_tree is None:
        return False
    tags = run_git("tag", "--list", "v*", cwd=repo_root, check=False).split()
    return any(get_tree_sha(repo_root, tag) == head_tree for tag in tags)


def main() -> int:
    """Enforce plugin.json version > latest git tag.

    Returns:
        ``0`` on success or when skipped. ``1`` when ``plugin.json["version"]``
        is not strictly ahead of the latest semver-style tag, or when either
        version string is unparseable.
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
        if latest_tag is None:
            logger.info("(no git tags yet — skipped)")
            return 0

        if _is_release_commit(repo_root):
            logger.info("(HEAD reproduces a published v* release tag — skipped)")
            return 0

        plugin_version_str = read_local_plugin_version(repo_root)
        tag_ver = _parse_semver(latest_tag)
        plugin_ver = _parse_semver(plugin_version_str) if plugin_version_str else None
        if tag_ver is None or plugin_ver is None:
            logger.error(
                "plugin_version: cannot compare. latest tag=%r, plugin.json version=%r",
                latest_tag,
                plugin_version_str,
            )
            return 1
        if plugin_ver <= tag_ver:
            logger.error(
                "plugin.json version %s must be strictly greater than the latest "
                "tag %s (%s). Bump .claude-plugin/plugin.json before the next "
                "commit.",
                plugin_ver,
                latest_tag,
                tag_ver,
            )
            return 1
        logger.info(
            "plugin.json %s > latest tag %s (%s)", plugin_ver, latest_tag, tag_ver
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
