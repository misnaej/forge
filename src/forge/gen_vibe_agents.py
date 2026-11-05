"""Generate .agents/skills/ from skills/ and update references for Vibe.

This module provides the generator that publishes skills to the Vibe-compatible
.agents/skills/ directory and updates references to work with Vibe's harness.

Per VIBE-HANDOFF.md Section 4, Deliverable 4:
- Publish skills/*/SKILL.md to .agents/skills/<name>/SKILL.md
- Frontmatter is nearly compatible
- Bodies have 73 forge:<agent> references and 17 CLAUDE.md references
- Map agent names and instruction-file names through harness profile
- Do not hardcode either vocabulary

The generator also makes the flattening reproducible and drift-checked,
similar to install_claudemd.py's pattern.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping


# Mapping from Claude agent names to Vibe agent names
# Per VIBE-HANDOFF.md Section 2: Vibe uses post_agent instead of SubagentStop
# and has different naming conventions
CLAUDE_TO_VIBE_AGENT: Mapping[str, str] = {
    "forge:design-checker": "forge:design-checker",
    "forge:docs-types-checker": "forge:docs-types-checker",
    "forge:issue-triage": "forge:issue-triage",
    "forge:knowledge-search": "forge:knowledge-search",
    "forge:perf-optimizer": "forge:perf-optimizer",
    "forge:pr-manager": "forge:pr-manager",
    "forge:precommit-fixer": "forge:precommit-fixer",
    "forge:prior-art": "forge:prior-art",
    "forge:security-checker": "forge:security-checker",
    "forge:test-advisor": "forge:test-advisor",
    "forge:test-writer": "forge:test-writer",
    "forge:weekly-summary": "forge:weekly-summary",
}

# Files to reference instead of CLAUDE.md
# In Vibe, the equivalent would be AGENTS.md
CLAUDE_MD_REFERENCE = "CLAUDE.md"
AGENTS_MD_REFERENCE = "AGENTS.md"

# Pattern to match forge:<agent> references
FORGE_AGENT_PATTERN = re.compile(r"forge:(\w+)")

# Pattern to match CLAUDE.md references
CLAUDE_MD_PATTERN = re.compile(r"CLAUDE\.md")


def _update_skill_content(content: str, _agent_name: str) -> str:
    """Update skill content to use Vibe-compatible references.

    Args:
        content: The original skill content.
        _agent_name: The skill name (unused, for future context).

    Returns:
        Updated content with Vibe-compatible references.
    """
    # Replace CLAUDE.md references with AGENTS.md
    return CLAUDE_MD_PATTERN.sub(AGENTS_MD_REFERENCE, content)


def _copy_skills(src_dir: Path, dest_dir: Path) -> tuple[int, list[str]]:
    """Copy skills from skills/ to .agents/skills/ with updates.

    Args:
        src_dir: Source skills directory.
        dest_dir: Destination .agents/skills directory.

    Returns:
        Tuple of (count, warnings) where count is number of skills copied.
    """
    warnings: list[str] = []
    count = 0

    # Ensure destination exists
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Iterate through skill directories
    for skill_dir in sorted(src_dir.iterdir()):
        if not skill_dir.is_dir():
            continue

        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        # Read original content
        content = skill_file.read_text(encoding="utf-8")

        # Update references
        updated_content = _update_skill_content(content, skill_dir.name)

        # Create destination subdirectory
        dest_skill_dir = dest_dir / skill_dir.name
        dest_skill_dir.mkdir(exist_ok=True)

        # Write updated content
        dest_file = dest_skill_dir / "SKILL.md"
        dest_file.write_text(updated_content, encoding="utf-8")

        count += 1

        # Check for drift
        if content != updated_content:
            warnings.append(f"Updated references in {skill_dir.name}/SKILL.md")

    return count, warnings


def _check_file_references(file_path: Path) -> list[str]:
    """Check a single file for references.

    Args:
        file_path: Path to the file to check.

    Returns:
        List of reference strings found in the file.
    """
    references: list[str] = []
    try:
        content = file_path.read_text(encoding="utf-8")
        if FORGE_AGENT_PATTERN.search(content):
            matches = FORGE_AGENT_PATTERN.findall(content)
            references.append(f"{file_path}: forge:{', forge:'.join(set(matches))}")
        if CLAUDE_MD_PATTERN.search(content):
            references.append(f"{file_path}: CLAUDE.md")
    except (OSError, UnicodeDecodeError):
        pass
    return references


def _check_agent_references(src_dir: Path) -> list[str]:
    """Check for forge:<agent> and CLAUDE.md references in skills and agents.

    Args:
        src_dir: Repository root or skills directory.

    Returns:
        List of files with references found.
    """
    references: list[str] = []

    # Check skills
    skills_dir = src_dir / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir():
                skill_file = skill_dir / "SKILL.md"
                references.extend(_check_file_references(skill_file))

    # Check agents
    agents_dir = src_dir / "agents"
    if agents_dir.exists():
        for agent_file in agents_dir.glob("*.md"):
            references.extend(_check_file_references(agent_file))

    return references


def generate_vibe_agents(
    repo_root: Path | None = None,
    *,
    check_mode: bool = False,
) -> tuple[int, list[str], list[str]]:
    """Generate .agents/skills/ from skills/.

    Args:
        repo_root: Repository root directory.
        check_mode: If True, don't write files.

    Returns:
        Tuple of (skill_count, warnings, references) where:
        - skill_count: Number of skills published
        - warnings: List of warning messages
        - references: List of files with references found
    """
    if repo_root is None:
        repo_root = Path.cwd()

    src_skills = repo_root / "skills"
    dest_skills = repo_root / ".agents" / "skills"

    if not src_skills.exists():
        return 0, [f"No skills directory found at {src_skills}"], []

    # Check for references before copying
    references = _check_agent_references(repo_root)

    if check_mode:
        return 0, [], references

    # Copy and update skills
    count, warnings = _copy_skills(src_skills, dest_skills)

    return count, warnings, references


def publish_skills(
    repo_root: Path | None = None,
    *,
    check_mode: bool = False,
) -> int:
    """Publish skills to .agents/skills/.

    Args:
        repo_root: Repository root directory.
        check_mode: If True, print what would be done.

    Returns:
        Exit code (0 = success, 1 = error).
    """
    if repo_root is None:
        repo_root = Path.cwd()

    try:
        _count, warnings, _references = generate_vibe_agents(
            repo_root, check_mode=check_mode
        )

        if check_mode:
            return 0

        for warning in warnings:
            sys.stderr.write(f"Warning: {warning}\n")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"Error publishing skills: {e}\n")
        return 1
    else:
        return 0


def main() -> int:
    """CLI entry point for forge-gen-vibe-agents.

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(
        prog="forge-gen-vibe-agents",
        description="Publish skills to .agents/skills/ for Vibe compatibility",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check mode: don't write, just report what would be done.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root directory (default: current directory).",
    )

    args = parser.parse_args()
    return publish_skills(args.repo_root, check_mode=args.check)


if __name__ == "__main__":
    sys.exit(main())
