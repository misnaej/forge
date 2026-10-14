"""forge-backlog-index — render and write the ``📋 Backlog Index`` issue.

The Backlog Index is one pinned issue listing every open issue by tier
(FOUNDATION §14). Its body is a pure function of GitHub state, so it is
rendered here rather than by an agent (FOUNDATION §7 "Mechanical
first"): the same backlog always produces the same page, and the guards
that protect the write live in code instead of prose an agent may skip.

The body is rebuilt from scratch on every run and never read back. Tier
lines omit the labels the page already states — the tier, which the
section heading names, and the labels each lane exists to show — so the
page grows as slowly as it can with the backlog.

Nothing is written unless the listing is provably complete, exactly one
open issue carries the Index title, and the body is non-empty and within
GitHub's limit. A body over the limit first sheds its lowest-tier lines,
oldest activity first, each tier's dropped lines replaced by one count;
when tiers 1 and 2 alone do not fit, the run refuses and the old Index
stands. Issue titles are world-writable, so they are neutralised before
they reach the posted body: no mention pings, no planted references.

Exit codes:
    0  written (or rendered, with ``--dry-run``)
    1  refused — incomplete listing, no or several Index issues, empty or
       oversized body, or a failed write; the old Index is left standing
    2  GitHub could not be read
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import quote_plus

from forge.gh_comments import GH_LIST_TIMEOUT
from forge.git_utils import configure_cli_logging, emit, require_cli
from forge.plan_staleness import (
    BLOCKED,
    PLAN_READY,
    PermissionSource,
    WriterCheck,
    authenticated_plan,
    parse_requires,
)
from forge.plan_staleness_gh import GhSource


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from forge.plan_staleness_gh import Issue


configure_cli_logging()
logger = logging.getLogger(__name__)

INDEX_TITLE: Final[str] = "📋 Backlog Index"
BODY_LIMIT: Final[int] = 65_536

EXIT_OK: Final[int] = 0
EXIT_REFUSED: Final[int] = 1
EXIT_UNREADABLE: Final[int] = 2

NEEDS_RECHECK: Final[str] = "needs-recheck"
NEEDS_ENDORSEMENT: Final[str] = "needs-endorsement"
NEEDS_TRIAGE: Final[str] = "needs-triage"
WAITING_UPSTREAM: Final[str] = "waiting-upstream"

TIERS: Final[tuple[tuple[str, str], ...]] = (
    ("tier-1-critical", "🔥 Tier 1 — Critical"),
    ("tier-2-high", "⚡ Tier 2 — High Priority"),
    ("tier-3-standard", "📋 Tier 3 — Standard"),
    ("tier-4-low", "🌱 Tier 4 — Low Priority"),
)
# Trimmed first to last; tiers 1 and 2 are never trimmed.
TRIMMABLE_TIERS: Final[tuple[str, ...]] = ("tier-4-low", "tier-3-standard")

# Labels a tier line never repeats: its own section heading or a lane states them.
_STATED_LABELS: Final[frozenset[str]] = frozenset(
    {
        *(label for label, _ in TIERS),
        BLOCKED,
        PLAN_READY,
        NEEDS_RECHECK,
        NEEDS_ENDORSEMENT,
        NEEDS_TRIAGE,
    }
)

_ZWSP: Final[str] = "\u200b"
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_MENTION_RE: Final[re.Pattern[str]] = re.compile(r"@(?=\w)")
_REFERENCE_RE: Final[re.Pattern[str]] = re.compile(r"#(?=\d)")
_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\]\(")
_PAIR_RE: Final[re.Pattern[str]] = re.compile(r"^([^=]+)=(.+)$")


class BacklogSource(PermissionSource, Protocol):
    """The GitHub reads the Index needs; every method returns None on failure."""

    def open_issues(self) -> list[Issue] | None:
        """Return every open issue, comments included.

        Returns:
            The issues, or None when the listing could not be read completely.
        """

    def open_issue_count(self) -> int | None:
        """Return how many issues GitHub counts as open.

        Returns:
            The total, or None when it could not be read.
        """

    def repo_slug(self) -> str | None:
        """Return the repository's ``owner/name``.

        Returns:
            The slug, or None when it could not be read.
        """


@dataclass(frozen=True)
class Lane:
    """A caller-declared extra section whose issues leave the tier sections."""

    label: str
    heading: str


@dataclass(frozen=True)
class IndexInputs:
    """Everything the render reads besides the issues themselves."""

    today: str
    lanes: tuple[Lane, ...] = ()
    recheck: Mapping[int, int] = field(default_factory=dict)
    plan_dates: Mapping[int, str] = field(default_factory=dict)
    slug: str | None = None


@dataclass(frozen=True)
class Rendered:
    """A rendered body and how many lines each tier shed to fit."""

    body: str
    trimmed: dict[str, int]


def safe_title(title: str) -> str:
    """Return *title* as inert single-line markdown.

    Titles are written by anyone who can open an issue, and the Index is
    posted under the caller's account: a raw ``@name`` would ping, a raw
    ``#N`` would plant a cross-reference on another issue, and a raw link
    would render as one. A zero-width space breaks each without changing
    what a reader sees.

    Args:
        title: Issue title as GitHub reports it.

    Returns:
        The neutralised title.
    """
    text = _WHITESPACE_RE.sub(" ", title).strip()
    text = _MENTION_RE.sub("@" + _ZWSP, text)
    text = _REFERENCE_RE.sub("#" + _ZWSP, text)
    return _LINK_RE.sub("]" + _ZWSP + "(", text)


def _tier_of(issue: Issue) -> str | None:
    """Return the highest tier label *issue* carries, if any.

    Args:
        issue: Issue whose labels are inspected.

    Returns:
        The tier label, or None when the issue carries none.
    """
    return next((label for label, _ in TIERS if label in issue.labels), None)


def _head(issue: Issue) -> str:
    """Return the ``- #N — Title`` start every Index line shares.

    Args:
        issue: Issue to introduce.

    Returns:
        The markdown list-item prefix with a neutralised title.
    """
    return f"- #{issue.number} — {safe_title(issue.title)}"


def _tier_line(issue: Issue, hidden: frozenset[str]) -> str:
    """Return *issue*'s tier-section line, without labels the page already states.

    Args:
        issue: Issue to render.
        hidden: Labels omitted from the line because the page already states them.

    Returns:
        The line: head, remaining labels and last-activity date.
    """
    shown = [f"`{label}`" for label in issue.labels if label not in hidden]
    parts = [_head(issue)]
    if shown:
        parts.append(", ".join(shown))
    parts.append(f"_activity: {issue.updated_at[:10] or 'unknown'}_")
    return " — ".join(parts)


def _more_line(tier: str, count: int, slug: str | None) -> str:
    """Return the line standing in for *count* trimmed entries of *tier*.

    Args:
        tier: Tier label whose entries were trimmed.
        count: Number of trimmed entries.
        slug: Repository ``owner/name`` for a link, or None for a ``gh`` hint.

    Returns:
        The replacement line.
    """
    query = f"is:issue is:open label:{tier}"
    if slug:
        where = f"[full list](https://github.com/{slug}/issues?q={quote_plus(query)})"
    else:
        where = f"`gh issue list --label {tier}`"
    return f"- _…and {count} more — {where}_"


def _blocker(issue: Issue) -> str:
    """Return what *issue* waits on, read from its ``Requires:`` line.

    Args:
        issue: Blocked or waiting issue.

    Returns:
        Comma-separated issue references, ``external`` or ``see issue``.
    """
    refs = parse_requires([issue.body]).refs
    if refs:
        return ", ".join(f"#{n}" for n in refs)
    if WAITING_UPSTREAM in issue.labels:
        return "external"
    return "see issue"


def _section(heading: str, lines: Sequence[str], count: int | None = None) -> str:
    """Return one ``## heading (N)`` section.

    Args:
        heading: Section title.
        lines: Entry lines under the heading.
        count: Count shown in the heading; defaults to ``len(lines)``.

    Returns:
        The section text.
    """
    total = len(lines) if count is None else count
    return "\n".join([f"## {heading} ({total})", *lines])


def _lane_sections(issues: Sequence[Issue], inputs: IndexInputs) -> list[str]:
    """Return the status lanes that follow the tier sections.

    Args:
        issues: Every listed issue.
        inputs: Render inputs supplying recheck overlaps and plan dates.

    Returns:
        One section per status lane.
    """

    def having(label: str) -> list[Issue]:
        """Return the issues carrying a label.

        Args:
            label: Label name an issue must carry to be selected.

        Returns:
            The issues whose labels include ``label``, in listed order.
        """
        return [i for i in issues if label in i.labels]

    recheck = [
        i for i in issues if NEEDS_RECHECK in i.labels or i.number in inputs.recheck
    ]
    return [
        _section(
            "✅ Plan-Ready",
            [
                f"{_head(i)} — _validated: "
                f"{inputs.plan_dates.get(i.number, 'unknown')}_"
                for i in having(PLAN_READY)
            ],
        ),
        _section(
            "🔁 Needs Recheck",
            [
                f"{_head(i)} — _touched by PR #{inputs.recheck[i.number]}_"
                if i.number in inputs.recheck
                else _head(i)
                for i in recheck
            ],
        ),
        _section("🤝 Needs Endorsement", [_head(i) for i in having(NEEDS_ENDORSEMENT)]),
        _section(
            "🚫 Blocked / Waiting",
            [
                f"{_head(i)} — _blocker: {_blocker(i)}_"
                for i in issues
                if BLOCKED in i.labels or WAITING_UPSTREAM in i.labels
            ],
        ),
        _section("🆕 Needs Triage", [_head(i) for i in issues if _tier_of(i) is None]),
    ]


def _compose(
    issues: Sequence[Issue],
    tiers: Mapping[str, list[Issue]],
    keep: Mapping[str, int],
    inputs: IndexInputs,
) -> str:
    """Return the body with each tier cut to its first ``keep[tier]`` lines.

    Args:
        issues: Every listed issue, in number order.
        tiers: Issues per tier label, most recently active first.
        keep: Number of lines each tier keeps.
        inputs: Render inputs (date, lanes, slug, recheck, plan dates).

    Returns:
        The full Index body.
    """
    hidden = _STATED_LABELS | {lane.label for lane in inputs.lanes}
    parts = [
        (
            "> **Auto-generated by `forge-backlog-index`. Do not edit by hand.**\n"
            f"> Last triage: {inputs.today}. "
            "To re-triage: invoke the `issue-triage` agent in `triage` mode."
        ),
    ]
    for label, heading in TIERS:
        members = tiers[label]
        shown = members[: keep[label]]
        lines = [_tier_line(i, hidden) for i in shown]
        if len(shown) < len(members):
            lines.append(_more_line(label, len(members) - len(shown), inputs.slug))
        parts.append(_section(heading, lines, count=len(members)))
    parts.extend(
        _section(lane.heading, [_head(i) for i in issues if lane.label in i.labels])
        for lane in inputs.lanes
    )
    parts.extend(_lane_sections(issues, inputs))
    return "\n\n".join(parts) + "\n"


def render(
    issues: Iterable[Issue], inputs: IndexInputs, *, max_chars: int = BODY_LIMIT
) -> Rendered | None:
    """Render the Index body, trimming the lowest tiers until it fits.

    Args:
        issues: Open issues to list (the Index issue itself excluded).
        inputs: Date, extra lanes, recheck overlaps, plan dates and slug.
        max_chars: Body limit in characters.

    Returns:
        The body and per-tier trimmed counts, or None when tiers 1 and 2
        alone exceed *max_chars*.
    """
    ordered = sorted(issues, key=lambda i: i.number)
    exclusive = {lane.label for lane in inputs.lanes}
    tiers: dict[str, list[Issue]] = {label: [] for label, _ in TIERS}
    for issue in ordered:
        tier = _tier_of(issue)
        if tier is not None and not exclusive.intersection(issue.labels):
            tiers[tier].append(issue)
    for members in tiers.values():
        members.sort(key=lambda i: i.updated_at, reverse=True)

    keep = {label: len(members) for label, members in tiers.items()}
    body = _compose(ordered, tiers, keep, inputs)
    for tier in TRIMMABLE_TIERS:
        while len(body) > max_chars and keep[tier] > 0:
            # Drop at least enough lines to cover the excess, then re-measure:
            # the count line's own length shifts by a few characters.
            excess = len(body) - max_chars
            dropped = 0
            while keep[tier] > 0 and dropped < excess:
                keep[tier] -= 1
                dropped += len(_tier_line(tiers[tier][keep[tier]], frozenset())) + 1
            body = _compose(ordered, tiers, keep, inputs)
    if len(body) > max_chars:
        return None
    trimmed = {
        t: len(tiers[t]) - keep[t] for t in TRIMMABLE_TIERS if keep[t] < len(tiers[t])
    }
    return Rendered(body=body, trimmed=trimmed)


def _plan_dates(issues: Iterable[Issue], source: PermissionSource) -> dict[int, str]:
    """Return each plan-ready issue's validated date from its authenticated plan.

    A plan whose author's access cannot be looked up stays unknown and is
    left out, which renders as ``validated: unknown``.

    Args:
        issues: Issues to scan; only plan-ready ones are read.
        source: Permission lookups used to authenticate each plan's author.

    Returns:
        Issue number mapped to its plan's ``YYYY-MM-DD`` creation date.
    """
    is_writer = WriterCheck(source)
    dates: dict[int, str] = {}
    for issue in issues:
        if PLAN_READY not in issue.labels:
            continue
        plan = authenticated_plan(issue.comments, is_writer).plan
        if plan is not None:
            dates[issue.number] = plan.created_at[:10]
    return dates


def gh_issue_edit(number: int, body: str) -> int:
    """Overwrite issue *number*'s body with *body* via ``gh issue edit``.

    Args:
        number: Issue number.
        body: New body, passed on stdin.

    Returns:
        ``gh``'s exit code; 1 when it timed out.
    """
    try:
        proc = subprocess.run(
            ["gh", "issue", "edit", str(number), "--body-file", "-"],
            input=body,
            text=True,
            capture_output=True,
            timeout=GH_LIST_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.exception(
            "gh issue edit #%d timed out after %ds", number, GH_LIST_TIMEOUT
        )
        return 1
    if proc.returncode != 0:
        logger.error("gh issue edit #%d failed: %s", number, proc.stderr.strip())
    return proc.returncode


@dataclass(frozen=True)
class RunOptions:
    """What the caller asked for on the command line."""

    inputs: IndexInputs
    dry_run: bool = False
    max_chars: int = BODY_LIMIT


class _RefusedError(Exception):
    """A guard stopped the run; carries the exit code and the line to print."""

    def __init__(self, code: int, message: str) -> None:
        """Record why the run stopped.

        Args:
            code: Exit code to return.
            message: Line to print.
        """
        super().__init__(message)
        self.code = code
        self.message = message


def _locate_index(source: BacklogSource) -> tuple[int, list[Issue]]:
    """Return the Index issue's number and every other open issue.

    Args:
        source: GitHub reads.

    Returns:
        The Index issue number and the remaining open issues.

    Raises:
        _RefusedError: When the listing is unreadable or short, or there is
            not exactly one open Index issue.
    """
    issues = source.open_issues()
    total = source.open_issue_count()
    if issues is None or total is None:
        raise _RefusedError(
            EXIT_UNREADABLE,
            "unknown: could not read the open issues from GitHub; "
            "the Index was not written",
        )
    if len(issues) != total:
        raise _RefusedError(
            EXIT_REFUSED,
            f"refused: the listing returned {len(issues)} of {total} open issues; "
            "the old Index is left standing — retry",
        )
    index = [i.number for i in issues if i.title == INDEX_TITLE]
    if len(index) != 1:
        found = ", ".join(f"#{n}" for n in index) or "none"
        raise _RefusedError(
            EXIT_REFUSED,
            f"refused: expected exactly one open issue titled {INDEX_TITLE!r}, "
            f"found {found} "
            "— create one with the issue-triage `bootstrap` mode, or close the extras",
        )
    return index[0], [i for i in issues if i.number != index[0]]


def _checked_render(
    listed: list[Issue], inputs: IndexInputs, max_chars: int
) -> Rendered:
    """Render the body, refusing one that is oversized or empty.

    Args:
        listed: Open issues to list, the Index issue excluded.
        inputs: Render inputs.
        max_chars: Body limit in characters.

    Returns:
        The rendered body and trimmed counts.

    Raises:
        _RefusedError: When tiers 1 and 2 alone exceed *max_chars*, or the
            body is empty.
    """
    rendered = render(listed, inputs, max_chars=max_chars)
    if rendered is None:
        raise _RefusedError(
            EXIT_REFUSED,
            f"refused: tiers 1\N{EN DASH}2 alone exceed {max_chars} characters; "
            "the old Index is left standing",
        )
    if not rendered.body.strip():
        raise _RefusedError(
            EXIT_REFUSED,
            "refused: the rendered body is empty; the old Index is left standing",
        )
    return rendered


def run(
    source: BacklogSource,
    options: RunOptions,
    write: Callable[[int, str], int] = gh_issue_edit,
) -> int:
    """Read the backlog, render the Index and write it when every guard passes.

    Args:
        source: GitHub reads.
        options: Render inputs, dry-run flag and size limit.
        write: Writes a body to an issue number; returns an exit code.

    Returns:
        0 written, 1 refused, 2 GitHub unreadable.
    """
    try:
        index, listed = _locate_index(source)
        inputs = replace(
            options.inputs,
            plan_dates={**_plan_dates(listed, source), **options.inputs.plan_dates},
            slug=options.inputs.slug or source.repo_slug(),
        )
        rendered = _checked_render(listed, inputs, options.max_chars)
    except _RefusedError as refusal:
        emit(refusal.message)
        return refusal.code
    size = f"{len(rendered.body)}/{options.max_chars}"
    trimmed = [f"trimmed: {tier} {n}" for tier, n in rendered.trimmed.items()]
    if options.dry_run:
        emit(rendered.body)
        emit("\n".join([f"chars: {size}", *trimmed]))
        return EXIT_OK
    if write(index, rendered.body) != 0:
        emit(f"refused: writing Index #{index} failed; see the error above")
        return EXIT_REFUSED
    emit(
        "\n".join(
            [f"wrote: Index #{index} — {len(listed)} issues, {size} chars", *trimmed]
        )
    )
    return EXIT_OK


def _lane_arg(value: str) -> Lane:
    """Argparse type for ``--lane LABEL=HEADING``.

    Args:
        value: Command-line text to parse.

    Returns:
        The parsed lane.
    """
    match = _PAIR_RE.match(value)
    if match is None:
        msg = f"expected LABEL=HEADING, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return Lane(label=match[1].strip(), heading=match[2].strip())


def _recheck_arg(value: str) -> tuple[int, int]:
    """Argparse type for ``--recheck ISSUE=PR``.

    Args:
        value: Command-line text to parse.

    Returns:
        The issue and PR numbers.
    """
    match = _PAIR_RE.match(value)
    msg = f"expected ISSUE=PR numbers, got {value!r}"
    if match is None:
        raise argparse.ArgumentTypeError(msg)
    try:
        return int(match[1].lstrip("#")), int(match[2].lstrip("#"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(msg) from exc


def _build_parser() -> argparse.ArgumentParser:
    """Return the ``forge-backlog-index`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="forge-backlog-index",
        description="Render the 📋 Backlog Index from live GitHub data and write it "
        "(exit 0 written, 1 refused, 2 GitHub unreadable).",
    )
    parser.add_argument(
        "--lane",
        type=_lane_arg,
        action="append",
        default=[],
        metavar="LABEL=HEADING",
        help="Extra section for issues carrying LABEL; they leave the tier sections.",
    )
    parser.add_argument(
        "--recheck",
        type=_recheck_arg,
        action="append",
        default=[],
        metavar="ISSUE=PR",
        help="List ISSUE under Needs Recheck as touched by merged PR.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the body and its size; write nothing.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    source: BacklogSource | None = None,
    write: Callable[[int, str], int] = gh_issue_edit,
) -> int:
    """Run ``forge-backlog-index``.

    Args:
        argv: Argument vector (defaults to ``sys.argv``).
        source: GitHub reads; defaults to :class:`GhSource`.
        write: Index writer; defaults to :func:`gh_issue_edit`.

    Returns:
        0 written, 1 refused, 2 GitHub unreadable.
    """
    args = _build_parser().parse_args(argv)
    if source is None:
        require_cli("gh", caller="forge-backlog-index")
        source = GhSource()
    options = RunOptions(
        inputs=IndexInputs(
            today=datetime.now(UTC).date().isoformat(),
            lanes=tuple(args.lane),
            recheck=dict(args.recheck),
        ),
        dry_run=args.dry_run,
    )
    return run(source, options, write)


if __name__ == "__main__":
    sys.exit(main())
