"""forge-emergency — one-shot deferred-verification bypass with a public ledger.

When something must ship NOW, the expensive part of forge's PR flow is
the verification ceremony — the reporter round, the fix-adoption cycle,
the authored wrap-up. This CLI arms exactly ONE bypass of that ceremony:
the wrap-up gate accepts a ``wrapup-mode: emergency`` wrap-up while the
sentinel is armed, then the sentinel is spent. Everything else stays
fully enforced — the entire pre-commit battery (CI runs the same checks,
so a local bypass would only move the red to CI and block the merge) and
every FOUNDATION §2 safety hook (none of them read the sentinel).

The mode is impossible to use quietly:

1. ``forge-emergency start --reason <why>`` files the **ledger issue**
   FIRST (label ``emergency-mode``, tier-1) — no ledger, no mode — then
   writes the gitignored ``.forge-emergency`` sentinel (ledger number,
   expiry, reason).
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
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from forge.git_utils import configure_cli_logging, emit, repo_root
from forge.pr_delta import extract_verified_shas


configure_cli_logging()
logger = logging.getLogger(__name__)


SENTINEL_RELPATH = Path(".forge-emergency")

_DEFAULT_TTL_HOURS = 4.0
_MAX_TTL_HOURS = 24.0

_LEDGER_LABEL = "emergency-mode"


@dataclass(frozen=True)
class EmergencyState:
    """The armed (or spent) one-shot bypass recorded in the sentinel file.

    Attributes:
        ledger_issue: Number of the public ledger issue for this event.
        reason: The human-stated justification given at ``start``.
        expires_at: ISO-8601 UTC instant after which the arm is void.
        spent: ``True`` once the single allowed bypass was consumed.
    """

    ledger_issue: int
    reason: str
    expires_at: str
    spent: bool = False


def read_state(root: Path) -> EmergencyState | None:
    """Return the sentinel state, or ``None`` when absent or unreadable.

    Corrupt sentinel content degrades to ``None`` (disarmed) — the mode
    fails closed, never open.

    Args:
        root: Repo root.

    Returns:
        The recorded state, or ``None``.
    """
    path = root / SENTINEL_RELPATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return EmergencyState(
            ledger_issue=int(data["ledger_issue"]),
            reason=str(data["reason"]),
            expires_at=str(data["expires_at"]),
            spent=bool(data.get("spent", False)),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


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


def armed_state(root: Path) -> EmergencyState | None:
    """Return the state only when the bypass is currently usable.

    Usable means: sentinel present and parseable, not yet spent, and not
    expired.

    Args:
        root: Repo root.

    Returns:
        The armed state, or ``None``.
    """
    state = read_state(root)
    if state is None or state.spent:
        return None
    try:
        expires = datetime.fromisoformat(state.expires_at)
    except ValueError:
        return None
    if datetime.now(UTC) >= expires:
        return None
    return state


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


def _cmd_consume(root: Path) -> int:
    """Spend the armed bypass (called by the wrap-up gate hook).

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
    write_state(root, replace(state, spent=True))
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    _gh(
        "issue",
        "comment",
        str(state.ledger_issue),
        "--body",
        f"Bypass consumed at `{head or 'unknown'}` — one PR publishes "
        "without verification. Retro-verification owed.",
    )
    emit(f"emergency: bypass consumed (ledger #{state.ledger_issue}).")
    return 0


def _repayment_evidence(ledger_issue: int) -> tuple[int | None, bool]:
    """Return ``(pr_number, repaid)`` for the ledger's emergency PR.

    The PR number comes from the ledger comments (the ``/pr`` flow posts
    ``PR #N`` after publication). Repaid means the PR's newest posted
    ``verified-at:`` SHA prefixes its current head — real verification
    landed after delivery.

    Args:
        ledger_issue: The ledger issue number.

    Returns:
        ``(pr_number, repaid)`` — ``(None, False)`` when no PR is
        recorded or ``gh`` fails.
    """
    proc = _gh(
        "issue",
        "view",
        str(ledger_issue),
        "--json",
        "comments",
        "--jq",
        '[.comments[].body] | join("\\n")',
    )
    if proc.returncode != 0:
        return None, False
    pr_number = None
    for token in proc.stdout.replace("PR #", "\nPR #").splitlines():
        if token.startswith("PR #") and token[4:].split()[0].isdigit():
            pr_number = int(token[4:].split()[0])
    if pr_number is None:
        return None, False
    pr = _gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "headRefOid,comments",
    )
    if pr.returncode != 0:
        return pr_number, False
    try:
        data = json.loads(pr.stdout)
    except json.JSONDecodeError:
        return pr_number, False
    shas = extract_verified_shas(
        "\n".join(c.get("body", "") for c in data.get("comments", []))
    )
    head = data.get("headRefOid", "")
    return pr_number, bool(shas and head.startswith(shas[-1]))


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
    pr_number, repaid = _repayment_evidence(state.ledger_issue)
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
    _gh("issue", "close", str(state.ledger_issue))
    (root / SENTINEL_RELPATH).unlink(missing_ok=True)
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
            "pre-commit and every safety hook stay fully enforced."
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
        help="spend the armed bypass (called by the wrap-up gate hook)",
    )
    sub.add_parser("end", help="close the ledger once the debt is repaid")
    args = parser.parse_args(argv)
    root = repo_root()
    if args.command == "start":
        return _cmd_start(root, args.reason, args.ttl)
    if args.command == "status":
        return _cmd_status(root)
    if args.command == "consume":
        return _cmd_consume(root)
    return _cmd_end(root)


if __name__ == "__main__":
    sys.exit(main())
