"""forge-update-branch — bring a feature branch up to date with its base.

A base sync is not an edit: it is mostly work already reviewed on the
base, plus whatever conflicts the merge raises. This command owns the
whole sync so no agent carries the procedure and no merge needs a human
keystroke (FOUNDATION §2, §6):

1. Refuse on a protected or detached branch; fetch the base.
2. A dirty tree is secured first as a ``wip-sync:`` checkpoint commit.
3. Merge the base without committing, so this command owns the commit.
4. Resolve the conflicts whose answer is mechanical:

   - **already seen** — the base's version of the file already appears
     in this branch's own history, so the base brings nothing new there
     and the branch side is right. A stacked branch meets this after
     every parent squash-merge: the squash commit shares no ancestry with
     the parent branch, so git cannot tell the child already holds it;
   - **generated artifacts** — regenerated from the merged tree, once
     nothing else conflicts (:func:`forge.resync.regenerate_artifact`);
   - **the rolling-next version slot** — :func:`forge.rebump.rebump`.

5. Anything left stops the run: the paths are listed, an intake lesson
   records the stop, and the next step is named — ``forge:merge-resolver``
   edits the files, then ``forge-update-branch --continue``.
6. Commit the merge. A merge only this command resolved commits
   without the local gate, like a merge git completes itself; CI and the
   next ordinary commit check it. A merge with hand-edited files runs the
   pre-commit gate, because those edits are new code. The branch is not
   pushed: publication goes through the gated push path
   (``forge-commit``), so nothing reaches the remote unchecked.

``--continue`` needs no saved state: the conflicted set and both sides
are recomputed from ``HEAD`` and ``MERGE_HEAD``. It refuses unless the
merge in progress is of the base, nothing is unresolved, and the staged
result differs from git's own automatic merge only at conflicted paths.

Exit codes:
    0  merged (or already up to date) and committed locally
    1  refused — nothing changed, or the merge left in progress
    3  stopped on conflicts that need judgment; the merge stays in progress
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from forge.commit import (
    EXIT_OK,
    EXIT_REFUSED,
    CommitRefusedError,
    checkpoint,
    current_branch,
)
from forge.config import load_config
from forge.gh_comments import ValidationError, validate_no_ai_attribution
from forge.git_utils import (
    configure_cli_logging,
    create_commit,
    fetch_quietly,
    git_env_overrides_removed,
    is_ancestor,
    merge_in_progress,
    repo_root,
    run_git,
    unmerged_paths,
)
from forge.intake import configured_dir, new_entry
from forge.pr_delta import regen_commands
from forge.rebump import CHANGELOG_PATH, PLUGIN_PATH, rebump
from forge.resync import regenerate_artifact


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)

EXIT_STOPPED: Final = 3
#: Read by ``forge-precommit``: the merge commit runs no steps. Set only by
#: this command, and honoured only while a merge is in progress.
MERGE_SYNC_ENV: Final = "FORGE_MERGE_SYNC"
#: Files the version-slot resolver owns.
_SLOT_PATHS: Final = frozenset({PLUGIN_PATH, CHANGELOG_PATH})
_MAX_LISTED: Final = 20
_LS_FILES_U_RE: Final = re.compile(r"^(\d+) ([0-9a-f]+) ([123])\t(.+)$")
#: Leading fields of a raw-diff line: ``:<mode> <mode> <old blob> <new blob>``.
_RAW_FIELDS: Final = 4
_TREE_RE: Final = re.compile(r"^[0-9a-f]{40,64}$")
#: Index stages of an unmerged path.
_OURS: Final = 2
_THEIRS: Final = 3
#: Line openings git writes into a conflicted text file.
_MARKERS: Final = ("<<<<<<< ", ">>>>>>> ", "||||||| ")
#: Characters an intake record never takes from a file name.
_UNSAFE_NAME_RE: Final = re.compile(r"[`\x00-\x1f\x7f]")


class UpdateRefusedError(Exception):
    """A guard refused; the message names the remedy possible in this state."""


@dataclass
class Resolution:
    """What one resolve pass did, and what it left."""

    seen: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    slot: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)

    @property
    def automatic(self) -> list[str]:
        """Every path this pass resolved, in report order."""
        return [*self.seen, *self.generated, *self.slot]


def _stage_blobs(root: Path, path: str) -> dict[int, str]:
    """Return the index stage blobs of an unmerged *path* (1 base, 2 ours, 3 theirs).

    Args:
        root: Repo root.
        path: An unmerged path.

    Returns:
        Stage number to blob id; a side that deleted the file is absent.
    """
    out = run_git("ls-files", "-u", "--", path, cwd=root, check=False)
    blobs: dict[int, str] = {}
    for line in out.splitlines():
        match = _LS_FILES_U_RE.match(line)
        if match:
            blobs[int(match[3])] = match[2]
    return blobs


def _branch_blobs(root: Path, path: str) -> set[str]:
    """Return the versions of *path* this branch wrote and the base never had.

    Only non-merge commits in ``MERGE_HEAD..HEAD`` count, and only the
    blob each one produced: a version the branch held before it forked,
    or received by merging the base, is base history, so the base going
    back to it is a real change (a revert), never "already seen". A
    stacked branch's parent commits are in the range — the base holds
    only their squash — so the parent's final version counts.

    Args:
        root: Repo root, mid-merge.
        path: Repo-relative path.

    Returns:
        Blob ids the branch's own commits produced for *path*.
    """
    out = run_git(
        "log",
        "--no-merges",
        "--raw",
        "--no-abbrev",
        "--no-renames",
        "--format=",
        "MERGE_HEAD..HEAD",
        "--",
        path,
        cwd=root,
        check=False,
    )
    blobs: set[str] = set()
    for line in out.splitlines():
        parts = line.split("\t", 1)[0].split()
        if len(parts) >= _RAW_FIELDS and line.startswith(":"):
            blobs.add(parts[3])
    return blobs


def _take_ours(root: Path, path: str) -> None:
    """Resolve *path* to the branch side and stage it.

    Args:
        root: Repo root.
        path: An unmerged path both sides still hold.
    """
    run_git("checkout", "--ours", "--", path, cwd=root)
    run_git("add", "--", path, cwd=root)


def _has_markers(path: Path) -> bool:
    """Return whether *path* still holds a git conflict marker line.

    Args:
        path: Working-tree file.

    Returns:
        ``True`` when a line opens with a marker, or the file is unreadable.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return True
    return any(line.startswith(_MARKERS) for line in lines)


