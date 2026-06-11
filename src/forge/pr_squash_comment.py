r"""forge-pr-squash-comment — validate, wrap, and post the squash-merge message.

Accepts structured ``--title`` / ``--bullet`` arguments, validates them
against FOUNDATION §6 squash-merge rules, wraps the result in a literal
triple-backtick fence, and posts it as a PR comment via ``gh``.

Usage:

    forge-pr-squash-comment --pr 61 \\
        --title "feat(#60): pr-manager delta-mode + verified-at" \\
        --bullet "pr_delta.py centralizes thresholds and regex" \\
        --bullet "5 reporter agents stamp verified-at SHA" \\
        --bullet "pr-manager short-circuits small follow-ups" \\
        --bullet "audit enforces the contract by name allowlist"

    forge-pr-squash-comment --pr 61 --dry-run --title ... --bullet ...
        # prints the wrapped body to stdout, no gh call

    forge-pr-squash-comment --patch 4575789522 --title ... --bullet ...
        # rewrites an existing comment via the GitHub REST API

Rules (FOUNDATION §6 "Squash-merge messages"):

- title matches conventional-commit ``<type>(...)?: <subject>``
- 3-5 ``--bullet`` entries
- total whitespace-split word count (title + bullets) ≤ 50
- no Claude / AI attribution patterns

Output: the body posted to GitHub is the literal text below (the inner
fence is a real ``` block, not escapes):

    **Squash-merge message** (copy verbatim):

    ```
    <title>

    - <bullet 1>
    - <bullet 2>
    - <bullet 3>
    ```
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from typing import Final

from forge.git_utils import configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


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

# Patterns that signal AI attribution. Case-insensitive substring match.
AI_ATTRIBUTION_PATTERNS: Final[tuple[str, ...]] = (
    "claude",
    "anthropic",
    "co-authored-by:",
    "🤖",
    "generated with",
    "assisted by ai",
    "ai-generated",
)


class ValidationError(ValueError):
    """Raised when the input fails a FOUNDATION §6 squash-merge rule."""


def _validate_title(title: str) -> None:
    """Reject titles outside the conventional-commit format.

    Args:
        title: Raw title string.

    Raises:
        ValidationError: When the title is empty, longer than one line,
            or does not match :data:`TITLE_RE`.
    """
    if not title.strip():
        msg = "title is empty"
        raise ValidationError(msg)
    if "\n" in title:
        msg = "title must be a single line"
        raise ValidationError(msg)
    if not TITLE_RE.match(title):
        msg = (
            f"title {title!r} is not conventional-commit format. "
            f"Expected '<type>(<scope>)?: <subject>' where type is one of: "
            f"{', '.join(CONVENTIONAL_COMMIT_TYPES)}"
        )
        raise ValidationError(msg)


def _validate_bullets(bullets: list[str]) -> None:
    """Enforce bullet count + non-empty content.

    Args:
        bullets: List of ``--bullet`` strings as passed by the caller.

    Raises:
        ValidationError: When the count is outside ``[MIN_BULLETS,
            MAX_BULLETS]`` or any bullet is whitespace-only.
    """
    n = len(bullets)
    if not MIN_BULLETS <= n <= MAX_BULLETS:
        msg = f"got {n} bullet(s); FOUNDATION §6 requires {MIN_BULLETS}-{MAX_BULLETS}"
        raise ValidationError(msg)
    for i, b in enumerate(bullets, start=1):
        if not b.strip():
            msg = f"bullet {i} is empty"
            raise ValidationError(msg)


def _validate_word_count(title: str, bullets: list[str]) -> None:
    """Enforce the ≤ ``MAX_WORDS`` cap on title + bullets combined.

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Raises:
        ValidationError: When the total whitespace-split word count
            exceeds :data:`MAX_WORDS`.
    """
    total = len(title.split()) + sum(len(b.split()) for b in bullets)
    if total > MAX_WORDS:
        msg = (
            f"squash-merge message is {total} words; FOUNDATION §6 caps at {MAX_WORDS}"
        )
        raise ValidationError(msg)


def _validate_no_ai_attribution(title: str, bullets: list[str]) -> None:
    """Reject Claude / AI attribution per FOUNDATION §2.

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Raises:
        ValidationError: When the title or any bullet contains a token
            from :data:`AI_ATTRIBUTION_PATTERNS` (case-insensitive).
    """
    blob = "\n".join([title, *bullets]).lower()
    for pat in AI_ATTRIBUTION_PATTERNS:
        if pat in blob:
            msg = (
                f"AI attribution pattern detected: {pat!r}. "
                "FOUNDATION §2 forbids Claude/AI attribution in commits "
                "and PR content."
            )
            raise ValidationError(msg)


def build_body(title: str, bullets: list[str]) -> str:
    """Build the GitHub comment body around a validated message.

    Wraps the title + bullets in a literal triple-backtick fence and
    prepends the "copy verbatim" cue. Caller is responsible for having
    passed validated inputs (or running :func:`validate` first).

    Args:
        title: Squash title (single line, conventional-commit form).
        bullets: 3-5 bullet strings.

    Returns:
        Markdown body suitable for ``gh pr comment --body-file -``.
    """
    bullet_lines = "\n".join(f"- {b}" for b in bullets)
    fence = "```"
    return (
        "**Squash-merge message** (copy verbatim):\n\n"
        f"{fence}\n"
        f"{title}\n\n"
        f"{bullet_lines}\n"
        f"{fence}\n"
    )


def validate(title: str, bullets: list[str]) -> None:
    """Run every FOUNDATION §6 check in order.

    Args:
        title: Squash title.
        bullets: Bullet strings.

    Raises:
        ValidationError: At the first failing rule. The exception
            message names the rule and (when applicable) the observed
            vs. allowed values.
    """
    _validate_title(title)
    _validate_bullets(bullets)
    _validate_word_count(title, bullets)
    _validate_no_ai_attribution(title, bullets)


def _post_new_comment(pr_number: int, body: str) -> int:
    """Post *body* as a new comment on PR ``pr_number``.

    Args:
        pr_number: GitHub PR number.
        body: Pre-built comment body.

    Returns:
        Exit code from ``gh pr comment``.
    """
    proc = subprocess.run(
        ["gh", "pr", "comment", str(pr_number), "--body-file", "-"],
        input=body,
        text=True,
        check=False,
    )
    return proc.returncode


_REPO_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"^[\w.-]+/[\w.-]+$")


def _patch_existing_comment(comment_id: int, body: str) -> int:
    """Rewrite an existing PR comment via the REST API.

    Args:
        comment_id: GitHub issue/PR comment id (numeric).
        body: Replacement body.

    Returns:
        Exit code from ``gh api``. Non-zero ``gh`` output is captured
        and logged rather than echoed to the terminal so a failed call
        does not splatter the user-supplied body into the surrounding
        log.
    """
    repo = _current_repo()
    if repo is None:
        logger.error("could not detect current GitHub repo; aborting --patch")
        return 1
    if not _REPO_SLUG_RE.match(repo):
        logger.error("gh returned suspect repo slug %r; aborting --patch", repo)
        return 1
    proc = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "PATCH",
            f"repos/{repo}/issues/comments/{comment_id}",
            "-f",
            f"body={body}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        logger.error(
            "gh api PATCH failed (exit %d): %s", proc.returncode, proc.stderr.strip()
        )
    return proc.returncode


def _current_repo() -> str | None:
    """Return ``<owner>/<repo>`` for the current working directory.

    Returns:
        Slug from ``gh repo view --json nameWithOwner``, or ``None``
        when ``gh`` is missing or no remote is configured.
    """
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)["nameWithOwner"]
    except (json.JSONDecodeError, KeyError):
        return None


def main() -> int:
    """Validate the message, build the body, and post (or print) it.

    Returns:
        ``0`` on success. ``1`` on validation failure (with the
        offending rule named on stderr) or non-zero ``gh`` exit.
    """
    parser = argparse.ArgumentParser(
        prog="forge-pr-squash-comment",
        description=(
            "Validate, fence-wrap, and post a squash-merge message as a "
            "PR comment. Replaces hand-built heredoc templates in "
            "pr-manager. Rules per FOUNDATION §6."
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--pr",
        type=int,
        help="PR number to comment on (creates a new comment).",
    )
    target.add_argument(
        "--patch",
        type=int,
        metavar="COMMENT_ID",
        help="Rewrite an existing comment instead of posting a new one.",
    )
    target.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the wrapped body to stdout; do not call gh.",
    )
    parser.add_argument(
        "--title",
        required=True,
        help="Squash title (conventional-commit format).",
    )
    parser.add_argument(
        "--bullet",
        action="append",
        default=[],
        metavar="TEXT",
        help="Bullet line. Repeat 3-5 times.",
    )
    args = parser.parse_args()

    try:
        validate(args.title, args.bullet)
    except ValidationError as exc:
        sys.stderr.write(f"forge-pr-squash-comment: {exc}\n")
        return 1

    body = build_body(args.title, args.bullet)

    if args.dry_run:
        sys.stdout.write(body)
        return 0

    if args.patch is not None:
        return _patch_existing_comment(args.patch, body)
    return _post_new_comment(args.pr, body)


if __name__ == "__main__":
    sys.exit(main())
