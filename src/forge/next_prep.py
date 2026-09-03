"""forge-next-prep — prepare main for the next task (fetch, pull, tag, prune).

Single source of truth for the mechanical git work the ``/next`` skill
runs at the start of a fresh task. Extracted from inline bash in the
skill so the version-compare + tag-bump logic is testable and reusable.

The skill remains responsible for user-interaction: refusing to run on a
dirty tree, confirming destructive steps, presenting the report.

Operations (in order, each idempotent):

1. ``git fetch --prune`` — refresh remote tracking.
2. ``git switch <target>`` (``git checkout`` fallback for git < 2.23),
   then ``git pull --ff-only`` — sync to latest.
3. **Optional auto-tag** (``--tag``): if ``.claude-plugin/plugin.json``
   has a ``version`` strictly ahead of the latest ``v*`` tag, tag the
   merge commit and push the tag. Forge's rolling-next workflow. On a
   single-track repo with no plugin manifest the flag warns and skips
   (per-merge tagging is a plugin-repo pattern — ``forge-release`` cuts
   release tags there).
4. **Prune stale branches** (``--prune-branches``, default ON): delete
   local branches whose remote shows ``[origin/...: gone]``. Uses
   ``git branch -d`` (safe) — never ``-D``.

Exits 0 on success, 1 if the target branch can't fast-forward
(divergent state).
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path

from forge.changelog_fragments import discover_fragments
from forge.config import (
    is_fragments_mode,
    load_config,
)
from forge.git_utils import (
    configure_cli_logging,
    create_annotated_tag,
    fetch_tags_best_effort,
    latest_v_tag,
    parse_semver,
    read_local_plugin_version,
    run_git,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


# Applied via fullmatch — a bare `$` would tolerate one trailing newline.
_SEMVER_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_GONE_BRANCH_RE = re.compile(r"^\*?\s*(\S+)\s+[0-9a-f]+\s+\[origin/\S+: gone\]")


def _is_newer(plugin_ver: str, latest_tag: str | None) -> bool:
    """Return True when ``v<plugin_ver>`` would sort *after* ``latest_tag``.

    Compares parsed semver tuples (shared parser from
    ``forge.git_utils.parse_semver``) — no subprocess.

    Args:
        plugin_ver: Bare semver string from plugin.json.
        latest_tag: Latest existing ``v*`` tag or ``None``.

    Returns:
        ``True`` when ``plugin_ver`` is strictly newer than ``latest_tag``
        (or when no tags exist yet, or when ``latest_tag`` is unparseable
        as semver).
    """
    if latest_tag is None:
        return True
    plugin_tuple = parse_semver(plugin_ver)
    tag_tuple = parse_semver(latest_tag)
    if plugin_tuple is None:
        return False  # malformed plugin.json — guarded upstream, defensive here
    if tag_tuple is None:
        return True  # non-semver tag (e.g. "v0.1-pre") — treat plugin as newer
    return plugin_tuple > tag_tuple


def tag_staleness_warning(repo_root: Path) -> str | None:
    """Return a warning when the integration branch owes a rolling-next tag.

    Fires only on the configured ``base_branch`` and only when
    ``plugin.json``'s version is strictly newer than the latest ``v*``
    tag — i.e. a merge bumped the rolling-next version but
    ``forge-next-prep --tag`` was never run, so the tag silently lags and
    the pre-commit guard keeps passing without forcing the next bump. This
    is the advisory the ``forge-post-merge`` hook surfaces on ``git pull``.
    Detection only — it never tags or pushes (an irreversible release must
    stay an explicit human action).

    Args:
        repo_root: Git repo root.

    Returns:
        A one-line warning string, or ``None`` when not on ``base_branch``,
        when there is no plugin manifest, or when the tag is already
        current.
    """
    current = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_root, check=False)
    if not current or current != load_config(repo_root).base_branch:
        return None
    plugin_ver = read_local_plugin_version(repo_root)
    if plugin_ver is None:
        return None
    latest = latest_v_tag(repo_root)
    if not _is_newer(plugin_ver, latest):
        return None
    return (
        f"plugin.json {plugin_ver} is ahead of the latest tag "
        f"{latest or '(none)'} — run `forge-next-prep --tag` to tag this release."
    )


def _tag_misuse_warning(repo_root: Path) -> str | None:
    """Return a warning when ``--tag`` is used outside the rolling-next model.

    Per-merge tagging is the plugin-repo pattern: the rolling-next
    manifest names the version to tag. A single-track repo with no
    ``.claude-plugin/plugin.json`` releases via ``forge-release``
    instead, so ``--tag`` there is almost always a command copied from
    forge's own workflow — warn loudly rather than no-op silently.

    Args:
        repo_root: Repo root.

    Returns:
        A one-line warning when the repo has no plugin manifest;
        ``None`` when ``--tag`` is applicable.
    """
    if read_local_plugin_version(repo_root) is not None:
        return None
    return (
        "--tag skipped: no .claude-plugin/plugin.json and a single-track "
        "branch model — per-merge tagging is the rolling-next plugin-repo "
        "pattern. Cut release tags with `forge-release` instead "
        "(docs/consumer-release.md)."
    )


def _maybe_tag_release(repo_root: Path) -> str | None:
    """Tag and push ``v<plugin.json.version>`` when newer than the latest tag.

    Idempotent: no-op when plugin.json is missing, the version field is
    non-semver, or the version is not strictly ahead of the latest tag.

    Args:
        repo_root: Repo root.

    Returns:
        The tag name on success (e.g. ``"v1.2.10"``), or ``None`` when
        no tagging was needed / possible.
    """
    plugin_ver = read_local_plugin_version(repo_root)
    if plugin_ver is None:
        return None
    latest = latest_v_tag(repo_root)
    if not _is_newer(plugin_ver, latest):
        return None
    tag = f"v{plugin_ver}"
    create_annotated_tag(repo_root, tag)
    run_git("push", "origin", tag, cwd=repo_root)
    return tag


def _gone_branches(repo_root: Path) -> list[str]:
    """Return local branch names whose tracking remote is ``[origin/...: gone]``.

    Args:
        repo_root: Repo root.

    Returns:
        Branch names (no leading ``* `` star, no whitespace). Empty list
        when nothing is gone or no branches exist.
    """
    raw = run_git("branch", "-vv", cwd=repo_root, check=False)
    out: list[str] = []
    for line in raw.splitlines():
        match = _GONE_BRANCH_RE.match(line)
        if match:
            out.append(match.group(1))
    return out


def _prune_gone_branches(repo_root: Path) -> tuple[list[str], list[str]]:
    """``git branch -d`` every branch whose remote is gone.

    Safe ``-d`` only — never ``-D``. Branches with unmerged commits are
    reported but not deleted.

    Args:
        repo_root: Repo root.

    Returns:
        Tuple ``(deleted, skipped)`` of branch names.
    """
    deleted: list[str] = []
    skipped: list[str] = []
    for branch in _gone_branches(repo_root):
        proc = subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            deleted.append(branch)
        else:
            skipped.append(branch)
    return deleted, skipped


def _log_prune_result(repo_root: Path) -> None:
    """Prune stale local branches and log the outcome.

    Args:
        repo_root: Working directory for git operations.
    """
    deleted, skipped = _prune_gone_branches(repo_root)
    if deleted:
        logger.info("Pruned stale branches: %s", ", ".join(deleted))
    if skipped:
        logger.warning(
            "Skipped branches with unmerged commits (use -D manually if "
            "you really want to drop them): %s",
            ", ".join(skipped),
        )
    if not deleted and not skipped:
        logger.info("No stale branches to prune.")


def main() -> int:
    """Refresh main, optionally tag the release, prune stale local branches.

    The target branch defaults to ``main`` for repos without
    ``[tool.forge]``. Forge's own repo overrides this internally to
    support its release workflow — that's not a pattern consumers need
    to replicate.

    Returns:
        ``0`` on success, ``1`` when the target branch cannot
        fast-forward (divergent state — user intervention needed).
    """
    parser = argparse.ArgumentParser(
        prog="forge-next-prep",
        description=(
            "Prepare main for the next task: fetch + pull --ff-only, "
            "optionally tag the rolling-next release, prune stale local "
            "branches. Used by the /next skill."
        ),
    )
    parser.add_argument(
        "--tag",
        action="store_true",
        help=(
            "Tag plugin.json's version when it's ahead of the latest v* tag "
            "and push the tag (forge's rolling-next workflow). Off by default."
        ),
    )
    parser.add_argument(
        "--no-prune-branches",
        action="store_true",
        help="Skip the stale-branch prune step.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help=(
            "Skip the fetch/checkout/pull steps and operate on the current "
            "HEAD (tags are still fetched for the version comparison). For "
            "CI tag jobs that check out the exact validated SHA — syncing "
            "to the branch tip would re-introduce the race the pinned "
            "checkout exists to prevent."
        ),
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    cfg = load_config(repo_root)

    target_branch = cfg.base_branch

    if args.no_sync:
        # CI tag path: HEAD is already the exact commit CI validated;
        # only the tag set needs refreshing for the version comparison.
        for note in fetch_tags_best_effort(repo_root):
            logger.warning("%s", note)
        return _tag_and_report(repo_root, args)

    logger.info("Fetching from origin...")
    run_git("fetch", "--prune", cwd=repo_root)

    logger.info("Checking out %s and pulling...", target_branch)
    # Prefer ``git switch``: it operates only on branches, so it's
    # unambiguous when the branch name collides with a working-tree path
    # (e.g. a directory named ``dev/``, which forge itself has). Falls
    # back to ``git checkout`` for git < 2.23 where ``switch`` does not
    # exist. The fallback may still hit the collision on those older
    # gits — a contributor seeing it should upgrade.
    # ``--`` end-of-options separator: defense-in-depth so a malformed
    # ``[tool.forge].base_branch`` value starting with ``-`` (e.g.
    # ``"--detach"``) is treated as a branch name, not a flag. Branch
    # names come from a repo-owned file so this is self-inflicted only,
    # but the guard is free for ``switch`` (no "branch vs path" overload
    # to confuse).
    proc = subprocess.run(
        ["git", "switch", "--", target_branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        run_git("checkout", target_branch, cwd=repo_root)
    proc = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        logger.error(
            "%s cannot fast-forward — divergent state.\n%s",
            target_branch,
            (proc.stdout + proc.stderr).strip(),
        )
        return 1

    return _tag_and_report(repo_root, args)


def _tag_and_report(repo_root: Path, args: argparse.Namespace) -> int:
    """Run the post-sync tail: optional tag, optional prune, advisory.

    Shared by the normal (synced) path and ``--no-sync``, so the tag /
    prune / promotion-advisory behavior cannot drift between them.

    Args:
        repo_root: Repo root.
        args: Parsed CLI namespace (``tag``, ``no_prune_branches``).

    Returns:
        Always ``0`` — failures in these steps raise.
    """
    if args.tag:
        misuse = _tag_misuse_warning(repo_root)
        if misuse:
            logger.warning(misuse)
        else:
            tag = _maybe_tag_release(repo_root)
            if tag:
                logger.info("Tagged and pushed %s", tag)
            else:
                logger.info("No release tag needed.")

    if not args.no_prune_branches:
        _log_prune_result(repo_root)

    # Fragment-accumulation advisory. Self-gating on fragments mode
    # (FOUNDATION §16, pattern C): pending changelog.d/ entries mean an
    # unreleased bump is waiting — surface the release command; silent
    # for shared-heading repos and when nothing is pending.
    if is_fragments_mode(repo_root):
        pending_fragments = len(discover_fragments(repo_root))
        if pending_fragments:
            logger.info(
                "%d pending changelog fragment(s) — release when ready: "
                "forge-changelog release",
                pending_fragments,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
