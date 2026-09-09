"""forge-pr-wrapup — validate and post the PR wrap-up comment, retiring older ones.

The wrap-up is the verification record a reviewer trusts most: which
reporters ran at which commit, what they found, how each finding was
dispositioned. Two things go wrong when it is posted by hand. It grows —
``_TEMPLATE.md``'s report-by-exception rule ("a clean check is ONE line")
lives in agent prose, and prose is a remembered step — and it never gets
retired, so every earlier wrap-up on the PR keeps reading as a current
attestation of a tree that no longer exists. This CLI makes both
mechanical: ``validate`` refuses a body that narrates clean checks or
exceeds its budget, and ``post`` stamps the new wrap-up with a marker,
collapses every earlier marker-carrying wrap-up into a ``<details>``
block (history kept, one live attestation visible), and re-posts the
squash-merge comment so it stays the PR's newest.

The header contract (``verified-at:`` first line, optional
``wrapup-mode:`` and ``prior-art-searched:`` lines) is
``agents/_TEMPLATE.md``'s; ``block_unverified_pr_create`` and
``forge-pr-plan --freshness`` read the same lines, so the collapse keeps
the original body — header included — verbatim inside the details block.

Usage:

- ``forge-pr-wrapup validate <file>`` — exit 2 listing every violation
- ``forge-pr-wrapup post --pr N --body-file <file>`` — validate, post,
  collapse superseded wrap-ups, keep the squash comment last
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Final

from forge.gh_comments import (
    ValidationError,
    list_marker_comments,
    patch_comment,
    post_new_comment,
    validate_no_ai_attribution,
)
from forge.git_utils import configure_cli_logging, emit
from forge.pr_delta import VERIFIED_AT_RE, extract_verified_shas
from forge.pr_squash_comment import ensure_last


configure_cli_logging()
logger = logging.getLogger(__name__)

# Invisible in rendered markdown, greppable in the raw body: how a later
# post recognizes the wrap-ups it supersedes. Same convention as the
# squash-comment marker.
WRAPUP_MARKER: Final[str] = "<!-- forge:pr-wrapup -->"

# Sections every wrap-up carries (pr-manager's posting contract). Extra
# sections (Issue Management, Testing, ...) are allowed; these must exist.
REQUIRED_SECTIONS: Final[tuple[str, ...]] = (
    "Design Check",
    "Security Review",
    "Documentation Check",
    "Code Quality",
    "CI Status",
    "Recommendation",
)

# Sections that are status lines by nature — one line whatever they say.
STATUS_SECTIONS: Final[tuple[str, ...]] = (
    "Code Quality",
    "CI Status",
    "Recommendation",
)

# Report by exception, made numeric: an all-clean wrap-up is header + one
# summary line + one line per section — 120 words is generous for that;
# every findings section (a reporter that did not simply PASS) earns 40
# more for what/where/disposition prose.
WORD_BUDGET: Final[int] = 120
FINDING_ALLOWANCE: Final[int] = 40

# A section opening with one of these is clean and must be exactly one line.
_CLEAN_PREFIXES: Final[tuple[str, ...]] = ("pass", "skipped", "n/a")

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(```|~~~)")
_MODE_RE: Final[re.Pattern[str]] = re.compile(r"^wrapup-mode:\s*\S+", re.IGNORECASE)
_PRIOR_ART_RE: Final[re.Pattern[str]] = re.compile(r"^prior-art-searched:")

SUPERSEDED_SUMMARY: Final[str] = (
    "Superseded by verified-at {new} — this wrap-up verified {old}"
)


def _strip_fences(lines: list[str]) -> list[str]:
    """Return *lines* without fenced code blocks (fence lines included).

    Args:
        lines: List of markdown lines.

    Returns:
        Lines with fenced code blocks (and their delimiters) removed.
    """
    kept: list[str] = []
    inside = False
    for line in lines:
        if _FENCE_RE.match(line):
            inside = not inside
            continue
        if not inside:
            kept.append(line)
    return kept


def _split(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split the body into its head and its ``## `` sections, in order.

    Args:
        text: The wrap-up markdown.

    Returns:
        ``(head_lines, [(title, body_lines), ...])`` with fenced blocks
        already removed — fences carry quoted tool output, never prose
        the budget should count.
    """
    head: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in _strip_fences(text.splitlines()):
        match = _HEADING_RE.match(line)
        if match:
            current = []
            sections.append((match.group("title"), current))
        elif current is None:
            head.append(line)
        else:
            current.append(line)
    return head, sections


