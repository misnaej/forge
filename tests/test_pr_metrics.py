"""Tests for ``forge-metrics`` — the per-PR cost database.

# MOCKING STRATEGY: GitHub is the only fake. ``gh_api`` is replaced by
# :class:`FakeGitHub`, which answers the three endpoints the CLI calls from
# an in-memory dict; ``require_cli`` is a no-op. The database is a real
# SQLite file under ``tmp_path`` and transcripts are real JSONL files.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import TYPE_CHECKING
from urllib.parse import unquote

import pytest

from forge import agent_profile, pr_metrics
from tests.test_agent_profile import _iso, _write_jsonl
from tests.test_agent_profile_tokens import _at, _request, on_branch


if TYPE_CHECKING:
    from pathlib import Path


class FakeGitHub:
    """Answers ``gh api`` calls for pulls, PR comments and the merged list."""

    def __init__(self) -> None:
        """Start with no PRs."""
        self.pulls: dict[int, dict[str, object]] = {}
        self.wrapups: dict[int, int] = {}
        self.heads: dict[int, str] = {}
        self.fetched: list[int] = []

    def add(self, number: int, head: str, **extra: object) -> None:
        """Register a merged PR.

        Args:
            number: PR number.
            head: Head branch.
            **extra: Fields overriding the defaults.
        """
        self.heads[number] = head
        self.pulls[number] = {
            "number": number,
            "title": f"Title {number}",
            "head": {"ref": head},
            "merged_at": f"2026-09-30T{number:02d}:00:00Z",
            "changed_files": 2,
            "additions": 30,
            "deletions": 10,
            "commits": 3,
            **extra,
        }

    def __call__(self, *args: str, **_kw: object) -> str | None:
        """Stand in for :func:`forge.git_utils.gh_api`.

        Args:
            *args: The ``gh api`` argv tail.
            **_kw: Ignored (timeout).

        Returns:
            What ``gh api --jq`` would print, ``None`` for unknown calls.
        """
        endpoint = next(a for a in args if a.startswith("repos/"))
        tail = endpoint.split("/", 3)[3]
        if "&head=" in tail:
            head = unquote(tail.split("&head=", 1)[1].split(":", 1)[1])
            return "\n".join(
                str(self.pulls[n]["merged_at"])
                for n, ref in self.heads.items()
                if ref == head
            )
        if tail.startswith("pulls?"):
            return "\n".join(
                f"{n}\t{head}" for n, head in sorted(self.heads.items(), reverse=True)
            )
        number = int(tail.split("/")[1])
        if tail.startswith("issues/"):
            return str(self.wrapups.get(number, 0))
        self.fetched.append(number)
        pull = self.pulls.get(number)
        return json.dumps(pull) if pull else None


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGitHub:
    """A fake GitHub wired into the CLI; repo root is *tmp_path*.

    Returns:
        The fake, for registering PRs.
    """
    fake = FakeGitHub()
    monkeypatch.setattr(pr_metrics, "gh_api", fake)
    monkeypatch.setattr(pr_metrics, "require_cli", lambda *_a, **_k: None)
    monkeypatch.setattr(pr_metrics, "repo_root", lambda: tmp_path)
    return fake


@pytest.fixture
def transcripts(tmp_path: Path) -> Path:
    """A transcript dir: feat/a main + helper, feat/b main, main-branch work.

    Returns:
        The directory.
    """
    root = tmp_path / "projects" / "checkout"
    _write_jsonl(
        root / "s1.jsonl",
        [
            on_branch("feat/a", _request(0, "a1", usage=(100, 0, 0, 10))),
            on_branch("feat/a", _request(60, "a2", usage=(100, 0, 0, 10))),
            on_branch("feat/b", _request(200, "b1", usage=(5, 0, 0, 0))),
            on_branch("main", _request(300, "m1", usage=(40, 0, 0, 0))),
        ],
    )
    sub = root / "s1" / "subagents" / "agent-x.jsonl"
    _write_jsonl(sub, [on_branch("feat/a", _request(30, "h1", usage=(50, 0, 0, 0)))])
    sub.with_name("agent-x.meta.json").write_text(
        json.dumps({"agentType": "design-checker-acme"})
    )
    # Two review passes: design-checker ran twice, security-checker once.
    for agent_id, agent_type, at in (
        ("d1", "forge:design-checker", 10),
        ("d2", "forge:design-checker", 40),
        ("s1", "forge:security-checker", 41),
    ):
        path = root / "s1" / "subagents" / f"agent-{agent_id}.jsonl"
        _write_jsonl(path, [on_branch("feat/a", _request(at, f"r{agent_id}"))])
        path.with_name(f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": agent_type})
        )
    return root


def _run(db: Path, transcripts: Path, *argv: str) -> int:
    """Run the CLI against *db* and *transcripts*.

    Args:
        db: Database file.
        transcripts: Transcript directory.
        *argv: Subcommand and its arguments.

    Returns:
        The exit code.
    """
    return pr_metrics.main(["--db", str(db), "--transcripts", str(transcripts), *argv])


def _tokens(db: Path, pr: int) -> dict[str, int]:
    """Input tokens per agent type recorded for *pr*.

    Args:
        db: Database file.
        pr: PR number.

    Returns:
        Agent type → input tokens.
    """
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT agent_type, input FROM pr_tokens WHERE pr = ?", (pr,)
        )
        return dict(rows.fetchall())


def test_record_attributes_tokens_by_head_branch(
    tmp_path: Path, github: FakeGitHub, transcripts: Path
) -> None:
    """Behavior: only head-branch usage is charged; rounds = busiest reporter's runs.

    Wrap-ups posted are kept apart from review rounds.
    """
    github.add(1, "feat/a")
    github.wrapups[1] = 1
    db = tmp_path / "m.sqlite"

    assert _run(db, transcripts, "record", "--pr", "1") == 0

    assert _tokens(db, 1) == {
        "main": 200,
        "design-checker-acme": 50,
        "forge:design-checker": 0,
        "forge:security-checker": 0,
    }
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT head_branch, review_rounds, wrapups_posted, files, wall_s, "
            "precommit_runs FROM prs WHERE pr = 1"
        ).fetchone()
    assert row == ("feat/a", 2, 1, 2, 60.0, 0)


def test_reused_branch_name_splits_usage_at_each_merge(
    tmp_path: Path, github: FakeGitHub, transcripts: Path
) -> None:
    """Behavior: a branch name reused by a later PR is not charged twice.

    PR 1 merged 35s in: it gets the records up to then. PR 2 (same head
    name, merged later) gets only the records after PR 1's merge.
    """
    github.add(1, "feat/a", merged_at=_iso(_at(35)))
    github.add(2, "feat/a")
    db = tmp_path / "m.sqlite"

    _run(db, transcripts, "record", "--pr", "1")
    _run(db, transcripts, "record", "--pr", "2")

    assert _tokens(db, 1) == {
        "main": 100,
        "design-checker-acme": 50,
        "forge:design-checker": 0,
    }
    assert _tokens(db, 2) == {
        "main": 100,
        "forge:design-checker": 0,
        "forge:security-checker": 0,
    }


def test_record_replaces_previous_rows(
    tmp_path: Path, github: FakeGitHub, transcripts: Path
) -> None:
    """Behavior: recording a PR again leaves one row set, the latest."""
    github.add(1, "feat/a")
    db = tmp_path / "m.sqlite"
    _run(db, transcripts, "record", "--pr", "1")
    github.add(1, "feat/b", additions=99)

    _run(db, transcripts, "record", "--pr", "1")

    assert _tokens(db, 1) == {"main": 5}
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT pr, additions FROM prs").fetchall() == [(1, 99)]


def test_base_branch_usage_lands_in_unattributed_and_never_shrinks(
    tmp_path: Path, github: FakeGitHub, transcripts: Path
) -> None:
    """Behavior: base-branch tokens are kept per day; a smaller rescan keeps the max.

    A later scan sees less once old transcripts are purged; the fuller
    figure already stored must survive it.
    """
    github.add(1, "feat/a")
    db = tmp_path / "m.sqlite"
    _run(db, transcripts, "record", "--pr", "1")
    empty = tmp_path / "empty"
    empty.mkdir()

    _run(db, empty, "record", "--pr", "1")

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT agent_type, input FROM unattributed").fetchall()
    assert rows == [("main", 40)]


def test_backfill_skips_recorded_prs_and_branches_without_usage(
    tmp_path: Path, github: FakeGitHub, transcripts: Path
) -> None:
    """Behavior: backfill records only new PRs whose branch has transcript usage."""
    github.add(1, "feat/a")
    github.add(2, "feat/b")
    github.add(3, "feat/never-seen")
    db = tmp_path / "m.sqlite"
    _run(db, transcripts, "record", "--pr", "1")
    github.fetched.clear()

    assert _run(db, transcripts, "backfill") == 0

    assert github.fetched == [2]
    with sqlite3.connect(db) as conn:
        assert [r[0] for r in conn.execute("SELECT pr FROM prs ORDER BY pr")] == [1, 2]


def test_record_fails_when_github_cannot_be_read(
    tmp_path: Path, github: FakeGitHub, transcripts: Path
) -> None:
    """Behavior: an unreadable PR exits 1 and records nothing."""
    db = tmp_path / "m.sqlite"
    assert _run(db, transcripts, "record", "--pr", "9") == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM prs").fetchone() == (0,)


def test_record_parses_only_files_that_can_hold_the_pr(
    tmp_path: Path,
    github: FakeGitHub,
    transcripts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavior: record skips files written before the PR or not on its branch.

    A full walk of every transcript is what made ``record`` slow; a file
    older than the PR's opening (less a day) or never naming its branch
    or the base branch is never parsed.
    """
    stale = transcripts / "old.jsonl"
    _write_jsonl(stale, [on_branch("feat/a", _request(5, "old", usage=(999, 0, 0, 0)))])
    os.utime(stale, (0, 0))
    _write_jsonl(
        transcripts / "other.jsonl",
        [on_branch("feat/zzz", _request(6, "z", usage=(1, 0, 0, 0)))],
    )
    parsed: list[str] = []
    real = agent_profile._iter_jsonl

    def spy(path: Path) -> object:
        """Note *path*, then parse it as usual.

        Args:
            path: The transcript being parsed.

        Returns:
            The real record iterator.
        """
        parsed.append(path.name)
        return real(path)

    monkeypatch.setattr(agent_profile, "_iter_jsonl", spy)
    github.add(1, "feat/a", created_at="2026-09-01T00:00:00Z")
    db = tmp_path / "m.sqlite"

    _run(db, transcripts, "record", "--pr", "1")

    assert "old.jsonl" not in parsed
    assert "other.jsonl" not in parsed
    assert "s1.jsonl" in parsed
    assert _tokens(db, 1)["main"] == 200
