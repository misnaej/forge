"""The one length convention for text an agent writes.

A length limit on agent-written text (a commit message, a squash message,
a PR wrap-up, a PR description, a changelog bullet) is a **ceiling, never
a target**. The right length is the shortest text that carries the
information; how much changed, and how many processes it altered, decides
what that is. A one-line fix earns a few words in each place. A change
altering several processes earns more — because there is more to say, not
because a larger number is permitted.

**Nothing is written toward the number.** An agent that fills the
allowance, or lands just under it, has misread the rule: the ceiling is
where text stops being acceptable, not where it should arrive.

Three bands, and :func:`length_verdict` is the only place they are
decided:

- **At or under the ceiling** — silent. The expected state.
- **Over the ceiling** — warns once and still ships. A written message is
  not re-cut over a word or two; the next one is shorter.
- **Past :data:`REFUSAL_PERCENT` of the ceiling** — refused. Text that
  far over is not a rounding error, and the margin that used to sit here
  was *silent*, which made every stated number a floor and rewarded
  exactly the filling this convention forbids.

A ceiling small enough that the percentage is narrower than one useful
edit states its refusal threshold outright instead (``refuse_over``), so
the band stays decided here rather than in the caller. A changelog
bullet is the one such kind: 20% of twelve words is two, and refusing an
entry two words long buys nothing.

What makes a message quick to read is what it leaves out, which is a
content rule (FOUNDATION §6, §8) rather than a count — the measured waste
was process state and bookkeeping, not prose. A refusal is always
recoverable by cutting; nothing about it needs a bypass.
"""

from __future__ import annotations

from typing import Final, Literal


#: How far past a ceiling text may go before it is refused rather than
#: warned about, as a percentage of the ceiling.
REFUSAL_PERCENT: Final[int] = 20

#: The rule, in the voice every writer of budgeted text is told it in.
#: One string so a commit message, a squash message, a PR description, a
#: wrap-up slot and a changelog bullet all state it identically (§12) —
#: and in this register, because "~N words" reads as a length to write
#: toward and a mild ceiling gets treated as a target. Measured over one
#: session: every budgeted artifact overshot on its first attempt and was
#: cut afterwards, and a cut made last lands on whatever sits last rather
#: than on what matters least.
BUDGET_RULE: Final = (
    "AS SHORT AS POSSIBLE, and NEVER above the ceiling: it is a limit, "
    "not a length to write toward. Rank what must be said, divide the "
    "words, write once — never draft and then cut."
)


def refusal_threshold(ceiling: int, *, refuse_over: int | None = None) -> int:
    """Return the word count past which text of this kind is refused.

    Integer arithmetic, rounded down, so the threshold never depends on
    float rounding.

    Args:
        ceiling: The stated limit for that kind of text.
        refuse_over: The last acceptable count, stated outright, for a
            ceiling small enough that :data:`REFUSAL_PERCENT` of it is
            narrower than one useful edit. It is the threshold itself,
            never a width added to the ceiling, so a reader of the
            call site sees the number that refuses.

    Returns:
        The last acceptable count; one more than this is refused.

    Raises:
        ValueError: When *refuse_over* is below *ceiling*, which would
            refuse text before it had been warned about.
    """
    if refuse_over is None:
        return ceiling + ceiling * REFUSAL_PERCENT // 100
    if refuse_over < ceiling:
        msg = (
            f"refuse_over={refuse_over} is below the ceiling {ceiling}: "
            "text would be refused before it is warned about"
        )
        raise ValueError(msg)
    return refuse_over


def length_verdict(
    words: int, ceiling: int, *, refuse_over: int | None = None
) -> Literal["ok", "warn", "refuse"]:
    """Return which band *words* falls in for its *ceiling*.

    The one comparison every length check makes, so "over" and "too far
    over" mean the same thing everywhere and no caller reintroduces a
    private margin. Exceeding the ceiling by one word warns: a ceiling
    that tolerates a percentage in silence is a target with a stricter
    name.

    Args:
        words: Words counted in the text.
        ceiling: The stated limit for that kind of text.
        refuse_over: The refusal threshold, where the percentage band
            does not fit the ceiling; see :func:`refusal_threshold`.

    Returns:
        ``"ok"`` at or under the ceiling, ``"warn"`` above it, and
        ``"refuse"`` past :func:`refusal_threshold`.

    Raises:
        ValueError: When *refuse_over* is below *ceiling*. Checked
            before the bands are, so an incoherent pair fails on the
            first call rather than on the first text long enough to
            reach the comparison.
    """
    threshold = refusal_threshold(ceiling, refuse_over=refuse_over)
    if words <= ceiling:
        return "ok"
    if words <= threshold:
        return "warn"
    return "refuse"