def stage_hand_resolved(root: Path) -> list[str]:
    """Stage conflicted files whose markers are gone, so a resolver only edits.

    Only paths both sides still hold: a modify/delete conflict leaves a
    marker-free file whether or not anyone decided it, so those wait for
    an explicit ``git add`` or ``git rm``.

    Args:
        root: Repo root, mid-merge.

    Returns:
        The paths staged.
    """
    staged = []
    for path in unmerged_paths(root):
        blobs = _stage_blobs(root, path)
        if _OURS in blobs and _THEIRS in blobs and not _has_markers(root / path):
            run_git("add", "--", path, cwd=root)
            staged.append(path)
    return staged


def resolve(root: Path) -> Resolution:
    """Resolve every conflicted path whose answer is mechanical, and stage it.

    Order matters: already-seen paths first; generated artifacts only once
    no source conflicts remain (they are regenerated from the merged
    tree); the version slot last, because its resolver refuses while any
    other path is unmerged.

    Args:
        root: Repo root, mid-merge.

    Returns:
        What was resolved, and the paths that need judgment.

    Raises:
        UpdateRefusedError: When a generator fails or the slot resolver
            refuses for a reason other than other conflicts.
    """
    done = Resolution()
    for path in unmerged_paths(root):
        blobs = _stage_blobs(root, path)
        theirs, ours = blobs.get(_THEIRS), blobs.get(_OURS)
        if theirs and ours and theirs in _branch_blobs(root, path):
            _take_ours(root, path)
            done.seen.append(path)
    commands = regen_commands(root)
    left = unmerged_paths(root)
    sources = [p for p in left if p not in commands and p not in _SLOT_PATHS]
    if sources:
        done.remaining = left
        return done
    for path in [p for p in left if p in commands]:
        if not regenerate_artifact(root, path, commands[path]):
            msg = (
                f"regenerating {path} failed (see above); fix the generator "
                "error, then run `forge-update-branch --continue`."
            )
            raise UpdateRefusedError(msg)
        run_git("add", "--", path, cwd=root)
        done.generated.append(path)
    if set(unmerged_paths(root)) & _SLOT_PATHS:
        outcome = rebump(root)
        if outcome.refusal:
            done.remaining = unmerged_paths(root)
            logger.warning("forge-update-branch: version slot: %s", outcome.refusal)
            return done
        done.slot.extend(outcome.staged)
    done.remaining = unmerged_paths(root)
    return done