def _prior_art_block(head: list[str]) -> set[int]:
    """Return the indexes of the ``prior-art-searched:`` block within *head*.

    The block is the ``prior-art-searched:`` line and every following
    line up to the next blank one — the reporter's evidence, which the
    budget does not count.

    Args:
        head: Header lines before the first section.

    Returns:
        Set of line indexes in the prior-art block.
    """
    indexes: set[int] = set()
    inside = False
    for i, line in enumerate(head):
        if _PRIOR_ART_RE.match(line):
            inside = True
        elif inside and not line.strip():
            inside = False
        if inside:
            indexes.add(i)
    return indexes


def _check_head(head: list[str]) -> list[str]:
    """Validate the header contract and the single summary line.

    Args:
        head: Lines before the first ``## `` section.

    Returns:
        Violations, empty when the head is well-formed.
    """
    problems: list[str] = []
    if not head or not VERIFIED_AT_RE.match(head[0]):
        problems.append("first line must be the `verified-at: <sha>` header")
    evidence = _prior_art_block(head)
    summary_lines = [
        line
        for i, line in enumerate(head[1:], start=1)
        if line.strip()
        and i not in evidence
        and not _MODE_RE.match(line)
        and not line.startswith("# ")
        and not line.startswith("<!--")
    ]
    if len(summary_lines) != 1:
        problems.append(
            "exactly one summary line belongs between the header and the first "
            f"section (found {len(summary_lines)}); the rest is narration"
        )
    return problems


def _is_clean(body: list[str]) -> bool:
    """Return whether a section reports a clean check (its first line says so).

    Args:
        body: Section body lines.

    Returns:
        True when the first non-blank line starts with a clean prefix.
    """
    first = next((line.strip() for line in body if line.strip()), "")
    return first.lower().startswith(_CLEAN_PREFIXES)


def _check_sections(sections: list[tuple[str, list[str]]]) -> list[str]:
    """Validate presence and one-line shape of the required sections.

    Args:
        sections: ``(title, body_lines)`` pairs in document order.

    Returns:
        Violations, empty when every required section is present and
        every clean or status section is exactly one line.
    """
    problems: list[str] = []
    titles = {title.casefold(): body for title, body in sections}
    for required in REQUIRED_SECTIONS:
        body = titles.get(required.casefold())
        if body is None:
            problems.append(f"missing section `## {required}`")
            continue
        n_lines = sum(1 for line in body if line.strip())
        if n_lines <= 1:
            continue
        if required in STATUS_SECTIONS:
            problems.append(
                f"`## {required}` is a status line — one line, found {n_lines}"
            )
        elif _is_clean(body):
            problems.append(
                f"`## {required}` is clean (PASS) but runs {n_lines} lines — a clean "
                "check is ONE line; prose is for findings"
            )
    return problems


def _count_words(head: list[str], sections: list[tuple[str, list[str]]]) -> int:
    """Count the words the budget applies to (prose, not headers or evidence).

    Args:
        head: Header lines before the first section.
        sections: List of (title, body_lines) section tuples.

    Returns:
        Total word count of budgeted prose.
    """
    evidence = _prior_art_block(head)
    counted: list[str] = [
        line
        for i, line in enumerate(head)
        if i not in evidence
        and not VERIFIED_AT_RE.match(line)
        and not _MODE_RE.match(line)
        and not line.startswith("<!--")
    ]
    for _title, body in sections:
        counted.extend(line for line in body if not line.startswith("<!--"))
    return sum(len(line.split()) for line in counted)


def _check_budget(head: list[str], sections: list[tuple[str, list[str]]]) -> list[str]:
    """Enforce the word budget, scaled by the number of findings sections.

    Args:
        head: Lines before the first section.
        sections: ``(title, body_lines)`` pairs in document order.

    Returns:
        One violation when the body exceeds ``WORD_BUDGET`` plus
        ``FINDING_ALLOWANCE`` per required non-clean, non-status section.
    """
    titles = {title.casefold(): body for title, body in sections}
    findings = sum(
        1
        for required in REQUIRED_SECTIONS
        if required not in STATUS_SECTIONS
        and (body := titles.get(required.casefold())) is not None
        and not _is_clean(body)
    )
    budget = WORD_BUDGET + FINDING_ALLOWANCE * findings
    words = _count_words(head, sections)
    if words > budget:
        return [
            (
                f"{words} words over a budget of {budget} ({WORD_BUDGET} + "
                f"{FINDING_ALLOWANCE} x {findings} findings); clean checks "
                "are one line"
            )
        ]
    return []


