"""forge-emergency — one-shot deferred-verification bypass with a public ledger.

When something must ship NOW, the expensive part of forge's PR flow is
the verification ceremony — the reporter round, the fix-adoption cycle,
the authored wrap-up. This CLI arms exactly ONE bypass of that ceremony:
the wrap-up gate accepts a ``wrapup-mode: emergency`` wrap-up while the
sentinel is armed, then the sentinel is spent.

Almost everything else stays enforced. The pre-commit battery holds,
because CI runs the same checks and a local bypass would only move the
red to CI and block the merge — with two carve-outs where that reasoning
does not reach: ``env_sync`` and ``plugin_sync`` self-skip in CI
(FOUNDATION §15), so there is no downstream run to move anything to, and
leaving them enforced once refused a release commit over a condition its
own remedy could not clear until that commit landed. Every FOUNDATION §2
safety hook stays fully enforced — none of them read the sentinel.

The mode is impossible to use quietly:

1. ``FORGE_EMERGENCY_ACK=1 forge-emergency start --reason <why>`` files
   the **ledger issue** FIRST (label ``emergency-mode``, tier-1) — no
   ledger, no mode — then writes the gitignored ``.forge-emergency``
   sentinel (ledger number, expiry, reason). The env-var prefix is the
   auditable human-authorization marker (same convention as
   ``FORGE_SKIP_WRAPUP_GATE``); ``start`` refuses without it. The
   no-ledger-no-mode guarantee holds for CLI-managed sentinels — a
   hand-written sentinel is the same trust boundary as editing the
   hooks themselves and is not defended against.
2. ``forge-pr-create`` spends the sentinel on its single allowed
   publication in two steps. It **claims** the bypass just before
   ``gh pr create`` — the branch is written into the sentinel under an
   exclusive lock, so two publishes cannot both use it — and **commits**
   the spend (sentinel marked spent, PR number recorded, ledger
   commented with the head SHA) only once the PR exists. A definite
   failure with no PR **releases** the claim, so the bypass survives for
   a retry; an unclear outcome keeps it claimed and names the recovery
   (``record-pr``, which accepts only an open PR on the claimed branch,
   or ``end --abandon``).
   A second emergency needs a fresh ``start`` — fresh reason, fresh
   ledger.
3. The TTL (default 4h, clamped to 24h) only backstops an armed-but-
   unused sentinel; expiry disarms automatically.
4. Repayment happens after the fix is delivered: the emergency PR gets
   its retroactive verification (a real wrap-up whose ``verified-at:``
   names the PR head), and ``forge-emergency end`` closes the ledger
   once that evidence exists — otherwise it reports the outstanding
   debt and leaves the tier-1 issue open.

Agents may run ``start`` only on an explicit user instruction — never on
their own judgment (FOUNDATION §6).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from forge.emergency_state import (
    SENTINEL_RELPATH,
    EmergencyState,
    lifecycle,
    read_state,
)
from forge.git_utils import (
    configure_cli_logging,
    emit,
    repo_root,
    resolve_current_branch,
)
from forge.pr_plan import wrapup_freshness


configure_cli_logging()
logger = logging.getLogger(__name__)


# Mutex for the claim's read-check-write, created O_EXCL so two concurrent
# publishes cannot both claim. It holds no state of its own: the claim is
# recorded in the sentinel, and the lock is removed once that write is done.
_CLAIM_LOCK = Path(".forge-emergency.lock")

# The one wording of how to settle a claimed publish that recorded no PR —
# status, end and forge-pr-create all print it.
RECOVERY_HINT = (
    "If the PR exists, run `forge-emergency record-pr <N>` (it checks that "
    "PR is open on the claimed branch); if not, a human runs "
    "`FORGE_EMERGENCY_ACK=1 forge-emergency end --abandon`."
)

_DEFAULT_TTL_HOURS = 4.0
_MAX_TTL_HOURS = 24.0

_LEDGER_LABEL = "emergency-mode"


def write_state(root: Path, state: EmergencyState) -> None:
    """Write the sentinel file and make sure it stays out of version control.

    Args:
        root: Repo root.
        state: State to record.
    """
    # Write-then-rename, so a crash mid-write never leaves a truncated
    # sentinel — which would read as "no emergency" and drop the debt.
    path = root / SENTINEL_RELPATH
    _ensure_gitignored(root, str(SENTINEL_RELPATH))
    # Its siblings — the claim lock and this temp file — are transient, but
    # a crash can leave either behind, and neither belongs in a commit.
    _ensure_gitignored(root, f"{SENTINEL_RELPATH}.*")
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ensure_gitignored(root: Path, name: str) -> None:
    """Append *name* to the root ``.gitignore`` when not already covered.

    Args:
        root: Repo root.
        name: Repo-relative path to ignore.
    """
    gitignore = root / ".gitignore"
    lines = (
        gitignore.read_text(encoding="utf-8").splitlines()
        if gitignore.is_file()
        else []
    )
    if name in (line.strip() for line in lines):
        return
    with gitignore.open("a", encoding="utf-8") as fh:
        fh.write(f"{name}\n")


def _gh(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` command, captured, never raising on non-zero exit.

    Args:
        *args: Arguments after ``gh``.

    Returns:
        The completed process.
    """
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


