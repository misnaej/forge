"""forge-check-main-tags — keep minor release tags on the base branch.

Forge's dual-track release model tags every merge to ``dev`` via
``forge-next-prep --tag``, then promotes ``dev → base`` (``main``) with a
squash commit. The minor tag ``vX.Y.0`` must end up on **base's** squash
commit so ``git describe origin/<base>`` — and the version setuptools-scm
derives from a ``@base`` checkout — names the right release. Nothing else
in the release flow does that move, so historically the minor tag stayed
on the dev commit and base described as a stale predecessor
(``docs/release-process.md`` §2, the previously-unenforced invariant).

This CLI is the enforcer. It joins a minor tag to its base commit by
**release fingerprint** (tree content minus ``CHANGELOG.md``, see
:func:`forge.git_utils.release_tree_fingerprint`): a promotion squashes
dev's history into a new commit whose tree equals the tagged dev commit's
tree — except for the curated ``@main`` CHANGELOG entry the release
branch finalizes (``docs/release-process.md`` §5). Ignoring that one file
keeps the join deterministic while tolerating the per-release CHANGELOG
divergence; any *other* difference leaves the tag unaligned.

Modes:

- default (verify) — report every minor tag not sitting on its base
  commit; exit non-zero on drift. Read-only; CI-safe.
- ``--fix`` — force-move each drifting minor tag onto its base commit.
- ``--dry-run`` — print the moves that ``--fix`` would make; mutate
  nothing.

Self-skips in single-branch repos (``base_branch == dev_branch``), so
consumers on a trunk-based flow can wire it harmlessly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from forge.config import load_config
from forge.git_utils import (
    configure_cli_logging,
    parse_semver,
    release_tree_fingerprint,
    run_git,
)
from forge.run_context import git_auth_mode, is_non_interactive


configure_cli_logging()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TagState:
    """Where a minor tag currently sits versus where it belongs.

    Attributes:
        tag: The minor tag name (``vX.Y.0``).
        target: Base-branch commit SHA whose release fingerprint
            reproduces the tag's, or ``None`` when no base commit
            reproduces it (the minor was never promoted).
        current: Commit SHA the tag currently points at, or ``None`` when
            the tag is unresolvable.
    """

    tag: str
    target: str | None
    current: str | None

    @property
    def needs_move(self) -> bool:
        """``True`` when a base commit reproduces the tag but it sits elsewhere."""
        return self.target is not None and self.current != self.target


def _short(sha: str | None) -> str:
    """Return a 9-char abbreviation of *sha*, or ``(none)`` when absent.

    Args:
        sha: Full commit SHA or ``None``.

    Returns:
        The abbreviated SHA, or the literal ``"(none)"``.
    """
    return sha[:9] if sha else "(none)"


def _minor_tags(repo_root: Path) -> list[str]:
    """Return every ``vX.Y.0`` tag (patch == 0), semver-sorted ascending.

    Args:
        repo_root: Repo root for the git invocation.

    Returns:
        Minor tag names; empty when the repo has no ``v*`` tags.
    """
    raw = run_git("tag", "--list", "v*", cwd=repo_root, check=False)
    minors = [
        tag
        for tag in raw.split()
        if (parsed := parse_semver(tag)) is not None and parsed[2] == 0
    ]
    return sorted(minors, key=lambda tag: parse_semver(tag) or (0, 0, 0))


def _base_tree_index(repo_root: Path, base_ref: str) -> dict[str, str]:
    """Map each base commit's release fingerprint to its commit SHA.

    Newest commit wins on a fingerprint collision: ``git log`` lists
    commits newest-first and only the first occurrence is recorded, so the
    most recent base commit reproducing a release is the move target. The
    key is the release fingerprint (tree content minus ``CHANGELOG.md``,
    see :func:`forge.git_utils.release_tree_fingerprint`), so a base squash
    commit whose only difference from the tagged dev release is the curated
    ``@main`` CHANGELOG still matches — while any other file difference
    keeps the match release-exact and the tag unaligned.

    Args:
        repo_root: Repo root for the git invocation.
        base_ref: Branch ref to walk (e.g. ``origin/main``).

    Returns:
        ``{fingerprint: commit_sha}`` for every commit reachable from
        *base_ref*; empty when the ref is unresolvable.
    """
    commits = run_git(
        "log", "--format=%H", base_ref, cwd=repo_root, check=False
    ).split()
    index: dict[str, str] = {}
    for commit in commits:
        fingerprint = release_tree_fingerprint(repo_root, commit)
        if fingerprint and fingerprint not in index:
            index[fingerprint] = commit
    return index


def _tag_states(repo_root: Path, base_ref: str) -> list[_TagState]:
    """Resolve every minor tag's current vs. target commit on *base_ref*.

    Args:
        repo_root: Repo root for the git invocation.
        base_ref: Base branch ref (e.g. ``origin/main``).

    Returns:
        One :class:`_TagState` per minor tag, in ascending tag order.
    """
    index = _base_tree_index(repo_root, base_ref)
    states: list[_TagState] = []
    for tag in _minor_tags(repo_root):
        fingerprint = release_tree_fingerprint(repo_root, tag)
        current = run_git("rev-list", "-n1", tag, cwd=repo_root, check=False)
        states.append(
            _TagState(
                tag=tag,
                target=index.get(fingerprint) if fingerprint else None,
                current=current or None,
            )
        )
    return states


def _force_move_tag(repo_root: Path, tag: str, commit_sha: str) -> None:
    """Annotated-retag *tag* at *commit_sha* and force-push it.

    The single seam that mutates a published tag. Annotated (``-a``) to
    match ``forge-next-prep``'s tag convention — a lightweight retag would
    change the tag's object type and perturb ``git describe`` output.

    Args:
        repo_root: Repo root for the git invocation.
        tag: Minor tag to move.
        commit_sha: Destination commit on the base branch.
    """
    run_git("tag", "-f", "-a", tag, "-m", tag, commit_sha, cwd=repo_root)
    run_git("push", "--force", "origin", tag, cwd=repo_root)


def _report_unreproduced(states: list[_TagState], base_ref: str) -> None:
    """Warn about minor tags whose release fingerprint no base commit reproduces.

    Such a tag names a minor that was never promoted to the base branch,
    so it cannot be aligned — that is a promotion gap, not a tag-placement
    bug.

    Args:
        states: All resolved tag states.
        base_ref: Base branch ref, for the message.
    """
    for state in states:
        if state.target is None:
            logger.warning(
                "%s: no commit on %s reproduces its release fingerprint — "
                "promote that minor before it can be aligned.",
                state.tag,
                base_ref,
            )


def _verify(states: list[_TagState], base_ref: str) -> int:
    """Report drift read-only and return the process exit code.

    Args:
        states: All resolved tag states.
        base_ref: Base branch ref, for messages.

    Returns:
        ``1`` when any minor tag is misplaced, ``0`` otherwise.
    """
    misplaced = [state for state in states if state.needs_move]
    if not misplaced:
        logger.info("All %d minor tag(s) sit on %s.", len(states), base_ref)
        return 0
    for state in misplaced:
        logger.error(
            "%s points at %s but %s reproduces its fingerprint at %s — run with --fix.",
            state.tag,
            _short(state.current),
            base_ref,
            _short(state.target),
        )
    return 1


def _repair(
    repo_root: Path,
    states: list[_TagState],
    base_ref: str,
    *,
    dry_run: bool,
) -> int:
    """Move every misplaced minor tag onto its base commit (or preview).

    Args:
        repo_root: Repo root for the git invocation.
        states: All resolved tag states.
        base_ref: Base branch ref, for messages.
        dry_run: When ``True``, log the planned moves without mutating.

    Returns:
        ``1`` when a real move is required but the environment cannot
        push (non-interactive with no git auth, FOUNDATION §15); ``0``
        otherwise.
    """
    misplaced = [state for state in states if state.needs_move]
    if not misplaced:
        logger.info("Nothing to align — all minor tags already on %s.", base_ref)
        return 0
    if not dry_run and is_non_interactive() and git_auth_mode() == "none":
        logger.error(
            "Cannot push tag moves: non-interactive with no git auth "
            "(FOUNDATION §15). Run interactively or set GH_TOKEN / an SSH key.",
        )
        return 1
    for state in misplaced:
        # needs_move guarantees target is not None
        if state.target is None:
            continue
        if dry_run:
            logger.info(
                "[dry-run] would move %s: %s → %s (%s)",
                state.tag,
                _short(state.current),
                _short(state.target),
                base_ref,
            )
            continue
        _force_move_tag(repo_root, state.tag, state.target)
        logger.info("Moved %s → %s (%s).", state.tag, _short(state.target), base_ref)
    return 0


def main() -> int:
    """Verify or repair minor release tags on the base branch.

    Returns:
        ``0`` on success / when skipped, ``1`` on verify drift or when a
        fix cannot push in a non-interactive environment.
    """
    parser = argparse.ArgumentParser(
        prog="forge-check-main-tags",
        description=(
            "Verify (default) or repair (--fix) that every minor release "
            "tag vX.Y.0 sits on the base branch's squash commit, matched by "
            "release fingerprint (tree content minus CHANGELOG.md). "
            "Self-skips single-branch repos."
        ),
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Force-move every misplaced minor tag onto its base commit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the moves --fix would make; mutate nothing.",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    cfg = load_config(repo_root)
    if cfg.base_branch == cfg.dev_branch:
        logger.info(
            "(single-branch repo: base == dev — main-tag alignment N/A, skipped)"
        )
        return 0

    base_ref = f"origin/{cfg.base_branch}"
    run_git("fetch", "origin", "--tags", "--quiet", cwd=repo_root, check=False)
    states = _tag_states(repo_root, base_ref)
    if not states:
        logger.info("(no minor v*.0 tags — nothing to check)")
        return 0

    _report_unreproduced(states, base_ref)
    if args.fix or args.dry_run:
        return _repair(repo_root, states, base_ref, dry_run=args.dry_run)
    return _verify(states, base_ref)


if __name__ == "__main__":
    sys.exit(main())
