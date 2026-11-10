"""forge-commit — commit named files and push them, with forge's git guards in code.

The commit step every forge workflow ends in is mechanical: stage exactly
the files the caller names, commit with the caller's message, check the
commit holds those files, push. A command does that in one call instead
of a helper agent's model round trips (FOUNDATION §7 "Mechanical first").

Claude Code hooks only see commands an agent types, never what a CLI runs
internally, so every guard that would have stopped an agent typing these
git commands is enforced here (FOUNDATION §7: a CLI performing a
§2-guarded effect enforces that guard in its own code):

- never on a protected branch (``[tool.forge].base_branch``) or a detached HEAD;
- the pre-commit verdict must pass first (an armed ``forge-emergency``
  lets stale or missing logs through, never a failed step);
- the message is conventional, a 50-word target (warned up to 55, refused
  past it), with no AI attribution;
- the index must equal the named files before the commit, and the commit
  may add only what the hook regenerates (generated docs, the cadence stamp);
- the pre-commit hook runs on the commit (git's per-process overrides
  that could skip it are dropped);
- pushes never force and never delete; a rejected push is reported, the
  commit stays local.

Usage:

- ``forge-commit -m "fix: x" path/a path/b`` — commit, then push.
- ``forge-commit -m "fix: x" --no-push path/a`` — commit only.
- ``forge-commit --push-only`` — push commits that already exist (a merge
  commit, work committed earlier).
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from forge.agent_profile import render_edit_receipt, subagent_edits
from forge.config import load_config
from forge.emergency_state import active_state
from forge.gh_comments import ValidationError, validate_no_ai_attribution
from forge.git_utils import (
    configure_cli_logging,
    create_commit,
    git_env_overrides_removed,
    push_branch,
    repo_root,
    run_git,
)
from forge.pr_squash_comment import MAX_WORDS, TITLE_RE
from forge.precommit import verdict
from forge.regen_docs import API_DIGEST_DOC, CLI_REFERENCE_DOC
from forge.smart_test.lifecycle import STAMP_RELPATH
from forge.text_budget import ceiling


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_PUSH_FAILED: Final = 2

#: Files the pre-commit hook itself regenerates and stages into a commit it
#: lets through (the generated docs, the full-run cadence stamp); a commit
#: may hold these beyond the named files.
HOOK_STAGED_PATHS: Final = frozenset(
    {API_DIGEST_DOC, CLI_REFERENCE_DOC, str(STAMP_RELPATH)}
)
#: A push failing on these is GitHub being briefly unavailable, not a
#: rejection; it is retried a few times before being reported.
_TRANSIENT_PUSH: Final = re.compile(
    r"Permission denied \(publickey\)|Internal Server Error|fatal error in commit_refs"
    r"|Could not read from remote repository|Connection (?:reset|closed|timed out)",
)
PUSH_ATTEMPTS: Final = 4
PUSH_RETRY_DELAY_S: Final = 20


class CommitRefusedError(Exception):
    """A guard refused the request; the message says which and why."""


@dataclass(frozen=True)
class Request:
    """What the caller asked forge-commit to do."""

    message: str
    paths: tuple[str, ...]
    push: bool


def check_message(message: str) -> None:
    """Refuse a commit message forge's rules forbid.

    Args:
        message: The full commit message (title, optional body).

    Raises:
        CommitRefusedError: When the title is not conventional, the message runs
            past the ceiling of :data:`forge.pr_squash_comment.MAX_WORDS`
            (the 50-word target commit and squash messages share; see
            :mod:`forge.text_budget`), or it carries AI attribution.
    """
    title = message.strip().splitlines()[0] if message.strip() else ""
    if not TITLE_RE.match(title):
        msg = f"commit title {title!r} is not conventional (`<type>(scope)?: subject`)."
        raise CommitRefusedError(msg)
    words = len(message.split())
    limit = ceiling(MAX_WORDS)
    if words > limit:
        msg = (
            f"commit message is {words} words; budget is {MAX_WORDS}, "
            f"at most {limit} — cut {words - limit}."
        )
        raise CommitRefusedError(msg)
    if words > MAX_WORDS:
        logger.warning(
            "forge-commit: message is %d words, over the %d target (within %d).",
            words,
            MAX_WORDS,
            limit,
        )
    try:
        validate_no_ai_attribution(message)
    except ValidationError as exc:
        raise CommitRefusedError(str(exc)) from exc


def current_branch(root: Path) -> str:
    """The checked-out branch, refused when protected or detached.

    Args:
        root: Repo root.

    Returns:
        The branch name.

    Raises:
        CommitRefusedError: On a detached HEAD or the protected base branch.
    """
    branch = run_git("branch", "--show-current", cwd=root, check=False).strip()
    if not branch:
        msg = "HEAD is detached; check out a feature branch first."
        raise CommitRefusedError(msg)
    protected = load_config(root).base_branch
    if branch == protected:
        msg = f"{branch!r} is the protected base branch; branch first (FOUNDATION §6)."
        raise CommitRefusedError(msg)
    return branch


def check_verdict(root: Path) -> None:
    """Refuse unless the latest pre-commit verdict passed on this tree.

    An armed ``forge-emergency`` lets stale, missing or unknown logs
    through, because arming it changed the tree those logs describe; a
    failed step still refuses, armed or not.

    Args:
        root: Repo root.

    Raises:
        CommitRefusedError: When the verdict did not pass (and the emergency does not
            excuse it).
    """
    passed, lines = verdict(root)
    if passed:
        return
    failed = [line for line in lines if line.startswith("FAIL")]
    if active_state(root) is not None and not failed:
        logger.warning("forge-commit: emergency armed — committing on stale evidence.")
        return
    detail = "\n".join(lines[-6:])
    msg = (
        "pre-commit verdict did not pass; run forge:precommit-fixer first, then "
        "rerun forge-commit (the named files are left staged; it stages them "
        f"again).\n{detail}"
    )
    raise CommitRefusedError(msg)


def _git_paths(root: Path, *args: str) -> set[str]:
    """Run a ``-z`` path-listing git command and return its paths.

    Args:
        root: Repo root.
        *args: Git argv tail whose output is NUL-separated paths.

    Returns:
        The repo-relative paths.
    """
    return {p for p in run_git(*args, cwd=root).split("\0") if p}


def _staged(root: Path) -> set[str]:
    """Paths currently staged.

    Args:
        root: Repo root.

    Returns:
        The staged paths, repo-relative.
    """
    return _git_paths(root, "diff", "--cached", "--name-only", "-z")


def stage_exactly(root: Path, paths: tuple[str, ...]) -> None:
    """Stage *paths* and refuse when the index then holds anything else.

    Args:
        root: Repo root.
        paths: The files to commit, repo-relative.

    Raises:
        CommitRefusedError: When no path is named, something unrelated was already
            staged, or the staged set does not equal *paths* (a named
            file with no change, or a path git expanded).
    """
    if not paths:
        msg = "name the files to commit."
        raise CommitRefusedError(msg)
    already = _staged(root) - set(paths)
    if already:
        msg = f"other files are already staged: {sorted(already)}; unstage them first."
        raise CommitRefusedError(msg)
    # A removal staged earlier (`git rm`) is gone from both index and tree,
    # so `git add` cannot match it; it is already staged as it should be.
    removed = _git_paths(
        root, "diff", "--cached", "--name-only", "--diff-filter=D", "-z"
    )
    to_add = [p for p in paths if p not in removed]
    if to_add:
        try:
            run_git("add", "--", *to_add, cwd=root)
        except subprocess.CalledProcessError as exc:
            msg = f"could not stage the named files (a path git does not know?): {exc}"
            raise CommitRefusedError(msg) from exc
    staged = _staged(root)
    if staged != set(paths):
        msg = (
            "staged files do not match the request: "
            f"missing {sorted(set(paths) - staged)}, "
            f"extra {sorted(staged - set(paths))}."
        )
        raise CommitRefusedError(msg)


def committed_paths(root: Path) -> set[str]:
    """Paths the HEAD commit changed.

    A merge is diffed against its first parent explicitly: ``git show``
    prints a merge's combined diff, which lists only paths differing from
    every parent, so a resolved merge read as missing its staged files.
    ``--diff-merges=first-parent``, not ``-m --first-parent``: ``-m`` emits
    one diff per parent, so the paths became the union of both sides.

    Args:
        root: Repo root.

    Returns:
        The repo-relative paths in HEAD's diff against its first parent
        (every path, for a root commit).
    """
    return _git_paths(
        root,
        "diff-tree",
        "-r",
        "--root",
        "--diff-merges=first-parent",
        "--no-commit-id",
        "--name-only",
        "-z",
        "HEAD",
    )


def push(root: Path, branch: str) -> int:
    """Push *branch*, retrying briefly when GitHub is momentarily unavailable.

    Never forces and never deletes (:func:`forge.git_utils.push_branch`).
    Sets the upstream when the branch has none yet.

    Args:
        root: Repo root.
        branch: The branch to push.

    Returns:
        :data:`EXIT_OK` when pushed, :data:`EXIT_PUSH_FAILED` otherwise
        (the commits stay local).
    """
    upstream = run_git(
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{u}",
        cwd=root,
        check=False,
    ).strip()
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        result = push_branch(root, branch, set_upstream=not upstream)
        if result.ok:
            logger.info("forge-commit: pushed %s to origin/%s.", branch, branch)
            return EXIT_OK
        transient = bool(_TRANSIENT_PUSH.search(result.stderr or ""))
        if not transient or attempt == PUSH_ATTEMPTS:
            logger.error(
                "forge-commit: push failed (commits stay local): %s", result.stderr
            )
            return EXIT_PUSH_FAILED
        logger.warning(
            "forge-commit: push attempt %d failed (transient); retrying in %ds.",
            attempt,
            PUSH_RETRY_DELAY_S,
        )
        time.sleep(PUSH_RETRY_DELAY_S)
    return EXIT_PUSH_FAILED


def commit_and_push(root: Path, request: Request) -> int:
    """Run the guarded commit, then the push.

    Args:
        root: Repo root.
        request: The message, files and whether to push.

    Returns:
        An exit code.

    Raises:
        CommitRefusedError: When any guard refuses; nothing is committed then.
    """
    branch = current_branch(root)
    check_message(request.message)
    # Stage before reading the verdict: a new file is untracked until now,
    # pre-commit passes over untracked files, and the verdict would refuse
    # it as unchecked. Once staged, the commit's own pre-commit hook checks
    # it like every other file, so nothing goes unchecked.
    stage_exactly(root, request.paths)
    check_verdict(root)
    logger.info(
        "%s",
        render_edit_receipt(subagent_edits(root, paths=set(request.paths))),
    )
    try:
        create_commit(root, request.message)
    except subprocess.CalledProcessError as exc:
        msg = (
            "the commit was blocked (pre-commit hook or git); nothing committed. "
            f"Fix the failing step with forge:precommit-fixer. Detail: {exc}"
        )
        raise CommitRefusedError(msg) from exc
    committed = committed_paths(root)
    extra = committed - set(request.paths)
    if set(request.paths) - committed or extra - HOOK_STAGED_PATHS:
        msg = (
            "the commit holds other files than requested; it stays local, "
            "nothing pushed. Report it — never rewrite the commit."
        )
        raise CommitRefusedError(msg)
    if extra:
        logger.info("forge-commit: pre-commit hook added %s.", ", ".join(sorted(extra)))
    sha = run_git("rev-parse", "--short", "HEAD", cwd=root).strip()
    logger.info("forge-commit: committed %s on %s.", sha, branch)
    return push(root, branch) if request.push else EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface."""
    parser = argparse.ArgumentParser(
        prog="forge-commit",
        description=(
            "Commit exactly the named files with forge's guards enforced, then "
            "push (never force, never to a protected branch)."
        ),
    )
    parser.add_argument("paths", nargs="*", help="Files to commit (repo-relative).")
    parser.add_argument("-m", "--message", help="Commit message (conventional title).")
    parser.add_argument("--no-push", action="store_true", help="Commit only.")
    parser.add_argument(
        "--push-only",
        action="store_true",
        help="Push commits that already exist (no new commit).",
    )
    return parser