def _base_ref(root: Path, base: str | None) -> tuple[str, str]:
    """Return the base branch name and its remote-tracking ref.

    Args:
        root: Repo root.
        base: ``--base`` value, or ``None`` for ``[tool.forge].base_branch``.

    Returns:
        ``(name, "origin/<name>")``.

    Raises:
        UpdateRefusedError: On a dash-prefixed name (never passed to git).
    """
    name = base or load_config(root).base_branch
    if name.startswith("-"):
        msg = f"base {name!r} is not a branch name."
        raise UpdateRefusedError(msg)
    return name, f"origin/{name}"


def _dirty(root: Path) -> bool:
    """Return whether the tree has any change, untracked files included.

    Args:
        root: Repo root.

    Returns:
        ``True`` when ``git status`` lists anything.
    """
    return bool(run_git("status", "--porcelain", "-z", cwd=root).strip("\0"))


def _record_stop(root: Path, base_ref: str, done: Resolution) -> Path | None:
    """Write an intake lesson for a stop, so the escape's use is measured.

    The command knows the git state; the main agent adds why the
    conflict needed judgment. A class that recurs is filed as an issue.

    Args:
        root: Repo root.
        base_ref: The merged base ref.
        done: The resolve pass that stopped.

    Returns:
        The lesson's path, or ``None`` when it could not be written.
    """
    try:
        path = new_entry(
            configured_dir(root),
            "lesson",
            "update-branch-stop",
            f"forge-update-branch stopped on {len(done.remaining)} conflict(s)",
            "forge-update-branch",
        )
    except (OSError, ValueError) as exc:
        logger.warning(
            "forge-update-branch: could not write the intake record: %s", exc
        )
        return None

    # Paths come from the base, which anyone with push access there wrote:
    # strip what could close the code span or start a new line.
    def inert(name: str) -> str:
        return _UNSAFE_NAME_RE.sub("?", name)

    happened = [
        f"Merging {base_ref} stopped on paths that need judgment:",
        *(f"- `{inert(p)}`" for p in done.remaining),
    ]
    if done.automatic:
        resolved = ", ".join(f"`{inert(p)}`" for p in done.automatic)
        happened.append(f"Resolved automatically: {resolved}.")
    happened.append("Why they needed judgment: (main agent fills this in.)")
    text = path.read_text(encoding="utf-8").replace(
        "## What happened\n", "## What happened\n\n" + "\n".join(happened) + "\n", 1
    )
    path.write_text(text, encoding="utf-8")
    return path


def _listed(paths: list[str]) -> str:
    """Return *paths* comma-joined, capped for a commit message.

    Args:
        paths: Paths to list.

    Returns:
        At most :data:`_MAX_LISTED` paths, then a count of the rest.
    """
    shown = ", ".join(paths[:_MAX_LISTED])
    rest = len(paths) - _MAX_LISTED
    return f"{shown} and {rest} more" if rest > 0 else shown


def _message(base_ref: str, branch: str, done: Resolution) -> str:
    """Return the merge commit message, naming every automatic resolution.

    Args:
        base_ref: The merged base ref.
        branch: The branch being updated.
        done: Every resolve pass's result, combined.

    Returns:
        The message; checked for AI attribution, not for length, because
        its body is a generated list.

    Raises:
        UpdateRefusedError: When the message would carry AI attribution.
    """
    lines = [f"Merge {base_ref} into {branch}"]
    body = [
        f"Kept the branch side (already in its history): {_listed(done.seen)}"
        if done.seen
        else "",
        f"Regenerated: {_listed(done.generated)}" if done.generated else "",
        f"Version slot: {_listed(done.slot)}" if done.slot else "",
    ]
    body = [line for line in body if line]
    if body:
        lines.extend(["", *body])
    message = "\n".join(lines)
    try:
        validate_no_ai_attribution(message)
    except ValidationError as exc:
        raise UpdateRefusedError(str(exc)) from exc
    return message


def _blob(root: Path, rev: str, path: str) -> str | None:
    """Return the blob *path* holds at *rev*, or ``None`` when absent.

    Args:
        root: Repo root.
        rev: A commit, or ``""`` for the index.
        path: Repo-relative file path.

    Returns:
        The blob id.
    """
    out = run_git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"{rev}:{path}",
        cwd=root,
        check=False,
        log_errors=False,
    ).strip()
    return out or None


