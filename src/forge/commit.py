"""forge-commit — commit named files, push them only with evidence.

The commit step every forge workflow ends in is mechanical: stage exactly
the files the caller names, commit with the caller's message, check the
commit holds those files. A command does that in one call instead
of a helper agent's model round trips (FOUNDATION §7 "Mechanical first").
Committing is local by default; a push is opt-in (``--push`` /
``--push-only``) and gated on publication evidence.

Claude Code hooks only see commands an agent types, never what a CLI runs
internally, so every guard that would have stopped an agent typing these
git commands is enforced here (FOUNDATION §7: a CLI performing a
§2-guarded effect enforces that guard in its own code):

- never on a protected branch (``[tool.forge].base_branch``) or a detached HEAD;
- the pre-commit verdict must pass before anything is pushed (an armed
  ``forge-emergency`` lets stale or missing logs through, never a failed
  step) — the gate sits on the push seam, so every publication path pays it,
  including ``--push-only`` and any deferred-checkpoint recovery;
- verification precedes publication (FOUNDATION §6, first half): a push
  needs a wrap-up naming the current HEAD (``code_health/pr_wrapup.md``),
  an open PR for the branch (review-fix pushes), or an armed
  ``forge-emergency`` — see :func:`check_publication_evidence`;
  ``FORGE_SKIP_WRAPUP_GATE=1`` is the recorded user bypass;
- the message is conventional, with a 45-word advisory target (well over
  it warns, nothing is refused on length), and no AI attribution;
- the index must equal the named files before the commit, and the commit
  may add only what the hook regenerates (generated docs, the cadence stamp);
- the pre-commit hook runs on the commit (git's per-process overrides
  that could skip it are dropped);
- pushes never force and never delete; a rejected push is reported, the
  commit stays local.

Usage:

- ``forge-commit -m "fix: x" path/a path/b`` — commit only (local).
- ``forge-commit -m "fix: x" --push path/a`` — commit, then push if the
  publication-evidence gate allows.
- ``forge-commit --push-only`` — push commits that already exist, through
  the same gates.
- ``forge-commit --wip-sync -m "wip-sync: <what>"`` — checkpoint the whole
  dirty tree; the gate is deferred (``forge-update-branch`` takes one
  itself before merging the base). Pushes only with ``--push``.

Merges are not commits of the caller's own edits: while one is in
progress every form refuses and names ``forge-update-branch --continue``.
"""

from __future__ import annotations

import argparse
import logging
import os
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
    GhLookupError,
    code_health_dir,
    configure_cli_logging,
    create_commit,
    find_open_pr_by_head,
    git_env_overrides_removed,
    merge_in_progress,
    push_branch,
    ref_exists,
    repo_root,
    run_git,
)
from forge.pr_squash_comment import TITLE_RE
from forge.pr_wrapup import WRAPUP_NAME, wrapup_names_head
from forge.precommit import verdict
from forge.regen_docs import API_DIGEST_DOC, CLI_REFERENCE_DOC
from forge.smart_test.lifecycle import STAMP_RELPATH
from forge.text_budget import length_verdict, refusal_threshold


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_PUSH_FAILED: Final = 2

# A commit message's own target, not the squash message's. The two are
# different artifacts — a commit explains one step, a squash message the
# whole PR — so reaching into the other module's budget coupled numbers
# that have no reason to move together.
COMMIT_MAX_WORDS: Final = 45

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
#: The message marker of a checkpoint commit (`forge-commit --wip-sync`, which
#: `forge-update-branch` calls to secure a dirty tree before it syncs).
WIP_SYNC_PREFIX: Final = "wip-sync:"
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
    base: str | None = None


def check_message(message: str) -> None:
    """Refuse a commit message forge's rules forbid.

    Args:
        message: The full commit message (title, optional body).

    Raises:
        CommitRefusedError: When the title is not conventional, or the
            message carries AI attribution. Never for length — a long
            message warns and commits (see :mod:`forge.text_budget`).
    """
    title = message.strip().splitlines()[0] if message.strip() else ""
    if not TITLE_RE.match(title):
        msg = f"commit title {title!r} is not conventional (`<type>(scope)?: subject`)."
        raise CommitRefusedError(msg)
    check_budget_and_attribution(message)


