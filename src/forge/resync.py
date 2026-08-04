"""forge-resync — regenerate managed artifacts and open a resync PR.

Forge-managed artifacts committed in consumer repos (``FOUNDATION.md``,
``docs/api-digest.md``, ``docs/cli-reference.md``, badges, hook
wrappers) drift on every forge release. This CLI owns the recurring
cleanup loop deterministically:

1. Preflight — clean working tree and ``gh`` required.
2. Dedup guard — an open ``chore/forge-resync-*`` PR already exists →
   report it and stop instead of opening a second.
3. Regenerate everything (``install-forge-bootstrap``, non-interactive
   steps self-skip per FOUNDATION §15).
4. No diff → "in sync", exit 0.
5. Diff → branch ``chore/forge-resync-<forge-version>``, commit, push,
   open a PR against ``[tool.forge].base_branch`` via ``gh``, then
   return to the starting branch.

It only ever pushes its own resync branch — protected branches are
never written. The PR body flags that mechanical regen does not surface
adoption-required changes; ``forge-upgrade --check`` lists those.

Invocation surfaces: manual run, a scheduled CI workflow
(``docs/ci-recipe.md``), and ``/next`` offering it on detected drift.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from importlib import metadata
from typing import TYPE_CHECKING

from forge.config import load_config
from forge.git_utils import configure_cli_logging, repo_root, require_cli, run_git
from forge.install_bootstrap import run_in_process as _bootstrap_run
from forge.run_context import progress_logger


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


_BRANCH_PREFIX = "chore/forge-resync-"

_PR_BODY = """\
Automated regeneration of forge-managed artifacts (`install-forge-bootstrap`)
after a forge release moved their canonical content.

**Review note:** this PR is mechanical regen only. New forge capabilities
(new CLIs, opt-in pre-commit steps, config keys) and contract changes are
NOT surfaced by regen — run `forge-upgrade --check` for pending Action
items and review the upgrade notes for the version range before merging.
"""


def _forge_version() -> str:
    """Return the installed forge-scripts version for branch naming.

    Returns:
        The ``importlib.metadata`` version string, with any local-build
        suffix (``+g<sha>...``) stripped so the branch name stays a
        valid, stable git ref; ``"unknown"`` when forge-scripts is not
        installed as a distribution.
    """
    try:
        return metadata.version("forge-scripts").split("+")[0]
    except metadata.PackageNotFoundError:
        return "unknown"


def _working_tree_dirty(root: Path) -> bool:
    """Return ``True`` when the working tree has any pending change.

    Args:
        root: Repo root passed to ``git`` as cwd.

    Returns:
        ``True`` on any staged, unstaged, or untracked entry.
    """
    return bool(run_git("status", "--porcelain", cwd=root).strip())


def _open_resync_pr_url() -> str | None:
    """Return the URL of an already-open resync PR, or ``None``.

    The dedup guard: resync branches share the ``chore/forge-resync-``
    prefix, so one open PR with that head means a resync is already in
    review and a second run must not open a duplicate.

    Returns:
        The open resync PR's URL, or ``None`` when none exists (or the
        listing fails — creation then proceeds and ``gh`` surfaces any
        real conflict).
    """
    proc = subprocess.run(
        ["gh", "pr", "list", "--state", "open", "--json", "headRefName,url"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for pr in prs:
        if str(pr.get("headRefName", "")).startswith(_BRANCH_PREFIX):
            return str(pr.get("url", ""))
    return None


def _run_bootstrap() -> int:
    """Run ``install-forge-bootstrap`` in-process and return its exit code.

    Delegates the argv-swap re-entry to
    :func:`forge.install_bootstrap.run_in_process` (shared with
    ``forge-upgrade --continue``); this wrapper only adds the CI
    progress banner.

    Returns:
        The bootstrap's exit code (0 = every step passed or self-skipped).
    """
    with progress_logger("bootstrap"):
        return _bootstrap_run()


def _publish_resync(root: Path, version: str, base_branch: str) -> int:
    """Branch, commit, push the regen diff and open the resync PR.

    Args:
        root: Repo root passed to ``git`` as cwd.
        version: Installed forge version (names the branch).
        base_branch: PR base — the consumer's ``[tool.forge].base_branch``.

    Returns:
        ``0`` on success; ``1`` when ``gh pr create`` fails (the pushed
        branch is left in place for a manual retry).

    Raises:
        subprocess.CalledProcessError: When a git step (``add`` /
            ``commit`` / ``push``) fails — propagated after the
            ``finally`` block has switched back to the starting branch.
    """
    start_branch = run_git("branch", "--show-current", cwd=root).strip()
    branch = f"{_BRANCH_PREFIX}{version}"
    try:
        run_git("switch", "-c", branch, cwd=root)
        run_git("add", "-A", cwd=root)
        run_git(
            "commit",
            "-m",
            f"chore: resync forge-managed artifacts ({version})",
            cwd=root,
        )
        run_git("push", "-u", "origin", branch, cwd=root)
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                f"chore: resync forge-managed artifacts ({version})",
                "--body",
                _PR_BODY,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        if start_branch:
            run_git("switch", start_branch, cwd=root)
    if proc.returncode != 0:
        logger.error(
            "gh pr create failed (branch %s pushed — open the PR manually):\n%s",
            branch,
            proc.stderr.strip(),
        )
        return 1
    logger.info("✓ resync PR opened: %s", proc.stdout.strip())
    return 0


def main() -> int:
    """Run the resync loop; see the module docstring for the steps.

    Returns:
        ``0`` when in sync, deduplicated, or the PR was opened; ``1`` on
        a dirty tree or failed PR creation; the bootstrap's exit code
        when regeneration itself fails.
    """
    parser = argparse.ArgumentParser(
        prog="forge-resync",
        description=(
            "Regenerate forge-managed artifacts and open a dedup-guarded "
            "resync PR when they drifted."
        ),
    )
    parser.parse_args()

    root = repo_root()
    require_cli(
        "gh",
        caller="forge-resync",
        hint="Install the GitHub CLI (https://cli.github.com) and retry.",
    )

    if _working_tree_dirty(root):
        logger.error(
            "forge-resync: working tree not clean — commit or stash first "
            "(regen must not mix with in-flight changes)."
        )
        return 1

    existing = _open_resync_pr_url()
    if existing:
        logger.info("✓ resync PR already open — nothing to do: %s", existing)
        return 0

    rc = _run_bootstrap()
    if rc != 0:
        logger.error("forge-resync: bootstrap failed (exit %d) — aborting.", rc)
        return rc

    if not _working_tree_dirty(root):
        logger.info("✓ managed artifacts in sync — nothing to do.")
        return 0

    version = _forge_version()
    base_branch = load_config(root).base_branch
    return _publish_resync(root, version, base_branch)


if __name__ == "__main__":
    sys.exit(main())
