"""Tests for ``forge.smart_test.lifecycle`` — test-lifecycle mechanics."""

# MOCKING STRATEGY: behavior-class throughout — real git fixtures (via
# ``tests.conftest.init_git_repo``) exercise ``days_since_last_touch`` and
# ``lifecycle_skippable`` end-to-end, backdating commits via
# ``GIT_COMMITTER_DATE`` / ``GIT_AUTHOR_DATE`` env so "N days ago" is exact
# rather than approximated with ``time.sleep``. Stamp and history I/O
# (``read_stamp`` / ``write_stamp`` / ``append_history``) hit the real
# filesystem under ``tmp_path``. No subprocess or module monkeypatching.

from __future__ import annotations

import datetime as _dt
import subprocess
from typing import TYPE_CHECKING

from forge.smart_test import lifecycle
from tests.conftest import GIT_ENV as _GIT_ENV
from tests.conftest import init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


def _commit_file(repo: Path, rel: str, content: str, *, days_ago: float = 0) -> None:
    """Commit *rel* with content *content*, backdated by *days_ago* days.

    Args:
        repo: Git repo root (already initialized).
        rel: Repo-relative file path to write and commit.
        content: File content.
        days_ago: How far in the past to backdate the commit's author and
            committer dates (both — ``days_since_last_touch`` reads
            ``%ct``, the committer date).
    """
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=repo, env=_GIT_ENV, check=True)
    when = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(days=days_ago)
    env = dict(_GIT_ENV)
    env["GIT_AUTHOR_DATE"] = when.isoformat()
    env["GIT_COMMITTER_DATE"] = when.isoformat()
    subprocess.run(
        ["git", "commit", "-q", "-m", f"touch {rel}"], cwd=repo, env=env, check=True
    )


# ---------------------------------------------------------------------------
# development_marked_files
# ---------------------------------------------------------------------------


def test_development_marked_files_module_pytestmark_matched(tmp_path: Path) -> None:
    """A top-level ``pytestmark = pytest.mark.development`` marks the file."""
    (tmp_path / "test_a.py").write_text(
        "import pytest\n\npytestmark = pytest.mark.development\n", encoding="utf-8"
    )
    result = lifecycle.development_marked_files(tmp_path, {"test_a.py"})
    assert result == {"test_a.py"}


def test_development_marked_files_list_form_matched(tmp_path: Path) -> None:
    """``pytestmark`` as a list containing the development mark still matches."""
    (tmp_path / "test_b.py").write_text(
        "import pytest\n\npytestmark = [pytest.mark.development, pytest.mark.slow]\n",
        encoding="utf-8",
    )
    result = lifecycle.development_marked_files(tmp_path, {"test_b.py"})
    assert result == {"test_b.py"}


