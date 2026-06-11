"""install-forge-labels — install the forge canonical label schema into a repository.

Idempotent label installer. Reads the canonical schema and creates any
missing labels in the current repository via `gh label create`. Existing
labels are left alone.

Usage:
    install-forge-labels [--repo OWNER/REPO]

If --repo is omitted, uses the current directory's GitHub remote.

Exit codes:
    0: All labels present (created or pre-existing)
    1: gh CLI missing or auth failure
    2: One or more labels failed to create
"""

import argparse
import json
import logging
import re
import subprocess
import sys

from forge.git_utils import configure_cli_logging, require_cli


configure_cli_logging()
logger = logging.getLogger(__name__)


# Canonical label schema (must match FOUNDATION.md § 14).
CANONICAL_LABELS: list[dict[str, str]] = [
    # Tier
    {
        "name": "tier-1-critical",
        "color": "B60205",
        "description": "Blocks other work, breaks CI, or security-urgent",
    },
    {"name": "tier-2-high", "color": "D93F0B", "description": "Important + high ROI"},
    {
        "name": "tier-3-standard",
        "color": "0075CA",
        "description": "Normal features / refactors",
    },
    {"name": "tier-4-low", "color": "CCCCCC", "description": "Nice-to-have / research"},
    {
        "name": "needs-triage",
        "color": "FBCA04",
        "description": "Newly opened, awaiting tier assignment",
    },
    # State
    {
        "name": "blocked",
        "color": "E99695",
        "description": "Waiting on a dependency or upstream",
    },
    {
        "name": "needs-discussion",
        "color": "FBCA04",
        "description": "Team input required before work starts",
    },
    {
        "name": "waiting-upstream",
        "color": "D4C5F9",
        "description": "Blocked on external release",
    },
    {
        "name": "stale",
        "color": "999999",
        "description": "No activity > 180 days; review for closure",
    },
    # Type
    {"name": "bug", "color": "D73A4A", "description": "Something is broken"},
    {"name": "feature", "color": "A2EEEF", "description": "New capability"},
    {
        "name": "refactor",
        "color": "7B68EE",
        "description": "Internal improvement, no behaviour change",
    },
    {"name": "docs", "color": "0075CA", "description": "Documentation only"},
    {"name": "tech-debt", "color": "BFD4F2", "description": "Cleanup / consolidation"},
    {"name": "security", "color": "B60205", "description": "Security-sensitive"},
    {"name": "research", "color": "C5DEF5", "description": "Investigation / spike"},
    # Surface
    {
        "name": "quick-win",
        "color": "28A745",
        "description": "Easy + isolated + low-risk",
    },
    {"name": "architecture", "color": "7B68EE", "description": "Cross-cutting design"},
    {"name": "performance", "color": "FF6B6B", "description": "Performance-sensitive"},
    {
        "name": "ci-testing",
        "color": "FFA500",
        "description": "CI / test infrastructure",
    },
    {"name": "breaking-change", "color": "B60205", "description": "Public API break"},
]


def _existing_labels(repo: str | None) -> set[str]:
    """Return set of existing label names in the repo.

    Args:
        repo: Repository in OWNER/REPO format, or None for current directory's remote.

    Returns:
        Set of existing label names.
    """
    cmd = ["gh", "label", "list", "--limit", "200", "--json", "name"]
    if repo:
        cmd += ["--repo", repo]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error("gh label list failed: %s", result.stderr.strip())
        return set()
    return {entry["name"] for entry in json.loads(result.stdout)}


def _create_label(label: dict[str, str], repo: str | None) -> bool:
    """Create one label. Returns True on success.

    Args:
        label: Label configuration dict with name, color, and description.
        repo: Repository in OWNER/REPO format, or None for current directory's remote.

    Returns:
        True if label was created successfully, False otherwise.
    """
    cmd = [
        "gh",
        "label",
        "create",
        label["name"],
        "--color",
        label["color"],
        "--description",
        label["description"],
    ]
    if repo:
        cmd += ["--repo", repo]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error("Failed to create %s: %s", label["name"], result.stderr.strip())
        return False
    return True


_REPO_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def main() -> int:
    """Install canonical foundation labels in the current GitHub repo.

    Returns:
        ``0`` on success; ``1`` if gh is missing/unauthenticated; ``2`` if any
        label failed to create.
    """
    parser = argparse.ArgumentParser(
        prog="install-forge-labels",
        description="Install Forge canonical labels.",
    )
    parser.add_argument("--repo", help="OWNER/REPO (defaults to current dir's remote)")
    args = parser.parse_args()

    if args.repo and not _REPO_PATTERN.fullmatch(args.repo):
        logger.error("--repo must be OWNER/REPO (got %r)", args.repo)
        return 1

    require_cli("gh", caller="install-forge-labels")
    # Verify gh is authenticated.
    auth = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, check=False
    )
    if auth.returncode != 0:
        logger.error("gh CLI not authenticated. Run `gh auth login` first.")
        return 1

    existing = _existing_labels(args.repo)
    if not existing and args.repo:
        # Defensive: if `gh label list` returned empty, the repo may be wrong.
        logger.error("Could not list labels in %s (does the repo exist?).", args.repo)
        return 1

    created = 0
    skipped = 0
    failed = 0
    for label in CANONICAL_LABELS:
        if label["name"] in existing:
            skipped += 1
            continue
        if _create_label(label, args.repo):
            logger.info("Created: %s", label["name"])
            created += 1
        else:
            failed += 1

    logger.info(
        "\nSummary: %d created, %d already existed, %d failed",
        created,
        skipped,
        failed,
    )
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
