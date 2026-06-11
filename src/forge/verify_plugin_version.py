"""Enforce that ``.claude-plugin/plugin.json["version"]`` > latest git tag.

Standalone phase CLI for the ``plugin_version`` step in the forge
pre-commit sequence. Implements the rolling-next invariant: the manifest
version always names the next release about to be tagged, so consumers
pinning by tag never receive a stale manifest.

Skipped when:
- ``.claude-plugin/plugin.json`` does not exist (consumer repo without
  a plugin manifest).
- The repo has no git tags yet (pre-release repo).
- ``HEAD`` is the release commit (the commit pointed to by the latest
  tag). On that single commit, ``plugin.json`` may equal the tag.

``forge-precommit`` shells out to this CLI; agents may invoke it
standalone to refresh just ``plugin_version.log``.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from forge.git_utils import capturing_to_step_log, configure_cli_logging, parse_semver


configure_cli_logging()
logger = logging.getLogger(__name__)


# Re-exported alias preserved for backwards compatibility with internal
# imports (e.g. forge.next_prep) — the canonical implementation lives in
# forge.git_utils.parse_semver.
_parse_semver = parse_semver


def _is_release_commit(repo_root: Path, tag: str) -> bool:
    """Return True when ``HEAD`` carries the same file content as *tag*.

    Compares the git **tree** SHA of ``HEAD`` against the tag's tree
    SHA — not the commit SHA. Tree equality means the working
    file-state is identical; commit identity is irrelevant. This is
    the right semantic for the rolling-next guard: the rule "you must
    bump plugin.json past the latest tag" only applies when the
    commit actually changes file content. Three cases that should
    skip and do:

    1. The literal release commit (HEAD == tag commit). Same commit,
       trivially same tree.
    2. A ``-s ours``-style merge that absorbs another branch with
       no file diff (e.g. dev → main promotion back-merges). The
       merge commit has a new SHA + new parents but its tree equals
       the pre-merge tree, which equals the tag's tree.
    3. ``git commit --allow-empty`` or a revert that nets to zero
       file change — same reasoning.

    Args:
        repo_root: Git repo root.
        tag: Tag name (e.g. ``"v1.1.2"``).

    Returns:
        ``True`` when ``HEAD``'s tree SHA equals *tag*'s tree SHA;
        ``False`` when either resolution fails or the trees differ.
    """
    tag_tree = subprocess.run(
        ["git", "rev-parse", f"{tag}^{{tree}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    head_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(tag_tree) and tag_tree == head_tree


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

        tag_proc = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if tag_proc.returncode != 0:
            logger.info("(no git tags yet — skipped)")
            return 0

        latest_tag = tag_proc.stdout.strip()
        if _is_release_commit(repo_root, latest_tag):
            logger.info("(HEAD is the %s release commit — skipped)", latest_tag)
            return 0

        plugin_data = json.loads(plugin.read_text())
        plugin_version_str = plugin_data.get("version", "")
        tag_ver = _parse_semver(latest_tag)
        plugin_ver = _parse_semver(plugin_version_str)
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