def _dispatch(args: argparse.Namespace, root: Path) -> int:
    """Route parsed arguments to push-only or a guarded commit.

    Args:
        args: Parsed CLI arguments.
        root: Repo root.

    Returns:
        An exit code from the selected path.

    Raises:
        CommitRefusedError: When the argument combination is invalid or a
            guard refuses.
    """
    # --push-only re-checks the branch, not the commits it publishes: each
    # was already refused or admitted by this command or the commit hook.
    if args.push_only:
        if args.paths or args.message:
            msg = "--push-only takes no files and no message."
            raise CommitRefusedError(msg)
        return push(root, current_branch(root))
    if not args.message:
        msg = "a commit needs -m/--message."
        raise CommitRefusedError(msg)
    request = Request(args.message, tuple(args.paths), push=not args.no_push)
    return commit_and_push(root, request)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``forge-commit``.

    Args:
        argv: Argument list; ``None`` reads :data:`sys.argv`.

    Returns:
        ``0`` on success, ``1`` when a guard refused, ``2`` when the push
        failed (commits stay local).
    """
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        # Every git call, not just the commit: an inherited GIT_DIR or
        # GIT_INDEX_FILE would otherwise stage into or inspect another repo.
        with git_env_overrides_removed():
            return _dispatch(args, repo_root())
    except CommitRefusedError as exc:
        logger.warning("forge-commit: refused — %s", exc)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
