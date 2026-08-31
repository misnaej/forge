"""Test-lifecycle mechanics for forge-smart-test (FOUNDATION §8).

Graph-independent support module (layering-exempt, like ``git_helpers``):
owns the three lifecycle artifacts the depth model consumes —

- **classification**: a test file whose module level carries
  ``pytestmark = pytest.mark.development`` (alone or in a list) is a
  *development* file; unmarked files are *behavior* tests, the permanent
  default class.
- **lifecycle deselection**: development files untouched for
  ``lifecycle_skip_days`` (default 30) leave ordinary full runs — always
  reported, never silent — and re-enter automatically the moment their
  file changes. Deletion is deliberately not implemented.
- **the full-run stamp** ``.forge-full-run``: a tracked one-line ISO
  timestamp giving every contributor and CI the shared age of the last
  truly-all run; the pre-commit step escalates when it exceeds
  ``full_run_max_age_hours`` (default 48).

A one-line history record per full run lands in
``code_health/smart_test_history.log`` (append-only, telemetry-history
pattern) carrying the record-only health metrics: wall time, file
counts, development fraction, lifecycle skips, and depth-2 differential
mismatches.
"""

from __future__ import annotations

import ast
import datetime as _dt
import re
import time
from dataclasses import dataclass
from pathlib import Path

from forge.git_utils import run_git


@dataclass(frozen=True)
class RunMetrics:
    """Per-run metrics appended to the smart-test history ledger."""

    label: str
    wall_s: float
    total_files: int
    dev_files: int
    lifecycle_skipped: int
    differential_mismatches: int


STAMP_RELPATH = Path(".forge-full-run")
HISTORY_RELPATH = Path("code_health") / "smart_test_history.log"

DEFAULT_SKIP_DAYS = 30
DEFAULT_STAMP_MAX_AGE_HOURS = 48

# Paths whose changes cannot affect test outcomes; everything else
# non-Python escalates the run to full (the safe-fallback guarantee).
DEFAULT_NONPYTHON_IGNORE = (
    "*.md",
    ".claude-plugin/plugin.json",
    str(STAMP_RELPATH),
    ".plan/*",
    ".gitignore",
)

# Tolerance for clock skew before a future-dated stamp is treated as
# invalid — a forged or mis-merged future stamp must escalate, never
# silently suppress the cadence guarantee (security review, #396).
_STAMP_FUTURE_TOLERANCE_S = 300.0

_FAILED_LINE_RE = re.compile(r"^FAILED ([^\s:]+\.py)", re.MULTILINE)


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
        except OSError:
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
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "pytestmark" in names and "development" in ast.dump(node.value):
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


def read_stamp(repo_root: Path) -> _dt.datetime | None:
    """Return the last truly-all run's timestamp, or ``None``.

    Args:
        repo_root: Git repo root.

    Returns:
        The parsed UTC timestamp, or ``None`` when the stamp is missing
        or unparsable (both mean: escalate).
    """
    try:
        raw = (repo_root / STAMP_RELPATH).read_text(encoding="utf-8").strip()
        return _dt.datetime.fromisoformat(raw)
    except (OSError, ValueError):
        return None


def write_stamp(repo_root: Path) -> Path:
    """Write the stamp with the current UTC time.

    Args:
        repo_root: Git repo root.

    Returns:
        The stamp path (for the caller to ``git add`` into the same
        commit — the stamp rides the commit that earned it).
    """
    path = repo_root / STAMP_RELPATH
    now = _dt.datetime.now(tz=_dt.UTC).replace(microsecond=0)
    path.write_text(now.isoformat() + "\n", encoding="utf-8")
    return path


def stamp_age_hours(repo_root: Path) -> float | None:
    """Return the stamp's age in hours, or ``None`` when unreadable.

    Args:
        repo_root: Git repo root.

    Returns:
        Hours since the last truly-all run, or ``None`` (escalate).
    """
    stamp = read_stamp(repo_root)
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.UTC)
    now = _dt.datetime.now(tz=_dt.UTC)
    delta_s = (now - stamp).total_seconds()
    if delta_s < -_STAMP_FUTURE_TOLERANCE_S:
        # A future-dated stamp would silently disable the cadence
        # guarantee — treat it as invalid so the caller escalates.
        return None
    return max(0.0, delta_s / 3600.0)


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
    path = repo_root / HISTORY_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(tz=_dt.UTC).replace(microsecond=0).isoformat()
    frac = (metrics.dev_files / metrics.total_files) if metrics.total_files else 0.0
    line = (
        f"ts={ts} label={metrics.label} wall_s={metrics.wall_s:.1f} "
        f"files={metrics.total_files} dev_files={metrics.dev_files} "
        f"dev_fraction={frac:.3f} lifecycle_skipped={metrics.lifecycle_skipped} "
        f"differential_mismatches={metrics.differential_mismatches}\n"
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
