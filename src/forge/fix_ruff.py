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

   Under :func:`forge.run_context.is_ci` both run in reporting mode
   instead (``ruff format --check``, ``ruff check --no-fix``). Repairing
   is a gate only where the repair can be kept: a commit carries it
   locally, while CI commits nothing, so a repair there is computed,
   discarded, and the step still exits 0 — leaving every fixable finding,
   formatting included, unable to fail the run.
4. ``git add`` back only the files that were staged before ruff ran, so
   ruff's fixes land in the commit being made and nothing the author left
   unstaged is swept in. A partly-staged file (staged, then edited again)
   that ruff rewrites cannot be re-staged without its unstaged edits, so
   the run fails naming it and re-stages nothing.
5. Write the combined output to ``code_health/ruff.log``.

Before any ruff invocation, the binary on PATH is version-checked against
the repo's own ruff pin in ``pyproject.toml`` (FOUNDATION §5 one-minor pin):
rule behavior and unsafe autofixes differ across ruff versions — a foreign
ruff has renamed ``ast.NodeVisitor.visit_*`` dispatch methods and broken
verifiers at runtime while reporting success — so a mismatch fails loudly
with exit 2 and writes the refusal to ``code_health/ruff.log``, in CI
reporting mode included, and no file is ever rewritten by it.

Both ruff invocations are idempotent — when nothing needs fixing they
are near-instant no-ops. Exit code is the exit code of ``ruff check``:
0 when every violation cleared, non-zero when residue remains (rules
without an autofix locally). In reporting mode any finding is residue,
and a formatting difference fails on its own too (``ruff format --check``
exit code).

