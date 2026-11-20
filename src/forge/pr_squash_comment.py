r"""forge-pr-squash-comment — post the squash-merge message and keep it last.

The message reaches GitHub's squash dialog by hand, so the comment
carries its two halves in two separate fences: the title in one, the
3-5 bullet body in the other, each a one-gesture copy into the matching
field. GitHub prefills the title field from the PR title, so a posting
run also **forces the PR title to match** the title it posts — prefill
and message never drift apart.

Every posting run leaves exactly one squash comment, and leaves it as
the PR's newest comment — the human merging scrolls to the bottom, not
through the review history.

Usage:

    forge-pr-squash-comment --pr 61 \\
        --title "feat(#60): pr-manager delta-mode + verified-at" \\
        --bullet "pr_delta.py centralizes thresholds and regex" \\
        --bullet "5 reporter agents stamp verified-at SHA" \\
        --bullet "audit enforces the contract by name allowlist"
        # syncs the PR title, posts a fresh comment, then deletes the
        # older squash comments

    forge-pr-squash-comment --dry-run --title ... --bullet ...
        # prints the wrapped body to stdout and the per-part word
        # counts to stderr, no gh call

    forge-pr-squash-comment --pr 61
        # no bullets: re-posts the existing squash comment verbatim at
        # the bottom after later PR activity, quiet no-op when it is
        # already newest. Runs automatically from the
        # `keep_squash_comment_last` Claude Code hook

Rules (FOUNDATION §6 "Squash-merge messages"):

- title matches conventional-commit ``<type>(...)?: <subject>``
- 3-5 ``--bullet`` entries, each optionally opening with the changelog
  bullets it covers (``[1,4-6] text``); every changelog bullet must be
  claimed by some entry
- total whitespace-split word count (title + bullets): 45-word ceiling;
  over it warns and still posts, however far over
- no Claude / AI attribution patterns

A failing run names every broken rule at once. Length is never one; a
message over the ceiling prints a per-part breakdown as a warning (also
under ``--dry-run``) and posts (see
:func:`~forge.text_budget.length_verdict`).

Only a comment whose body starts with :data:`SQUASH_MARKER` is treated
as a squash comment, so a wrap-up that quotes the marker is never
deleted or re-posted in its place.

Output: the body posted to GitHub is the literal text below (the inner
fences are real ``` blocks, not escapes):

    <!-- forge:squash-merge-message -->
    **Squash-merge message** — copy each fence verbatim into the matching
    field of the squash dialog. The PR title is synced to the title below,
    so GitHub's prefill already matches.

    **Title**

    ```
    <title>
    ```

    **Body**

    ```
    - <bullet 1>
    - <bullet 2>
    - <bullet 3>
    ```
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from typing import Final

from forge.changelog_fragments import (
    branch_added_fragments,
    fragment_bullets,
    is_fragments_mode,
    validate_fragment,
)
from forge.config import load_config
from forge.gh_comments import (
    GH_LIST_TIMEOUT,
    ValidationError,
    delete_comment,
    list_marker_comments,
    parse_paged_json,
    post_new_comment,
    validate_no_ai_attribution,
)
from forge.git_utils import (
    configure_cli_logging,
    gh_api,
    repo_root,
    resolve_pr_base_ref,
)
from forge.text_budget import length_verdict


configure_cli_logging()
logger = logging.getLogger(__name__)


# Canonical source: `forge-gen-commit-types` renders the shell hook's
# regex from this tuple and FOUNDATION §6 names it by path.
CONVENTIONAL_COMMIT_TYPES: Final[tuple[str, ...]] = (
    "feat",
    "fix",
    "refactor",
    "test",
    "docs",
    "chore",
    "perf",
    "ci",
    "build",
    "style",
    "revert",
)

# Conventional commit: `<type>(<scope>)?!?: <subject>` — scope optional,
# allows multiple `#N` refs separated by commas inside parens; a `!`
# right before the colon marks a breaking change.
TITLE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<type>" + "|".join(CONVENTIONAL_COMMIT_TYPES) + r")"
    r"(?:\((?P<scope>[^)]+)\))?"
    r"(?P<breaking>!)?"
    r": (?P<subject>.+)$",
)

MIN_BULLETS: Final[int] = 3
MAX_BULLETS: Final[int] = 5
# The ceiling, never a length to write toward, and what "cut N" is
# measured against. Over it the message posts with a warning, and that is
# all length ever does here: what a message must not do is leave part of
# the merge undescribed, which coverage refuses. Length was the wrong
# lever to block on — across the last 60 merged messages, 26% carried a
# bullet reporting process state or bookkeeping (tests passed, a fragment
# exists, forge's own word counts), 9% of all bullet words. The content
# rules in FOUNDATION §6 cut that; a counter never could.
MAX_WORDS: Final[int] = 45
# A breakdown line only has to identify the part to shorten, not show it.
PREVIEW_CHARS: Final[int] = 40

# Invisible in rendered markdown, greppable in the raw body: how a later
# run recognizes the squash comments it must supersede. Same convention
# as `forge:c4:*` / `forge:badges:*` managed blocks.
SQUASH_MARKER: Final[str] = "<!-- forge:squash-merge-message -->"


def _check_title(title: str) -> list[str]:
    """Return the conventional-commit problems with *title*.

    Args:
        title: Raw title string.

    Returns:
        One message when the title is empty, longer than one line, or
        does not match :data:`TITLE_RE`; an empty list otherwise.
    """
    if not title.strip():
        return ["title is empty"]
    if "\n" in title:
        return ["title must be a single line"]
    if not TITLE_RE.match(title):
        return [
            (
                f"title {title!r} is not conventional-commit format. "
                f"Expected '<type>(<scope>)?!?: <subject>' where type is one of: "
                f"{', '.join(CONVENTIONAL_COMMIT_TYPES)}"
            )
        ]
    return []


def _check_bullets(bullets: list[str]) -> list[str]:
    """Return the bullet-count problem and one problem per bad bullet.

    A bullet is one line, like the title: the body renders inside a fixed
    code fence, so a line break could close that fence early and turn the
    rest of the comment into live markdown.

    Args:
        bullets: List of ``--bullet`` strings as passed by the caller.

    Returns:
        A message when the count is outside ``[MIN_BULLETS, MAX_BULLETS]``,
        followed by one message per whitespace-only or multi-line bullet.
    """
    problems: list[str] = []
    n = len(bullets)
    if not MIN_BULLETS <= n <= MAX_BULLETS:
        problems.append(
            f"got {n} bullet(s); FOUNDATION §6 requires {MIN_BULLETS}-{MAX_BULLETS}"
        )
    for i, b in enumerate(bullets, start=1):
        if not b.strip():
            problems.append(f"bullet {i} is empty")
        elif "\n" in b or "\r" in b:
            problems.append(f"bullet {i} must be a single line")
    return problems


def _word_total(title: str, bullets: list[str]) -> int:
    """Count whitespace-split words across the title and every bullet.

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Returns:
        The combined word count measured against :data:`MAX_WORDS`.
    """
    return len(title.split()) + sum(len(b.split()) for b in bullets)


def _preview(text: str) -> str:
    """Shorten one message part to a printable first line for the breakdown.

    The preview is written raw to the terminal, so non-printable characters
    are dropped: a title or bullet cannot carry an escape sequence onto
    stderr.

    Args:
        text: A title or bullet.

    Returns:
        The first line with non-printable characters removed, cut to
        :data:`PREVIEW_CHARS` characters plus ``…`` when longer;
        ``(empty)`` when nothing printable remains.
    """
    lines = text.strip().splitlines()
    first = "".join(ch for ch in lines[0] if ch.isprintable()) if lines else ""
    if not first.strip():
        return "(empty)"
    if len(first) <= PREVIEW_CHARS:
        return first
    return first[:PREVIEW_CHARS].rstrip() + "…"


def _word_breakdown(title: str, bullets: list[str]) -> list[str]:
    """Render one indented line per message part: label, word count, preview.

    Shared by the over-cap problem and the ``--dry-run`` report, so an
    author sees which part to shorten in the same shape either way.

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Returns:
        One line for the title, then one per bullet.
    """
    parts = [("title", title)]
    parts.extend((f"bullet {i}", b) for i, b in enumerate(bullets, start=1))
    return [
        f"  {label:<9}{len(text.split()):>3}  {_preview(text)}" for label, text in parts
    ]


def word_cap_warning(title: str, bullets: list[str]) -> str | None:
    """Return the warning for a message over its ceiling, or ``None``.

    Fires at the first word over, and never refuses however far over it
    runs: a long message is a worse message, not an invalid one. What
    makes one invalid is leaving part of the merge undescribed
    (:func:`_check_coverage`).

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Returns:
        The warning (total, ceiling, per-part breakdown) when the
        message is over the ceiling, else ``None``.
    """
    total = _word_total(title, bullets)
    if length_verdict(total, MAX_WORDS) == "ok":
        return None
    header = (
        f"squash-merge message is {total} words — over the {MAX_WORDS}-word "
        f"ceiling (posting anyway; shorten the next one by dropping process "
        f"state, not prose)"
    )
    return "\n".join([header, *_word_breakdown(title, bullets)])


#: A bullet's coverage claim: the fragment bullets it describes, written
#: as a leading ``[1,4-6]`` before the bullet text. One token per bullet,
#: not a paired ``--covers`` flag: argparse pairs two appended options by
#: position, so a caller that passed them out of order would be read
#: wrongly in silence, and both callers here are generated command lines.
_COVERAGE_RE: Final = re.compile(
    r"^\s*\[(?P<spec>[\d,\s-]+)\]\s*(?P<text>.*)$", re.DOTALL
)

#: Widest range a single claim may span. A fragment carries a handful of
#: bullets, so anything beyond this is a typo'd bound rather than an
#: intent — and expanding one would build a set large enough to exhaust
#: memory.
_MAX_CLAIM_SPAN: Final = 100


def parse_coverage(bullet: str) -> tuple[frozenset[int], str]:
    """Split a bullet into the fragment items it claims and its own text.

    The claim is authoring input, never message content: a reader of
    ``main`` must not meet the indices, so the text returned here is what
    gets posted.

    Args:
        bullet: One ``--bullet`` value, with or without a leading
            ``[1,4-6]`` claim.

    Returns:
        ``(claimed indices, bullet text)``. A bullet with no claim
        returns an empty set and its text unchanged — allowed, since a
        bullet may describe a consequence no fragment line states, as
        long as every fragment line is claimed somewhere.
    """
    claimed: set[int] = set()
    text = bullet
    # Every leading group, not just the first: `[1][2] text` and
    # `[1] [2] text` are things an author writes, and stripping one left
    # the other in the body — which becomes the permanent `main` commit.
    while match := _COVERAGE_RE.match(text):
        claimed |= _parse_spec(match["spec"])
        text = match["text"]
    return frozenset(claimed), text.strip()


def _parse_spec(spec: str) -> set[int]:
    """Return the indices a claim's ``1,4-6`` body names.

    Args:
        spec: The text between the claim's brackets.

    Returns:
        The indices named. A malformed piece contributes nothing and so
        surfaces as an unclaimed item, which the refusal names — better
        than guessing what was meant. A range wider than
        :data:`_MAX_CLAIM_SPAN` is malformed by the same rule: a typo'd
        bound (``[1-99999999999]``) would otherwise build a set large
        enough to exhaust memory.
    """
    claimed: set[int] = set()
    for part in spec.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            lo, _, hi = piece.partition("-")
            low, high = lo.strip(), hi.strip()
            if (
                low.isdigit()
                and high.isdigit()
                and int(high) - int(low) <= _MAX_CLAIM_SPAN
            ):
                claimed.update(range(int(low), int(high) + 1))
            continue
        if piece.isdigit():
            claimed.add(int(piece))
    return claimed


def _pr_fragment_items() -> tuple[list[str], str | None]:
    """Return the bullets this branch's changelog fragment delivers.

    Reads the repository, so it is kept out of :func:`validate`. The
    fragment is found exactly as the one-fragment pre-commit gate finds
    it — whatever the branch adds against its PR base — so the two
    cannot disagree about which fragment belongs to this merge.

    Returns:
        ``(items, note)``. *items* is empty when the repo runs no
        fragments, when the branch adds none, or when the one it adds
        cannot be read; coverage is then not checked, and *note* says
        why. Several fragments is not this gate's error to raise —
        pre-commit already refuses that — so it reports and skips rather
        than guessing which one the message must cover.
    """
    root = repo_root()
    if root is None:
        return [], "not in a repository — coverage not checked"
    if not is_fragments_mode(root):
        return [], None
    base = load_config(root).base_branch
    added = branch_added_fragments(root, base_ref=resolve_pr_base_ref(root, base))
    if not added:
        return [], "branch adds no changelog fragment — coverage not checked"
    if len(added) > 1:
        names = ", ".join(sorted(added))
        return [], (
            f"branch adds {len(added)} changelog fragments ({names}) — coverage "
            "not checked; pre-commit refuses more than one"
        )
    fragment, errors = validate_fragment(root / added[0])
    if fragment is None:
        detail = errors[0] if errors else "unreadable"
        return [], f"{added[0]} could not be read ({detail}) — coverage not checked"
    return fragment_bullets(fragment.body), None


def _check_coverage(claims: list[frozenset[int]], items: list[str]) -> list[str]:
    """Refuse a message that leaves part of the merge undescribed.

    The squash comment becomes the permanent ``main`` commit, and the
    fragment is the list of what the merge delivers. A message that
    describes some of it reads, forever after, as though that was all
    that shipped — which no word count detects: a message well under its
    ceiling can cover two of four deliverables.

    Grouping is free. A fragment is fine-grained and a message takes
    three to five bullets, so one bullet routinely covers several items;
    what is checked is that no item is left unclaimed.

    The check proves a claim was **made**, never that it is true: one
    bullet claiming every item passes. It catches forgetting, which is
    the failure that happens, not misrepresentation, which a reader of
    the diff catches.

    Args:
        claims: Each bullet's claimed indices, in bullet order.
        items: The fragment's bullets, 1-based by position.

    Returns:
        One problem naming every unclaimed item verbatim, and one naming
        any index claimed that the fragment does not have; empty when
        every item is claimed.
    """
    if not items:
        return []
    claimed: set[int] = set().union(*claims) if claims else set()
    problems: list[str] = []

    missing = [i for i in range(1, len(items) + 1) if i not in claimed]
    if missing:
        unclaimed = "\n".join(f"    {i}. {items[i - 1]}" for i in missing)
        problems.append(
            f"{len(missing)} changelog bullet(s) are covered by no squash "
            f"bullet — every line the merge delivers must be described:\n"
            f"{unclaimed}\n"
            f"  claim them with a leading [N] or [N-M] on the bullet that "
            f"describes them; one bullet may claim several"
        )

    unknown = sorted(i for i in claimed if i < 1 or i > len(items))
    if unknown:
        problems.append(
            f"coverage claims {unknown} but the fragment has "
            f"{len(items)} bullet(s) — renumber against --dry-run output"
        )
    return problems


def _check_attribution(title: str, bullets: list[str]) -> list[str]:
    """Return the Claude / AI attribution problem per FOUNDATION §2 (shared gate).

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Returns:
        The message :func:`forge.gh_comments.validate_no_ai_attribution`
        raises, or an empty list when the text is clean.
    """
    try:
        validate_no_ai_attribution("\n".join([title, *bullets]))
    except ValidationError as exc:
        return [str(exc)]
    return []


def build_body(title: str, bullets: list[str]) -> str:
    """Build the GitHub comment body around a validated message.

    Two fences behind the :data:`SQUASH_MARKER`, one per field of
    GitHub's squash dialog: the title, then the body. Separate fences
    because each is copied on its own — a single block holding both
    would have to be split by hand after pasting. Caller is responsible
    for having passed validated inputs (or running :func:`validate`
    first).

    Args:
        title: Squash title (single line, conventional-commit form).
        bullets: 3-5 bullet strings.

    Returns:
        Markdown body suitable for ``gh pr comment --body-file -``.
    """
    bullet_lines = "\n".join(f"- {b}" for b in bullets)
    fence = "```"
    return (
        f"{SQUASH_MARKER}\n"
        "**Squash-merge message** — copy each fence verbatim into the "
        "matching field of the squash dialog. The PR title is synced to "
        "the title below, so GitHub's prefill already matches.\n\n"
        "**Title**\n\n"
        f"{fence}\n{title}\n{fence}\n\n"
        "**Body**\n\n"
        f"{fence}\n{bullet_lines}\n{fence}\n"
    )


def validate(
    title: str,
    bullets: list[str],
    *,
    claims: list[frozenset[int]] | None = None,
    fragment_items: list[str] | None = None,
) -> list[str]:
    """Return every FOUNDATION §6 rule the message breaks.

    Every rule runs, so one failing run names all problems and the author
    fixes them in a single retry instead of one rule per round trip.

    Takes the fragment's bullets rather than reading them, so the rules
    stay testable without a checkout: resolving which fragment belongs to
    the PR reads the repository, and that happens in :func:`main`.

    Args:
        title: Squash title.
        bullets: Bullet strings, claims already stripped.
        claims: Each bullet's claimed fragment indices, in bullet order.
            ``None`` skips the coverage rule.
        fragment_items: The PR's fragment bullets, 1-based by position.
            ``None`` or empty skips the coverage rule — a repo with no
            fragment has nothing to cover.

    Returns:
        Problem messages in rule order (title, bullets, attribution,
        coverage); empty when the message is valid. Length never appears
        here: over its ceiling the message warns through
        :func:`word_cap_warning` and still posts.
    """
    problems = (
        _check_title(title)
        + _check_bullets(bullets)
        + _check_attribution(title, bullets)
    )
    if claims is not None and fragment_items:
        problems += _check_coverage(claims, fragment_items)
    return problems


def _list_squash_comments(pr_number: int) -> list[dict[str, object]] | None:
    """Return this CLI's own comments on *pr_number*, oldest first.

    Args:
        pr_number: GitHub PR number.

    Returns:
        Per :func:`forge.gh_comments.list_marker_comments` for
        :data:`SQUASH_MARKER`, anchored: only a comment whose body starts
        with the marker is a squash comment. A wrap-up that quotes the
        marker mid-text is not one, so it is never deleted as a
        superseded squash comment nor re-posted in its place.
    """
    return list_marker_comments(pr_number, SQUASH_MARKER, anchored=True)


def _latest_activity_at(pr_number: int) -> str | None:
    """Return the newest timestamp across every comment surface of a PR.

    Conversation comments, review-thread replies, and review
    submissions each live behind a different endpoint; a squash comment
    buried by a `/forge-pr-comments` reply is invisible to the conversation
    listing alone.

    Args:
        pr_number: GitHub PR number.

    Returns:
        The newest ISO-8601 UTC timestamp seen (GitHub normalizes to
        ``Z``, so lexicographic order is chronological order), or
        ``None`` when nothing could be read — the caller then re-posts
        rather than assuming the comment is still last.
    """
    endpoints = (
        (f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments", "created_at"),
        (f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments", "created_at"),
        (f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews", "submitted_at"),
    )
    stamps: list[str] = []
    for path, field in endpoints:
        raw = gh_api(
            path,
            "--paginate",
            "--jq",
            f"[.[] | .{field}]",
            timeout=GH_LIST_TIMEOUT,
        )
        if raw is None:
            return None
        stamps.extend(str(s) for s in parse_paged_json(raw) if s)
    return max(stamps) if stamps else None


def sync_pr_title(pr_number: int, title: str) -> bool:
    """Force the PR title to match the squash title.

    GitHub prefills the squash dialog's title field from the PR title,
    so the two must never disagree: an edited squash title that left
    the PR title behind would put a stale line in the permanent `main`
    commit. Reading first keeps the PR timeline free of no-op title
    events; an unreadable title still takes the write, because a
    redundant edit is cheaper than a silent mismatch.

    Args:
        pr_number: GitHub PR number.
        title: The validated squash title.

    Returns:
        ``True`` when the PR title already matched or was updated;
        ``False`` when the edit was rejected — the caller reports that,
        since the human then has to fix the title field by hand.
    """
    current = gh_api(f"repos/{{owner}}/{{repo}}/pulls/{pr_number}", "--jq", ".title")
    if current == title:
        return True
    proc = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "PATCH",
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}",
            "-f",
            f"title={title}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        logger.error(
            "could not sync PR #%d title to %r (exit %d): %s",
            pr_number,
            title,
            proc.returncode,
            proc.stderr.strip(),
        )
        return False
    if current is not None:
        logger.info("PR #%d title updated to match the squash title", pr_number)
    return True


def _prune(superseded: list[dict[str, object]] | None) -> None:
    """Delete the squash comments a fresh post has replaced.

    Runs after the new comment is live, so a failure here leaves a
    duplicate rather than a PR with no squash message — and never
    changes the exit code.

    Args:
        superseded: Comments captured *before* posting, or ``None``
            when the listing failed.
    """
    if superseded is None:
        logger.warning(
            "could not list existing comments; older squash comments may remain"
        )
        return
    for comment in superseded:
        delete_comment(int(str(comment["id"])))


def post_squash_comment(pr_number: int, body: str) -> int:
    """Post *body*, then delete the squash comments it supersedes.

    Args:
        pr_number: GitHub PR number.
        body: Pre-built comment body.

    Returns:
        ``0`` on a successful post. Cleanup is best-effort: the caller
        gets the post's exit code, and pruning failures only warn.
    """
    superseded = _list_squash_comments(pr_number)
    rc = post_new_comment(pr_number, body)
    if rc != 0:
        return rc
    _prune(superseded)
    return 0


def ensure_last(pr_number: int) -> int:
    """Re-post the existing squash comment so it is the newest again.

    What ``--pr`` does when no bullets are given, and what the
    ``keep_squash_comment_last`` hook runs after any command that
    comments on a PR: a quiet no-op while the squash comment is still
    the newest activity, a verbatim re-post (old one deleted) once
    anything — including a review-thread reply — has landed under it.

    Args:
        pr_number: GitHub PR number.

    Returns:
        ``0`` when the comment is last (already, or after re-posting);
        ``1`` when the PR could not be read or carries no squash
        comment to move.
    """
    existing = _list_squash_comments(pr_number)
    if existing is None:
        logger.error("could not read PR #%d comments; --ensure-last aborted", pr_number)
        return 1
    if not existing:
        logger.error(
            "PR #%d has no forge squash comment to move; post one with --bullet first",
            pr_number,
        )
        return 1
    newest_squash = str(existing[-1].get("created_at", ""))
    latest = _latest_activity_at(pr_number)
    if len(existing) == 1 and latest is not None and newest_squash >= latest:
        logger.info("squash comment is already the newest comment on PR #%d", pr_number)
        return 0
    rc = post_new_comment(pr_number, str(existing[-1]["body"]))
    if rc != 0:
        return rc
    _prune(existing)
    return 0


def main() -> int:
    """Validate the body, post it (or print it), and keep it last.

    Returns:
        ``0`` on success. ``1`` on validation failure (every broken
        rule named on stderr) or non-zero ``gh`` exit.
    """
    parser = argparse.ArgumentParser(
        prog="forge-pr-squash-comment",
        description=(
            "Post the squash-merge body as the PR's newest comment. "
            "The title is the PR title — GitHub prefills it. "
            "Rules per FOUNDATION §6."
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--pr",
        type=int,
        help=(
            "PR number to comment on. With --bullet: syncs the PR title, "
            "posts the message and prunes older squash comments. Without: "
            "re-posts the existing one so it is the newest comment again."
        ),
    )
    target.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the wrapped body to stdout and the per-part word counts "
            "to stderr; do not call gh."
        ),
    )
    parser.add_argument(
        "--title",
        default="",
        help=(
            "Squash title (conventional-commit format). Required with "
            "--bullet; the PR title is forced to match it."
        ),
    )
    parser.add_argument(
        "--bullet",
        action="append",
        default=[],
        metavar="TEXT",
        help=(
            "Bullet line, optionally opening with the changelog bullets it "
            "covers: '[1,4-6] text'. Repeat 3-5 times. Every changelog "
            "bullet must be claimed by some bullet or the post is refused; "
            "run --dry-run to see them numbered. The claim never appears in "
            "the posted message."
        ),
    )
    args = parser.parse_args()

    # Keeping the comment last is the default, not a mode: a bare
    # `--pr N` is the "PR moved on, move the comment" call.
    if args.pr is not None and not args.bullet:
        return ensure_last(args.pr)

    # The claim is authoring input; strip it before anything renders, so
    # no index can reach the comment that becomes the `main` commit.
    parsed = [parse_coverage(raw) for raw in args.bullet]
    claims = [claimed for claimed, _ in parsed]
    bullets = [text for _, text in parsed]
    fragment_items, fragment_note = _pr_fragment_items()
    if fragment_note:
        sys.stderr.write(f"forge-pr-squash-comment: {fragment_note}\n")
    # Before validation, not after: a dry run is how an author learns the
    # numbering a claim refers to, and a dry run with no claims yet is
    # exactly the call that fails the coverage rule. Printing this only
    # on the way to success would withhold it from the one caller who
    # needs it.
    if args.dry_run and fragment_items:
        numbered = "\n".join(
            f"  {i}. {item}" for i, item in enumerate(fragment_items, start=1)
        )
        sys.stderr.write(f"changelog bullets to cover:\n{numbered}\n")

    problems = validate(
        args.title, bullets, claims=claims, fragment_items=fragment_items
    )
    if problems:
        # A multi-line problem keeps the prefix on its first line only, so
        # the rest reads as one indented block.
        for problem in problems:
            sys.stderr.write(f"forge-pr-squash-comment: {problem}\n")
        return 1

    body = build_body(args.title, bullets)
    if warning := word_cap_warning(args.title, bullets):
        sys.stderr.write(f"forge-pr-squash-comment: warning: {warning}\n")

    if args.dry_run:
        # stdout stays exactly the body; the counts go to stderr beside it.
        sys.stdout.write(body)
        breakdown = "\n".join(_word_breakdown(args.title, bullets))
        total = _word_total(args.title, bullets)
        sys.stderr.write(f"{total}/{MAX_WORDS} words\n{breakdown}\n")
        return 0

    title_synced = sync_pr_title(args.pr, args.title)
    rc = post_squash_comment(args.pr, body)
    if rc != 0:
        return rc
    # The comment is live either way; a failed title sync is still a
    # non-zero run, because the prefill no longer matches what it says.
    return 0 if title_synced else 1


if __name__ == "__main__":
    sys.exit(main())
