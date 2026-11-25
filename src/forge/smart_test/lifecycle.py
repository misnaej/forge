"""Test-lifecycle mechanics for forge-smart-test (FOUNDATION §8).

Graph-independent support module (layering-exempt, like ``git_helpers``):
owns the lifecycle artifacts the depth model consumes —

- **classification**: a test file whose module level carries
  ``pytestmark = pytest.mark.development`` (alone or in a list) is a
  *development* file; unmarked files are *behavior* tests, the permanent
  default class.
- **lifecycle deselection**: development files untouched for
  ``lifecycle_skip_days`` (default 30) leave ordinary full runs — always
  reported, never silent — and re-enter automatically the moment their
  file changes. Deletion is deliberately not implemented.

Deselection is safe only because a periodic truly-all run executes what a
skip left out, and that run is CI's: ``forge-smart-test`` never deselects
under :func:`forge.run_context.is_ci`, so any CI ``full`` job is truly
all. Nothing here times it — the guarantee is the job, not a timestamp
(``forge-docs/ci-recipe.md``).

A one-line history record per full run lands in
``code_health/smart_test_history.log`` (append-only, telemetry-history
pattern) carrying the record-only health metrics: wall time, file
counts, development fraction, lifecycle skips, and depth-2 differential
mismatches.
"""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from forge.git_utils import code_health_dir, run_git
from forge.ledger import append_ledger_line


if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class RunMetrics:
    """Per-run metrics appended to the smart-test history ledger."""

    label: str
    wall_s: float
    total_files: int
    dev_files: int
    lifecycle_skipped: int
    differential_mismatches: int


HISTORY_NAME = "smart_test_history.log"

DEFAULT_SKIP_DAYS = 30

# Paths whose changes cannot affect test outcomes; everything else
# non-Python escalates the run to full (the safe-fallback guarantee).
DEFAULT_NONPYTHON_IGNORE = (
    "*.md",
    ".plan/*",
    ".gitignore",
)

_FAILED_LINE_RE = re.compile(r"^FAILED ([^\s:]+\.py)", re.MULTILINE)


def skip_days_from(cfg: dict[str, object]) -> float:
    """Return the quiet window from *cfg*, or the default.

    One reader, because two callers must agree: the run path decides what
    to deselect, and ``forge-smart-test --lifecycle`` reports what would
    be deselected. Were the coercion written twice, a change to one would
    make the report quietly describe a different run than the one that
    happens.

    Args:
        cfg: The ``[tool.forge.smart_test]`` table.

    Returns:
        The window in days; the default when the value is not numeric.
    """
    raw = cfg.get("lifecycle_skip_days", DEFAULT_SKIP_DAYS)
    return float(raw) if isinstance(raw, (int, float)) else float(DEFAULT_SKIP_DAYS)


def development_marked_files(repo_root: Path, test_files: set[str]) -> set[str]:
    """Return the subset of *test_files* classified as development tests.

    Args:
        repo_root: Git repo root.
        test_files: Repo-relative test-file paths to classify.

    Returns:
        Files whose module level matches the ``pytestmark`` development
        pattern; unreadable/missing files are treated as behavior tests
        (the safe default — never skip what cannot be classified).
    """
    marked: set[str] = set()
    for rel in test_files:
        path = repo_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A file we cannot decode carries no marker we can read; it
            # is a behavior test by default, which is the safe class.
            continue
        if _has_development_pytestmark(text):
            marked.add(rel)
    return marked


def _has_development_pytestmark(text: str) -> bool:
    """Return whether *text* carries a top-level development pytestmark.

    AST-based on purpose: only a real module-level ``pytestmark``
    assignment whose value names the ``development`` mark classifies —
    the same literal quoted inside a docstring or comment never does
    (security review, #396). An unparsable file is behavior by default.

    Args:
        text: The test module's source.

    Returns:
        ``True`` only for a genuine top-level assignment.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = {node.target.id}
            value = node.value
        else:
            continue
        if "pytestmark" not in names or value is None:
            continue
        mark_names = {a.attr for a in ast.walk(value) if isinstance(a, ast.Attribute)}
        if "development" in mark_names:
            return True
    return False


def days_since_last_touch(repo_root: Path, rel_path: str) -> float:
    """Return days since *rel_path*'s last commit.

    Args:
        repo_root: Git repo root.
        rel_path: Repo-relative file path.

    Returns:
        Age in days of the file's last commit; ``0.0`` when the file has
        no history (new/untracked files are never stale).
    """
    out = run_git(
        "log", "-1", "--format=%ct", "--", rel_path, cwd=repo_root, check=False
    )
    if not out.strip():
        return 0.0
    return max(0.0, (time.time() - int(out.strip())) / 86400.0)


def lifecycle_skippable(
    repo_root: Path,
    test_files: set[str],
    changed: set[str],
    *,
    skip_days: float = DEFAULT_SKIP_DAYS,
) -> set[str]:
    """Return development files an ordinary full run may deselect.

    A file qualifies only when all three hold: development-marked,
    untouched in git for *skip_days*, and not part of the current change
    set (an edit anywhere in the file re-includes it instantly).

    Args:
        repo_root: Git repo root.
        test_files: All candidate test files (repo-relative).
        changed: The current change set (re-inclusion trigger).
        skip_days: Quiet-window length in days.

    Returns:
        Files to deselect from ordinary full runs.
    """
    marked = development_marked_files(repo_root, test_files) - changed
    return {rel for rel in marked if days_since_last_touch(repo_root, rel) >= skip_days}


def failed_files(pytest_output: str) -> set[str]:
    """Extract failing test-file paths from pytest output.

    Args:
        pytest_output: Captured pytest stdout.

    Returns:
        Repo-relative ``.py`` paths named on ``FAILED`` lines.
    """
    return set(_FAILED_LINE_RE.findall(pytest_output))


def append_history(repo_root: Path, metrics: RunMetrics) -> None:
    """Append one record-only metrics line for a full run.

    Args:
        repo_root: Git repo root.
        metrics: Per-run metrics to append.
    """
    frac = (metrics.dev_files / metrics.total_files) if metrics.total_files else 0.0
    append_ledger_line(
        code_health_dir(repo_root) / HISTORY_NAME,
        {
            "label": metrics.label,
            "wall_s": f"{metrics.wall_s:.1f}",
            "files": metrics.total_files,
            "dev_files": metrics.dev_files,
            "dev_fraction": f"{frac:.3f}",
            "lifecycle_skipped": metrics.lifecycle_skipped,
            "differential_mismatches": metrics.differential_mismatches,
        },
    )
