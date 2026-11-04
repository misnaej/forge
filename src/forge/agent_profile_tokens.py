"""Token accounting for ``forge-agent-profile --tokens``.

Every request an agent makes re-sends its whole context, so cost is
dominated by prompt tokens, not output. This module turns Claude Code
transcript records into per-request usage and ranks it.

Two quirks of the transcript format shape it:

- One API request is written as several records (one per content block),
  each repeating the same ``message.id`` and ``usage``. Usage is counted
  once per message id, or every request is over-counted.
- A request's prompt is ``input + cache_creation + cache_read`` tokens.
  The first request's prompt is the agent's **fixed context** (system
  prompt, FOUNDATION, CLAUDE.md, agent instructions, tool definitions):
  what it pays before doing any work, and again on every later request.

It deliberately imports nothing from :mod:`forge.agent_profile`, which
imports it: records arrive as already-parsed dicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Final


if TYPE_CHECKING:
    from collections.abc import Iterable


#: A run whose fixed context is at least this share of its prompt tokens,
#: on no more than :data:`FEW_TOOL_CALLS` tool calls, mostly paid to load.
FIXED_SHARE_THRESHOLD: Final = 0.8
FEW_TOOL_CALLS: Final = 3
#: A tool result at least this long (characters) is re-read on every
#: later request of the run.
LARGE_RESULT_CHARS: Final = 20_000
#: Main-session requests made while no skill was active.
NO_SKILL: Final = "(no skill)"
DELEGATION_TOOLS: Final = frozenset({"Agent", "Task"})
_THOUSAND: Final = 1_000
_MILLION: Final = 1_000_000

_HANDBACK_FROM = re.compile(r'<agent-message from="([A-Za-z0-9_-]+)"')


@dataclass
class TokenUsage:
    """Summed request usage for one run, skill, or agent type."""

    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    fixed_tokens: int = 0
    requests: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Tokens sent to the model across all requests."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    @property
    def total_tokens(self) -> int:
        """Prompt plus output tokens."""
        return self.prompt_tokens + self.output_tokens

    @property
    def fixed_cost(self) -> int:
        """Prompt tokens spent re-sending the fixed context, every request."""
        return min(self.prompt_tokens, self.fixed_tokens * self.requests)

    @property
    def fixed_share(self) -> float:
        """:attr:`fixed_cost` as a share of prompt tokens (``0`` when none)."""
        prompt = self.prompt_tokens
        return self.fixed_cost / prompt if prompt else 0.0

    def add(self, other: TokenUsage) -> None:
        """Add *other*'s counts into this usage.

        Args:
            other: Usage to add; its fixed context sums too, so an
                aggregate's :attr:`fixed_cost` stays the sum of its runs'.
        """
        for item in fields(self):
            setattr(
                self, item.name, getattr(self, item.name) + getattr(other, item.name)
            )


def _printable(text: str) -> str:
    """*text* with every non-printable character turned into a space.

    Args:
        text: A label read from a transcript, which anyone can write.

    Returns:
        The label, safe to print to a terminal.
    """
    return "".join(ch if ch.isprintable() else " " for ch in text)


def _count(value: object) -> int:
    """A usage field as a non-negative int (``0`` for anything else).

    Args:
        value: The decoded JSON value.

    Returns:
        *value* when it is a non-negative ``int`` (``bool`` excluded), else ``0``.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


class UsageTally:
    """Adds each request's usage once, however many records repeat it."""

    def __init__(self) -> None:
        """Start an empty tally."""
        self.usage = TokenUsage()
        self._seen: set[str] = set()

    def add_record(self, record: dict[str, Any]) -> bool:
        """Count *record*'s usage unless its message was already counted.

        Args:
            record: One assistant transcript record.

        Returns:
            ``True`` when *record* starts a request not seen before; a
            record without a message id always counts as new.
        """
        message = record.get("message")
        if not isinstance(message, dict):
            return False
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            if message_id in self._seen:
                return False
            self._seen.add(message_id)
        usage = message.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        fresh = _count(usage.get("input_tokens"))
        written = _count(usage.get("cache_creation_input_tokens"))
        read = _count(usage.get("cache_read_input_tokens"))
        if self.usage.requests == 0:
            self.usage.fixed_tokens = fresh + written + read
        self.usage.requests += 1
        self.usage.input_tokens += fresh
        self.usage.cache_write_tokens += written
        self.usage.cache_read_tokens += read
        self.usage.output_tokens += _count(usage.get("output_tokens"))
        return True