def _create_ledger_issue(reason: str, expires_at: str) -> int | None:
    """File the public ledger issue; return its number, or ``None`` on failure.

    Args:
        reason: The stated justification.
        expires_at: Sentinel expiry instant (for the issue body).

    Returns:
        Issue number, or ``None`` when ``gh`` failed (no ledger → no mode).
    """
    body = (
        "Requires: nothing\n\n"
        "## Emergency-mode ledger\n\n"
        f"**Reason:** {reason}\n\n"
        f"**Armed until:** {expires_at}\n\n"
        "One-shot deferred-verification bypass (`forge-emergency`). The "
        "single allowed `gh pr create` is recorded below when consumed. "
        "This issue stays open until the emergency PR carries its "
        "retroactive verification (a posted wrap-up whose `verified-at:` "
        "names the PR head) — close via `forge-emergency end`."
    )
    proc = _gh(
        "issue",
        "create",
        "--label",
        f"{_LEDGER_LABEL},tier-1-critical",
        "--title",
        f"EMERGENCY MODE armed: {reason[:80]}",
        "--body",
        body,
    )
    if proc.returncode != 0:
        return None
    tail = proc.stdout.strip().rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _cmd_start(root: Path, reason: str, ttl_hours: float) -> int:
    """Arm the one-shot bypass: ledger issue first, then the sentinel.

    Args:
        root: Repo root.
        reason: Required justification.
        ttl_hours: Arm lifetime; clamped to ``(0, 24]``.

    Returns:
        ``0`` armed; ``1`` refused.
    """
    if os.environ.get("FORGE_EMERGENCY_ACK") != "1":
        emit(
            "emergency: refused — arming requires the explicit human "
            "authorization marker: FORGE_EMERGENCY_ACK=1 forge-emergency "
            "start --reason ... (agents: only on explicit user instruction)."
        )
        return 1
    if read_state(root) is not None:
        # Armed, claimed, spent-but-unrepaid or expired: each is a recorded
        # event whose ledger would be orphaned by overwriting its sentinel.
        emit(
            "emergency: refused — an emergency is already recorded here "
            "(see `forge-emergency status`). One event at a time: settle it "
            "with `forge-emergency end` first."
        )
        return 1
    ttl = min(max(ttl_hours, 0.1), _MAX_TTL_HOURS)
    expires_at = (datetime.now(UTC) + timedelta(hours=ttl)).isoformat()
    ledger = _create_ledger_issue(reason, expires_at)
    if ledger is None:
        emit(
            "emergency: could not create the ledger issue (gh missing, "
            "unauthenticated, or the label is absent) — no ledger, no mode."
        )
        return 1
    # No sentinel exists here, so any lock is an orphan left by a crashed
    # claim — a fresh event must not inherit it, or it would falsely
    # refuse this event's single legitimate claim.
    (root / _CLAIM_LOCK).unlink(missing_ok=True)
    write_state(
        root,
        EmergencyState(ledger_issue=ledger, reason=reason, expires_at=expires_at),
    )
    emit(
        f"⚠️ EMERGENCY MODE ARMED — one bypass, until {expires_at}. "
        f"Ledger: #{ledger}. The wrap-up gate will accept a single "
        "`wrapup-mode: emergency` wrap-up; everything else stays enforced."
    )
    return 0