def _check_attribution(text: str) -> list[str]:
    """Wrap the shared FOUNDATION §2 attribution gate as a violation list.

    Args:
        text: The wrap-up markdown to validate.

    Returns:
        Violations if AI attribution is detected, empty list otherwise.
    """
    try:
        validate_no_ai_attribution(text)
    except ValidationError as exc:
        return [str(exc)]
    return []


def validate_wrapup(text: str) -> list[str]:
    """Return every rule the wrap-up *text* breaks (empty means valid).

    Pure: text in, violations out — the rules are ``_TEMPLATE.md``'s
    header contract and report-by-exception paragraph, made checkable.

    Args:
        text: The wrap-up markdown as it would be posted.

    Returns:
        Human-readable violations in document order.
    """
    head, sections = _split(text)
    return (
        _check_head(head)
        + _check_sections(sections)
        + _check_budget(head, sections)
        + _check_attribution(text)
    )


def _collapse(existing: list[dict[str, object]], new_sha: str) -> int:
    """Fold every earlier wrap-up into a ``<details>`` block, once.

    Args:
        existing: Marker-carrying wrap-ups captured *before* the new post.
        new_sha: The new wrap-up's ``verified-at`` SHA, named in the summary.

    Returns:
        How many comments were collapsed; already-collapsed ones are skipped.
    """
    collapsed = 0
    for comment in existing:
        body = str(comment.get("body", ""))
        if body.lstrip().startswith("<details>"):
            continue
        shas = extract_verified_shas(body)
        old = shas[0] if shas else "?"
        summary = SUPERSEDED_SUMMARY.format(new=new_sha, old=old)
        new_body = f"<details><summary>{summary}</summary>\n\n{body}\n\n</details>\n"
        if patch_comment(int(str(comment["id"])), new_body):
            collapsed += 1
    return collapsed


def post_wrapup(pr_number: int, body: str) -> int:
    """Post *body* as the PR's wrap-up, retiring the ones it supersedes.

    Order matters: the earlier wrap-ups are listed before the post, so
    the new one can never be mistaken for a superseded one; the collapse
    runs after the post is live, so a failure there leaves an extra
    visible attestation rather than a PR with none; the squash comment is
    re-posted last (its own CLI's invariant) because a wrap-up posted
    through this CLI is invisible to the ``keep_squash_comment_last``
    hook, which only sees the outer Bash command.

    Args:
        pr_number: GitHub PR number.
        body: The validated wrap-up markdown (marker appended here).

    Returns:
        ``0`` when the post succeeded; the ``gh`` exit code otherwise.
    """
    existing = list_marker_comments(pr_number, WRAPUP_MARKER)
    stamped = body.rstrip("\n") + "\n\n" + WRAPUP_MARKER + "\n"
    rc = post_new_comment(pr_number, stamped)
    if rc != 0:
        return rc
    shas = extract_verified_shas(body)
    new_sha = shas[0] if shas else "?"
    if existing is None:
        logger.warning("could not list earlier wrap-ups; none were collapsed")
    else:
        n = _collapse(existing, new_sha)
        emit(
            f"Posted wrap-up (verified-at {new_sha}); "
            f"collapsed {n} superseded wrap-up(s)."
        )
    # Keeps the squash comment newest; a PR with no squash comment yet
    # just logs — that is the normal first-wrap-up case, not a failure.
    ensure_last(pr_number)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run ``validate`` or ``post``.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        ``0`` on success; ``2`` when validation fails (every violation
        printed) or the body file is unreadable; ``gh``'s exit code when
        the post fails.
    """
    parser = argparse.ArgumentParser(
        prog="forge-pr-wrapup",
        description="Validate and post the PR wrap-up comment (FOUNDATION §6).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser(
        "validate", help="check a wrap-up body; exit 2 listing violations"
    )
    val.add_argument(
        "file", type=Path, help="wrap-up markdown (code_health/pr_wrapup.md)"
    )
    post = sub.add_parser("post", help="validate, post, collapse superseded wrap-ups")
    post.add_argument("--pr", type=int, required=True, metavar="N", help="PR number")
    post.add_argument(
        "--body-file", type=Path, required=True, metavar="FILE", help="wrap-up markdown"
    )
    args = parser.parse_args(argv)
    path: Path = args.file if args.command == "validate" else args.body_file
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("pr-wrapup: cannot read %s", path)
        return 2
    problems = validate_wrapup(text)
    if problems:
        for problem in problems:
            emit(f"pr-wrapup: {problem}")
        return 2
    if args.command == "validate":
        emit(f"pr-wrapup: {path} is valid.")
        return 0
    return post_wrapup(args.pr, text)


if __name__ == "__main__":
    sys.exit(main())
