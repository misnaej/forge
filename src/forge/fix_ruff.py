"""Apply ruff fixes and write ``code_health/ruff.log``.

Owns the **ruff phase** of the forge pre-commit sequence (FOUNDATION §13,
single responsibility):

1. Resolve scan roots via :func:`forge.config.resolve_tool_roots` —
   granular ``[tool.forge.ruff].paths`` → repo-wide
   ``[tool.forge].source_dirs + test_dirs`` → smart auto-detect (the same
   roots every layout-aware forge tool uses).
2. Run ``ruff format`` in-place.
3. Run ``ruff check --fix --unsafe-fixes``. Unsafe fixes are on by
   default; forge always applies them.
4. ``git add`` back only the files that were staged before ruff ran, so
   ruff's fixes land in the commit being made and nothing the author left
   unstaged is swept in. A partly-staged file (staged, then edited again)
   that ruff rewrites cannot be re-staged without its unstaged edits, so
   the run fails naming it and re-stages nothing.
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

from forge.config import filter_under_roots, resolve_tool_roots, select_diff_files
from forge.git_utils import (
    SCOPE_DIFF,
    VALID_SCOPES,
    configure_cli_logging,
    path_escapes_repo,
    require_cli,
    stage_modified_paths,
    staged_unstaged_paths,
    write_step_log,
)


configure_cli_logging()
logger = logging.getLogger(__name__)

_WHOLE_REPO_ROOTS = frozenset({".", "./", ""})


def _staging_snapshot(
    repo_root: Path, roots: list[str]
) -> tuple[list[str], dict[str, bytes]] | None:
    """Record what is staged under *roots* before ruff rewrites anything.

    Args:
        repo_root: Git repo root.
        roots: Scan roots (dirs, or files in diff scope); ``.`` means all.

    Returns:
        ``(staged, partial)``: staged paths under *roots*, and the current
        bytes of those also carrying unstaged edits. ``None`` when git
        status cannot be read.
    """
    split = staged_unstaged_paths(repo_root)
    if split is None:
        return None
    staged, unstaged = split
    if not _WHOLE_REPO_ROOTS.intersection(roots):
        staged = filter_under_roots(staged, roots)
    unstaged_set = set(unstaged)
    partial = {
        p: (repo_root / p).read_bytes()
        for p in staged
        if p in unstaged_set and (repo_root / p).is_file()
    }
    return staged, partial


def _restage_or_refuse(
    repo_root: Path, snapshot: tuple[list[str], dict[str, bytes]] | None
) -> tuple[list[str], str | None]:
    """Fold ruff's fixes into the files that were staged, or refuse.

    A partly-staged file is never ``git add``-ed: adding it would carry its
    unstaged edits into the commit. If ruff rewrote one, its fix cannot
    land without them, so the run refuses instead.

    Args:
        repo_root: Git repo root.
        snapshot: :func:`_staging_snapshot` result, taken before ruff ran.

    Returns:
        ``(restaged, refusal)``: paths re-staged, and a refusal message
        (``None`` when the run may pass).
    """
    if snapshot is None:
        return [], None
    staged, partial = snapshot
    touched = sorted(
        p
        for p, before in partial.items()
        if not (repo_root / p).is_file() or (repo_root / p).read_bytes() != before
    )
    if touched:
        return [], (
            "ruff rewrote partly-staged file(s): "
            + ", ".join(touched)
            + ". Their unstaged edits would ride into the commit, so nothing "
            "was re-staged. Stage the rest or undo the extra edits, then "
            "commit again."
        )
    return stage_modified_paths(
        repo_root, [p for p in staged if p not in partial]
    ), None


def _validate_paths(repo_root: Path, paths: list[str]) -> list[str]:
    """Ensure every entry in *paths* resolves inside *repo_root*.

    Guards against directory-traversal arguments (e.g. ``../etc``) being
    passed through into the ruff subprocess. Accepts both directories
    (``scope=all`` / positional roots) and individual file paths
    (``scope=diff`` modified-file set) — the containment check is
    path-type agnostic.

    Args:
        repo_root: Git repo root the CLI was invoked from.
        paths: Candidate source dirs or files from argv, auto-detection,
            or the modified-file set.

    Returns:
        The validated list (unchanged on success).

    Raises:
        SystemExit: If any entry resolves outside *repo_root*. Exit 2
            (config error).
    """
    for raw in paths:
        if path_escapes_repo(repo_root, raw):
            sys.stderr.write(
                f"fix-forge-ruff: refusing path outside repo root: {raw}\n"
            )
            raise SystemExit(2)
    return paths


def main() -> int:
    """Apply ruff fixes and write ``code_health/ruff.log``.

    Returns:
        Exit code from ``ruff check --fix`` (0 if every violation was
        cleared, non-zero if residue remains). ``0`` when no source dirs
        are present or when ``--scope diff`` finds no modified files (the
        step is skipped in both cases).
    """
    parser = argparse.ArgumentParser(
        prog="fix-forge-ruff",
        description=(
            "Run `ruff format` + `ruff check --fix --unsafe-fixes` "
            "in-place, re-stage the files that were already staged (failing "
            "if ruff rewrote a partly-staged file), and write "
            "code_health/ruff.log."
        ),
    )
    parser.add_argument(
        "dirs",
        nargs="*",
        help="Source dirs to fix. If empty, resolve from "
        "[tool.forge].source_dirs / smart auto-detect.",
    )
    parser.add_argument(
        "--scope",
        choices=VALID_SCOPES,
        default="all",
        help="'all' (whole source tree, the default) or 'diff' (only files "
        "modified vs main). 'diff' ignores positional dirs.",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    if args.scope == SCOPE_DIFF:
        # select_diff_files already guarantees every path resolves inside the
        # repo, so no _validate_paths pass is needed here (unlike the argv
        # branch below, whose positional dirs are untrusted input).
        source_dirs = select_diff_files(repo_root)
        if not source_dirs:
            write_step_log(repo_root, "ruff", "(no modified files — skipped)")
            logger.info("No modified files vs main at %s", repo_root)
            return 0
    else:
        roots = args.dirs or resolve_tool_roots(repo_root, "ruff", include_tests=True)
        source_dirs = _validate_paths(repo_root, roots)
        if not source_dirs:
            write_step_log(repo_root, "ruff", "(no source dirs detected — skipped)")
            logger.info("No source directories found at %s", repo_root)
            return 0

    require_cli("ruff", caller="fix-forge-ruff")

    snapshot = _staging_snapshot(repo_root, source_dirs)
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
    restaged, refusal = _restage_or_refuse(repo_root, snapshot)

    sections = [
        "$ " + " ".join(format_cmd),
        (fmt_proc.stdout + fmt_proc.stderr).strip() or "(no output)",
        "",
        "$ " + " ".join(check_cmd),
        (chk_proc.stdout + chk_proc.stderr).strip() or "(no output)",
    ]
    if snapshot is None:
        sections.extend(["", "git status unreadable; nothing re-staged."])
    if restaged:
        sections.extend(["", "Re-staged: " + ", ".join(restaged)])
    if refusal:
        sections.extend(["", refusal])
    output = "\n".join(sections)
    write_step_log(repo_root, "ruff", output)
    logger.info("%s", output)
    return 1 if refusal and chk_proc.returncode == 0 else chk_proc.returncode


if __name__ == "__main__":
    sys.exit(main())
