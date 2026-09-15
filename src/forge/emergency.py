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
2. The wrap-up gate consumes the sentinel on its single allowed
   ``gh pr create`` (``forge-emergency consume``) and the consumption is
   ledger-commented with the head SHA. A second emergency needs a fresh
   ``start`` — fresh reason, fresh ledger.
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
    armed_state,
    read_state,
)
from forge.git_utils import configure_cli_logging, emit, repo_root
from forge.pr_plan import wrapup_freshness


configure_cli_logging()
logger = logging.getLogger(__name__)


# Consume-time mutex sibling — created O_EXCL so "exactly one" holds
# under concurrent gate invocations; removed together with the sentinel.
_CONSUME_LOCK = Path(".forge-emergency.lock")

_DEFAULT_TTL_HOURS = 4.0
_MAX_TTL_HOURS = 24.0

_LEDGER_LABEL = "emergency-mode"


def write_state(root: Path, state: EmergencyState) -> None:
    """Write the sentinel file and make sure it stays out of version control.

    Args:
        root: Repo root.
        state: State to record.
    """
    (root / SENTINEL_RELPATH).write_text(
        json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8"
    )
    _ensure_gitignored(root, str(SENTINEL_RELPATH))


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
    if armed_state(root) is not None:
        emit("emergency: already armed — one event at a time (see status).")
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
    # A fresh, disjoint event must never inherit mutex state from a
    # settled-but-unrepaid (or crash-interrupted) previous one — a stale
    # lock would falsely refuse this event's single legitimate consume.
    (root / _CONSUME_LOCK).unlink(missing_ok=True)
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
        ``0`` when armed, ``1`` otherwise (absent, spent, or expired).
    """
    state = read_state(root)
    if state is None:
        emit("emergency: not armed.")
        return 1
    armed = armed_state(root) is not None
    verdict = "ARMED" if armed else ("SPENT" if state.spent else "EXPIRED")
    emit(
        f"emergency: {verdict} — ledger #{state.ledger_issue}, "
        f"reason: {state.reason}, expires {state.expires_at}."
    )
    return 0 if armed else 1


def consume(root: Path) -> int:
    """Spend the armed bypass (called by forge-pr-create).

    Marks the sentinel spent and records the consumption on the ledger
    issue with the current head SHA. Refuses when nothing is armed.

    Args:
        root: Repo root.

    Returns:
        ``0`` consumed (gate may allow); ``1`` refused (gate must block).
    """
    state = armed_state(root)
    if state is None:
        emit("emergency: no armed bypass — gate stays closed.")
        return 1
    # One-shot under concurrency: O_CREAT|O_EXCL makes the armed→spent
    # transition a race-safe critical section — the second of two
    # concurrent consumers loses on EEXIST and the gate stays closed for
    # it. The lock lives until `end` removes it with the sentinel.
    try:
        os.close(os.open(root / _CONSUME_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        emit("emergency: bypass already being consumed — gate stays closed.")
        return 1
    write_state(root, replace(state, spent=True))
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
        f"Bypass consumed at `{head or 'unknown'}` — one PR publishes "
        "without verification. Retro-verification owed.",
    )
    if proc.returncode != 0:
        # The one-shot spend already happened (never double-spend on a
        # transient gh failure) — but the missing ledger line must be
        # LOUD, not silent: the hook forwards this stderr verbatim.
        emit(
            f"emergency: WARNING — ledger comment on #{state.ledger_issue} "
            "FAILED; the bypass is spent but unrecorded. Comment the head "
            "SHA on the ledger manually."
        )
    emit(f"emergency: bypass consumed (ledger #{state.ledger_issue}).")
    return 0


def _cmd_record_pr(root: Path, pr_number: int) -> int:
    """Record the emergency PR number structurally in the sentinel.

    Called by the ``/pr`` flow right after ``gh pr create``. Repayment
    reads this field — never free-text ledger comments, which anyone can
    post on a public issue (PR-number spoofing). A human-readable ledger
    comment is still posted as the audit trail, but it is not the source
    of truth.

    Args:
        root: Repo root.
        pr_number: The published emergency PR.

    Returns:
        ``0`` recorded; ``1`` when no sentinel exists to record into.
    """
    state = read_state(root)
    if state is None:
        emit("emergency: no sentinel — nothing to record the PR into.")
        return 1
    write_state(root, replace(state, pr_number=pr_number))
    _gh(
        "issue",
        "comment",
        str(state.ledger_issue),
        "--body",
        f"Emergency PR published: PR #{pr_number} (recorded in the "
        "sentinel; repayment closes this ledger).",
    )
    emit(f"emergency: PR #{pr_number} recorded (ledger #{state.ledger_issue}).")
    return 0


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


def _cmd_end(root: Path) -> int:
    """Close the ledger when the emergency PR's verification debt is repaid.

    Args:
        root: Repo root.

    Returns:
        ``0`` ledger closed (or nothing to do); ``1`` debt outstanding.
    """
    state = read_state(root)
    if state is None:
        emit("emergency: no sentinel — nothing to end.")
        return 0
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
        (root / SENTINEL_RELPATH).unlink(missing_ok=True)
        (root / _CONSUME_LOCK).unlink(missing_ok=True)
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
    _gh(
        "issue",
        "comment",
        str(state.ledger_issue),
        "--body",
        f"Repaid: PR #{pr_number} carries a posted wrap-up naming its "
        "head. Closing the ledger.",
    )
    close = _gh("issue", "close", str(state.ledger_issue))
    (root / SENTINEL_RELPATH).unlink(missing_ok=True)
    (root / _CONSUME_LOCK).unlink(missing_ok=True)
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
    sub.add_parser("status", help="print armed/spent/expired state")
    sub.add_parser(
        "consume",
        help="spend the armed bypass (called by forge-pr-create)",
    )
    record = sub.add_parser(
        "record-pr",
        help="record the published emergency PR number in the sentinel",
    )
    record.add_argument("pr_number", type=int, help="the emergency PR number")
    sub.add_parser("end", help="close the ledger once the debt is repaid")
    args = parser.parse_args(argv)
    root = repo_root()
    if args.command == "start":
        return _cmd_start(root, args.reason, args.ttl)
    if args.command == "status":
        return _cmd_status(root)
    if args.command == "consume":
        return consume(root)
    if args.command == "record-pr":
        return _cmd_record_pr(root, args.pr_number)
    return _cmd_end(root)


if __name__ == "__main__":
    sys.exit(main())