def _cmd_status(root: Path) -> int:
    """Print the sentinel state.

    Args:
        root: Repo root.

    Returns:
        ``0`` when armed, ``1`` otherwise (absent, claimed, spent, or
        expired).
    """
    state = read_state(root)
    stage = lifecycle(state)
    if state is None:
        emit("emergency: not armed.")
        return 1
    emit(
        f"emergency: {stage.upper()} — ledger #{state.ledger_issue}, "
        f"reason: {state.reason}, expires {state.expires_at}."
    )
    if stage == "claimed":
        emit(
            f"emergency: a publish of '{state.claimed_branch}' claimed the "
            f"bypass but recorded no PR. {RECOVERY_HINT}"
        )
    return 0 if stage == "armed" else 1


def claim(root: Path, branch: str) -> int:
    """Reserve the armed bypass for one publish of *branch*, without spending it.

    Records the claim in the sentinel and posts nothing: a publish that
    then fails must be able to hand the bypass back with :func:`release`,
    and a claim held only in a lock file could be handed out twice by
    deleting that file.

    Args:
        root: Repo root.
        branch: The branch being published.

    Returns:
        ``0`` claimed (the publish may proceed); ``1`` refused.
    """
    # O_CREAT|O_EXCL serialises the read-check-write: the second of two
    # concurrent publishes loses on EEXIST, or reads the first one's claim.
    try:
        os.close(os.open(root / _CLAIM_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        emit(
            "emergency: another publish is claiming the bypass right now "
            f"(or one crashed mid-claim and left {_CLAIM_LOCK}) — gate stays "
            "closed. See `forge-emergency status`."
        )
        return 1
    try:
        state = read_state(root)
        if lifecycle(state) != "armed" or state is None:
            emit("emergency: no armed bypass — gate stays closed.")
            return 1
        write_state(root, replace(state, claimed_branch=branch))
        return 0
    finally:
        (root / _CLAIM_LOCK).unlink(missing_ok=True)


def commit(root: Path, pr_number: int) -> int:
    """Spend a claimed bypass now that its PR exists.

    Marks the sentinel spent with the PR number and records the spend on
    the ledger with the head SHA.

    Args:
        root: Repo root.
        pr_number: The PR the claimed publish created.

    Returns:
        ``0`` spent; ``1`` when there is no claim to commit.
    """
    state = read_state(root)
    if lifecycle(state) != "claimed" or state is None:
        emit("emergency: no claimed bypass to commit — the spend is unrecorded.")
        return 1
    write_state(root, replace(state, spent=True, pr_number=pr_number))
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    proc = _gh(
        "issue",
        "comment",
        str(state.ledger_issue),
        "--body",
        f"Bypass consumed at `{head or 'unknown'}` — PR #{pr_number} "
        "published without verification. Retro-verification owed.",
    )
    if proc.returncode != 0:
        # The spend already happened (never double-spend on a transient gh
        # failure) — but the missing ledger line must be LOUD, not silent.
        emit(
            f"emergency: WARNING — ledger comment on #{state.ledger_issue} "
            "FAILED; the bypass is spent but unrecorded. Comment the head "
            "SHA on the ledger manually."
        )
    emit(
        f"emergency: bypass consumed by PR #{pr_number} (ledger #{state.ledger_issue})."
    )
    return 0


def release(root: Path) -> None:
    """Hand a claimed bypass back after a publish that created no PR.

    Only the publish that took the claim calls this, and only once it
    knows no PR exists; a spent or unclaimed sentinel is left untouched.

    Args:
        root: Repo root.
    """
    state = read_state(root)
    if lifecycle(state) == "claimed" and state is not None:
        write_state(root, replace(state, claimed_branch=None))


def _cmd_record_pr(root: Path, pr_number: int) -> int:
    """Record the emergency PR number structurally in the sentinel.

    The recovery path: ``forge-pr-create`` records the number itself when
    it can tell the PR exists, and leaves the bypass claimed — naming this
    command — when it cannot. Repayment reads this field, never free-text
    ledger comments, which anyone can post on a public issue (PR-number
    spoofing); the ledger comment is the audit trail, not the source of
    truth. Only a bypass some publish actually used may be recorded, and
    only once: a merely-armed one was never spent, and re-recording would
    silently re-point which PR owes the verification. The number must
    also name an open PR on the claimed branch: otherwise any freshly
    verified PR could be recorded, and its wrap-up would repay a debt the
    real emergency PR still owes.

    Args:
        root: Repo root.
        pr_number: The published emergency PR.

    Returns:
        ``0`` recorded; ``1`` refused (no sentinel, never claimed, a PR
        already recorded, or *pr_number* not an open PR on the branch).
    """
    state = read_state(root)
    stage = lifecycle(state)
    if state is None:
        emit("emergency: no sentinel — nothing to record the PR into.")
        return 1
    if state.pr_number is not None:
        emit(
            f"emergency: refused — PR #{state.pr_number} is already recorded "
            f"on ledger #{state.ledger_issue}; it cannot be re-pointed."
        )
        return 1
    if stage not in {"claimed", "spent"}:
        emit(
            "emergency: refused — the bypass was never claimed by a publish, "
            "so no emergency PR exists to record. Publish with forge-pr-create."
        )
        return 1
    refusal = _pr_mismatch(root, state, pr_number)
    if refusal is not None:
        emit(f"emergency: refused — {refusal}")
        return 1
    write_state(root, replace(state, spent=True, pr_number=pr_number))
    proc = _gh(
        "issue",
        "comment",
        str(state.ledger_issue),
        "--body",
        f"Emergency PR published: PR #{pr_number} (recorded in the "
        "sentinel; repayment closes this ledger).",
    )
    if proc.returncode != 0:
        emit(
            f"emergency: WARNING — ledger comment on #{state.ledger_issue} "
            f"FAILED; PR #{pr_number} is recorded but the ledger does not "
            "say so. Comment it on the ledger manually."
        )
    emit(f"emergency: PR #{pr_number} recorded (ledger #{state.ledger_issue}).")
    return 0


def _pr_mismatch(root: Path, state: EmergencyState, pr_number: int) -> str | None:
    """Say why *pr_number* is not the emergency PR, or ``None`` when it is.

    Fails closed: a PR ``gh`` cannot describe is refused.

    Args:
        root: Repo root, for the branch when the sentinel names none.
        state: The sentinel state.
        pr_number: The PR offered for recording.

    Returns:
        A refusal reason, or ``None``.
    """
    branch = state.claimed_branch
    if branch is None:
        resolved = resolve_current_branch(root)
        branch = resolved[0] if resolved else None
    if branch is None:
        return "no branch identifies which PR the bypass was spent on."
    proc = _gh(
        "pr", "view", str(pr_number), "--json", "headRefName,state,isCrossRepository"
    )
    if proc.returncode != 0:
        return f"gh could not describe PR #{pr_number}, so it cannot be checked."
    try:
        view = json.loads(proc.stdout)
        head, pr_state = str(view["headRefName"]), str(view["state"])
        cross = view["isCrossRepository"]
    except (ValueError, TypeError, KeyError):
        return f"gh could not describe PR #{pr_number}, so it cannot be checked."
    if cross is not False:
        # A fork can open a PR from a branch of the same name; only a PR
        # from this repository can be the one this checkout published.
        return f"PR #{pr_number} comes from a fork, not from this repository."
    if head != branch or pr_state != "OPEN":
        return (
            f"PR #{pr_number} is {pr_state.lower()} on '{head}', not open on "
            f"'{branch}' — the branch the bypass was claimed for."
        )
    return None


def _repayment_evidence(state: EmergencyState) -> tuple[int | None, bool]:
    """Return ``(pr_number, repaid)`` for the sentinel's recorded PR.

    The PR number comes from the sentinel's structural ``pr_number``
    field (``record-pr``) — free-text ledger comments are never trusted
    (anyone can comment on a public issue). Repaid means the PR's newest
    posted ``verified-at:`` SHA prefixes its current head — real
    verification landed after delivery — the same freshness verdict the
    §6 monitor polls, so the two never disagree on what "verified" means.

    Args:
        state: The sentinel state.

    Returns:
        ``(pr_number, repaid)`` — ``(None, False)`` when no PR was
        recorded; ``(pr_number, False)`` when ``gh`` fails or no
        matching wrap-up exists.
    """
    if state.pr_number is None:
        return None, False
    return state.pr_number, wrapup_freshness(state.pr_number).fresh is True


def _clear(root: Path) -> None:
    """Remove the sentinel and its claim lock.

    Args:
        root: Repo root.
    """
    (root / SENTINEL_RELPATH).unlink(missing_ok=True)
    (root / _CLAIM_LOCK).unlink(missing_ok=True)


def _cmd_abandon(root: Path, state: EmergencyState, *, claimed: bool) -> int:
    """Close the ledger of an interrupted publish that recorded no PR.

    Gated on the human marker because it is the one path that closes a
    ledger with neither a repaid PR nor an unused arm behind it — the
    claim may have published a PR nobody recorded.

    Args:
        root: Repo root.
        state: The sentinel state.
        claimed: Whether a publish holds an unspent claim.

    Returns:
        ``0`` abandoned and closed; ``1`` refused or the close failed.
    """
    if not claimed:
        emit(
            "emergency: refused — --abandon settles only a claimed publish "
            "with no PR recorded; this one is not. Run `forge-emergency end`."
        )
        return 1
    if os.environ.get("FORGE_EMERGENCY_ACK") != "1":
        emit(
            "emergency: refused — abandoning requires the explicit human "
            "authorization marker: FORGE_EMERGENCY_ACK=1 forge-emergency end "
            "--abandon (agents: only on explicit user instruction)."
        )
        return 1
    _gh(
        "issue",
        "comment",
        str(state.ledger_issue),
        "--body",
        "Emergency abandoned by a human — a publish claimed the bypass but "
        "no PR was recorded. Closing the ledger.",
    )
    close = _gh("issue", "close", str(state.ledger_issue))
    _clear(root)
    if close.returncode != 0:
        emit(
            f"emergency: WARNING — closing ledger #{state.ledger_issue} "
            "FAILED; the emergency is abandoned but the issue is still open. "
            "Close it manually."
        )
        return 1
    emit(f"emergency: abandoned; ledger #{state.ledger_issue} closed.")
    return 0


def _cmd_end(root: Path, *, abandon: bool = False) -> int:
    """Close the ledger when the emergency PR's verification debt is repaid.

    Args:
        root: Repo root.
        abandon: Settle an interrupted publish (claimed, no PR recorded)
            instead of refusing it.

    Returns:
        ``0`` ledger closed (or nothing to do); ``1`` debt outstanding or
        refused.
    """
    state = read_state(root)
    if state is None:
        emit("emergency: no sentinel — nothing to end.")
        return 0
    claimed = lifecycle(state) == "claimed"
    if abandon:
        return _cmd_abandon(root, state, claimed=claimed)
    if claimed:
        # Cancelling here would close the ledger of a publish that may
        # have created a PR — exactly the debt the ledger exists to hold.
        emit(
            f"emergency: refused — a publish claimed the bypass on ledger "
            f"#{state.ledger_issue} but recorded no PR. {RECOVERY_HINT}"
        )
        return 1
    if not state.spent:
        # Armed but never consumed (or expired unused): cancel cleanly.
        _gh(
            "issue",
            "comment",
            str(state.ledger_issue),
            "--body",
            "Emergency cancelled — the bypass was never consumed. "
            "Closing the ledger; no verification debt exists.",
        )
        _gh("issue", "close", str(state.ledger_issue))
        _clear(root)
        emit(f"emergency: unused arm cancelled; ledger #{state.ledger_issue} closed.")
        return 0
    pr_number, repaid = _repayment_evidence(state)
    if not repaid:
        emit(
            f"emergency: debt outstanding on ledger #{state.ledger_issue} — "
            + (
                f"PR #{pr_number} has no posted wrap-up naming its head. "
                "Run the full verification (/pr) against it first."
                if pr_number
                else "no emergency PR recorded on the ledger yet."
            )
        )
        return 1
    return _close_repaid(root, state, pr_number)


def _close_repaid(root: Path, state: EmergencyState, pr_number: int | None) -> int:
    """Comment on and close the ledger once its verification debt is repaid.

    Args:
        root: Repo root.
        state: The sentinel whose ledger is being closed.
        pr_number: The emergency PR that carries the posted wrap-up.

    Returns:
        ``0`` ledger closed; ``1`` the close call failed and the issue is
        still open.
    """
    _gh(
        "issue",
        "comment",
        str(state.ledger_issue),
        "--body",
        f"Repaid: PR #{pr_number} carries a posted wrap-up naming its "
        "head. Closing the ledger.",
    )
    close = _gh("issue", "close", str(state.ledger_issue))
    _clear(root)
    if close.returncode != 0:
        emit(
            f"emergency: WARNING — closing ledger #{state.ledger_issue} "
            "FAILED; the debt is repaid but the issue is still open. "
            "Close it manually."
        )
        return 1
    emit(f"emergency: ledger #{state.ledger_issue} repaid and closed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the ``forge-emergency`` CLI.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Subcommand exit code (``0`` success / ``1`` refusal or debt).
    """
    parser = argparse.ArgumentParser(
        prog="forge-emergency",
        description=(
            "One-shot deferred-verification bypass with a public ledger "
            "issue. Arms exactly one `wrapup-mode: emergency` publication; "
            "every safety hook stays fully enforced, and so does pre-commit "
            "apart from the two environment-sync steps that self-skip in CI."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="arm one bypass (files the ledger first)")
    start.add_argument("--reason", required=True, help="why the emergency exists")
    start.add_argument(
        "--ttl",
        type=float,
        default=_DEFAULT_TTL_HOURS,
        help="arm lifetime in hours (default 4, max 24)",
    )
    sub.add_parser("status", help="print armed/claimed/spent/expired state")
    record = sub.add_parser(
        "record-pr",
        help=(
            "record the emergency PR number when forge-pr-create could not "
            "tell whether its publish created one"
        ),
    )
    record.add_argument("pr_number", type=int, help="the emergency PR number")
    end = sub.add_parser("end", help="close the ledger once the debt is repaid")
    end.add_argument(
        "--abandon",
        action="store_true",
        help=(
            "close the ledger of a claimed publish that recorded no PR "
            "(needs FORGE_EMERGENCY_ACK=1)"
        ),
    )
    args = parser.parse_args(argv)
    root = repo_root()
    if args.command == "start":
        return _cmd_start(root, args.reason, args.ttl)
    if args.command == "status":
        return _cmd_status(root)
    if args.command == "record-pr":
        return _cmd_record_pr(root, args.pr_number)
    return _cmd_end(root, abandon=args.abandon)


if __name__ == "__main__":
    sys.exit(main())