def test_development_marked_files_unmarked_excluded(tmp_path: Path) -> None:
    """A file with no ``pytestmark`` at all is not classified as development."""
    (tmp_path / "test_c.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    result = lifecycle.development_marked_files(tmp_path, {"test_c.py"})
    assert result == set()


def test_development_marked_files_unreadable_skipped_without_raising(
    tmp_path: Path,
) -> None:
    """A missing file is skipped silently rather than raising OSError."""
    result = lifecycle.development_marked_files(tmp_path, {"does_not_exist.py"})
    assert result == set()


def test_development_marked_files_comment_mention_not_matched(tmp_path: Path) -> None:
    """A ``pytestmark`` mention inside a comment does not count as marking.

    The line-anchored regex requires ``pytestmark`` at the very start of the
    line; a ``#``-prefixed comment line never matches.
    """
    (tmp_path / "test_d.py").write_text(
        "# pytestmark = pytest.mark.development (do not uncomment yet)\n"
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    result = lifecycle.development_marked_files(tmp_path, {"test_d.py"})
    assert result == set()


def test_development_marked_files_docstring_mention_not_matched(tmp_path: Path) -> None:
    """A ``pytestmark`` mention inside a docstring line does not count as marking.

    The docstring line is prefixed by other text, so it never starts with
    ``pytestmark`` at column 0 — the line anchor excludes it.
    """
    (tmp_path / "test_e.py").write_text(
        '"""Explains that pytestmark = pytest.mark.development is used elsewhere."""\n'
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    result = lifecycle.development_marked_files(tmp_path, {"test_e.py"})
    assert result == set()


# ---------------------------------------------------------------------------
# days_since_last_touch
# ---------------------------------------------------------------------------


def test_days_since_last_touch_backdated_commit_within_tolerance(
    tmp_path: Path,
) -> None:
    """A commit backdated 10 days ago reports ~10.0 days since touch."""
    init_git_repo(tmp_path)
    _commit_file(tmp_path, "old.py", "x = 1\n", days_ago=10)
    result = lifecycle.days_since_last_touch(tmp_path, "old.py")
    assert abs(result - 10.0) < 0.1


def test_days_since_last_touch_no_history_returns_zero(tmp_path: Path) -> None:
    """An untracked file with no git history returns 0.0 (never stale)."""
    init_git_repo(tmp_path)
    (tmp_path / "new.py").write_text("x = 1\n", encoding="utf-8")
    result = lifecycle.days_since_last_touch(tmp_path, "new.py")
    assert result == 0.0


# ---------------------------------------------------------------------------
# lifecycle_skippable
# ---------------------------------------------------------------------------


def test_lifecycle_skippable_marked_stale_and_unchanged_included(
    tmp_path: Path,
) -> None:
    """A development-marked file, stale, and outside the change set is skippable."""
    init_git_repo(tmp_path)
    _commit_file(
        tmp_path,
        "test_stale.py",
        "import pytest\n\npytestmark = pytest.mark.development\n",
        days_ago=40,
    )
    result = lifecycle.lifecycle_skippable(
        tmp_path, {"test_stale.py"}, changed=set(), skip_days=30
    )
    assert result == {"test_stale.py"}


def test_lifecycle_skippable_changed_file_excluded(tmp_path: Path) -> None:
    """A stale, marked file currently in the change set is NOT skippable."""
    init_git_repo(tmp_path)
    _commit_file(
        tmp_path,
        "test_stale.py",
        "import pytest\n\npytestmark = pytest.mark.development\n",
        days_ago=40,
    )
    result = lifecycle.lifecycle_skippable(
        tmp_path, {"test_stale.py"}, changed={"test_stale.py"}, skip_days=30
    )
    assert result == set()


def test_lifecycle_skippable_fresh_file_excluded(tmp_path: Path) -> None:
    """A recently-touched marked file is not stale enough to be skippable."""
    init_git_repo(tmp_path)
    _commit_file(
        tmp_path,
        "test_fresh.py",
        "import pytest\n\npytestmark = pytest.mark.development\n",
        days_ago=1,
    )
    result = lifecycle.lifecycle_skippable(
        tmp_path, {"test_fresh.py"}, changed=set(), skip_days=30
    )
    assert result == set()


def test_lifecycle_skippable_unmarked_stale_excluded(tmp_path: Path) -> None:
    """A stale but unmarked file is never skippable (not a development test)."""
    init_git_repo(tmp_path)
    _commit_file(
        tmp_path, "test_plain.py", "def test_ok():\n    assert True\n", days_ago=40
    )
    result = lifecycle.lifecycle_skippable(
        tmp_path, {"test_plain.py"}, changed=set(), skip_days=30
    )
    assert result == set()


# ---------------------------------------------------------------------------
# stamp round-trip — write_stamp / read_stamp / stamp_age_hours
# ---------------------------------------------------------------------------


def test_stamp_round_trip_write_then_read_is_tz_aware(tmp_path: Path) -> None:
    """A written stamp reads back as a tz-aware datetime close to now."""
    lifecycle.write_stamp(tmp_path)
    stamp = lifecycle.read_stamp(tmp_path)
    assert stamp is not None
    assert stamp.tzinfo is not None
    delta = abs((_dt.datetime.now(tz=_dt.UTC) - stamp).total_seconds())
    assert delta < 5


def test_read_stamp_missing_returns_none(tmp_path: Path) -> None:
    """A never-written stamp file reads as ``None``."""
    assert lifecycle.read_stamp(tmp_path) is None


def test_stamp_age_hours_missing_returns_none(tmp_path: Path) -> None:
    """``stamp_age_hours`` returns ``None`` when the stamp is missing (escalate)."""
    assert lifecycle.stamp_age_hours(tmp_path) is None


def test_read_stamp_garbage_content_returns_none(tmp_path: Path) -> None:
    """Unparsable stamp content reads as ``None`` rather than raising."""
    (tmp_path / lifecycle.STAMP_RELPATH).write_text(
        "not-a-timestamp\n", encoding="utf-8"
    )
    assert lifecycle.read_stamp(tmp_path) is None


def test_stamp_age_hours_garbage_content_returns_none(tmp_path: Path) -> None:
    """``stamp_age_hours`` on unparsable content returns ``None`` (escalate)."""
    (tmp_path / lifecycle.STAMP_RELPATH).write_text("garbage\n", encoding="utf-8")
    assert lifecycle.stamp_age_hours(tmp_path) is None


def test_stamp_age_hours_naive_timestamp_coerced_to_utc(tmp_path: Path) -> None:
    """A naive (no-tzinfo) stamp is coerced to UTC and yields a sane age."""
    naive = (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(hours=5)).replace(tzinfo=None)
    (tmp_path / lifecycle.STAMP_RELPATH).write_text(
        naive.isoformat() + "\n", encoding="utf-8"
    )
    age = lifecycle.stamp_age_hours(tmp_path)
    assert age is not None
    assert abs(age - 5.0) < 0.1


def test_stamp_age_hours_future_stamp_clamps_to_zero(tmp_path: Path) -> None:
    """A stamp in the future clamps the age to 0.0 rather than going negative."""
    future = _dt.datetime.now(tz=_dt.UTC) + _dt.timedelta(hours=10)
    (tmp_path / lifecycle.STAMP_RELPATH).write_text(
        future.isoformat() + "\n", encoding="utf-8"
    )
    assert lifecycle.stamp_age_hours(tmp_path) == 0.0


# ---------------------------------------------------------------------------
# failed_files
# ---------------------------------------------------------------------------


def test_failed_files_extracts_two_failed_lines() -> None:
    """Two ``FAILED`` lines yield both file paths."""
    output = (
        "FAILED tests/test_a.py::test_x - AssertionError\n"
        "FAILED tests/test_b.py::test_y - ValueError\n"
    )
    assert lifecycle.failed_files(output) == {"tests/test_a.py", "tests/test_b.py"}


def test_failed_files_no_failures_returns_empty_set() -> None:
    """Output with no ``FAILED`` lines yields an empty set."""
    assert lifecycle.failed_files("2 passed in 0.01s\n") == set()


def test_failed_files_non_python_failed_line_not_matched() -> None:
    """A ``FAILED`` line naming a non-``.py`` path is not captured."""
    output = "FAILED some_binary_test - exit code 1\n"
    assert lifecycle.failed_files(output) == set()


# ---------------------------------------------------------------------------
# append_history
# ---------------------------------------------------------------------------


def test_append_history_writes_one_well_formed_line_and_creates_dir(
    tmp_path: Path,
) -> None:
    """The first call creates ``code_health/`` and writes one parseable line."""
    metrics = lifecycle.RunMetrics(
        label="full",
        wall_s=12.5,
        total_files=10,
        dev_files=2,
        lifecycle_skipped=1,
        differential_mismatches=0,
    )
    lifecycle.append_history(tmp_path, metrics)
    log = tmp_path / lifecycle.HISTORY_RELPATH
    assert log.exists()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert "label=full" in line
    assert "wall_s=12.5" in line
    assert "files=10" in line
    assert "dev_files=2" in line
    assert "dev_fraction=0.200" in line
    assert "lifecycle_skipped=1" in line
    assert "differential_mismatches=0" in line


def test_append_history_zero_total_files_no_zero_division(tmp_path: Path) -> None:
    """``total_files=0`` yields ``dev_fraction=0.000`` without raising."""
    metrics = lifecycle.RunMetrics(
        label="all",
        wall_s=0.1,
        total_files=0,
        dev_files=0,
        lifecycle_skipped=0,
        differential_mismatches=0,
    )
    lifecycle.append_history(tmp_path, metrics)
    log = tmp_path / lifecycle.HISTORY_RELPATH
    assert "dev_fraction=0.000" in log.read_text(encoding="utf-8")


def test_append_history_two_calls_append(tmp_path: Path) -> None:
    """A second call appends a second line rather than overwriting the first."""
    for label in ("full", "all"):
        metrics = lifecycle.RunMetrics(
            label=label,
            wall_s=1.0,
            total_files=1,
            dev_files=0,
            lifecycle_skipped=0,
            differential_mismatches=0,
        )
        lifecycle.append_history(tmp_path, metrics)
    log = tmp_path / lifecycle.HISTORY_RELPATH
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "label=full" in lines[0]
    assert "label=all" in lines[1]