def tool_result_chars(block: dict[str, Any]) -> int:
    """Characters a ``tool_result`` block puts back into the conversation.

    Args:
        block: A ``tool_result`` content block.

    Returns:
        The length of its text content, ``0`` when it carries none.
    """
    content = block.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return 0


def _record_text(record: dict[str, Any]) -> str:
    """The plain text a user record carries, for hand-back detection.

    Args:
        record: One transcript record.

    Returns:
        Its string content, or its text blocks joined; ``""`` when none.
    """
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


@dataclass
class SessionTokens:
    """What one main-session transcript says about tokens and delegations."""

    by_skill: dict[str, TokenUsage] = field(default_factory=dict)
    #: Delegating ``tool_use`` id → the skill active when it was issued.
    skill_of_call: dict[str, str] = field(default_factory=dict)
    #: Delegating ``tool_use`` id → characters of the brief it sent.
    brief_chars: dict[str, int] = field(default_factory=dict)
    #: Subagent id → characters of its hand-back report in this session.
    handback_chars: dict[str, int] = field(default_factory=dict)

    @property
    def usage(self) -> TokenUsage:
        """All skills' usage summed."""
        total = TokenUsage()
        for usage in self.by_skill.values():
            total.add(usage)
        return total


def parse_session(records: Iterable[dict[str, Any]]) -> SessionTokens:
    """Split a main-session transcript's tokens by the skill that was active.

    Args:
        records: The session transcript's records, in file order.

    Returns:
        Per-skill usage plus the delegations it made: each brief's size,
        the skill that issued it, and each subagent's hand-back size.
    """
    session = SessionTokens()
    tallies: dict[str, UsageTally] = {}
    for record in records:
        kind = record.get("type")
        if kind == "assistant":
            skill = record.get("attributionSkill")
            skill = _printable(skill) if isinstance(skill, str) and skill else NO_SKILL
            tallies.setdefault(skill, UsageTally()).add_record(record)
            _note_delegations(session, record, skill)
        elif kind == "user":
            _note_handback(session, record)
    session.by_skill = {skill: tally.usage for skill, tally in tallies.items()}
    return session


def _note_delegations(
    session: SessionTokens, record: dict[str, Any], skill: str
) -> None:
    """Record each delegation an assistant record makes.

    Args:
        session: The session being built; updated in place.
        record: One assistant record.
        skill: The skill active for it.
    """
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in DELEGATION_TOOLS:
            continue
        call_id = block.get("id")
        tool_input = block.get("input")
        if not isinstance(call_id, str) or not isinstance(tool_input, dict):
            continue
        prompt = tool_input.get("prompt")
        session.skill_of_call[call_id] = skill
        session.brief_chars[call_id] = len(prompt) if isinstance(prompt, str) else 0


def _note_handback(session: SessionTokens, record: dict[str, Any]) -> None:
    """Record a subagent hand-back delivered into the conversation.

    A report can arrive more than once (queued, then delivered); the
    longest copy is kept rather than summing repeats.

    Args:
        session: The session being built; updated in place.
        record: One user record.
    """
    text = _record_text(record)
    match = _HANDBACK_FROM.search(text)
    if match is None:
        return
    agent_id = match.group(1)
    session.handback_chars[agent_id] = max(
        session.handback_chars.get(agent_id, 0), len(text)
    )


@dataclass(frozen=True)
class TokenRow:
    """One ranked line: an agent type or a skill, and what it cost."""

    label: str
    runs: int
    usage: TokenUsage


