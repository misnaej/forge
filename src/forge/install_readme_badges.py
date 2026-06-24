"""install-forge-readme-badges — write a status-badge block into the README.

Opt-in via ``[tool.forge.badges] enabled = true``. Injects a **drift-aware
managed block** delimited by ``<!-- forge:badges:start -->`` /
``<!-- forge:badges:end -->`` so a consumer's own README prose outside the
block survives every re-run (the same managed-marker idea as
``FOUNDATION.md`` / ``.githooks/*``).

Badge sources, in preference order:

- **shields.io / hosted** where an official source exists — CI (GitHub
  Actions workflow badge), Python version (from ``requires-python``), Ruff,
  License (from ``[project].license``), the ``forge-scripts`` channel, and a
  Claude Code badge.
- **Local SVG** when there is no hosted equivalent: the docstring-coverage
  badge ``.badges/DocstringCoverage.svg`` is *referenced* when present —
  forge already generates it (``verify-forge-docstring-coverage`` with
  ``[tool.forge.docstring_coverage] badge = true``), so this stays DRY
  rather than re-generating anything.

A badge whose inputs are missing (no git remote, no workflow, no license) is
simply omitted. ``--check`` verifies the block is current without writing.

Usage:

- ``install-forge-readme-badges`` — write / refresh the block
- ``install-forge-readme-badges --check`` — verify only (CI / drift)
"""

from __future__ import annotations

import argparse
import logging
import re
import urllib.parse
from typing import TYPE_CHECKING

from forge.config import read_pyproject_raw
from forge.git_utils import configure_cli_logging, run_git
from forge.git_utils import repo_root as get_repo_root
from forge.upgrade import find_pin


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


_START = "<!-- forge:badges:start -->"
_END = "<!-- forge:badges:end -->"
_SHIELDS = "https://img.shields.io"


def _shields_static(label: str, message: str, color: str) -> str:
    """Build a static shields.io badge image URL.

    Applies shields.io's literal-character escaping within each segment
    (``-`` → ``--``, ``_`` → ``__``, space → ``_``) so a dash in a value like
    ``Apache-2.0`` renders literally instead of splitting the badge, then
    percent-escapes any remaining URL-unsafe characters.

    Args:
        label: Left (grey) text.
        message: Right (colored) text.
        color: shields.io color name or hex.

    Returns:
        The badge image URL.
    """

    def _esc(part: str) -> str:
        shielded = part.replace("-", "--").replace("_", "__").replace(" ", "_")
        return urllib.parse.quote(shielded, safe="")

    seg = "-".join(_esc(p) for p in (label, message, color))
    return f"{_SHIELDS}/badge/{seg}"


def _md(alt: str, image: str, link: str | None = None) -> str:
    """Render one markdown badge (optionally wrapped in a link).

    Args:
        alt: Image alt text.
        image: Badge image URL.
        link: Optional href; when given the badge becomes a link.

    Returns:
        The markdown snippet.
    """
    img = f"![{alt}]({image})"
    return f"[{img}]({link})" if link else img


def _git_remote_slug(root: Path) -> str | None:
    """Return ``owner/repo`` from the ``origin`` remote, or ``None``.

    Args:
        root: Repo root.

    Returns:
        The GitHub ``owner/repo`` slug parsed from the origin URL (SSH or
        HTTPS form), or ``None`` when there is no origin or it is not a
        recognizable GitHub URL.
    """
    url = run_git("remote", "get-url", "origin", cwd=root, check=False)
    if not url:
        return None
    match = re.search(r"github\.com[:/]+(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$", url)
    if match is None:
        return None
    slug = match.group("slug")
    # Restrict to GitHub's own owner/repo charset before it flows into URL
    # f-strings and markdown — a crafted remote can't inject markdown.
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", slug):
        return None
    return slug