def _conflicted_and_automerge(root: Path) -> tuple[set[str], str]:
    """Return the paths the merge conflicts on and git's automatic merge tree.

    Args:
        root: Repo root, mid-merge.

    Returns:
        The conflicted paths and the tree ``git merge-tree`` writes (its
        conflicted paths hold conflict markers).

    Raises:
        UpdateRefusedError: When git cannot compute the merge.
    """
    out = run_git(
        "merge-tree",
        "--write-tree",
        "--name-only",
        "-z",
        "HEAD",
        "MERGE_HEAD",
        cwd=root,
        check=False,
        log_errors=False,
    )
    # Exit 1 (conflicts) still prints the tree, then the conflicted names,
    # then an empty field before git's informational messages.
    sections = out.split("\0\0", 1)[0].split("\0")
    if not _TREE_RE.match(sections[0]):
        msg = "git could not recompute the merge (git 2.38+ needed); finish by hand."
        raise UpdateRefusedError(msg)
    return {p for p in sections[1:] if p}, sections[0]


def _check_resolution(root: Path, done: Resolution) -> list[str]:
    """Check the staged result and return the paths edited by hand.

    A run that stopped already resolved some paths, which ``--continue``
    no longer sees as unmerged; those taken as already seen are added to
    *done* again so the merge commit message lists every one.

    Args:
        root: Repo root, mid-merge, nothing unmerged.
        done: This run's automatic resolutions; extended in place.

    Returns:
        Conflicted paths whose staged content matches neither side.

    Raises:
        UpdateRefusedError: When a path outside the conflicted set differs
            from git's automatic merge.
    """
    conflicted, automerge = _conflicted_and_automerge(root)
    for path in sorted(conflicted - set(done.automatic)):
        theirs = _blob(root, "MERGE_HEAD", path)
        if _blob(root, "", path) == _blob(
            root, "HEAD", path
        ) and theirs in _branch_blobs(root, path):
            done.seen.append(path)
    automatic = set(done.automatic)
    staged = run_git("write-tree", cwd=root).strip()
    differing = {
        p
        for p in run_git(
            "diff-tree",
            "-r",
            "--name-only",
            "-z",
            "--no-renames",
            automerge,
            staged,
            cwd=root,
        ).split("\0")
        if p
    }
    outside = sorted(differing - conflicted - automatic)
    if outside:
        msg = (
            f"staged changes outside the conflicted files: {outside}. A merge "
            "commit holds only the merge; restore those paths "
            "(`git restore --staged --worktree --source=MERGE_HEAD -- <path>` "
            "or `--source=HEAD`), then rerun `--continue`; commit the edits "
            "afterwards with forge-commit."
        )
        raise UpdateRefusedError(msg)
    return sorted(
        p
        for p in conflicted - automatic
        if _blob(root, "", p)
        not in {_blob(root, "HEAD", p), _blob(root, "MERGE_HEAD", p)}
    )


def _commit(root: Path, message: str, *, gated: bool) -> None:
    """Commit the merge, with or without the pre-commit gate.

    Args:
        root: Repo root, mid-merge.
        message: The merge commit message.
        gated: Run the pre-commit steps (hand edits present).

    Raises:
        UpdateRefusedError: When the commit fails; the merge stays in progress.
    """
    # Gated: pin the deferral off, so an inherited value cannot skip the gate.
    env = {MERGE_SYNC_ENV: "0" if gated else "1"}
    try:
        create_commit(root, message, env=env)
    except subprocess.CalledProcessError as exc:
        msg = (
            "the merge commit was blocked by the pre-commit gate; the merge "
            "stays in progress. Run forge:precommit-fixer, then "
            "`forge-update-branch --continue`."
            if gated
            else f"git refused the merge commit; the merge stays in progress. {exc}"
        )
        raise UpdateRefusedError(msg) from exc


def _finish(root: Path, base_ref: str, done: Resolution) -> int:
    """Check and commit a fully resolved merge.

    Args:
        root: Repo root, mid-merge.
        base_ref: The merged base ref.
        done: Every automatic resolution made.

    Returns:
        An exit code.
    """
    branch = current_branch(root)
    hand = _check_resolution(root, done)
    if hand:
        logger.info(
            "forge-update-branch: hand-edited %s — running the pre-commit gate.",
            ", ".join(hand),
        )
    _commit(root, _message(base_ref, branch, done), gated=bool(hand))
    sha = run_git("rev-parse", "--short", "HEAD", cwd=root).strip()
    logger.info(
        "forge-update-branch: merged %s into %s (%s); not pushed — publish via "
        "forge-commit, which gates the push on the pre-commit verdict.",
        base_ref,
        branch,
        sha,
    )
    return EXIT_OK


