"""forge-release — cut an annotated ``vX.Y.Z`` release tag for tag-versioned repos.

The single-track counterpart to forge's own rolling-next flow: for a
repo whose version is *derived from* its ``v*`` tags (setuptools-scm)
rather than driven by a ``.claude-plugin/plugin.json`` manifest, the tag
IS the release. This CLI owns the orchestration so consumers don't
reimplement it: guard checks, semver bump off the latest ``v*`` tag, a
CHANGELOG gate, then annotated tag + push.

Guards, in order (all failures reported at once, exit ``1``):

1. **Clean working tree** — a release tag must point at committed state.
2. **On the base branch** (``[tool.forge].base_branch``, default
   ``main``) — single-track releases are cut from the trunk.
3. **Single-track release model** — refuses on a dual-track repo
   (``dev_branch != base_branch``; promote via its release flow instead)
   or when a ``.claude-plugin/plugin.json`` manifest drives versioning
   (use ``forge-next-prep --tag``). Two different version sources, two
   different orchestrators.
4. **CHANGELOG gate** — when ``CHANGELOG.md`` exists it must already
   carry a ``## vX.Y.Z`` heading for the tag being cut. A repo with no
   CHANGELOG gets a warning, not a failure.

``--dry-run`` reports the tag that would be cut and stops before
mutating anything. Exits ``0`` on success or dry-run, ``1`` on guard
failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from forge.changelog import changelog_lacks_entry
from forge.config import ForgeConfig, load_config
from forge.git_utils import (
    configure_cli_logging,
    latest_v_tag,
    next_version,
    read_local_plugin_version,
    run_git,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


def _dirty_tree_error(repo_root: Path) -> str | None:
    """Return an error when the working tree has uncommitted changes.

    Args:
        repo_root: Repo root for the git invocation.

    Returns:
        One-line error string, or ``None`` when the tree is clean.
    """
    if run_git("status", "--porcelain", cwd=repo_root, check=False):
        return "Working tree is dirty — commit or stash before tagging a release."
    return None


def _wrong_branch_error(repo_root: Path, base_branch: str) -> str | None:
    """Return an error when ``HEAD`` is not on *base_branch*.

    Args:
        repo_root: Repo root for the git invocation.
        base_branch: The configured release trunk (e.g. ``"main"``).

    Returns:
        One-line error string, or ``None`` when on *base_branch*.
    """
    current = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_root, check=False)
    if current != base_branch:
        return (
            f"On branch {current or '(detached HEAD)'!s}, not {base_branch} — "
            f"single-track releases are tagged on {base_branch}."
        )
    return None


def _wrong_release_model_error(repo_root: Path, cfg: ForgeConfig) -> str | None:
    """Return an error when this repo's release model isn't single-track.

    Two disqualifiers, checked independently: a dual-track branch config
    (releases reach base via promotion, not direct tagging) and a plugin
    manifest (the version source is ``plugin.json``, owned by
    ``forge-next-prep --tag``). Manifest presence alone is not the
    signal — a dual-track repo without a manifest must also be refused.

    Args:
        repo_root: Repo root.
        cfg: Loaded ``[tool.forge]`` configuration.

    Returns:
        One-line error string, or ``None`` when single-track tag-versioned.
    """
    if cfg.dual_track:
        return (
            f"Dual-track repo ({cfg.dev_branch} → {cfg.base_branch}) — "
            "releases are cut by the promotion flow, not forge-release."
        )
    if read_local_plugin_version(repo_root) is not None:
        return (
            ".claude-plugin/plugin.json drives this repo's versioning — "
            "use `forge-next-prep --tag` (rolling-next flow) instead."
        )
    return None


def _changelog_gate_error(repo_root: Path, tag: str) -> str | None:
    """Return an error when ``CHANGELOG.md`` exists but lacks *tag*'s entry.

    A repo that keeps no CHANGELOG is warned, not blocked — the gate
    enforces curation where curation is practiced.

    Args:
        repo_root: Repo root.
        tag: Release tag about to be cut, e.g. ``"v1.3.0"``.

    Returns:
        One-line error string, or ``None`` when the entry is present or
        no ``CHANGELOG.md`` exists.
    """
    changelog = repo_root / "CHANGELOG.md"
    if not changelog.is_file():
        logger.warning("No CHANGELOG.md — skipping the CHANGELOG gate.")
        return None
    if changelog_lacks_entry(changelog.read_text(encoding="utf-8"), tag):
        return (
            f"CHANGELOG.md has no `## {tag}` entry — author the release "
            f"notes before tagging."
        )
    return None


def _cut_release(repo_root: Path, tag: str) -> None:
    """Create the annotated *tag* on ``HEAD`` and push it to ``origin``.

    Args:
        repo_root: Repo root.
        tag: Release tag to create, e.g. ``"v1.3.0"``.
    """
    run_git("tag", "-a", tag, "-m", tag, "HEAD", cwd=repo_root)
    if run_git("remote", "get-url", "origin", cwd=repo_root, check=False):
        run_git("push", "origin", tag, cwd=repo_root)
        logger.info("Tagged and pushed %s", tag)
    else:
        logger.warning("Tagged %s locally — no `origin` remote to push to.", tag)


def main() -> int:
    """Cut the next ``vX.Y.Z`` release tag off the latest ``v*`` tag.

    Returns:
        ``0`` on success or ``--dry-run``, ``1`` when any guard refuses.
    """
    parser = argparse.ArgumentParser(
        prog="forge-release",
        description=(
            "Cut a vX.Y.Z release tag for a single-track, tag-versioned "
            "(setuptools-scm) repo: clean tree + on base branch + CHANGELOG "
            "entry present, then annotated tag + push."
        ),
    )
    parser.add_argument(
        "--bump",
        required=True,
        choices=("major", "minor", "patch"),
        help="Semver increment to apply to the latest v* tag.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the tag that would be cut and exit without tagging.",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    cfg = load_config(repo_root)

    run_git("fetch", "--tags", "--quiet", "origin", cwd=repo_root, check=False)
    latest = latest_v_tag(repo_root)
    tag = next_version(latest, args.bump)

    errors = [
        err
        for err in (
            _dirty_tree_error(repo_root),
            _wrong_branch_error(repo_root, cfg.base_branch),
            _wrong_release_model_error(repo_root, cfg),
            _changelog_gate_error(repo_root, tag),
        )
        if err
    ]
    if errors:
        for err in errors:
            logger.error("%s", err)
        return 1

    logger.info(
        "Latest tag: %s → next (%s bump): %s", latest or "(none)", args.bump, tag
    )
    if args.dry_run:
        logger.info("Dry run — no tag created.")
        return 0

    _cut_release(repo_root, tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