def _ci_badge(root: Path, slug: str | None, workflow: str | None) -> str | None:
    """Build the GitHub Actions CI badge for the chosen workflow, if any.

    Args:
        root: Repo root.
        slug: ``owner/repo`` slug, or ``None``.
        workflow: The ``[tool.forge.badges] workflow`` override (a filename
            under ``.github/workflows``), or ``None`` to use the first
            workflow alphabetically.

    Returns:
        The markdown badge, or ``None`` when there is no slug, no workflow
        directory, or the named override does not exist.
    """
    if slug is None:
        return None
    wf_dir = root / ".github" / "workflows"
    if workflow:
        # Restrict the override to a bare filename — reject any path separator
        # (absolute paths, `..` traversal) so the `is_file` probe can't become
        # a filesystem-existence oracle and the raw value can't reach the URL.
        bare = "/" not in workflow and "\\" not in workflow
        wf = workflow if bare and (wf_dir / workflow).is_file() else None
    else:
        found = sorted(wf_dir.glob("*.y*ml"))
        wf = found[0].name if found else None
    if wf is None:
        return None
    img = f"https://github.com/{slug}/actions/workflows/{wf}/badge.svg"
    return _md("CI", img, f"https://github.com/{slug}/actions/workflows/{wf}")


def _python_badge(data: dict) -> str | None:
    """Build the Python-version badge from ``requires-python``.

    Args:
        data: Parsed ``pyproject.toml``.

    Returns:
        The markdown badge (e.g. ``python 3.11+``), or ``None`` when
        ``requires-python`` is absent or has no parseable floor.
    """
    spec = data.get("project", {}).get("requires-python", "")
    match = re.search(r"(\d+\.\d+)", str(spec))
    if not match:
        return None
    image = _shields_static("python", f"{match.group(1)}+", "blue")
    return _md("Python", image, "https://www.python.org/downloads/")


def _license_badge(data: dict) -> str | None:
    """Build the License badge from ``[project].license``.

    Args:
        data: Parsed ``pyproject.toml``.

    Returns:
        The markdown badge, or ``None`` when no license text/expression is
        declared. Supports both ``license = "MIT"`` and the table form
        ``license = { text = "MIT" }``.
    """
    lic = data.get("project", {}).get("license")
    name = lic.get("text") if isinstance(lic, dict) else lic
    if not isinstance(name, str) or not name:
        return None
    return _md("License", _shields_static("License", name, "green"))


def _ruff_badge() -> str:
    """Return the static Ruff endpoint badge.

    Returns:
        The markdown badge linking to the Ruff project.
    """
    image = (
        f"{_SHIELDS}/endpoint?url="
        "https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"
    )
    return _md("Ruff", image, "https://github.com/astral-sh/ruff")


def _claude_code_badge() -> str:
    """Return the static Claude Code badge.

    Returns:
        The markdown badge (forge ships an optional Claude Code plugin).
    """
    return _md("Claude Code", f"{_SHIELDS}/badge/Claude_Code-555?logo=claude")


def _forge_badge(root: Path) -> str:
    """Build a forge-channel badge from the ``forge-scripts`` pip pin.

    Args:
        root: Repo root.

    Returns:
        The markdown badge naming the pinned channel/ref (e.g. ``forge main``),
        falling back to a plain ``forge`` badge when no pin is found.
    """
    pin = find_pin(root)
    ref = pin.ref if pin is not None else "enabled"
    return _md("forge", _shields_static("forge", ref, "blue"))


def _coverage_badge(root: Path) -> str | None:
    """Reference the local docstring-coverage SVG when forge has generated it.

    Args:
        root: Repo root.

    Returns:
        The markdown badge pointing at ``.badges/DocstringCoverage.svg``, or
        ``None`` when that file does not exist (the consumer hasn't opted into
        ``[tool.forge.docstring_coverage] badge = true``).
    """
    rel = ".badges/DocstringCoverage.svg"
    if not (root / rel).is_file():
        return None
    return _md("Docstring coverage", rel)


def build_badges(root: Path) -> list[str]:
    """Assemble the ordered list of markdown badges for this repo.

    Args:
        root: Repo root.

    Returns:
        The badges whose inputs are present, in display order. Badges with
        missing inputs (no remote, no license, …) are omitted.
    """
    data = read_pyproject_raw(root)
    badges_cfg = data.get("tool", {}).get("forge", {}).get("badges", {})
    workflow = badges_cfg.get("workflow") if isinstance(badges_cfg, dict) else None
    slug = _git_remote_slug(root)
    candidates = [
        _ci_badge(root, slug, str(workflow) if workflow else None),
        _python_badge(data),
        _ruff_badge(),
        _license_badge(data),
        _forge_badge(root),
        _claude_code_badge(),
        _coverage_badge(root),
    ]
    return [b for b in candidates if b]


