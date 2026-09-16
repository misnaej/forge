"""Reading the ``forge-emergency`` sentinel — the half with no dependencies.

``forge-emergency`` arms a one-shot, ledger-backed bypass by writing a
gitignored sentinel file. Two kinds of code care about it: the CLI that
files the ledger and manages the arm, and the gates that must stand down
while it is armed. Only the first needs ``gh``, the PR surface, or
anything else forge ships.

Splitting the reader out is what lets a gate ask. ``forge.emergency``
reaches the PR-planning surface, which reaches back into
``forge.precommit`` — so a pre-commit step importing the CLI closes a
cycle. This module imports nothing from forge at all, so anything may
read the sentinel without taking on the CLI's dependencies.

Reading fails **closed**: an absent, unreadable, or corrupt sentinel is
"not armed", never "armed". A bypass that could be switched on by a
damaged file would be no bypass discipline at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


SENTINEL_RELPATH = Path(".forge-emergency")


@dataclass(frozen=True)
class EmergencyState:
    """The armed (or spent) one-shot bypass recorded in the sentinel file.

    Attributes:
        ledger_issue: Number of the public ledger issue for this event.
        reason: The human-stated justification given at ``start``.
        expires_at: ISO-8601 UTC instant after which the arm is void.
        spent: ``True`` once the single allowed bypass was consumed.
        pr_number: The emergency PR, recorded structurally by
            ``record-pr`` after publication — repayment never trusts
            free-text ledger comments (anyone can comment on a public
            issue).
    """

    ledger_issue: int
    reason: str
    expires_at: str
    spent: bool = False
    pr_number: int | None = None


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
            pr_number=(
                int(data["pr_number"]) if data.get("pr_number") is not None else None
            ),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _unexpired(state: EmergencyState | None) -> EmergencyState | None:
    """Return *state* while its expiry is still in the future.

    Both predicates below need this, and a future correction — naive
    datetimes raise ``TypeError`` on the comparison rather than
    ``ValueError`` on the parse — should land in one place rather than
    two that drifted apart.

    Args:
        state: A read sentinel, or ``None``.

    Returns:
        *state* while unexpired, otherwise ``None``.
    """
    if state is None:
        return None
    try:
        expires = datetime.fromisoformat(state.expires_at)
    except ValueError:
        return None
    return None if datetime.now(UTC) >= expires else state


def active_state(root: Path) -> EmergencyState | None:
    """Return the state while the emergency is still running, spent or not.

    Two different questions wear the same word. *Armed* asks whether the
    one allowed publication is still available, and is answered by
    :func:`armed_state` — spending it must end that. *Active* asks
    whether this PR is still being expedited, which does not end at
    publication: the conflicts, the review fixes and the follow-up
    commits all come afterwards.

    Reading ``spent`` for the second question is what made the bypass
    die exactly when it was still needed — a merge conflict surfaced
    minutes after the PR opened and met the full battery, on a branch
    whose emergency had not expired. The publication stays one-shot; the
    commit-time stand-down lasts until the sentinel expires or is
    closed.

    Args:
        root: Repo root.

    Returns:
        The state while unexpired, or ``None``.
    """
    return _unexpired(read_state(root))


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
    return None if state is None or state.spent else _unexpired(state)
