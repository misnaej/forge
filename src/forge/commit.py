"""forge-commit — stage, commit and push in one guarded call.

A commit is a fixed sequence — check the pre-commit record, stage, commit,
push, record it — so it runs as one program call instead of an agent's
turn loop (FOUNDATION §7 "Mechanical first"). Hooks see only the command
an agent types, never this CLI's own ``git`` calls, so every guard a
commit needs is enforced here in code:

1. Read the state: current and base branch, an in-progress merge and its
   prepared message, the latest ``precommit_timing.log`` and each log's
   freshness verdict.
2. :func:`plan_commit` refuses — exit 2, nothing staged — on the base
   branch, an AI attribution, a non-conventional subject (normal commits
   only), a ``wip-sync:`` pairing mismatch, or a pre-commit record that is
   missing, stale or failed.
3. Stage ``--all`` or the named paths, then commit. The git pre-commit
   hook still runs as the real gate; when it blocks, its report is printed
   verbatim (exit 2). ``--no-verify``, ``--force`` and ``--amend`` are
   never passed.
4. Push, setting the upstream when the branch has none. A failed push
   exits 1 with the local commit intact.
5. Record the commit: the ``.plan/CONTINUATION.md`` line (after a push, or
   with ``--no-push``), a ``code_health/commit_history.log`` ledger line,
   and the subagent edit receipt for the committed files.

``--wip-sync`` makes FOUNDATION §2's checkpoint commit: ``git add -A``,
``FORGE_WIP_SYNC=1`` for its own git call only, a ``wip-sync:`` subject,
no pre-commit record needed, and no push — the checkpoint stays local.
Finishing an in-progress merge takes git's prepared message unless
``-m``/``-F`` is given. ``--push-only`` pushes commits that already exist.

Exit codes: ``0`` done; ``1`` a push failed (after a commit, or with
``--push-only``) or a record step failed; ``2`` nothing was committed —
refused, or the pre-commit hook blocked.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from forge import continuation_append
from forge.agent_profile import render_edit_receipt, subagent_edits
from forge.config import load_config
from forge.gh_comments import ValidationError, validate_no_ai_attribution
from forge.git_utils import (
    configure_cli_logging,
    create_commit,
    emit,
    merge_in_progress,
    merge_message,
    push_branch,
    repo_root,
    resolve_current_branch,
    run_git,
)
from forge.ledger import append_ledger_line
from forge.pr_squash_comment import TITLE_RE
from forge.precommit import freshness_verdicts, timing_markers


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


configure_cli_logging()
logger = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_LATE_FAILURE: Final = 1
EXIT_NOTHING_COMMITTED: Final = 2

WIP_SYNC_PREFIX: Final = "wip-sync:"
WIP_SYNC_ENV: Final = "FORGE_WIP_SYNC"
TIMING_LOG: Final = "precommit_timing"
COMMIT_LEDGER: Final = "commit_history.log"

_EXECUTED_MARKERS: Final = frozenset({"PASS", "WARN", "FAIL"})
_TRUSTED_VERDICTS: Final = frozenset({"fresh", "n/a"})
_REMEDY: Final = "run forge:precommit-fixer, then commit again"


@dataclass(frozen=True)
class CommitRequest:
    """What the caller asked to commit.

    Attributes:
        message: The commit message, or ``None`` to finish a merge with
            git's prepared message.
        paths: Paths to stage.
        stage_all: Stage every change (``git add -A``).
        wip_sync: Make FOUNDATION §2's checkpoint commit.
    """

    message: str | None
    paths: tuple[str, ...] = ()
    stage_all: bool = False
    wip_sync: bool = False


@dataclass(frozen=True)
class RepoState:
    """The repository facts a commit decision reads.

    Attributes:
        branch: The checked-out branch, or ``None`` on a detached HEAD.
        base_branch: The protected base branch (``[tool.forge].base_branch``).
        in_merge: Whether a merge is in progress.
        merge_msg: Git's prepared merge message, comment lines removed.
        wip_sync_env: Whether ``FORGE_WIP_SYNC=1`` is already set.
        timing_log: ``precommit_timing.log``'s text, or ``None`` when absent.
        verdicts: Log name → freshness verdict (``fresh``, ``stale``, …).
    """

    branch: str | None
    base_branch: str
    in_merge: bool = False
    merge_msg: str | None = None
    wip_sync_env: bool = False
    timing_log: str | None = None
    verdicts: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommitPlan:
    """A commit every check allows.

    Attributes:
        message: The message to commit with.
        mode: ``normal``, ``merge`` or ``wip-sync``.
    """

    message: str
    mode: str


@dataclass(frozen=True)
class Refusal:
    """Why nothing may be committed.

    Attributes:
        reason: The failed check and its remedy, in one sentence.
    """

    reason: str


@dataclass(frozen=True)
class _Committed:
    """A commit that landed, and what happened to it afterwards."""

    sha: str
    plan: CommitPlan
    branch: str
    staged: list[str]
    commit_s: float
    pushed: bool | None
    push_s: float


def plan_commit(request: CommitRequest, state: RepoState) -> CommitPlan | Refusal:
    """Decide whether *request* may be committed in *state*.

    Pure by design: every refusal is decided from facts the caller has
    already read, so each check is testable without a repository and the
    order of checks is visible in one place.

    Args:
        request: What the caller asked to commit.
        state: The repository facts to decide on.

    Returns:
        The commit to make, or the first check that refuses it.
    """
    mode = _mode(request, state)
    message = request.message
    if message is None and mode == "merge":
        message = state.merge_msg
    reason = (
        _branch_refusal(state)
        or _selection_refusal(request, mode)
        or _message_refusal(message, request, state, mode)
        or (None if mode == "wip-sync" else _evidence_refusal(state))
    )
    if reason is not None:
        return Refusal(reason)
    return CommitPlan(message=message or "", mode=mode)


def _mode(request: CommitRequest, state: RepoState) -> str:
    """Return the commit mode: ``wip-sync``, ``merge`` or ``normal``.

    Args:
        request: What the caller asked to commit.
        state: The repository facts to decide on.

    Returns:
        One of ``wip-sync`` (checkpoint commit), ``merge`` (finishing a merge),
        or ``normal`` (regular commit).
    """
    if request.wip_sync:
        return "wip-sync"
    if state.in_merge:
        return "merge"
    return "normal"


def _branch_refusal(state: RepoState) -> str | None:
    """Refuse a detached HEAD or the protected base branch.

    Args:
        state: The repository facts to decide on.

    Returns:
        A refusal reason if the branch is invalid (detached HEAD or protected base
        branch), or ``None`` if the branch is acceptable.
    """
    if state.branch is None:
        return "no branch is checked out (detached HEAD) — check out a branch first"
    if state.branch == state.base_branch:
        return (
            f"'{state.branch}' is the protected base branch — "
            "commit on a feature branch"
        )
    return None


def _selection_refusal(request: CommitRequest, mode: str) -> str | None:
    """Refuse a staging selection the mode cannot honour.

    Args:
        request: What the caller asked to commit.
        mode: The commit mode (``wip-sync``, ``merge``, or ``normal``).

    Returns:
        A refusal reason if the staging selection conflicts with the mode,
        or ``None`` if it is acceptable.
    """
    if mode == "wip-sync" and request.paths:
        return "--wip-sync stages every change — do not name paths"
    if mode == "merge" and request.paths:
        return (
            "a merge is in progress — stage the resolutions and finish it "
            "without naming paths (or pass --all)"
        )
    if mode == "normal" and not (request.stage_all or request.paths):
        return "nothing selected — name the paths to commit, or pass --all"
    return None


def _wip_sync_refusal(
    subject: str, request: CommitRequest, state: RepoState
) -> str | None:
    """Check for wip-sync pairing mismatches and env conflicts.

    Args:
        subject: The first line of the commit message.
        request: What the caller asked to commit.
        state: The repository facts to decide on.

    Returns:
        A refusal reason if the ``wip-sync`` mode is misconfigured (subject
        prefix or environment variable mismatch), or ``None`` if it is valid.
    """
    if subject.startswith(WIP_SYNC_PREFIX) != request.wip_sync:
        if request.wip_sync:
            return f"--wip-sync needs a subject starting with {WIP_SYNC_PREFIX!r}"
        return f"a {WIP_SYNC_PREFIX!r} subject needs --wip-sync"
    if state.wip_sync_env and not request.wip_sync:
        return (
            f"{WIP_SYNC_ENV}=1 is set in the environment — unset it, "
            "or pass --wip-sync for a checkpoint commit"
        )
    return None


def _message_refusal(
    message: str | None, request: CommitRequest, state: RepoState, mode: str
) -> str | None:
    """Refuse a missing, attributed, mispaired or non-conventional message.

    Args:
        message: The commit message, or ``None`` if not provided.
        request: What the caller asked to commit.
        state: The repository facts to decide on.
        mode: The commit mode (``wip-sync``, ``merge``, or ``normal``).

    Returns:
        A refusal reason if the message is invalid (missing, attributed, or
        non-conventional), or ``None`` if it is acceptable.
    """
    if message is None or not message.strip():
        return "no commit message — pass -m or -F"
    subject = message.strip().splitlines()[0]
    reason = _wip_sync_refusal(subject, request, state)
    if reason is not None:
        return reason
    try:
        validate_no_ai_attribution(message)
    except ValidationError as exc:
        return str(exc)
    if mode == "normal" and not TITLE_RE.match(subject):
        return (
            f"subject {subject!r} is not conventional-commit format "
            "('<type>(<scope>)!?: <subject>')"
        )
    return None


def _evidence_refusal(state: RepoState) -> str | None:
    """Refuse when the latest pre-commit run is missing, stale or failed.

    Only the steps that run executed (PASS, WARN, FAIL) are judged: a SKIP
    row made no claim about this tree. WARN is non-blocking by the step's
    own contract. The git hook re-runs every step at commit time, so this
    check is a fast refusal before a commit the hook would block — and a
    check that fixes were made before committing, not a replacement gate.

    Args:
        state: The repository facts to decide on.

    Returns:
        A refusal reason if the pre-commit record is missing, stale, or failed,
        or ``None`` if the record is fresh and passing.
    """
    if state.timing_log is None:
        return f"no pre-commit record (code_health/{TIMING_LOG}.log) — {_REMEDY}"
    if state.verdicts.get(TIMING_LOG) != "fresh":
        return f"the pre-commit record describes a different tree — {_REMEDY}"
    markers = timing_markers(state.timing_log)
    failed = sorted(name for name, marker in markers.items() if marker == "FAIL")
    if failed:
        return f"pre-commit steps failed: {', '.join(failed)} — {_REMEDY}"
    stale = sorted(
        name
        for name, marker in markers.items()
        if marker in _EXECUTED_MARKERS
        and state.verdicts.get(name) not in _TRUSTED_VERDICTS
    )
    if stale:
        return (
            f"pre-commit logs are not fresh for this tree: {', '.join(stale)} — "
            f"{_REMEDY}"
        )
    return None


def read_state(root: Path) -> RepoState:
    """Read the repository facts :func:`plan_commit` decides on.

    Args:
        root: Repository root.

    Returns:
        The current state. A branch resolved only from CI's
        ``GITHUB_HEAD_REF`` counts as detached: there is no local branch
        to commit on.
    """
    current = resolve_current_branch(root)
    in_merge = merge_in_progress(root)
    try:
        timing_log = (root / "code_health" / f"{TIMING_LOG}.log").read_text(
            encoding="utf-8"
        )
    except OSError:
        timing_log = None
    return RepoState(
        branch=current[0] if current is not None and current[1] == "local" else None,
        base_branch=load_config(root).base_branch,
        in_merge=in_merge,
        merge_msg=merge_message(root) if in_merge else None,
        wip_sync_env=os.environ.get(WIP_SYNC_ENV) == "1",
        timing_log=timing_log,
        verdicts=freshness_verdicts(root) if timing_log is not None else {},
    )


def _stage(root: Path, request: CommitRequest) -> list[str]:
    """Stage the selection and return the staged paths.

    Args:
        root: Repository root.
        request: The caller's selection.

    Returns:
        Repo-relative staged paths.
    """
    if request.wip_sync or request.stage_all:
        run_git("add", "-A", cwd=root, log_errors=False)
    elif request.paths:
        run_git("add", "--", *request.paths, cwd=root, log_errors=False)
    staged = run_git("diff", "--cached", "--name-only", cwd=root, check=False)
    return [line for line in staged.splitlines() if line.strip()]


def _commit(root: Path, plan: CommitPlan) -> tuple[str, float] | None:
    """Commit the index, printing the hook's report when it blocks.

    Args:
        root: Repository root.
        plan: The allowed commit.

    Returns:
        ``(sha, seconds)``, or ``None`` when nothing was committed.
    """
    env = {WIP_SYNC_ENV: "1"} if plan.mode == "wip-sync" else None
    started = time.monotonic()
    try:
        create_commit(root, plan.message, env=env, log_errors=False)
    except subprocess.CalledProcessError as exc:
        emit("forge-commit: the commit was blocked — nothing committed. Output:")
        for stream in (exc.stdout, exc.stderr):
            if stream and stream.strip():
                emit(stream.rstrip())
        return None
    return run_git("rev-parse", "HEAD", cwd=root), time.monotonic() - started


def _push(root: Path, branch: str) -> tuple[bool, float]:
    """Push *branch*, setting its upstream when it has none.

    Args:
        root: Repository root.
        branch: The branch to push.

    Returns:
        ``(pushed, seconds)``.
    """
    upstream = run_git(
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{u}",
        cwd=root,
        check=False,
    )
    started = time.monotonic()
    result = push_branch(root, branch, set_upstream=not upstream)
    elapsed = time.monotonic() - started
    if result.ok:
        emit(f"pushed: {branch}" + ("" if upstream else " (upstream set)"))
    else:
        emit(
            f"forge-commit: push FAILED — the commit is safe locally:\n{result.stderr}"
        )
    return result.ok, elapsed


def _record(root: Path, committed: _Committed) -> bool:
    """Record the commit: handoff line, ledger line, edit receipt.

    Args:
        root: Repository root.
        committed: The commit that landed.

    Returns:
        Whether every record step succeeded.
    """
    subject = committed.plan.message.strip().splitlines()[0]
    ok = True
    if committed.pushed is not False:
        status = continuation_append.main(
            ["--commit", committed.sha[:7], "--", subject], repo_root=root
        )
        ok = status == 0
    push_state = {True: "yes", False: "failed", None: "skipped"}[committed.pushed]
    try:
        append_ledger_line(
            root / "code_health" / COMMIT_LEDGER,
            {
                "sha": committed.sha[:7],
                "branch": committed.branch,
                "mode": committed.plan.mode,
                "files": len(committed.staged),
                "commit_s": f"{committed.commit_s:.1f}",
                "pushed": push_state,
                "push_s": f"{committed.push_s:.1f}",
            },
            tail=("subject", subject),
        )
    except OSError as exc:
        emit(f"forge-commit: could not write code_health/{COMMIT_LEDGER}: {exc}")
        ok = False
    emit(render_edit_receipt(subagent_edits(root, paths=committed.staged)))
    return ok


def _run(
    root: Path, request: CommitRequest, plan: CommitPlan, *, branch: str, push: bool
) -> int:
    """Stage, commit, push and record an allowed commit.

    Args:
        root: Repository root.
        request: The caller's selection.
        plan: The allowed commit.
        branch: The checked-out branch.
        push: Whether to push after committing.

    Returns:
        The process exit code.
    """
    try:
        staged = _stage(root, request)
    except subprocess.CalledProcessError as exc:
        emit(
            f"forge-commit: staging failed — nothing committed:\n"
            f"{(exc.stderr or '').strip()}"
        )
        return EXIT_NOTHING_COMMITTED
    if not staged and plan.mode != "merge":
        emit("forge-commit: nothing staged — nothing committed")
        return EXIT_NOTHING_COMMITTED
    outcome = _commit(root, plan)
    if outcome is None:
        return EXIT_NOTHING_COMMITTED
    sha, commit_s = outcome
    emit(f"committed: {sha[:7]} {plan.message.strip().splitlines()[0]}")
    pushed, push_s = _push(root, branch) if push else (None, 0.0)
    committed = _Committed(
        sha=sha,
        plan=plan,
        branch=branch,
        staged=staged,
        commit_s=commit_s,
        pushed=pushed,
        push_s=push_s,
    )
    record_ok = _record(root, committed)
    if pushed is False or not record_ok:
        return EXIT_LATE_FAILURE
    return EXIT_OK


def _push_only(root: Path) -> int:
    """Push existing commits on the current branch.

    Args:
        root: Repository root.

    Returns:
        The process exit code.
    """
    state = read_state(root)
    reason = _branch_refusal(state)
    if reason is not None:
        emit(f"forge-commit: refused — {reason}")
        return EXIT_NOTHING_COMMITTED
    pushed, _elapsed = _push(root, state.branch or "")
    return EXIT_OK if pushed else EXIT_LATE_FAILURE


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Argument vector; ``None`` reads ``sys.argv``.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="forge-commit",
        description=(
            "Stage, commit and push in one guarded call: refuses the base "
            "branch, AI attribution, a non-conventional subject and a "
            "missing, stale or failed pre-commit record; the git pre-commit "
            "hook still runs. Exit 0 done, 1 push or record step failed, "
            "2 nothing committed."
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("-m", "--message", help="Commit message.")
    source.add_argument(
        "-F", "--file", type=Path, help="Read the commit message from a file."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="stage_all",
        help="Stage every change (git add -A).",
    )
    parser.add_argument(
        "--wip-sync",
        action="store_true",
        help="Checkpoint commit before a base sync (FOUNDATION §2): stages "
        "everything; the subject must start with 'wip-sync:'.",
    )
    parser.add_argument(
        "--no-push", action="store_true", help="Commit without pushing."
    )
    parser.add_argument(
        "--push-only",
        action="store_true",
        help="Push existing commits; commit nothing.",
    )
    parser.add_argument("paths", nargs="*", help="Paths to stage and commit.")
    args = parser.parse_args(argv)
    if args.push_only and (
        args.message
        or args.file
        or args.stage_all
        or args.wip_sync
        or args.no_push
        or args.paths
    ):
        parser.error("--push-only takes no other options")
    if args.stage_all and args.paths:
        parser.error("pass --all or paths, not both")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``forge-commit``.

    Args:
        argv: Argument vector; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` done; ``1`` a push or record step failed; ``2`` nothing
        was committed.
    """
    args = _parse_args(argv)
    root = repo_root()
    if args.push_only:
        return _push_only(root)
    try:
        message = (
            args.file.read_text(encoding="utf-8")
            if args.file is not None
            else args.message
        )
    except OSError as exc:
        emit(f"forge-commit: cannot read the message file: {exc}")
        return EXIT_NOTHING_COMMITTED
    request = CommitRequest(
        message=message,
        paths=tuple(args.paths),
        stage_all=args.stage_all,
        wip_sync=args.wip_sync,
    )
    state = read_state(root)
    outcome = plan_commit(request, state)
    if isinstance(outcome, Refusal):
        emit(f"forge-commit: refused — {outcome.reason}")
        return EXIT_NOTHING_COMMITTED
    # A checkpoint secures work before a base merge; it stays local, as the
    # FOUNDATION §2 ladder's checkpoint always has.
    push = not (args.no_push or args.wip_sync)
    return _run(root, request, outcome, branch=state.branch or "", push=push)


if __name__ == "__main__":
    sys.exit(main())