def _stop(root: Path, base_ref: str, done: Resolution) -> int:
    """Report the conflicts that need judgment and leave the merge in progress.

    Args:
        root: Repo root, mid-merge.
        base_ref: The merged base ref.
        done: The resolve pass.

    Returns:
        :data:`EXIT_STOPPED`.
    """
    if done.automatic:
        logger.info("forge-update-branch: resolved %s", ", ".join(done.automatic))
    record = _record_stop(root, base_ref, done)
    logger.warning(
        "forge-update-branch: stopped — these conflicts need judgment: %s.\n"
        "Next: spawn forge:merge-resolver to resolve them (or resolve and "
        "`git add` them yourself), then run `forge-update-branch --continue`."
        "%s",
        ", ".join(done.remaining),
        f"\nIntake record: {record.relative_to(root)} — add why it needed judgment."
        if record
        else "",
    )
    return EXIT_STOPPED


def start(root: Path, base: str | None) -> int:
    """Merge the base into the current branch and resolve what is mechanical.

    Args:
        root: Repo root.
        base: ``--base`` value.

    Returns:
        An exit code.

    Raises:
        UpdateRefusedError: When a guard refuses.
    """
    current_branch(root)
    if merge_in_progress(root):
        msg = (
            "a merge is already in progress; "
            "finish it with `forge-update-branch --continue`."
        )
        raise UpdateRefusedError(msg)
    name, base_ref = _base_ref(root, base)
    if not fetch_quietly(root, "origin", name):
        msg = f"could not fetch {base_ref}; check the network and remote, then rerun."
        raise UpdateRefusedError(msg)
    if is_ancestor(root, base_ref, "HEAD"):
        logger.info("forge-update-branch: already up to date with %s.", base_ref)
        return EXIT_OK
    if _dirty(root):
        checkpoint(root, f"wip-sync: before updating from {base_ref}")
    # Exit 1 on conflicts is expected; whether the merge started is read
    # from MERGE_HEAD, not from the exit status.
    run_git(
        "merge",
        "--no-commit",
        "--no-ff",
        "--no-edit",
        "--end-of-options",
        base_ref,
        cwd=root,
        check=False,
        log_errors=False,
    )
    if not merge_in_progress(root):
        msg = (
            f"git merge of {base_ref} did not start (an untracked file in the way?); "
            "see `git status`, then rerun."
        )
        raise UpdateRefusedError(msg)
    done = resolve(root)
    if done.remaining:
        return _stop(root, base_ref, done)
    return _finish(root, base_ref, done)


def resume(root: Path, base: str | None) -> int:
    """Finish a merge in progress once its judgment conflicts are resolved.

    Args:
        root: Repo root.
        base: ``--base`` value.

    Returns:
        An exit code.

    Raises:
        UpdateRefusedError: When a guard refuses.
    """
    current_branch(root)
    if not merge_in_progress(root):
        msg = "no merge in progress; run `forge-update-branch` to start one."
        raise UpdateRefusedError(msg)
    _, base_ref = _base_ref(root, base)
    # The base may have moved since the merge started; its old tip is fine.
    if not is_ancestor(root, "MERGE_HEAD", base_ref):
        msg = (
            f"the merge in progress is not of {base_ref}; finish it by hand, "
            "or pass the branch it merges with --base."
        )
        raise UpdateRefusedError(msg)
    hand = stage_hand_resolved(root)
    if hand:
        logger.info("forge-update-branch: staged hand-resolved %s", ", ".join(hand))
    done = resolve(root)
    if done.remaining:
        return _stop(root, base_ref, done)
    return _finish(root, base_ref, done)


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface."""
    parser = argparse.ArgumentParser(
        prog="forge-update-branch",
        description=(
            "Merge the base into the current branch, resolve the predictable "
            "conflicts, and commit the merge (exit 0 done, 1 refused, "
            "3 stopped on conflicts that need judgment). The branch is not "
            "pushed; publish via forge-commit."
        ),
    )
    parser.add_argument(
        "--continue",
        dest="resume",
        action="store_true",
        help="Finish a merge in progress after resolving its remaining conflicts.",
    )
    parser.add_argument(
        "--base",
        help="Branch to merge from (default: [tool.forge].base_branch); a "
        "stacked branch passes its parent.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``forge-update-branch``.

    Args:
        argv: Argument list; ``None`` reads :data:`sys.argv`.

    Returns:
        The exit code (see the module docstring).
    """
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        with git_env_overrides_removed():
            root = repo_root()
            if args.resume:
                return resume(root, args.base)
            return start(root, args.base)
    except (UpdateRefusedError, CommitRefusedError) as exc:
        logger.warning("forge-update-branch: refused — %s", exc)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
