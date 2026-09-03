"""forge-rebump — mechanical post-merge version-slot + changelog resolution.

Rolling-next means every parallel PR carries the same "next"
``plugin.json`` version, so each merge collides every surviving branch
on that one line — plus, in shared-heading changelog repos, the top
``## vX.Y.Z`` heading stack. The resolution is fully mechanical; this
CLI performs it on a feature branch and stages the result. It never
commits and never pushes — the commit stays with the normal commit flow.

Two entry states, detected automatically:

1. **Mid-merge** (``MERGE_HEAD`` present): the version-slot conflict is
   live. Requires the unmerged set to be confined to
   ``.claude-plugin/plugin.json`` and ``CHANGELOG.md`` — any other
   conflicted path is a real code conflict and the tool refuses loudly.
   The branch's bump intent is classified from the merge stages:
   stage 1 (merge base — the branch's fork-point manifest) against
   stage 2 (ours — the branch's committed manifest), so the class is
   read from what *the branch itself* changed, never against the
   moving-target latest tag another PR just advanced.
2. **Clean tree** (post-tag rebump, no merge in progress): another PR's
   merge consumed the branch's slot without a textual conflict. Intent
   class comes from the same fork-point comparison, read at
   ``merge-base(HEAD, origin/<base_branch>)`` against the working-tree
   manifest. Conflict markers on disk without ``MERGE_HEAD`` mean a
   half-resolved state this tool refuses to guess about.

In both states the new slot is ``next_version(latest v* tag, class)``
unless the branch's version is already strictly ahead of the latest tag
(its slot is still open — kept as-is). The changelog half applies only
in shared-heading mode: mid-merge, the branch's unreleased top section
is restacked onto the base's changelog under the new heading; on a
clean tree the stale top heading is retitled. **Fragments mode**
(``[tool.forge.changelog].mode = "fragments"``) sidelines this tool
entirely: per-PR entries are unique files that cannot collide (changelog
half no-ops) and versions are assembler-owned — ``forge-changelog
release`` is the manifest's single writer, so the manifest half no-ops
too, and a fragments-mode manifest conflict (a release-PR race) is
refused with a pointer at ``forge-changelog release``.

Exits 0 on success (including nothing-to-do), 1 on refusal.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from forge.changelog import (
    restack_changelog,
    retitle_top_release,
    top_release_heading,
)
from forge.config import ForgeConfig, is_fragments_mode, load_config
from forge.git_utils import (
    classify_bump,
    configure_cli_logging,
    file_has_conflict_markers,
    latest_v_tag,
    merge_base_with_head,
    merge_in_progress,
    next_version,
    parse_semver,
    read_local_plugin_version,
    read_plugin_version_at_ref,
    render_plugin_version,
    run_git,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


PLUGIN_PATH = ".claude-plugin/plugin.json"
CHANGELOG_PATH = "CHANGELOG.md"


class _RefusalError(Exception):
    """Internal control flow: a state this tool must not resolve."""


@dataclass(frozen=True)
class RebumpOutcome:
    """Result of one rebump run.

    Attributes:
        version: The resolved manifest version (bare semver), or ``""``
            on refusal.
        bump_class: The classified branch intent (``"major"`` /
            ``"minor"`` / ``"patch"``), or ``""`` on refusal.
        changelog_action: What happened to the changelog — one of
            ``"restacked"``, ``"retitled"``, ``"fragments-noop"``,
            ``"unchanged"``, ``"no-changelog"``, or ``""`` on refusal.
        staged: Paths staged by this run, repo-relative.
        refusal: Human-readable reason the tool refused, or ``None`` on
            success.
    """

    version: str
    bump_class: str
    changelog_action: str
    staged: tuple[str, ...]
    refusal: str | None = None


def _require_latest_tag(repo_root: Path) -> str:
    """Return the latest ``v*`` tag, refusing when none exists.

    Args:
        repo_root: Git repo root.

    Returns:
        The latest tag name.

    Raises:
        _RefusalError: When the repo has no ``v*`` tag to rebump against.
    """
    latest = latest_v_tag(repo_root)
    if latest is None:
        msg = "no v* tag found — rolling-next rebump needs a release tag baseline."
        raise _RefusalError(msg)
    return latest


def _unmerged_paths(repo_root: Path) -> list[str]:
    """Return the repo-relative paths currently in an unmerged index state.

    Args:
        repo_root: Git repo root.

    Returns:
        Unmerged (conflicted) paths; empty when the index is clean.
    """
    raw = run_git("diff", "--name-only", "--diff-filter=U", cwd=repo_root, check=False)
    return [line for line in raw.splitlines() if line.strip()]


def _read_index_stage(repo_root: Path, stage: int, path: str) -> str | None:
    """Return *path*'s contents at merge-index *stage*, or ``None``.

    Args:
        repo_root: Git repo root.
        stage: Merge stage number (1 = base, 2 = ours, 3 = theirs).
        path: Repo-relative file path.

    Returns:
        File text at that stage, or ``None`` when the stage entry does
        not exist (e.g. the file is not conflicted).
    """
    proc = subprocess.run(
        ["git", "show", f":{stage}:{path}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def _guard_entry_state(repo_root: Path, cfg: ForgeConfig, *, mid_merge: bool) -> None:
    """Refuse states the tool must not touch.

    Args:
        repo_root: Git repo root.
        cfg: Loaded ``[tool.forge]`` config.
        mid_merge: Whether a merge is in progress.

    Raises:
        _RefusalError: On a protected branch, on unrelated conflicts
            (mid-merge), on a fragments-mode manifest conflict (a
            release-PR race owned by ``forge-changelog release``), or on
            stray conflict markers (clean tree).
    """
    branch = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_root, check=False)
    if branch == cfg.base_branch:
        msg = (
            f"on '{branch}' — rebump is a feature-branch recovery tool; "
            "the base branch owns its manifest via the merge flow."
        )
        raise _RefusalError(msg)
    if mid_merge:
        unmerged = _unmerged_paths(repo_root)
        extras = [p for p in unmerged if p not in (PLUGIN_PATH, CHANGELOG_PATH)]
        if extras:
            msg = (
                "unrelated merge conflicts present — resolve these by hand "
                f"first: {', '.join(sorted(extras))}"
            )
            raise _RefusalError(msg)
        if PLUGIN_PATH in unmerged and is_fragments_mode(repo_root):
            msg = (
                "fragments mode: a plugin.json conflict is a release-PR "
                "race. Recovery: take the BASE side of CHANGELOG.md and "
                "plugin.json, restore this branch's consumed fragments "
                "from the merge base (`git checkout $(git merge-base HEAD "
                "MERGE_HEAD) -- changelog.d/`), then re-run "
                "`forge-changelog release` to recompute."
            )
            raise _RefusalError(msg)
        return
    for path in (PLUGIN_PATH, CHANGELOG_PATH):
        if file_has_conflict_markers(repo_root / path):
            msg = (
                f"{path} holds conflict markers but no merge is in progress "
                "— a half-resolved state; finish or restart the merge first."
            )
            raise _RefusalError(msg)


def _mid_merge_versions(repo_root: Path) -> tuple[str | None, str | None]:
    """Return the ``(fork, ours)`` manifest versions during a merge.

    Prefers the merge-index stages (1 = base, 2 = ours). When both sides
    wrote the *same* version — the canonical same-slot collision — git
    auto-merges the manifest, drops it to stage 0, and the stage reads
    fail; then ``HEAD`` (ours, still the pre-merge branch tip) and the
    ``HEAD``/``MERGE_HEAD`` merge base supply the same two answers.

    Args:
        repo_root: Git repo root.

    Returns:
        ``(fork_version, ours_version)`` — either may be ``None`` when
        unreadable.
    """
    old = read_plugin_version_at_ref(repo_root, ":1")
    new = read_plugin_version_at_ref(repo_root, ":2")
    if new is None:
        new = read_plugin_version_at_ref(repo_root, "HEAD")
    if old is None:
        base = run_git("merge-base", "HEAD", "MERGE_HEAD", cwd=repo_root, check=False)
        old = read_plugin_version_at_ref(repo_root, base) if base else None
    return old, new


def _classify_intent(repo_root: Path, cfg: ForgeConfig, *, mid_merge: bool) -> str:
    """Classify the branch's own semver intent from its fork-point delta.

    Args:
        repo_root: Git repo root.
        cfg: Loaded ``[tool.forge]`` config.
        mid_merge: Whether a merge is in progress.

    Returns:
        ``"major"``, ``"minor"``, or ``"patch"`` (the default when the
        branch carries no classifiable manifest delta).

    Raises:
        _RefusalError: When the branch-side manifest version cannot be read.
    """
    if mid_merge:
        old, new = _mid_merge_versions(repo_root)
    else:
        fork = merge_base_with_head(repo_root, cfg.base_branch)
        old = read_plugin_version_at_ref(repo_root, fork) if fork else None
        new = read_local_plugin_version(repo_root)
    if new is None:
        msg = (
            f"cannot read the branch-side {PLUGIN_PATH} version — "
            "not a rolling-next plugin repo, or the manifest is malformed."
        )
        raise _RefusalError(msg)
    old_tuple = parse_semver(old) if old else None
    return classify_bump(old_tuple, parse_semver(new)) or "patch"


def _resolve_version(
    repo_root: Path, latest: str, bump_class: str, *, mid_merge: bool
) -> str:
    """Return the bare version the manifest should carry after the rebump.

    The branch keeps its own version when it is already strictly ahead
    of the latest tag (its slot is still open); otherwise it takes the
    next class-appropriate slot above the tag.

    Args:
        repo_root: Git repo root.
        latest: Latest ``v*`` tag.
        bump_class: The branch's classified intent.
        mid_merge: Whether a merge is in progress.

    Returns:
        Bare semver string for the manifest.
    """
    current = (
        _mid_merge_versions(repo_root)[1]
        if mid_merge
        else read_local_plugin_version(repo_root)
    )
    current_tuple = parse_semver(current) if current else None
    latest_tuple = parse_semver(latest)
    if current_tuple and latest_tuple and current_tuple > latest_tuple:
        # Reconstruct from the parsed tuple, never the raw manifest string:
        # the field is branch-authored (untrusted) and a JSON-escaped
        # payload would otherwise round-trip verbatim into the regex
        # rewrite and the changelog heading (CWE-116).
        return "{}.{}.{}".format(*current_tuple)
    return next_version(latest, bump_class).removeprefix("v")


def _render_plugin_version(
    repo_root: Path, version: str, *, mid_merge: bool
) -> str | None:
    """Return the manifest text carrying *version*, or ``None`` when current.

    Mid-merge the on-disk file may hold conflict markers, so the
    ours-stage content is the rewrite source; on a clean tree (or when
    the manifest auto-merged) the disk file is. Pure computation — the
    caller writes, so a later refusal leaves the tree untouched.

    Args:
        repo_root: Git repo root.
        version: Bare semver to write.
        mid_merge: Whether a merge is in progress.

    Returns:
        The rewritten manifest text, or ``None`` when the clean-tree
        file already carries *version* (nothing to write).

    Raises:
        _RefusalError: When the version field cannot be located, or when
            the rewritten text fails fail-closed validation (invalid
            JSON, or a version field not exactly the target).
    """
    source: str | None = None
    if mid_merge:
        source = _read_index_stage(repo_root, 2, PLUGIN_PATH)
    if source is None:
        source = (repo_root / PLUGIN_PATH).read_text(encoding="utf-8")
    # Rewrite + fail-closed CWE-116 validation live in the shared
    # git_utils.render_plugin_version core (also the `forge-changelog
    # release` writer); this wrapper only picks the source (stage 2 vs
    # disk) and converts the failure into a refusal.
    try:
        rewritten = render_plugin_version(source, version)
    except ValueError as exc:
        msg = f"{exc} Resolve by hand."
        raise _RefusalError(msg) from exc
    if rewritten == source and not mid_merge:
        return None
    return rewritten


def _render_changelog(
    repo_root: Path, version: str, *, mid_merge: bool
) -> tuple[str, str | None]:
    """Compute the changelog half of the rebump without touching disk.

    Args:
        repo_root: Git repo root.
        version: The resolved bare manifest version.
        mid_merge: Whether a merge is in progress.

    Returns:
        ``(action, text)`` — action is one of ``"restacked"``,
        ``"retitled"``, ``"fragments-noop"``, ``"unchanged"``,
        ``"no-changelog"``; text is the content to write, ``None`` when
        no write is needed.

    Raises:
        _RefusalError: On a CHANGELOG conflict in fragments mode (single-writer
            assembly makes that unexpected — a human must look), when a
            conflicted CHANGELOG's merge stages are unreadable, or when the
            branch side has no release heading to restack.
    """
    fragments = is_fragments_mode(repo_root)
    conflicted = mid_merge and CHANGELOG_PATH in _unmerged_paths(repo_root)
    if fragments:
        if conflicted:
            msg = (
                f"{CHANGELOG_PATH} is conflicted but the repo runs fragments "
                "mode — assembly is single-writer, so this conflict is "
                "unexpected; resolve it by hand."
            )
            raise _RefusalError(msg)
        return "fragments-noop", None
    changelog = repo_root / CHANGELOG_PATH
    if not changelog.is_file():
        return "no-changelog", None
    heading = f"v{version}"
    if conflicted:
        ours = _read_index_stage(repo_root, 2, CHANGELOG_PATH)
        theirs = _read_index_stage(repo_root, 3, CHANGELOG_PATH)
        if ours is None or theirs is None:
            msg = f"{CHANGELOG_PATH}: cannot read merge stages for restack."
            raise _RefusalError(msg)
        if top_release_heading(ours) is None:
            msg = (
                f"{CHANGELOG_PATH}: the branch side has no ## vX.Y.Z heading "
                "to restack — resolve the conflict by hand."
            )
            raise _RefusalError(msg)
        return "restacked", restack_changelog(ours, theirs, heading)
    text = changelog.read_text(encoding="utf-8")
    top = top_release_heading(text)
    if top is None or top == heading:
        return "unchanged", None
    return "retitled", retitle_top_release(text, heading)


def rebump(repo_root: Path) -> RebumpOutcome:
    """Resolve the rolling-next version slot and changelog stack, then stage.

    Compute-then-write: every refusal fires before the first disk write,
    so a refused run leaves the working tree exactly as it found it.

    Args:
        repo_root: Git repo root.

    Returns:
        A :class:`RebumpOutcome` — ``refusal`` set when the tree is in a
        state this tool must not resolve, populated fields otherwise.
    """
    try:
        cfg = load_config(repo_root)
        mid_merge = merge_in_progress(repo_root)
        _guard_entry_state(repo_root, cfg, mid_merge=mid_merge)
        fragments = is_fragments_mode(repo_root)
        latest = _require_latest_tag(repo_root)
        bump_class = _classify_intent(repo_root, cfg, mid_merge=mid_merge)
        version = _resolve_version(repo_root, latest, bump_class, mid_merge=mid_merge)
        # Fragments mode: versions are assembler-owned (forge-changelog
        # release is the manifest's single writer) — never render or
        # stage the manifest here, mirroring the changelog half's no-op.
        plugin_text = (
            None
            if fragments
            else _render_plugin_version(repo_root, version, mid_merge=mid_merge)
        )
        changelog_action, changelog_text = _render_changelog(
            repo_root, version, mid_merge=mid_merge
        )
    except _RefusalError as refusal:
        return RebumpOutcome("", "", "", (), refusal=str(refusal))
    to_stage = []
    # Mid-merge _render_plugin_version always returns text (its None
    # short-circuit is clean-tree-only), so this branch alone also covers
    # re-staging an auto-merged manifest.
    if plugin_text is not None:
        (repo_root / PLUGIN_PATH).write_text(plugin_text, encoding="utf-8")
        to_stage.append(PLUGIN_PATH)
    if changelog_text is not None:
        (repo_root / CHANGELOG_PATH).write_text(changelog_text, encoding="utf-8")
        to_stage.append(CHANGELOG_PATH)
    if to_stage:
        run_git("add", "--", *to_stage, cwd=repo_root)
    return RebumpOutcome(version, bump_class, changelog_action, tuple(to_stage))


def main() -> int:
    """Run the rebump against the current directory's repo.

    Returns:
        ``0`` on success (including nothing-to-do), ``1`` on refusal.
    """
    parser = argparse.ArgumentParser(
        prog="forge-rebump",
        description=(
            "Mechanically resolve the rolling-next plugin.json version slot "
            "(and, in shared-heading repos, the CHANGELOG stack) on a feature "
            "branch — mid-merge or after another PR's merge consumed the "
            "slot. Stages the result; never commits."
        ),
    )
    parser.parse_args()
    outcome = rebump(Path.cwd())
    if outcome.refusal:
        logger.error("Refusing to rebump: %s", outcome.refusal)
        return 1
    logger.info(
        "Resolved %s to %s (%s intent); changelog: %s.",
        PLUGIN_PATH,
        outcome.version,
        outcome.bump_class,
        outcome.changelog_action,
    )
    if outcome.staged:
        logger.info("Staged: %s", ", ".join(outcome.staged))
    else:
        logger.info("Nothing to stage — tree already resolved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
