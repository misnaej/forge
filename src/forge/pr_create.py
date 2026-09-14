"""forge-pr-create — publish a pull request from the branch being published.

The verification gate has to answer one question before a pull request
exists: does a wrap-up verify the tree about to be published? Answering it
used to mean a guard reading the ``gh pr create`` command an agent typed
and inferring the branch from its text. That inference cannot be made
reliable. The text carries free-form ``--title`` and ``--body`` prose that
can contain anything a flag looks like, a repeated flag resolves the
opposite way in a linear scan than it does in a real parser, and the guard
runs in the session's checkout, which is not necessarily the one holding
the branch. Two working steering proofs came out of review: both allowed
publishing a branch with no wrap-up at all.

This command removes the inference instead of improving it. It runs in the
checkout it publishes, so the branch is not a guess — it is where the
process is standing. It verifies that checkout's own wrap-up against that
checkout's own HEAD, earns the light and emergency escapes in the same
place, and only then creates the pull request. The guard's job shrinks to
refusing the raw command, which is a question about the command's *name*
and needs no understanding of its arguments.

That shape is not new here: ``forge-pr-wrapup post`` and
``block_raw_wrapup_post`` already moved wrap-up posting behind a command
for the same reason. A hook sees what an agent types, never what a forge
command does inside itself.

Usage:

- ``forge-pr-create --base main --title "…" --body-file body.md``
- ``forge-pr-create --draft --base main --title "…" --body-file body.md``
- extra ``gh`` flags after ``--`` are passed through untouched.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from typing import TYPE_CHECKING

from forge.emergency import consume as emergency_consume
from forge.git_utils import (
    configure_cli_logging,
    repo_root,
    resolve_current_branch,
    run_git,
)
from forge.pr_delta import extract_verified_shas
from forge.pr_plan import classify
from forge.pr_wrapup import WRAPUP_PATH


if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

configure_cli_logging()
logger = logging.getLogger(__name__)


def _verified(wrapup_text: str, head_sha: str) -> bool:
    """Whether the wrap-up names *head_sha* in a ``verified-at:`` header.

    Both abbreviations are accepted in either direction, because the
    wrap-up records a short SHA while ``rev-parse`` yields a full one.

    Args:
        wrapup_text: Full wrap-up contents.
        head_sha: The commit to match.

    Returns:
        ``True`` when one recorded SHA is a prefix of the other.
    """
    return any(
        head_sha.startswith(sha) or sha.startswith(head_sha)
        for sha in extract_verified_shas(wrapup_text)
    )


def _earn_light(root: Path, base: str) -> str | None:
    """Re-run the classifier so a light wrap-up is earned, not asserted.

    A ``light`` wrap-up skips the reporter round, so the escape is proved
    at publish time rather than taken on the author's word. The classifier
    is called as a function, not shelled out to and re-parsed: one
    implementation, and its verdict cannot drift from the CLI's. Fails
    CLOSED — any error or any verdict but light-code refuses.

    Args:
        root: Checkout holding the branch.
        base: Base branch the diff is classified against.

    Returns:
        A refusal reason, or ``None``.
    """
    try:
        mode = classify(root, f"origin/{base}", None).mode
    except Exception:
        logger.debug("classifier raised; refusing the light escape", exc_info=True)
        mode = ""
    if mode != "light-code":
        return (
            f"the wrap-up declares wrapup-mode: light but the classifier reads "
            f"this diff as '{mode or 'unclassifiable'}' against origin/{base} — "
            "the light escape is not earned. Author the full wrap-up."
        )
    return None


def _spend_emergency(root: Path) -> str | None:
    """Consume the armed emergency sentinel, or refuse.

    Exactly one publication per ``forge-emergency start``, which filed a
    public ledger issue first. Consumed through the same function the CLI
    calls, so the sentinel has one owner and cannot be spent twice by two
    code paths disagreeing. Fails CLOSED.

    Args:
        root: Checkout holding the branch.

    Returns:
        A refusal reason, or ``None`` when the bypass was spent.
    """
    if emergency_consume(root) != 0:
        return (
            "wrapup-mode: emergency but no armed bypass (not started, expired, "
            "or already spent). A human arms one with `forge-emergency start "
            "--reason ...` — agents only on explicit user instruction."
        )
    logger.warning(
        "EMERGENCY bypass consumed — this PR publishes without verification; "
        "retro-verification is owed on the ledger issue."
    )
    return None


def _gate(root: Path, branch: str, base: str) -> str | None:
    """Return why publication is refused, or ``None`` to allow it.

    Every check runs against *root*, which is the checkout holding the
    branch being published. That is the whole point of the command: the
    tree judged and the tree published are the same tree by construction,
    not by inference from command text.

    Args:
        root: Checkout holding the branch.
        branch: Branch being published, for the messages.
        base: Base branch the diff is classified against.

    Returns:
        A refusal reason, or ``None``.
    """
    wrapup = root / WRAPUP_PATH
    if not wrapup.is_file():
        return (
            f"no authored wrap-up at {wrapup} for branch '{branch}'. "
            "Author it (`/pr` Step 3.92) before publishing."
        )
    text = wrapup.read_text(encoding="utf-8", errors="replace")
    head = run_git("rev-parse", "HEAD", cwd=root, check=False).strip()
    if not head:
        return f"{root} is not a git checkout, so nothing can be verified."
    if not _verified(text, head):
        return (
            f"{wrapup} does not name {head[:7]} in a verified-at: line — "
            "it was authored for a different tree. Re-run /pr Step 3.92."
        )
    # Traceability is never deferred, only verification: the HEAD match
    # above applies to an emergency publication too.
    if re.search(r"^wrapup-mode:[ \t]*emergency[ \t]*$", text, re.MULTILINE):
        return _spend_emergency(root)
    if re.search(r"^wrapup-mode:[ \t]*light[ \t]*$", text, re.MULTILINE):
        return _earn_light(root, base)
    return None


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="forge-pr-create",
        description=(
            "Verify the branch being published, then create its pull request."
        ),
    )
    parser.add_argument("--base", required=True, help="Base branch for the PR.")
    parser.add_argument("--title", required=True, help="PR title.")
    parser.add_argument("--body-file", required=True, help="File holding the PR body.")
    parser.add_argument("--draft", action="store_true", help="Open it as a draft.")
    parser.add_argument(
        "passthrough",
        nargs=argparse.REMAINDER,
        help="Extra gh flags after `--`, passed through untouched.",
    )
    return parser


def main() -> int:
    """Entry point for ``forge-pr-create``.

    Returns:
        ``0`` when the pull request was created, ``2`` when publication is
        refused or the underlying command failed. Refusing is the default
        for anything unclear: a pull request cannot be un-published.
    """
    args = _build_parser().parse_args()
    root = repo_root()
    resolved = resolve_current_branch(root)
    if resolved is None:
        logger.error(
            "REFUSED: no branch resolves here, so nothing identifies what "
            "would be published."
        )
        return 2
    branch, _source = resolved

    refusal = _gate(root, branch, args.base)
    if refusal is not None:
        logger.error("REFUSED: %s", refusal)
        return 2

    extra = [a for a in args.passthrough if a != "--"]
    cmd = [
        "gh",
        "pr",
        "create",
        "--base",
        args.base,
        "--head",
        branch,
        "--title",
        args.title,
        "--body-file",
        args.body_file,
        *(["--draft"] if args.draft else []),
        *extra,
    ]
    logger.info("publishing '%s' — verified against %s", branch, root / WRAPUP_PATH)
    proc = subprocess.run(cmd, check=False)
    return 0 if proc.returncode == 0 else 2


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
