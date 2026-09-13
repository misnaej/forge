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
- 3-5 ``--bullet`` entries
- total whitespace-split word count (title + bullets) ≤ 50
- no Claude / AI attribution patterns

A failing run names every broken rule at once; a word-cap violation
also lists each part's word count and how many words to cut.

Output: the body posted to GitHub is the literal text below (the inner
fences are real ``` blocks, not escapes):

    <!-- forge:squash-merge-message -->
    **Squash-merge message** — copy each fence into the matching field
    of the squash dialog. The PR title is already synced to the title.

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

from forge.gh_comments import (
    GH_LIST_TIMEOUT,
    ValidationError,
    delete_comment,
    list_marker_comments,
    parse_paged_json,
    post_new_comment,
    validate_no_ai_attribution,
)
from forge.git_utils import configure_cli_logging, gh_api


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

# Conventional commit: `<type>(<scope>)?: <subject>` — scope optional,
# allows multiple `#N` refs separated by commas inside parens.
TITLE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<type>" + "|".join(CONVENTIONAL_COMMIT_TYPES) + r")"
    r"(?:\((?P<scope>[^)]+)\))?"
    r": (?P<subject>.+)$",
)

MIN_BULLETS: Final[int] = 3
MAX_BULLETS: Final[int] = 5
MAX_WORDS: Final[int] = 50
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
                f"Expected '<type>(<scope>)?: <subject>' where type is one of: "
                f"{', '.join(CONVENTIONAL_COMMIT_TYPES)}"
            )
        ]
    return []


def _check_bullets(bullets: list[str]) -> list[str]:
    """Return the bullet-count problem and one problem per empty bullet.

    Args:
        bullets: List of ``--bullet`` strings as passed by the caller.

    Returns:
        A message when the count is outside ``[MIN_BULLETS, MAX_BULLETS]``,
        followed by one message per whitespace-only bullet.
    """
    problems: list[str] = []
    n = len(bullets)
    if not MIN_BULLETS <= n <= MAX_BULLETS:
        problems.append(
            f"got {n} bullet(s); FOUNDATION §6 requires {MIN_BULLETS}-{MAX_BULLETS}"
        )
    problems.extend(
        f"bullet {i} is empty" for i, b in enumerate(bullets, start=1) if not b.strip()
    )
    return problems


def _word_total(title: str, bullets: list[str]) -> int:
    """Count whitespace-split words across the title and every bullet.

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Returns:
        The combined word count :data:`MAX_WORDS` caps.
    """
    return len(title.split()) + sum(len(b.split()) for b in bullets)


def _preview(text: str) -> str:
    """Shorten one message part to its first line, at most :data:`PREVIEW_CHARS`.

    Args:
        text: A title or bullet.

    Returns:
        The first non-blank line, cut with ``…`` when longer than
        :data:`PREVIEW_CHARS`; ``(empty)`` for a whitespace-only part.
    """
    stripped = text.strip()
    if not stripped:
        return "(empty)"
    first = stripped.splitlines()[0]
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


def _check_word_count(title: str, bullets: list[str]) -> list[str]:
    """Return the ≤ ``MAX_WORDS`` cap problem, with a per-part breakdown.

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Returns:
        An empty list at or under the cap. Over it, one problem whose
        first line names the total and how many words to cut, followed
        by the :func:`_word_breakdown` lines.
    """
    total = _word_total(title, bullets)
    if total <= MAX_WORDS:
        return []
    header = (
        f"squash-merge message is {total} words; FOUNDATION §6 caps at "
        f"{MAX_WORDS} (cut {total - MAX_WORDS})"
    )
    return ["\n".join([header, *_word_breakdown(title, bullets)])]


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


def validate(title: str, bullets: list[str]) -> list[str]:
    """Return every FOUNDATION §6 rule the message breaks.

    Every rule runs, so one failing run names all problems and the author
    fixes them in a single retry instead of one rule per round trip.

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Returns:
        Problem messages in rule order (title, bullets, word count,
        attribution); empty when the message is valid.
    """
    return (
        _check_title(title)
        + _check_bullets(bullets)
        + _check_word_count(title, bullets)
        + _check_attribution(title, bullets)
    )


def _list_squash_comments(pr_number: int) -> list[dict[str, object]] | None:
    """Return this CLI's own comments on *pr_number*, oldest first.

    Args:
        pr_number: GitHub PR number.

    Returns:
        Per :func:`forge.gh_comments.list_marker_comments` for
        :data:`SQUASH_MARKER`.
    """
    return list_marker_comments(pr_number, SQUASH_MARKER)


def _latest_activity_at(pr_number: int) -> str | None:
    """Return the newest timestamp across every comment surface of a PR.

    Conversation comments, review-thread replies, and review
    submissions each live behind a different endpoint; a squash comment
    buried by a `/pr-comments` reply is invisible to the conversation
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
        help="Bullet line. Repeat 3-5 times.",
    )
    args = parser.parse_args()

    # Keeping the comment last is the default, not a mode: a bare
    # `--pr N` is the "PR moved on, move the comment" call.
    if args.pr is not None and not args.bullet:
        return ensure_last(args.pr)

    problems = validate(args.title, args.bullet)
    if problems:
        # A multi-line problem (the word-cap breakdown) keeps the prefix on
        # its first line only, so the breakdown reads as one indented block.
        for problem in problems:
            sys.stderr.write(f"forge-pr-squash-comment: {problem}\n")
        return 1

    body = build_body(args.title, args.bullet)

    if args.dry_run:
        # stdout stays exactly the body; the counts go to stderr beside it.
        sys.stdout.write(body)
        breakdown = "\n".join(_word_breakdown(args.title, args.bullet))
        total = _word_total(args.title, args.bullet)
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
