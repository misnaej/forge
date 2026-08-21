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
  are detached. Two mode-specific rules on top of the guard list above:
  a missing ``CHANGELOG.md`` is a hard failure (there is nothing to
  declare from — unlike guard 4's warn-and-proceed on the ``--bump``
  path), and a top heading *behind* the latest tag fails as a stale
  checkout / un-bumped heading.

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

from forge.changelog import (
    changelog_lacks_entry,
    stranded_added_versions,
    top_release_heading,
)
from forge.config import ForgeConfig, load_config
from forge.git_utils import (
    configure_cli_logging,
    create_annotated_tag,
    fetch_tags_best_effort,
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


def _stranded_entries_error(repo_root: Path, tag: str) -> str | None:
    """Return an error when ``CHANGELOG.md`` changed since released *tag*.

    The idempotent no-op ("top heading == existing tag → nothing to do")
    has two very different causes. A true resting state — nothing merged
    since the release, or only no-version merges, which never touch the
    changelog. Or *stranded work*: an earlier tag-cut failed or raced, a
    later run tagged the heading on the wrong commit, and subsequent PRs
    appended entries under the already-released heading — their commits
    would ship untagged (setuptools-scm ``X.Y.Z.devN``) while CI stays
    green. The tag-side and ``HEAD``-side contents are classified by
    :func:`forge.changelog.stranded_added_versions` — the same canonical
    membership-based detector the ``changelog_version`` pre-commit step
    uses — so a restrand (new heading opened above the released one,
    entries moved out) counts as normal regardless of how git renders
    the diff. A wording fix to already-released text still counts as a
    gain (accepted bias, same as the pre-commit sibling: a false
    positive is a cheap re-run; a missed stranding ships features
    untagged). Depends on ``main()``'s upfront ``git fetch --tags``
    having run — a locally-missing tag object (fetch timed out /
    offline) degrades to no detection rather than a false positive.

    Args:
        repo_root: Repo root.
        tag: The already-released tag the top heading still declares.

    Returns:
        One-line error string when entries are stranded, else ``None``.
    """
    old_text = run_git(
        "show",
        f"{tag}:CHANGELOG.md",
        cwd=repo_root,
        check=False,
    )
    if not old_text:
        return None
    text = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not stranded_added_versions(old_text, text, tag):
        return None
    return (
        f"CHANGELOG.md changed since {tag} but the top heading still "
        f"declares {tag} — entries are stranded under an already-released "
        "heading and their commits would ship untagged. Open the next "
        "`## vX.Y.Z` heading, move the stranded entries under it, and "
        "merge; the next tag-release run will cut it."
    )


def _tag_exists(repo_root: Path, tag: str) -> bool:
    """Return whether *tag* already exists locally or on ``origin``.

    Args:
        repo_root: Repo root.
        tag: Tag name to look for.

    Returns:
        ``True`` when the tag is present in the local repo or the remote.
    """
    # `--` pins *tag* as a pattern, not an option — same hardening as
    # create_annotated_tag's argv.
    if run_git("tag", "--list", "--", tag, cwd=repo_root, check=False):
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


def _select_branch_guard(
    repo_root: Path, base_branch: str, *, from_changelog_mode: bool
) -> str | None:
    """Choose the appropriate branch guard for the release mode.

    Args:
        repo_root: Repo root for git invocations.
        base_branch: The configured release trunk (e.g. ``"main"``).
        from_changelog_mode: When ``True``, use the CI-tolerant detached-HEAD
            check; otherwise use the on-branch check for workstation mode.

    Returns:
        One-line error string, or ``None`` when the branch check passes.
    """
    if from_changelog_mode and is_ci():
        return _detached_head_error(repo_root, base_branch)
    return _wrong_branch_error(repo_root, base_branch)


def _prepare_from_changelog(
    repo_root: Path, cfg: ForgeConfig
) -> tuple[str | None, str | None]:
    """Resolve and validate the tag declared in CHANGELOG.md.

    Model check is performed BEFORE the idempotency short-circuit to ensure
    that a dual-track misconfiguration is never masked by a stale tag
    matching the declared version.

    Args:
        repo_root: Repo root.
        cfg: Loaded ``[tool.forge]`` configuration.

    Returns:
        ``(tag, error)`` tuple. On success, ``tag`` is the resolved version and
        ``error`` is ``None``. On failure, ``tag`` is ``None`` and ``error``
        describes the issue. Idempotency is handled here: if the tag
        already exists and no entries are stranded under its heading,
        returns ``("v...", None)`` so the caller can exit 0 before other
        guards; with stranded entries it returns ``(None, error)`` instead
        (see :func:`_stranded_entries_error`).
    """
    tag, declared_err = _declared_tag_or_error(repo_root)
    if declared_err or tag is None:
        return None, declared_err or "No release heading found."

    # Model check BEFORE the idempotency short-circuit: "wrong release
    # model" is a configuration signal independent of tag state — a
    # stale tag matching the declared version must not mask it.
    model_err = _wrong_release_model_error(repo_root, cfg)
    if model_err:
        return None, model_err

    if _tag_exists(repo_root, tag):
        stranded = _stranded_entries_error(repo_root, tag)
        if stranded:
            return None, stranded
        # Idempotent case — signal success early.
        logger.info("%s is already released — nothing to do.", tag)
        return tag, None

    # Check for stale CHANGELOG: declared version behind the latest tag.
    latest = latest_v_tag(repo_root)
    declared = parse_semver(tag)
    latest_parsed = parse_semver(latest) if latest else None
    if declared and latest_parsed and declared < latest_parsed:
        return (
            None,
            (
                f"CHANGELOG top heading {tag} is behind the latest tag "
                f"{latest} — stale checkout or un-bumped heading."
            ),
        )
    return tag, None


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
    create_annotated_tag(repo_root, tag)
    if not run_git("remote", "get-url", "origin", cwd=repo_root, check=False):
        logger.warning("Tagged %s locally — no `origin` remote to push to.", tag)
        return 0
    try:
        # race_tolerant suppresses run_git's failure log: a raced push is
        # an expected, benign outcome and must not emit ERROR lines that
        # alerting would flag. Genuine failures re-surface stderr below.
        run_git("push", "origin", tag, cwd=repo_root, log_errors=not race_tolerant)
    except subprocess.CalledProcessError as exc:
        if race_tolerant:
            for note in fetch_tags_best_effort(repo_root):
                logger.warning("%s", note)
            if _tag_exists(repo_root, tag):
                logger.info("%s appeared concurrently — already released.", tag)
                return 0
        # In race_tolerant mode run_git's own failure log was suppressed,
        # so append git's message here; one exception log either way.
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if race_tolerant and detail else "."
        logger.exception("Pushing %s to origin failed%s", tag, suffix)
        return 1
    logger.info("Tagged and pushed %s", tag)
    return 0


def main() -> int:
    """Cut the ``vX.Y.Z`` release tag — bumped off the latest tag, or declared.

    ``--bump`` computes the tag from the latest ``v*`` tag;
    ``--from-changelog`` cuts the version the CHANGELOG top heading
    declares.

    Returns:
        ``0`` on success, ``--dry-run``, or the idempotent
        already-released case; ``1`` when any guard refuses or the tag
        push fails.
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

    # Bounded + stdin-less: a stalled remote or credential prompt degrades
    # to a stale-tag view instead of hanging the release command.
    for note in fetch_tags_best_effort(repo_root):
        logger.warning("%s", note)

    errors: list[str] = []
    if args.from_changelog:
        tag, changelog_error = _prepare_from_changelog(repo_root, cfg)
        if changelog_error:
            logger.error("%s", changelog_error)
            return 1
        if tag is None:
            return 1
        # Idempotent case: tag already exists, logged in _prepare_from_changelog.
        if _tag_exists(repo_root, tag):
            return 0
    else:
        latest = latest_v_tag(repo_root)
        tag = next_version(latest, args.bump)

    branch_guard = _select_branch_guard(
        repo_root, cfg.base_branch, from_changelog_mode=args.from_changelog
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
        latest = latest_v_tag(repo_root)
        logger.info("CHANGELOG declares %s (latest tag: %s)", tag, latest or "(none)")
    else:
        latest = latest_v_tag(repo_root)
        logger.info(
            "Latest tag: %s → next (%s bump): %s", latest or "(none)", args.bump, tag
        )
    if args.dry_run:
        logger.info("Dry run — no tag created.")
        return 0

    return _cut_release(repo_root, tag, race_tolerant=args.from_changelog)


if __name__ == "__main__":
    sys.exit(main())
