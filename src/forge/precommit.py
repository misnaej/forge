"""forge-precommit — pre-commit dispatcher CLI.

Generic Python pre-commit check dispatcher. Auto-detects source
directories and runs a fixed sequence on every commit: ruff (always
``ruff format`` + ``ruff check --fix --unsafe-fixes`` in-place, with
modified tracked files re-staged via ``git add``), docstring
verification (over the diff vs main), test-name verification (over the
diff vs main), repo-structure verification (``REPO_STRUCTURE.md`` vs
the actual tree), plugin manifest JSON validation, Claude Code
plugin-version drift guard (when applicable), and ``pip-audit``
dependency vulnerability scan (non-blocking — warns but does not refuse
a commit). Shipped to consumers via the ``forge-scripts`` pip package
and invoked by ``.githooks/pre-commit`` after ``install-forge-githooks``.

Pytest is intentionally NOT part of the default sequence — it is too
slow for pre-commit and belongs in CI. Consumers that want it on commit
can call ``pytest`` directly in ``.githooks/pre-commit`` around the
``forge-precommit`` invocation.

Step outputs are written to ``code_health/<step>.log`` per FOUNDATION §13
so downstream tooling can read the latest results without re-running.

Usage:

- ``forge-precommit`` — run the default sequence
- ``forge-precommit --json`` — machine-readable summary on stdout

Consumers add repo-specific bash steps by editing ``.githooks/pre-commit``
directly — lines before the ``forge-precommit`` call run first; lines
after run only if the forge sequence passed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from forge.git_utils import (
    detect_existing_source_dirs,
    emit,
    require_cli,
    write_step_log,
)
from forge.git_utils import repo_root as get_repo_root


if TYPE_CHECKING:
    from pathlib import Path


# ANSI colors (suppressed if stdout isn't a TTY — keep machine-readable).
def _color(code: str) -> str:
    r"""Return *code* if stdout is a TTY, else an empty string.

    Args:
        code: ANSI escape sequence (e.g. ``"\033[0;32m"``).

    Returns:
        Either *code* unchanged (interactive terminal) or ``""`` (piped),
        so colorized output never pollutes machine-readable consumers.
    """
    return code if sys.stdout.isatty() else ""


GREEN = _color("\033[0;32m")
RED = _color("\033[0;31m")
YELLOW = _color("\033[0;33m")
NC = _color("\033[0m")


@dataclass
class StepResult:
    """Outcome of a single pre-commit step.

    Attributes:
        name: Step display name (used in stdout and the log filename slug).
        passed: True if the step exited 0.
        output: Captured combined stdout+stderr of the underlying command.
        skipped: True if the step was skipped (e.g. conditional check did
            not apply). A skipped step counts as passed for exit-code
            purposes but is reported distinctly in the summary.
        non_blocking: True if a failure of this step should NOT fail the
            overall pre-commit run. Used for advisory checks (e.g.
            ``pip-audit``) where a non-zero result is informational, not
            grounds to refuse a commit. Reported as ``WARN`` instead of
            ``FAIL`` in the step summary.
    """

    name: str
    passed: bool
    output: str
    skipped: bool = False
    non_blocking: bool = False


def _run(
    cmd: list[str],
    cwd: Path,
) -> tuple[bool, str]:
    """Run *cmd* and capture combined output.

    Args:
        cmd: Argv list (no shell).
        cwd: Working directory.

    Returns:
        Tuple of (passed, combined-output). ``passed`` is True if the
        command exited 0.
    """
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode == 0, output


# ---------------------------------------------------------------------------
# Individual steps
# ---------------------------------------------------------------------------


def step_ruff(repo_root: Path) -> StepResult:
    """Run ``fix-forge-ruff`` — owns the ruff phase end-to-end.

    Thin shell-out so the orchestration logic (auto-detect dirs, ruff
    format, ruff check --fix --unsafe-fixes, git-add re-stage, write
    log) lives in one place — ``forge.fix_ruff``. Same pattern as the
    other step CLIs (verify-forge-docstrings, verify-forge-test-naming,
    etc.).

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring fix-forge-ruff's exit code. Skipped
        when no source dirs exist.

    Raises:
        SystemExit: If ``fix-forge-ruff`` is not on PATH.
    """
    dirs = detect_existing_source_dirs(repo_root)
    if not dirs:
        return StepResult(
            name="ruff",
            passed=True,
            output="(no source dirs detected — skipped)",
            skipped=True,
        )
    require_cli("fix-forge-ruff", caller="forge-precommit")
    passed, output = _run(["fix-forge-ruff", *dirs], cwd=repo_root)
    return StepResult(name="ruff", passed=passed, output=output)


def step_docstrings(repo_root: Path) -> StepResult:
    """Run ``verify-forge-docstrings`` over the current diff vs main.

    The underlying CLI picks files via ``get_modified_files()`` — staged
    + unstaged + branch commits vs main, or HEAD~1 when on main. So this
    step is meaningful both as a pre-commit hook (where modified files
    include what's staged) and in CI (where the PR diff is picked up).

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` for this step. The CLI itself reports "no files
        to check" cleanly when nothing matches — never silently skipped
        from this dispatcher.

    Raises:
        SystemExit: If ``verify-forge-docstrings`` is not on PATH.
    """
    require_cli("verify-forge-docstrings", caller="forge-precommit")
    passed, output = _run(["verify-forge-docstrings"], cwd=repo_root)
    return StepResult(name="docstring_verification", passed=passed, output=output)


def step_docstring_coverage(repo_root: Path) -> StepResult:
    """Run ``verify-forge-docstring-coverage`` — full-codebase % reporter.

    Non-blocking by design. Ruff D100-D107 (enabled via
    ``select = ["ALL"]``) already block missing docstrings on
    modules, classes, public functions, and methods - the genuine
    enforcement layer for any repo using the foundation ruff config.
    This step adds aggregate % reporting, per-file table, badge
    generation, and catches the cases ruff misses (notably nested
    functions and closures). ``forge:precommit-fixer`` reads the log
    and addresses listed missing-docstring entries.

    When ``[tool.forge.docstring_coverage].badge = true`` the CLI
    also writes ``.badges/DocstringCoverage.svg``.

    The CLI self-skips with exit 0 when ``pyproject.toml`` or
    ``src/`` is missing, so consumer repos that haven't opted in
    don't fail the hook.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code.

    Raises:
        SystemExit: If ``verify-forge-docstring-coverage`` is not on PATH.
    """
    require_cli("verify-forge-docstring-coverage", caller="forge-precommit")
    passed, output = _run(["verify-forge-docstring-coverage"], cwd=repo_root)
    skipped = "skipped" in output
    return StepResult(
        name="docstring_coverage",
        passed=passed,
        output=output,
        skipped=skipped,
        non_blocking=True,
    )


def step_test_naming(repo_root: Path) -> StepResult:
    """Run ``verify-forge-test-naming`` over the current diff vs main.

    Like ``step_docstrings``, the underlying CLI selects files via the git
    diff vs main, so only modified test files are checked. The CLI is
    warning-only by design — it surfaces naming issues in the
    ``test_naming_check.log`` but always exits 0, so this step never
    refuses a commit.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` for this step.

    Raises:
        SystemExit: If ``verify-forge-test-naming`` is not on PATH.
    """
    require_cli("verify-forge-test-naming", caller="forge-precommit")
    passed, output = _run(["verify-forge-test-naming"], cwd=repo_root)
    return StepResult(name="test_naming_check", passed=passed, output=output)


def step_repo_structure(repo_root: Path) -> StepResult:
    """Run ``verify-forge-repo-structure``; hard-fail if missing (FOUNDATION §2).

    The underlying CLI parses ``REPO_STRUCTURE.md`` and compares the paths
    it documents against the actual repository tree, reporting drift in
    both directions. It exits 1 on drift, so this step refuses a commit
    when ``REPO_STRUCTURE.md`` is out of sync.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` for this step. Skipped when no ``REPO_STRUCTURE.md``
        exists at the repo root.

    Raises:
        SystemExit: If ``verify-forge-repo-structure`` is not on PATH.
    """
    if not (repo_root / "REPO_STRUCTURE.md").is_file():
        return StepResult(
            name="repo_structure_check",
            passed=True,
            output="(no REPO_STRUCTURE.md — skipped)",
            skipped=True,
        )
    require_cli("verify-forge-repo-structure", caller="forge-precommit")
    passed, output = _run(["verify-forge-repo-structure"], cwd=repo_root)
    return StepResult(name="repo_structure_check", passed=passed, output=output)


def step_manifest_json(repo_root: Path) -> StepResult:
    """Run ``verify-forge-manifest`` — owns the manifest-JSON validation phase.

    Thin shell-out matching the pattern of every other phase step. The
    CLI itself decides whether to skip (no ``.claude-plugin/`` dir) and
    writes the corresponding log entry.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code.

    Raises:
        SystemExit: If ``verify-forge-manifest`` is not on PATH.
    """
    require_cli("verify-forge-manifest", caller="forge-precommit")
    passed, output = _run(["verify-forge-manifest"], cwd=repo_root)
    skipped = "skipped" in output
    return StepResult(
        name="manifest_json", passed=passed, output=output, skipped=skipped
    )


def step_commit_types_parity(repo_root: Path) -> StepResult:
    """Run ``forge-gen-commit-types --check`` — managed-block parity guard.

    Verifies the conventional-commit alternation in
    ``claude-hooks/check_commit_format.sh`` matches the canonical
    ``CONVENTIONAL_COMMIT_TYPES`` tuple in ``forge.pr_squash_comment``.
    Self-skips when the hook file is absent (consumer repos that do
    not ship the forge Claude-plugin layout).

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code, or a skipped result
        when ``claude-hooks/check_commit_format.sh`` is absent.

    Raises:
        SystemExit: If ``forge-gen-commit-types`` is not on PATH.
    """
    if not (repo_root / "claude-hooks" / "check_commit_format.sh").is_file():
        return StepResult(
            name="commit_types_parity",
            passed=True,
            output="(no claude-hooks/check_commit_format.sh — skipped)",
            skipped=True,
        )
    require_cli("forge-gen-commit-types", caller="forge-precommit")
    passed, output = _run(["forge-gen-commit-types", "--check"], cwd=repo_root)
    return StepResult(name="commit_types_parity", passed=passed, output=output)


# Maximum residual ``pip-audit`` advisories allowed before the WARN
# escalates to a loud banner. The check is non-blocking either way;
# above this threshold the output is prefixed with a visible nudge
# asking the contributor to file a tracking issue if there isn't one —
# quietly accumulating residuals are the failure mode this step exists
# to surface.
_PIP_AUDIT_LOUDNESS_THRESHOLD = 10

# Matches a full ``pip-audit`` advisory ID:
# - ``PYSEC-YYYY-N`` (year + sequence number, both digit-only)
# - ``GHSA-xxxx-yyyy-zzzz`` (three 4-char alphanumeric segments)
# The text output prints one such ID per finding line, so counting
# matches equals counting reported findings.
_ADVISORY_ID_RE = re.compile(
    r"\b(?:PYSEC-\d+-\d+|GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})\b",
)


def _count_pip_audit_advisories(output: str) -> int:
    """Count advisory ID occurrences in a ``pip-audit`` text-mode output.

    Uses :func:`re.findall` over the canonical advisory-ID pattern
    (see :data:`_ADVISORY_ID_RE`) — every non-overlapping occurrence
    is counted. ``pip-audit`` prints exactly one advisory ID per
    finding line in its default text output, so the resulting count
    equals the number of reported findings.

    Args:
        output: Raw stdout/stderr captured from the ``pip-audit`` run.

    Returns:
        The number of advisory ID occurrences found.
    """
    return len(_ADVISORY_ID_RE.findall(output))


def step_pip_audit(repo_root: Path) -> StepResult:
    """Run ``pip-audit --skip-editable`` and report findings as non-blocking.

    ``pip-audit`` scans the current Python environment against the OSV
    vulnerability database. Findings are advisory — a CVE in a dev
    dependency should not refuse a commit, but the contributor should
    see it.

    Loudness escalation: when the residual advisory count exceeds
    :data:`_PIP_AUDIT_LOUDNESS_THRESHOLD`, the output is prefixed with
    a visible banner listing the count and nudging the contributor to
    file a tracking issue. The step remains non-blocking; only the
    rendering changes. Below the threshold the original short WARN
    line is preserved.

    Skipped when ``pip-audit`` is not on PATH. Non-blocking: a failing
    audit (CVEs found) sets ``passed=False`` AND ``non_blocking=True``
    so ``run_all`` reports ``WARN`` instead of ``FAIL`` and the overall
    exit code is unaffected.

    Args:
        repo_root: Git repo root (used as working directory).

    Returns:
        ``StepResult`` for this step. ``non_blocking=True`` when the
        step actually ran (skipped results inherit the dataclass default
        of ``False``; a skipped step counts as passed anyway).
    """
    if shutil.which("pip-audit") is None:
        return StepResult(
            name="pip_audit",
            passed=True,
            output="(pip-audit not on PATH — skipped)",
            skipped=True,
        )
    passed, output = _run(
        ["pip-audit", "--skip-editable", "--desc"],
        cwd=repo_root,
    )
    if not passed:
        count = _count_pip_audit_advisories(output)
        if count > _PIP_AUDIT_LOUDNESS_THRESHOLD:
            banner = (
                f"⚠️  pip_audit has {count} advisories "
                f"(> threshold {_PIP_AUDIT_LOUDNESS_THRESHOLD}). "
                "Consider filing a tracking issue if there isn't one — "
                "these accumulate silently.\n\n"
            )
            output = banner + output
    return StepResult(
        name="pip_audit",
        passed=passed,
        output=output,
        non_blocking=True,
    )


def step_cli_wiring(repo_root: Path) -> StepResult:
    """Run ``verify-forge-cli-wiring`` — assert every script has a real caller.

    The verifier greps the wiring source paths
    (``src/forge/install_bootstrap.py``, ``src/forge/precommit.py``,
    ``src/forge/audit/``, ``.githooks/``, ``claude-hooks/``, ``dev/``,
    ``agents/``, ``skills/``) for every ``[project.scripts]`` entry
    and fails on unreachable CLIs. Forge-specific wiring layout, so
    the step requires an explicit opt-in: add ``[tool.forge.cli_wiring]
    enabled = true`` to the consumer's ``pyproject.toml``. Self-skips
    otherwise so consumer repos with different layouts stay green.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code, or a skipped result
        when the consumer has not opted in.

    Raises:
        SystemExit: If ``verify-forge-cli-wiring`` is not on PATH.
    """
    if not _cli_wiring_enabled(repo_root):
        return StepResult(
            name="cli_wiring",
            passed=True,
            output=(
                "(no [tool.forge.cli_wiring] enabled = true in "
                "pyproject.toml — skipped)"
            ),
            skipped=True,
        )
    require_cli("verify-forge-cli-wiring", caller="forge-precommit")
    passed, output = _run(["verify-forge-cli-wiring"], cwd=repo_root)
    return StepResult(name="cli_wiring", passed=passed, output=output)


def _cli_wiring_enabled(repo_root: Path) -> bool:
    """Return True when the repo has opted into the cli_wiring check.

    Opt-in marker: ``[tool.forge.cli_wiring] enabled = true`` in
    ``pyproject.toml``. Forge itself sets this; consumer repos must
    enable explicitly because the default :data:`WIRING_SOURCES`
    glob list is forge-layout-specific.

    Args:
        repo_root: Git repo root.

    Returns:
        ``True`` when the marker is present and truthy; ``False``
        otherwise (including when ``pyproject.toml`` is missing or
        malformed).
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return False
    section = ((data.get("tool") or {}).get("forge") or {}).get("cli_wiring") or {}
    return bool(section.get("enabled"))


def step_plugin_version(repo_root: Path) -> StepResult:
    """Run ``verify-forge-plugin-version`` — owns the rolling-next guard.

    Thin shell-out matching the pattern of every other phase step. The
    CLI itself decides whether to skip (no plugin.json, no tags, release
    commit) and writes the corresponding log entry.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code.

    Raises:
        SystemExit: If ``verify-forge-plugin-version`` is not on PATH.
    """
    require_cli("verify-forge-plugin-version", caller="forge-precommit")
    passed, output = _run(["verify-forge-plugin-version"], cwd=repo_root)
    skipped = "skipped" in output
    return StepResult(
        name="plugin_version", passed=passed, output=output, skipped=skipped
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _write_log(repo_root: Path, result: StepResult) -> None:
    """Persist *result*'s output to ``code_health/<name>.log``.

    Thin wrapper over ``git_utils.write_step_log`` so every step writes
    via the shared helper.

    Args:
        repo_root: Git repo root.
        result: Step outcome to log.
    """
    write_step_log(repo_root, result.name, result.output)


def _print_step_line(result: StepResult) -> None:
    """Print a one-line status for *result* (SKIP/PASS/WARN/FAIL).

    Non-blocking failures render as ``WARN`` in yellow — visibly distinct
    from blocking ``FAIL`` so the contributor can tell at a glance that
    the commit is not blocked.

    Args:
        result: Step outcome.
    """
    if result.skipped:
        marker, color = "SKIP", ""
    elif result.passed:
        marker, color = "PASS", GREEN
    elif result.non_blocking:
        marker, color = "WARN", YELLOW
    else:
        marker, color = "FAIL", RED
    emit(f"{result.name:<24} {color}{marker}{NC}")


def run_all(
    repo_root: Path | None = None,
    *,
    print_progress: bool = True,
) -> list[StepResult]:
    """Run every step in order and return their results.

    ``step_ruff`` shells out to ``fix-forge-ruff`` which applies ruff
    fixes (``ruff format`` + ``ruff check --fix --unsafe-fixes``) and
    re-stages modified tracked files. Other steps verify only.

    Args:
        repo_root: Override the auto-detected git repo root. Useful in tests.
        print_progress: Print one-line PASS/FAIL/SKIP per step. Disable for
            JSON output to keep stdout machine-readable.

    Returns:
        List of ``StepResult``, one per step, in execution order.
    """
    root = repo_root if repo_root is not None else get_repo_root()
    results: list[StepResult] = []
    ruff_result = step_ruff(root)
    if print_progress:
        _print_step_line(ruff_result)
    _write_log(root, ruff_result)
    results.append(ruff_result)
    for step in (
        step_docstrings,
        step_docstring_coverage,
        step_test_naming,
        step_repo_structure,
        step_manifest_json,
        step_cli_wiring,
        step_commit_types_parity,
        step_plugin_version,
        step_pip_audit,
    ):
        result = step(root)
        if print_progress:
            _print_step_line(result)
        _write_log(root, result)
        results.append(result)
    return results


def main() -> int:
    """CLI entry point.

    Returns:
        ``0`` if every non-skipped step passed; ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="forge-precommit",
        description=(
            "Run the forge pre-commit check sequence: ruff (format + check, "
            "self-healing with --unsafe-fixes on failure) + docstring "
            "verification (diff vs main) + test-name verification (diff vs "
            "main) + repo-structure verification (REPO_STRUCTURE.md vs the "
            "tree) + plugin manifest JSON + plugin version drift guard + "
            "pip-audit (non-blocking) — when applicable. Ruff fixes apply "
            "automatically on every run; fixed files are re-staged. Pytest "
            "is not in the default sequence — run it in CI or wire it into "
            ".githooks/pre-commit explicitly. Used by any repo that adopts "
            "forge via install-forge-githooks."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON summary on stdout instead of human output.",
    )
    args = parser.parse_args()

    results = run_all(print_progress=not args.json)

    blocking_failures = [r for r in results if not r.passed and not r.non_blocking]
    non_blocking_warnings = [r for r in results if not r.passed and r.non_blocking]
    if args.json:
        emit(json.dumps([asdict(r) for r in results], indent=2))
    else:
        emit("")
        if blocking_failures:
            emit(f"{RED}Pre-commit checks failed:{NC}")
            for r in blocking_failures:
                emit(f"  - {r.name}: see code_health/{r.name}.log")
            if non_blocking_warnings:
                emit(
                    f"{YELLOW}Plus {len(non_blocking_warnings)} non-blocking "
                    f"warning(s) — see code_health/ logs.{NC}",
                )
        elif non_blocking_warnings:
            emit(
                f"{YELLOW}All blocking checks passed. "
                f"{len(non_blocking_warnings)} non-blocking warning(s):{NC}",
            )
            for r in non_blocking_warnings:
                emit(f"  - {r.name}: see code_health/{r.name}.log")
        else:
            emit(f"{GREEN}All checks passed.{NC}")

    return 1 if blocking_failures else 0


if __name__ == "__main__":
    sys.exit(main())