@dataclass(frozen=True)
class WasteFlag:
    """One mechanical waste finding, with the tokens it concerns."""

    kind: str
    run_label: str
    detail: str
    tokens: int


def rank(rows: Iterable[TokenRow]) -> list[TokenRow]:
    """Order rows by total tokens, costliest first.

    Args:
        rows: Rows to order.

    Returns:
        The rows, costliest first.
    """
    return sorted(rows, key=lambda row: -row.usage.total_tokens)


def fmt_tokens(count: int) -> str:
    """Compact token count: ``950``, ``42k``, ``3.1M``.

    Args:
        count: A token count.

    Returns:
        The formatted count.
    """
    if count >= _MILLION:
        return f"{count / _MILLION:.1f}M"
    if count >= _THOUSAND:
        return f"{count / _THOUSAND:.0f}k"
    return str(count)


def _table(title: str, rows: list[TokenRow]) -> list[str]:
    """A ranked token table.

    Args:
        title: The section heading.
        rows: Rows, already ranked.

    Returns:
        The table's report lines, including a trailing blank line.
    """
    header = (
        f"{'':<34} {'runs':>5} {'total':>7} {'per run':>8} "
        f"{'fixed ctx':>9} {'fixed %':>7} {'output':>7}"
    )
    lines = [title, header]
    for row in rows:
        usage = row.usage
        per_run = usage.total_tokens // row.runs if row.runs else 0
        fixed_each = usage.fixed_tokens // row.runs if row.runs else 0
        lines.append(
            f"{row.label[:34]:<34} {row.runs:>5} {fmt_tokens(usage.total_tokens):>7} "
            f"{fmt_tokens(per_run):>8} {fmt_tokens(fixed_each):>9} "
            f"{usage.fixed_share:>6.0%} {fmt_tokens(usage.output_tokens):>7}"
        )
    return [*lines, ""]


@dataclass(frozen=True)
class TokenReport:
    """Everything the ``--tokens`` view shows, already ranked."""

    #: Subagent usage per agent type.
    by_type: list[TokenRow]
    #: Subagent usage per skill that launched it.
    by_caller: list[TokenRow]
    #: Main-session usage per active skill.
    by_skill: list[TokenRow]
    #: Waste findings, largest first (capped).
    flags: list[WasteFlag]
    #: Findings before the cap.
    flag_total: int
    #: Subagent runs left out for want of token data.
    unknown_runs: int
    #: ``(delegations, brief chars, hand-back chars)``.
    delegation: tuple[int, int, int]


def render_token_report(report: TokenReport) -> str:
    """Render the ``--tokens`` report.

    Args:
        report: The ranked rows, flags and counts to show.

    Returns:
        The report text.
    """
    by_type, by_caller, by_skill = report.by_type, report.by_caller, report.by_skill
    flags, unknown_runs = report.flags, report.unknown_runs
    if not by_type and not by_skill:
        return (
            "No token data found — token counts come from Claude Code "
            "transcripts under ~/.claude/projects/."
        )
    lines = ["Token profile (fixed ctx = prompt before the first tool call)", ""]
    if by_type:
        lines += _table("Subagents, per agent type:", by_type)
    if by_caller:
        lines += _table("Subagents, per skill that launched them:", by_caller)
    if by_skill:
        lines += _table("Main session, per active skill:", by_skill)
    calls, briefs, handbacks = report.delegation
    if calls:
        lines += [
            (
                f"Delegations: {calls} — briefs {fmt_tokens(briefs)} chars sent, "
                f"reports {fmt_tokens(handbacks)} chars returned into the main "
                "conversation (re-read on every later request)."
            ),
            "",
        ]
    if unknown_runs:
        lines += [f"{unknown_runs} run(s) without token data left out.", ""]
    shown = f" (largest {len(flags)} shown)" if report.flag_total > len(flags) else ""
    lines.append(f"Waste flags: {report.flag_total}{shown}")
    lines += [
        f"  {fmt_tokens(flag.tokens):>6}  {flag.kind}: {flag.detail}"
        f"  [{flag.run_label}]"
        for flag in flags
    ]
    return "\n".join(lines)
