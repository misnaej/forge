"""Generate .agents/skills/ from skills/ and update references for Vibe.

This module provides the generator that publishes skills to the Vibe-compatible
.agents/skills/ directory and updates references to work with Vibe's harness.

Publishes skills for the Vibe harness:
- Publish skills/*/SKILL.md to .agents/skills/<name>/SKILL.md
- Frontmatter is nearly compatible
- Bodies carry forge:<agent> and CLAUDE.md references
- Map agent names and instruction-file names through harness profile
- Do not hardcode either vocabulary

The generator also makes the flattening reproducible and drift-checked,
similar to install_claudemd.py's pattern.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from forge.agent_harness import (
    PLUGIN_REFERENCE_RE,
    HarnessName,
    map_plugin_references,
)
from forge.instruction_files import flatten_agents_md, shipped_path


logger = logging.getLogger(__name__)


# Files to reference instead of CLAUDE.md
# In Vibe, the equivalent would be AGENTS.md
CLAUDE_MD_REFERENCE = "CLAUDE.md"
AGENTS_MD_REFERENCE = "AGENTS.md"

# Pattern to match CLAUDE.md references
CLAUDE_MD_PATTERN = re.compile(r"CLAUDE\.md")


def flatten_vibe_instructions(repo_root: Path) -> str | None:
    """Compose the flattened instruction file as this harness needs it.

    The flattened sources name forge's agents — FOUNDATION's delegation
    table is read as a rule, not as one call site — so the canonical
    namespace is mapped here. The single composer exists because the
    writer and both drift checks (``install-forge-vibe``, ``forge-doctor``)
    must agree byte for byte; mapping at the write alone would report the
    file permanently stale.

    Args:
        repo_root: Repository root containing the flatten sources.

    Returns:
        The content to write, or None when a source file is missing.
    """
    content = flatten_agents_md(repo_root)
    if content is None:
        return None
    return map_plugin_references(content, harness=HarnessName.VIBE.value)


def _update_skill_content(content: str) -> str:
    """Update skill content to use Vibe-compatible references.

    Both vocabularies are mapped through the harness profile rather than
    hardcoded here. The agent half matters most: a skill body carries the
    spawn instruction itself, and the canonical namespace does not resolve
    on Vibe, so an unmapped body instructs every session to call an agent
    type that cannot exist.

    Args:
        content: The original skill content.

    Returns:
        Updated content with Vibe-compatible references.
    """
    mapped = map_plugin_references(content, harness=HarnessName.VIBE.value)
    return CLAUDE_MD_PATTERN.sub(AGENTS_MD_REFERENCE, mapped)


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
        updated_content = _update_skill_content(content)

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
        if PLUGIN_REFERENCE_RE.search(content):
            matches = PLUGIN_REFERENCE_RE.findall(content)
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

    # The checkout's own skills/ win (forge's repo); consumer repos use
    # the copy shipped inside the installed package.
    src_skills = shipped_path(repo_root, "skills")
    dest_skills = repo_root / ".agents" / "skills"

    if not src_skills.exists():
        return 0, [f"No skills directory found at {src_skills}"], []

    # Check for references before copying; the source root holds both
    # skills/ and agents/ (local checkout or shipped package data).
    references = _check_agent_references(src_skills.parent)

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
    except Exception:
        logger.exception("Error publishing skills")
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
