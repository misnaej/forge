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

Two version sources, one orchestration:

- ``--bump major|minor|patch`` — compute the tag off the latest ``v*``
  tag. The manual workstation path.
- ``--from-changelog`` — cut the version the ``CHANGELOG.md`` top
  heading *declares* (the single-track convention's rolling-next
  analogue). Idempotent: already tagged → exit ``0`` "nothing to
  release", so a tag-on-merge CI job and a manual cut can race safely.
  Under CI (``forge.run_context.is_ci``) the on-branch guard becomes a
  ``HEAD == origin/<base_branch>`` check, since merge-event checkouts
  are detached.

``--dry-run`` reports the tag that would be cut and stops before
mutating anything. Exits ``0`` on success, dry-run, or an idempotent
nothing-to-release; ``1`` on guard failure.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from forge.changelog import changelog_lacks_entry, top_release_heading
from forge.config import ForgeConfig, load_config
from forge.git_utils import (
    configure_cli_logging,
    latest_v_tag,
    next_version,
    parse_semver,
    read_local_plugin_version,
    run_git,
)
from forge.run_context import is_ci


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


def _detached_head_error(repo_root: Path, base_branch: str) -> str | None:
    """Return an error unless ``HEAD`` is the tip of ``origin/<base_branch>``.

    The CI replacement for :func:`_wrong_branch_error`: a merge-event
    checkout is a detached ``HEAD``, so "on the branch" is unverifiable —
    what matters is that the commit being tagged IS the base branch's
    current remote tip (not a stale or unrelated SHA).

    Args:
        repo_root: Repo root for the git invocations.
        base_branch: The configured release trunk (e.g. ``"main"``).

    Returns:
        One-line error string, or ``None`` when ``HEAD`` matches the tip.
    """
    head = run_git("rev-parse", "HEAD", cwd=repo_root, check=False)
    tip = run_git("rev-parse", f"origin/{base_branch}", cwd=repo_root, check=False)
    if not tip:
        return f"origin/{base_branch} is unknown — fetch before tagging."
    if head != tip:
        return (
            f"HEAD ({head[:9]}) is not the tip of origin/{base_branch} "
            f"({tip[:9]}) — refusing to tag a non-tip commit."
        )
    return None


def _declared_tag_or_error(repo_root: Path) -> tuple[str | None, str | None]:
    """Resolve the tag ``--from-changelog`` should cut.

    Args:
        repo_root: Repo root.

    Returns:
        ``(tag, None)`` when the CHANGELOG declares a version, else
        ``(None, error)``.
    """
    changelog = repo_root / "CHANGELOG.md"
    if not changelog.is_file():
        return None, "--from-changelog needs a CHANGELOG.md at the repo root."
    declared = top_release_heading(changelog.read_text(encoding="utf-8"))
    if declared is None:
        return None, "CHANGELOG.md has no `## vX.Y.Z` heading to release."
    return declared, None


def _tag_exists(repo_root: Path, tag: str) -> bool:
    """Return whether *tag* already exists locally or on ``origin``.

    Args:
        repo_root: Repo root.
        tag: Tag name to look for.

    Returns:
        ``True`` when the tag is present in the local repo or the remote.
    """
    if run_git("tag", "--list", tag, cwd=repo_root, check=False):
        return True
    return bool(
        run_git(
            "ls-remote",
            "--tags",
            "origin",
            f"refs/tags/{tag}",
            cwd=repo_root,
            check=False,
        )
    )


def _cut_release(repo_root: Path, tag: str, *, race_tolerant: bool = False) -> int:
    """Create the annotated *tag* on ``HEAD`` and push it to ``origin``.

    Args:
        repo_root: Repo root.
        tag: Release tag to create, e.g. ``"v1.3.0"``.
        race_tolerant: Treat a failed push as success when the tag turns
            out to exist on the remote — a concurrent cut (merge-event CI
            job racing a manual run) produced the same release, which is
            the intended outcome, not an error.

    Returns:
        ``0`` on success (including a tolerated race), ``1`` when the
        push failed for any other reason.
    """
    run_git("tag", "-a", tag, "-m", tag, "HEAD", cwd=repo_root)
    if not run_git("remote", "get-url", "origin", cwd=repo_root, check=False):
        logger.warning("Tagged %s locally — no `origin` remote to push to.", tag)
        return 0
    try:
        run_git("push", "origin", tag, cwd=repo_root)
    except subprocess.CalledProcessError:
        if race_tolerant:
            run_git("fetch", "--tags", "--quiet", "origin", cwd=repo_root, check=False)
            if _tag_exists(repo_root, tag):
                logger.info("%s appeared concurrently — already released.", tag)
                return 0
        logger.exception("Pushing %s to origin failed.", tag)
        return 1
    logger.info("Tagged and pushed %s", tag)
    return 0


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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--bump",
        choices=("major", "minor", "patch"),
        help="Semver increment to apply to the latest v* tag.",
    )
    source.add_argument(
        "--from-changelog",
        action="store_true",
        help=(
            "Cut the version the CHANGELOG.md top heading declares. "
            "Idempotent (already tagged → exit 0); the tag-on-merge CI "
            "mode — see docs/ci-recipe.md."
        ),
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

    errors: list[str] = []
    if args.from_changelog:
        tag, declared_err = _declared_tag_or_error(repo_root)
        if declared_err or tag is None:
            logger.error("%s", declared_err or "No release heading found.")
            return 1
        if _tag_exists(repo_root, tag):
            logger.info("%s is already released — nothing to do.", tag)
            return 0
        declared = parse_semver(tag)
        latest_parsed = parse_semver(latest) if latest else None
        if declared and latest_parsed and declared < latest_parsed:
            errors.append(
                f"CHANGELOG top heading {tag} is behind the latest tag "
                f"{latest} — stale checkout or un-bumped heading."
            )
    else:
        tag = next_version(latest, args.bump)

    branch_guard = (
        _detached_head_error(repo_root, cfg.base_branch)
        if args.from_changelog and is_ci()
        else _wrong_branch_error(repo_root, cfg.base_branch)
    )
    errors.extend(
        err
        for err in (
            _dirty_tree_error(repo_root),
            branch_guard,
            _wrong_release_model_error(repo_root, cfg),
            _changelog_gate_error(repo_root, tag),
        )
        if err
    )
    if errors:
        for err in errors:
            logger.error("%s", err)
        return 1

    if args.from_changelog:
        logger.info("CHANGELOG declares %s (latest tag: %s)", tag, latest or "(none)")
    else:
        logger.info(
            "Latest tag: %s → next (%s bump): %s", latest or "(none)", args.bump, tag
        )
    if args.dry_run:
        logger.info("Dry run — no tag created.")
        return 0

    return _cut_release(repo_root, tag, race_tolerant=args.from_changelog)


if __name__ == "__main__":
    sys.exit(main())
