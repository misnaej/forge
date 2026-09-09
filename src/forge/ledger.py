"""Append-only ``key=value`` ledgers under ``code_health/``.

Several forge tools keep a per-workspace, cross-run record — one line
per run, ``ts=<iso>`` first, then ``key=value`` fields — so "what does
this cost, over time" stays answerable after the per-run log has been
overwritten (``telemetry_history.log``, ``smart_test_history.log``,
``agent_profile_history.log``). This module is the one writer and the
one parser for that shape, so the ledgers can never disagree on
timestamp format, separator, or tolerance to a damaged line.

Lines are written with two spaces between fields. An optional *tail*
field — one whose value may itself contain spaces, such as a command
line — is always last and is split off by the parser on its key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def append_ledger_line(
    path: Path,
    fields: Mapping[str, object],
    *,
    tail: tuple[str, str] | None = None,
) -> None:
    """Append one timestamped ``key=value`` line to *path*.

    Creates the parent directory on first use. The timestamp is added
    here — callers never format their own — so every ledger sorts and
    parses the same way.

    Args:
        path: Ledger file to append to.
        fields: Field values in the order they should appear after ``ts``.
            Values are rendered with ``str()``; any whitespace inside one
            becomes ``_`` so the field stays a single token (use *tail*
            for the one field that may contain spaces).
        tail: Optional ``(key, value)`` written last; its value may
            contain spaces, but line breaks are folded to spaces so one
            append is always one line.
    """
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    parts = [f"ts={ts}", *(f"{key}={_plain(value)}" for key, value in fields.items())]
    if tail is not None:
        parts.append(f"{tail[0]}={_one_line(tail[1])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("  ".join(parts) + "\n")


def _plain(value: object) -> str:
    """Render a field value as one whitespace-free token.

    Args:
        value: Object to render; converted to string first.

    Returns:
        Whitespace-free token with internal spaces replaced by underscores,
        or "-" if the result would be empty.
    """
    return "_".join(str(value).split()) or "-"


def _one_line(value: str) -> str:
    """Fold line breaks in a tail value so the record stays one line.

    Args:
        value: String that may contain line breaks.

    Returns:
        Single-line string with line breaks replaced by spaces.
    """
    return " ".join(value.splitlines())


def parse_ledger(text: str, *, tail_key: str | None = None) -> list[dict[str, str]]:
    """Parse ledger lines into field mappings, skipping damaged ones.

    Tolerant by design: a line that does not split into ``key=value``
    fields (hand-edited, truncated) is skipped rather than failing the
    whole read — a ledger is append-only and long-lived.

    Args:
        text: Raw ledger contents.
        tail_key: Key of the trailing field whose value may contain
            spaces; everything after ``  <tail_key>=`` is kept verbatim.

    Returns:
        One dict per parsable line, in file (chronological) order.
    """
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if "=" not in line:
            continue
        head, tail_value = line, None
        if tail_key is not None:
            head, sep, rest = line.partition(f"  {tail_key}=")
            if sep:
                tail_value = rest
        row: dict[str, str] = {}
        for field in head.split():
            key, eq, value = field.partition("=")
            if eq:
                row[key] = value
        if tail_value is not None and tail_key is not None:
            row[tail_key] = tail_value
        if row:
            rows.append(row)
    return rows