Can be invoked directly (e.g. by the ``forge-precommit-fixer`` agent to
refresh just the ruff log) or by ``forge-precommit`` as part of the full
sequence — output is the same in both cases.
"""

import argparse
import logging
import re
import shutil
import subprocess
import sys
import tomllib
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
from forge.run_context import is_ci


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


def _repo_ruff_pin(repo_root: Path) -> str | None:
    """Return the repo's own ruff requirement string from ``pyproject.toml``.

    FOUNDATION §5 pins ruff per repo (one minor, ``>=X.Y,<X.Y+1``); that
    pin is the contract a ruff binary on PATH must satisfy. Scans
    ``project.dependencies``, ``project.optional-dependencies`` and
    ``dependency-groups`` for the first entry named ``ruff``.

    Args:
        repo_root: Repo root holding ``pyproject.toml``.

    Returns:
        The requirement string (e.g. ``"ruff>=0.16,<0.17"``), or ``None``
        when the repo pins no ruff (nothing to check against).
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    candidates = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        candidates.extend(group)
    for group in data.get("dependency-groups", {}).values():
        candidates.extend(group)
    for req in candidates:
        name = re.split(r"[><=!~;\[ ]", req.strip(), maxsplit=1)[0].strip().lower()
        if name == "ruff":
            return req.strip()
    return None


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable tuple of ints.

    Args:
        version: Dotted version (digits and dots only, as captured by the
            caller's regex).

    Returns:
        A tuple of integers, one per dot-separated part.
    """
    return tuple(int(part) for part in version.split("."))


_CLAUSE_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _pin_bounds_allow(pin: str, version: str) -> bool:
    """Check *version* against *pin* without the ``packaging`` dependency.

    Covers the FOUNDATION §5 pin forms: any comma-separated clauses of
    ``>=``, ``<=``, ``>``, ``<``, ``==``, ``!=`` against dotted versions,
    compared as zero-padded tuples. Fails closed on an unrecognized
    clause — an unknown constraint must never pass silently.

    Args:
        pin: Full requirement string including the project name
            (e.g. ``"ruff>=0.16,<0.17"``).
        version: Dotted version of the ruff binary (e.g. ``"0.16.0"``).

    Returns:
        True when *version* satisfies every clause of *pin*.
    """
    spec = re.sub(r"^[A-Za-z0-9_.-]+", "", pin)
    parsed = _version_tuple(version)
    for raw_clause in spec.split(","):
        clause = raw_clause.strip()
        if not clause:
            continue
        match = re.match(r"(>=|<=|==|!=|>|<)\s*([0-9]+(?:\.[0-9]+)*)", clause)
        if not match:
            return False
        bound = _version_tuple(match.group(2))
        width = max(len(parsed), len(bound))
        left = parsed + (0,) * (width - len(parsed))
        right = bound + (0,) * (width - len(bound))
        if not _CLAUSE_OPS[match.group(1)](left, right):
            return False
    return True


def _specifier_allows(pin: str, version: str) -> bool:
    """Evaluate requirement *pin* against *version*.

    Uses ``packaging`` when importable (pulled in transitively by
    pip-audit; deliberately not a declared dependency), otherwise the
    stdlib-only :func:`_pin_bounds_allow` fallback.

    Args:
        pin: Full requirement string including the project name.
        version: Dotted version of the ruff binary.

    Returns:
        True when *version* satisfies *pin*.
    """
    try:
        from packaging.requirements import Requirement  # noqa: PLC0415
        from packaging.version import Version  # noqa: PLC0415
    except ImportError:
        return _pin_bounds_allow(pin, version)
    return Version(version) in Requirement(pin).specifier


def _ruff_version_mismatch(repo_root: Path) -> str | None:
    """Return a loud error string when the ruff on PATH fails the repo pin.

    The repo's ruff pin in ``pyproject.toml`` is the contract: rule
    behavior differs across ruff versions, and a foreign ruff's unsafe
    autofixes have renamed ``ast.NodeVisitor.visit_*`` methods, breaking
    verifiers at runtime while the step reported success. An unparseable
    ``ruff --version`` with a pin present fails closed.

    Args:
        repo_root: Repo root holding ``pyproject.toml``.

    Returns:
        None when the ruff on PATH satisfies the pin or the repo pins no
        ruff; otherwise the refusal message naming the binary, its
        version, the pin, and the remedy.
    """
    pin = _repo_ruff_pin(repo_root)
    if pin is None:
        return None
    proc = subprocess.run(
        ["ruff", "--version"], capture_output=True, text=True, check=False
    )
    match = re.search(r"(\d+(?:\.\d+)+)", proc.stdout + proc.stderr)
    version = match.group(1) if match else None
    if version is not None and _specifier_allows(pin, version):
        return None
    ruff_path = shutil.which("ruff") or "(resolved via PATH)"
    return (
        f"fix-forge-ruff: ruff on PATH is {version or 'an unparseable version'} "
        f"({ruff_path}), but pyproject.toml pins {pin!r}.\n"
        "Rule behavior differs across ruff versions — a foreign ruff's unsafe "
        "autofixes have renamed ast.NodeVisitor visit_* methods and broken "
        "verifiers at runtime while reporting success. Refusing to run it: put "
        "the project env first on PATH (the env where forge-scripts is "
        "installed carries the pinned ruff)."
    )


def _require_pinned_ruff(repo_root: Path) -> None:
    """Abort unless the ruff on PATH satisfies the repo's pyproject pin.

    Same fail-loudly contract as :func:`forge.git_utils.require_cli`: a
    foreign ruff must never rewrite files, so a mismatch writes the
    refusal to ``code_health/ruff.log`` and exits 2 before any ruff
    invocation (CI reporting mode included).

    Args:
        repo_root: Repo root holding ``pyproject.toml``.

    Raises:
        SystemExit: Exit code 2 when the ruff on PATH does not satisfy
            the pin (or its version cannot be parsed while a pin exists).
    """
    mismatch = _ruff_version_mismatch(repo_root)
    if mismatch is None:
        return
    write_step_log(repo_root, "ruff", mismatch)
    sys.stderr.write(mismatch + "\n")
    logger.error("%s", mismatch)
    raise SystemExit(2)


def main() -> int:
    """Apply ruff fixes and write ``code_health/ruff.log``.

    Returns:
        Exit code from ``ruff check`` (0 if every violation was cleared,
        non-zero if residue remains); in CI reporting mode also non-zero
        when ``ruff format --check`` finds a difference. ``0`` when no source dirs
        are present or when ``--scope diff`` finds no modified files (the
        step is skipped in both cases).

    Raises:
        SystemExit: Exit code 2 from :func:`_require_pinned_ruff` when the
            ruff on PATH does not satisfy the repo's pyproject pin — no
            ruff invocation ran.
    """
    parser = argparse.ArgumentParser(
        prog="fix-forge-ruff",
        description=(
            "Run `ruff format` + `ruff check --fix --unsafe-fixes` "
            "in-place, re-stage the files that were already staged (failing "
            "if ruff rewrote a partly-staged file), and write "
            "code_health/ruff.log. In CI, report only: `ruff format --check` "
            "+ `ruff check --no-fix`, nothing rewritten or re-staged."
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

    # Version guard: a foreign ruff on PATH must never rewrite files — its
    # rule behavior and unsafe autofixes differ across versions. Runs in
    # CI reporting mode too: a wrong ruff there silently passes findings.
    _require_pinned_ruff(repo_root)

    # Repairing is only a gate where the repair can be kept. A commit
    # carries it locally, so fixing in place is the dev loop. CI commits
    # nothing: a repair there is computed, discarded, and the run still
    # exits 0, so every fixable finding passes unseen. Report instead.
    reporting_only = is_ci()
    snapshot = None if reporting_only else _staging_snapshot(repo_root, source_dirs)
    format_cmd = [
        "ruff",
        "format",
        *(["--check"] if reporting_only else []),
        "--",
        *source_dirs,
    ]
    check_cmd = [
        "ruff",
        "check",
        *(["--no-fix"] if reporting_only else ["--fix", "--unsafe-fixes"]),
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
    restaged: list[str] = []
    refusal: str | None = None
    if not reporting_only:
        restaged, refusal = _restage_or_refuse(repo_root, snapshot)

    sections = [
        "$ " + " ".join(format_cmd),
        (fmt_proc.stdout + fmt_proc.stderr).strip() or "(no output)",
        "",
        "$ " + " ".join(check_cmd),
        (chk_proc.stdout + chk_proc.stderr).strip() or "(no output)",
    ]
    if reporting_only:
        sections.extend(["", "CI: reporting only — findings are not repaired."])
    elif snapshot is None:
        sections.extend(["", "git status unreadable; nothing re-staged."])
    if restaged:
        sections.extend(["", "Re-staged: " + ", ".join(restaged)])
    if refusal:
        sections.extend(["", refusal])
    output = "\n".join(sections)
    write_step_log(repo_root, "ruff", output)
    logger.info("%s", output)
    if refusal and chk_proc.returncode == 0:
        return 1
    # Reporting only, so a formatting difference is a finding rather than
    # something already repaired, and it has to fail on its own.
    return chk_proc.returncode or (fmt_proc.returncode if reporting_only else 0)


if __name__ == "__main__":
    sys.exit(main())