def render_block(badges: list[str]) -> str:
    """Wrap *badges* in the forge-managed marker block.

    Args:
        badges: Markdown badge snippets.

    Returns:
        The full managed block (start marker, space-joined badges, end marker).
    """
    return f"{_START}\n{' '.join(badges)}\n{_END}"


def inject(readme: str, block: str) -> str:
    """Insert or replace the managed badge block in *readme* (drift-aware).

    When the markers already exist, only the content between them is
    replaced — everything else the consumer wrote is preserved. Otherwise the
    block is inserted just after the first level-1 heading (``# Title``), or at
    the very top when there is no heading.

    Args:
        readme: Current README text.
        block: The rendered managed block.

    Returns:
        The updated README text.
    """
    pattern = re.compile(re.escape(_START) + r".*?" + re.escape(_END), re.DOTALL)
    if pattern.search(readme):
        return pattern.sub(block, readme)
    lines = readme.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            rest = lines[i + 1 :]
            if rest and rest[0] == "":
                rest = rest[1:]  # don't double an existing blank line after the H1
            new = [*lines[: i + 1], "", block, "", *rest]
            return "\n".join(new) + ("\n" if readme.endswith("\n") else "")
    return f"{block}\n\n{readme}"


def _get_readme_path(root: Path) -> tuple[Path | None, int]:
    """Load and validate the README path from config.

    Args:
        root: Repo root.

    Returns:
        A tuple (readme_path, exit_code). ``(Path, 0)`` when the path
        is valid and ready to use; ``(None, 0)`` when badges are not
        enabled (caller should skip silently); ``(None, 1)`` when a
        configuration error prevents proceeding (caller should exit with
        that code).
    """
    cfg = read_pyproject_raw(root).get("tool", {}).get("forge", {}).get("badges", {})
    if not (isinstance(cfg, dict) and cfg.get("enabled") is True):
        logger.info("[tool.forge.badges] enabled is not true — skipped.")
        return None, 0

    rel_readme = str(cfg.get("readme", "README.md"))
    readme = (root / rel_readme).resolve()
    # Refuse a `readme` value that escapes the repo (absolute path or `..`) —
    # an absolute string would replace `root` entirely under `/`.
    if not readme.is_relative_to(root.resolve()):
        logger.error(
            "[tool.forge.badges] readme %r escapes the repo — refusing.", rel_readme
        )
        return None, 1
    if not readme.is_file():
        logger.error("%s not found — nothing to update.", readme.name)
        return None, 1
    return readme, 0


def main() -> int:
    """CLI entry point.

    Returns:
        ``0`` on success (written, already-current, ``--check`` passing, or
        badges not enabled in ``[tool.forge.badges]`` — treated as a skip);
        ``1`` when ``--check`` finds drift, when the configured README path
        escapes the repo, or when the README file does not exist.
    """
    parser = argparse.ArgumentParser(
        prog="install-forge-readme-badges",
        description=(
            "Write a drift-aware status-badge block into the README. "
            "Opt-in via [tool.forge.badges] enabled = true."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the block is current without writing (exit 1 on drift).",
    )
    args = parser.parse_args()

    root = get_repo_root()
    readme, exit_code = _get_readme_path(root)
    if readme is None:
        return exit_code

    current = readme.read_text(encoding="utf-8")
    updated = inject(current, render_block(build_badges(root)))

    if args.check:
        if current == updated:
            logger.info("README badge block is current.")
            return 0
        logger.error("README badge block is stale — run install-forge-readme-badges.")
        return 1

    if current == updated:
        logger.info("README badge block already current — no change.")
        return 0
    readme.write_text(updated, encoding="utf-8")
    logger.info("Updated the forge badge block in %s.", readme.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
