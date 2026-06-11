"""Apply ruff fixes and write ``code_health/ruff.log``.

Owns the **ruff phase** of the forge pre-commit sequence (FOUNDATION §13,
single responsibility):

1. Auto-detect source dirs (``src``, ``tests``, ``forge`` — same set the
   verify-forge-* CLIs use).
2. Run ``ruff format`` in-place.
3. Run ``ruff check --fix --unsafe-fixes``. Unsafe fixes are on by
   default; forge always applies them.
4. ``git add`` every modified tracked file so the staged tree matches
   the fixed tree.
5. Write the combined output to ``code_health/ruff.log``.

Both ruff invocations are idempotent — when nothing needs fixing they
are near-instant no-ops. Exit code is the exit code of ``ruff check
--fix``: 0 when every violation cleared, non-zero when residue remains
(rules without an autofix).

Can be invoked directly (e.g. by the ``forge:precommit-fixer`` agent to
refresh just the ruff log) or by ``forge-precommit`` as part of the full
sequence — output is the same in both cases.
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from forge.git_utils import (
    configure_cli_logging,
    detect_existing_source_dirs,
    require_cli,
    write_step_log,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


def _restage_modified(repo_root: Path, source_dirs: list[str]) -> list[str]:
    """``git add`` tracked files modified inside *source_dirs*.

    Scoped on purpose: only files under the ruff-managed source dirs are
    re-staged. Unrelated in-progress edits the developer left unstaged
    elsewhere in the working tree are not silently folded into the
    commit. Pathspec-scoped ``git diff`` keeps the contract explicit.

    Args:
        repo_root: Git repo root.
        source_dirs: Pathspecs (relative to *repo_root*) limiting which
            modifications are eligible for re-staging.

    Returns:
        Newly-staged file paths (relative to repo root). Empty when no
        in-scope files changed or not in a git repo.
    """
    if not (repo_root / ".git").exists():
        return []
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--", *source_dirs],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    files = [line for line in proc.stdout.splitlines() if line.strip()]
    if not files:
        return []
    subprocess.run(
        ["git", "add", "--", *files],
        cwd=repo_root,
        check=False,
    )
    return files


def _validate_dirs(repo_root: Path, dirs: list[str]) -> list[str]:
    """Ensure every entry in *dirs* resolves inside *repo_root*.

    Guards against directory-traversal arguments (e.g. ``../etc``) being
    passed through into the ruff subprocess.

    Args:
        repo_root: Git repo root the CLI was invoked from.
        dirs: Candidate source directories from argv or auto-detection.

    Returns:
        The validated list (unchanged on success).

    Raises:
        SystemExit: If any entry resolves outside *repo_root*. Exit 2
            (config error).
    """
    repo_real = repo_root.resolve()
    for raw in dirs:
        candidate = (repo_root / raw).resolve()
        if repo_real != candidate and repo_real not in candidate.parents:
            sys.stderr.write(
                f"fix-forge-ruff: refusing path outside repo root: {raw}\n"
            )
            raise SystemExit(2)
    return dirs


def main() -> int:
    """Apply ruff fixes and write ``code_health/ruff.log``.

    Returns:
        Exit code from ``ruff check --fix`` (0 if every violation was
        cleared, non-zero if residue remains). ``0`` when no source dirs
        are present (the step is skipped).
    """
    parser = argparse.ArgumentParser(
        prog="fix-forge-ruff",
        description=(
            "Run `ruff format` + `ruff check --fix --unsafe-fixes` "
            "in-place, re-stage modified tracked files, and write "
            "code_health/ruff.log."
        ),
    )
    parser.add_argument(
        "dirs",
        nargs="*",
        help="Source dirs to fix. If empty, auto-detect from candidate list.",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    source_dirs = _validate_dirs(
        repo_root, args.dirs or detect_existing_source_dirs(repo_root)
    )

    if not source_dirs:
        write_step_log(repo_root, "ruff", "(no source dirs detected — skipped)")
        logger.info("No source directories found at %s", repo_root)
        return 0

    require_cli("ruff", caller="fix-forge-ruff")

    format_cmd = ["ruff", "format", "--", *source_dirs]
    check_cmd = [
        "ruff",
        "check",
        "--fix",
        "--unsafe-fixes",
        "--no-cache",
        "--",
        *source_dirs,
    ]
    fmt_proc = subprocess.run(
        format_cmd, cwd=repo_root, capture_output=True, text=True, check=False
    )
    chk_proc = subprocess.run(
        check_cmd, cwd=repo_root, capture_output=True, text=True, check=False
    )
    restaged = _restage_modified(repo_root, source_dirs)

    sections = [
        "$ " + " ".join(format_cmd),
        (fmt_proc.stdout + fmt_proc.stderr).strip() or "(no output)",
        "",
        "$ " + " ".join(check_cmd),
        (chk_proc.stdout + chk_proc.stderr).strip() or "(no output)",
    ]
    if restaged:
        sections.extend(["", "Re-staged: " + ", ".join(restaged)])
    output = "\n".join(sections)
    write_step_log(repo_root, "ruff", output)
    logger.info("%s", output)
    return chk_proc.returncode


if __name__ == "__main__":
    sys.exit(main())