def check_budget_and_attribution(message: str) -> None:
    """Warn about a long message, and refuse AI attribution.

    The half of :func:`check_message` a ``wip-sync:`` checkpoint shares:
    its title is a marker, not a conventional type, but the length and
    attribution rules still hold.

    The word count is a **ceiling, not a target** — the shortest message
    that carries what changed and why, never one written toward the
    number. Over it commits with one warning, because sending the author
    back to re-cut a written message costs more than the words. Far over
    it is refused: what makes a message quick to read is the process
    state it omits (FOUNDATION §6) rather than its count, and text that
    long is carrying exactly that.

    Args:
        message: The full commit message.

    Raises:
        CommitRefusedError: When the message carries AI attribution, or
            runs past the refusal threshold for its ceiling.
    """
    words = len(message.split())
    verdict = length_verdict(words, COMMIT_MAX_WORDS)
    if verdict == "refuse":
        msg = (
            f"forge-commit: message is {words} words, past the "
            f"{refusal_threshold(COMMIT_MAX_WORDS)} allowed for a "
            f"{COMMIT_MAX_WORDS}-word ceiling. Cut process state (test counts, "
            "green checks) and bookkeeping, never what changed or why."
        )
        raise CommitRefusedError(msg)
    if verdict == "warn":
        logger.warning(
            "forge-commit: message is %d words, over the %d-word ceiling — "
            "committing anyway. The words to lose are process state (test "
            "counts, green checks), never what changed or why. The ceiling is "
            "a limit, not a length to aim for.",
            words,
            COMMIT_MAX_WORDS,
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


def refuse_mid_merge(root: Path) -> None:
    """Refuse while a merge is in progress: merges belong to ``forge-update-branch``.

    A merge is mostly work already reviewed on the base, so a list of the
    caller's own files cannot describe it; ``forge-update-branch
    --continue`` checks and commits it instead.

    Args:
        root: Repo root.

    Raises:
        CommitRefusedError: When ``MERGE_HEAD`` exists.
    """
    if merge_in_progress(root):
        msg = (
            "a merge is in progress; forge-commit commits your own edits, not "
            "merges. Finish it with `forge-update-branch --continue`."
        )
        raise CommitRefusedError(msg)


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
        "pre-commit verdict did not pass; run forge-precommit-fixer first, then "
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


def _staged(root: Path, *diff_filter: str) -> set[str]:
    """Paths currently staged, a rename counted as its old and new path.

    ``--no-renames`` because :func:`committed_paths` reads ``diff-tree``,
    which never pairs renames: both reads must agree on what a rename is,
    or a staged rename is refused before or after it is committed.

    Args:
        root: Repo root.
        *diff_filter: Extra ``git diff`` arguments (e.g. ``--diff-filter=D``).

    Returns:
        The staged paths, repo-relative.
    """
    return _git_paths(
        root, "diff", "--cached", "--name-only", "--no-renames", *diff_filter, "-z"
    )


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
        if already & _staged(root, "--diff-filter=D") and set(paths) & _staged(
            root, "--diff-filter=A"
        ):
            msg += " If this is a rename, name both the old and new paths."
        raise CommitRefusedError(msg)
    # A removal staged earlier (`git rm`, or the old half of a `git mv`) is
    # gone from both index and tree, so `git add` cannot match it; it is
    # already staged as it should be.
    removed = _staged(root, "--diff-filter=D")
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

    Never a merge: :func:`refuse_mid_merge` sends those to
    ``forge-update-branch``.

    Args:
        root: Repo root.

    Returns:
        The repo-relative paths in HEAD's diff against its parent (every
        path, for a root commit).
    """
    return _git_paths(
        root,
        "diff-tree",
        "-r",
        "--root",
        "--no-commit-id",
        "--name-only",
        "-z",
        "HEAD",
    )


def check_publication_evidence(root: Path, branch: str) -> None:
    """Refuse a push that lacks publication evidence (FOUNDATION §6, first half).

    Verification precedes publication: a branch reaches the remote only
    when a wrap-up names the current HEAD (the /pr flow's evidence, the
    same one ``forge-pr-create`` checks), an open PR already exists for
    the branch (review-fix pushes are no rework), or an emergency is
    armed. ``FORGE_SKIP_WRAPUP_GATE=1`` is the recorded user bypass,
    honoured here with the same parity as ``block_unverified_pr_create``.
    An unreadable ``gh`` listing fails closed — an unknown PR state is not
    evidence of no PR.

    Args:
        root: Repo root.
        branch: The branch about to be pushed.

    Raises:
        CommitRefusedError: When no evidence allows the push; nothing is
            pushed and the message names the remedy.
    """
    if os.environ.get("FORGE_SKIP_WRAPUP_GATE") == "1":
        logger.warning(
            "forge-commit: FORGE_SKIP_WRAPUP_GATE=1 — skipping the "
            "publication-evidence gate (recorded user bypass)."
        )
        return
    if active_state(root) is not None:
        logger.warning(
            "forge-commit: emergency armed — pushing without fresh "
            "verification evidence."
        )
        return
    head = run_git("rev-parse", "HEAD", cwd=root, check=False).strip()
    wrapup = code_health_dir(root) / WRAPUP_NAME
    wrapup_stale = False
    if wrapup.is_file() and head:
        try:
            text = wrapup.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text and wrapup_names_head(text, head):
            return
        wrapup_stale = True
    try:
        pr_url = find_open_pr_by_head(root, branch)
    except GhLookupError as exc:
        msg = (
            f"could not determine whether an open PR exists for {branch!r} "
            f"({exc}), so the push is refused rather than run unverified. "
            "Author the wrap-up (/pr Step 3.92) so it names the current HEAD, "
            "or fix the gh failure and retry."
        )
        raise CommitRefusedError(msg) from exc
    if pr_url:
        logger.info("forge-commit: open PR at %s covers this branch.", pr_url)
        return
    if wrapup_stale:
        msg = (
            f"{wrapup} does not name {head[:7]} in a verified-at: line — it "
            "was authored for a different tree. Re-run /pr Step 3.92 so the "
            "wrap-up names the current HEAD, then push again."
        )
    else:
        msg = (
            f"no publication evidence for {branch!r}: no wrap-up naming the "
            f"current HEAD ({wrapup}) and no open PR. Verification precedes "
            "publication (FOUNDATION §6) — run /pr so the wrap-up names HEAD, "
            "then push with forge-commit --push / --push-only. The commit "
            "stays local."
        )
    raise CommitRefusedError(msg)


def push(root: Path, branch: str, *, tool: str = "forge-commit") -> int:
    """Push *branch* once the verdict and publication-evidence gates pass.

    Retries on transient errors.

    Every publication path goes through here, so the verdict gate pays any
    deferral (``FORGE_MERGE_SYNC`` / ``FORGE_WIP_SYNC``) at push time instead
    of never, and :func:`check_publication_evidence` enforces FOUNDATION
    §6's first half. Never forces and never deletes
    (:func:`forge.git_utils.push_branch`). Sets the upstream when the branch
    has none yet.

    Args:
        root: Repo root.
        branch: The branch to push.
        tool: The command named in the log lines (the caller's own name).

    Returns:
        :data:`EXIT_OK` when pushed, :data:`EXIT_PUSH_FAILED` otherwise
        (the commits stay local).

    Raises:
        CommitRefusedError: When the pre-commit verdict did not pass or no
            publication evidence allows the push (and no armed emergency
            or recorded bypass excuses it); nothing is pushed.
    """
    check_verdict(root)
    check_publication_evidence(root, branch)
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
            logger.info("%s: pushed %s to origin/%s.", tool, branch, branch)
            return EXIT_OK
        transient = bool(_TRANSIENT_PUSH.search(result.stderr or ""))
        if not transient or attempt == PUSH_ATTEMPTS:
            logger.error(
                "%s: push failed (commits stay local): %s", tool, result.stderr
            )
            return EXIT_PUSH_FAILED
        logger.warning(
            "%s: push attempt %d failed (transient); retrying in %ds.",
            tool,
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
    refuse_mid_merge(root)
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
        env = {"FORGE_PR_BASE_REF": request.base} if request.base else None
        create_commit(root, request.message, env=env)
    except subprocess.CalledProcessError as exc:
        msg = (
            "the commit was blocked (pre-commit hook or git); nothing committed. "
            f"Fix the failing step with forge-precommit-fixer. Detail: {exc}"
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


def checkpoint(root: Path, message: str, *, push_after: bool = False) -> int:
    """Commit the whole working tree as a ``wip-sync:`` checkpoint.

    Secures dirty work before merging the base in; ``forge-update-branch``
    calls it itself (``forge-docs/git-safety.md`` "Base sync"). The gate is
    deferred, as for the human ``FORGE_WIP_SYNC=1`` form: the pre-commit hook
    reads that variable and runs no steps, and the next real commit and the PR squash
    run the full battery. So no verdict is read and no file list is
    compared: the checkpoint takes everything, untracked files included.

    Args:
        root: Repo root.
        message: The commit message; must start with ``wip-sync:``.
        push_after: Push the branch once committed, through the same gates
            (verdict deferred by the checkpoint, publication evidence not).

    Returns:
        An exit code.

    Raises:
        CommitRefusedError: On a protected or detached branch, a message
            without the ``wip-sync:`` marker, past the word ceiling or with
            AI attribution, or a clean tree.
    """
    branch = current_branch(root)
    refuse_mid_merge(root)
    if not message.startswith(WIP_SYNC_PREFIX):
        msg = f"a checkpoint message must start with {WIP_SYNC_PREFIX!r}."
        raise CommitRefusedError(msg)
    check_budget_and_attribution(message)
    run_git("add", "-A", cwd=root)
    if not _staged(root):
        msg = "nothing to checkpoint: the working tree is clean."
        raise CommitRefusedError(msg)
    logger.warning(
        "forge-commit: wip-sync checkpoint — gate deferred to the next real commit."
    )
    try:
        create_commit(root, message, env={"FORGE_WIP_SYNC": "1"})
    except subprocess.CalledProcessError as exc:
        msg = f"the checkpoint commit failed; nothing committed. Detail: {exc}"
        raise CommitRefusedError(msg) from exc
    sha = run_git("rev-parse", "--short", "HEAD", cwd=root).strip()
    logger.info("forge-commit: checkpoint %s on %s.", sha, branch)
    return push(root, branch) if push_after else EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface."""
    parser = argparse.ArgumentParser(
        prog="forge-commit",
        description=(
            "Commit exactly the named files with forge's guards enforced. "
            "Commits are local by default; --push / --push-only push, gated on "
            "publication evidence (never force, never to a protected branch)."
        ),
    )
    parser.add_argument("paths", nargs="*", help="Files to commit (repo-relative).")
    parser.add_argument("-m", "--message", help="Commit message (conventional title).")
    parser.add_argument(
        "--push",
        action="store_true",
        help=(
            "Push after committing. The push is refused unless publication "
            "evidence exists: a wrap-up naming the current HEAD, an open PR "
            "for the branch, or an armed forge-emergency."
        ),
    )
    parser.add_argument(
        "--push-only",
        action="store_true",
        help="Push commits that already exist (no new commit), through the same gates.",
    )
    parser.add_argument(
        "--wip-sync",
        action="store_true",
        help=(
            "Checkpoint the whole working tree (forge-update-branch takes one "
            "itself): no files named, message starts 'wip-sync:', gate "
            "deferred to the next real commit. Pushes only with --push."
        ),
    )
    parser.add_argument(
        "--base",
        metavar="REF",
        help=(
            "Explicit PR base ref for this commit's pre-commit gate, for a "
            "stacked branch before its PR exists: threaded to the hook as "
            "FORGE_PR_BASE_REF — below GITHUB_BASE_REF, above the open-PR "
            "and configured-base fallbacks. Real commits only."
        ),
    )
    return parser


def _check_push_only_args(args: argparse.Namespace) -> None:
    """Refuse a ``--push-only`` invocation that also carries commit arguments.

    Args:
        args: Parsed CLI arguments.

    Raises:
        CommitRefusedError: When ``--push`` or commit arguments are present.
    """
    if args.push:
        msg = "--push-only already pushes; --push with --push-only is redundant."
        raise CommitRefusedError(msg)
    if args.paths or args.message or args.wip_sync or args.base:
        msg = "--push-only takes no files, no message, no --wip-sync and no --base."
        raise CommitRefusedError(msg)


def _check_wip_sync_args(args: argparse.Namespace) -> None:
    """Refuse a ``--wip-sync`` invocation that carries files or a base.

    Args:
        args: Parsed CLI arguments.

    Raises:
        CommitRefusedError: When paths or ``--base`` are present.
    """
    if args.paths:
        msg = "--wip-sync takes no files: a checkpoint commits the whole tree."
        raise CommitRefusedError(msg)
    if args.base:
        msg = "--wip-sync defers the gate; --base applies to real commits only."
        raise CommitRefusedError(msg)


def _validated_base(root: Path, base: str | None) -> str | None:
    """Validate an explicit ``--base`` ref, returning it unchanged when valid.

    Args:
        root: Repo root.
        base: The ``--base`` value, or ``None``.

    Returns:
        *base* when it names a resolvable ref, else ``None``.

    Raises:
        CommitRefusedError: When the value looks like an option or no ref
            resolves to it.
    """
    if not base:
        return None
    if base.startswith("-"):
        msg = f"--base looks like an option: {base!r}."
        raise CommitRefusedError(msg)
    if not ref_exists(root, f"origin/{base}") and not ref_exists(root, base):
        msg = (
            f"--base {base!r} is not a resolvable ref (tried origin/{base} and {base})."
        )
        raise CommitRefusedError(msg)
    return base


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
    # --push-only publishes existing commits; the push seam gates on the
    # verdict and on publication evidence, so deferred checks
    # (FORGE_MERGE_SYNC / FORGE_WIP_SYNC) are paid here at push time.
    if args.push_only:
        _check_push_only_args(args)
        return push(root, current_branch(root))
    if not args.message:
        msg = "a commit needs -m/--message."
        raise CommitRefusedError(msg)
    if args.wip_sync:
        _check_wip_sync_args(args)
        return checkpoint(root, args.message, push_after=args.push)
    request = Request(
        args.message,
        tuple(args.paths),
        push=args.push,
        base=_validated_base(root, args.base),
    )
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
