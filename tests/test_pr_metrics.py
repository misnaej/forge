"""Tests for ``forge-metrics`` — the per-PR cost database.

# MOCKING STRATEGY: GitHub is the only fake. ``gh_api`` is replaced by
# :class:`FakeGitHub`, which answers the three endpoints the CLI calls from
# an in-memory dict; ``require_cli`` is a no-op and the origin slug is
# fixed to ``acme/widget``. ``XDG_DATA_HOME`` points under ``tmp_path``, so
# the database, the repo-id map and legacy files are real files there;
# transcripts are real JSONL files.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import TYPE_CHECKING
from urllib.parse import unquote

import pytest

from forge import agent_profile, pr_metrics
from forge.agent_profile_tokens import TokenUsage
from tests.test_agent_profile import _iso, _write_jsonl
from tests.test_agent_profile_tokens import _at, _request, on_branch


if TYPE_CHECKING:
    from pathlib import Path


SLUG = "acme/widget"


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

    Machine data lives under ``tmp_path / "data"``.

    Returns:
        The fake, for registering PRs.
    """
    fake = FakeGitHub()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(pr_metrics, "gh_api", fake)
    monkeypatch.setattr(pr_metrics, "require_cli", lambda *_a, **_k: None)
    monkeypatch.setattr(pr_metrics, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(pr_metrics, "github_slug", lambda _root: SLUG)
    return fake


def _key(pr: int) -> str:
    """The stored key of *pr* in ``acme/widget``.

    Args:
        pr: PR number.

    Returns:
        Its ``pr_key``.
    """
    repo_id = pr_metrics.lookup_repo_id(SLUG)
    assert repo_id is not None
    return pr_metrics.pr_key(pr_metrics.machine_secret(), repo_id, pr)


@pytest.fixture
def transcripts(tmp_path: Path) -> Path:
    """A transcript dir: feat/a main + helper, feat/b main, main-branch work.

    Returns:
        The directory.
    """
    root = tmp_path / "projects" / "checkout"
    first = _request(0, "a1", usage=(100, 0, 0, 10))
    message = first["message"]
    assert isinstance(message, dict)
    first["message"] = {**message, "model": "claude-test-1"}
    _write_jsonl(
        root / "s1.jsonl",
        [
            on_branch("feat/a", first),
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
            "SELECT agent_type, input FROM pr_tokens WHERE pr_key = ?", (_key(pr),)
        )
        return dict(rows.fetchall())


def test_record_attributes_tokens_by_head_branch(
    tmp_path: Path, github: FakeGitHub, transcripts: Path
) -> None:
    """Behavior: only head-branch usage is charged; rounds = busiest reporter's runs.

    Wrap-ups posted are kept apart from review rounds; the repo's own
    agent type is stored as ``other``; the main session's model is kept.
    """
    github.add(1, "feat/a")
    github.wrapups[1] = 1
    db = tmp_path / "m.sqlite"

    assert _run(db, transcripts, "record", "--pr", "1") == 0

    assert _tokens(db, 1) == {
        "main": 200,
        "other": 50,
        "forge:design-checker": 0,
        "forge:security-checker": 0,
    }
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT merged_on, tool, model, review_rounds, wrapups_posted, files, "
            "wall_s, precommit_runs FROM prs WHERE pr_key = ?",
            (_key(1),),
        ).fetchone()
    assert row == ("2026-09-30", "claude-code", "claude-test-1", 2, 1, 2, 60.0, 0)


def test_database_holds_no_repo_pr_or_branch_name(
    tmp_path: Path, github: FakeGitHub, transcripts: Path
) -> None:
    """Behavior: after record, no title, branch, slug or PR number is in the file.

    The repo-id map is the one file naming the repo.
    """
    github.add(9876543, "feat/a", title="Secret Title", merged_at=_iso(_at(3600)))
    db = tmp_path / "m.sqlite"

    assert _run(db, transcripts, "record", "--pr", "9876543") == 0

    data = db.read_bytes()
    for leak in (b"Secret", b"feat/a", b"acme", b"widget", b"9876543", b"checkout"):
        assert leak not in data
    ids = json.loads((tmp_path / "data" / "forge" / "repo-ids.json").read_text())
    assert sorted(ids) == [pr_metrics.HMAC_KEY_FIELD, SLUG]
    assert ids[pr_metrics.HMAC_KEY_FIELD].encode() not in data


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
        "other": 50,
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
    """Behavior: recording a PR again leaves one row set, the latest, by pr_key."""
    github.add(1, "feat/a")
    db = tmp_path / "m.sqlite"
    _run(db, transcripts, "record", "--pr", "1")
    github.add(1, "feat/b", additions=99)

    _run(db, transcripts, "record", "--pr", "1")

    assert _tokens(db, 1) == {"main": 5}
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT pr_key, additions FROM prs").fetchall()
    assert rows == [(_key(1), 99)]


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
        rows = conn.execute(
            "SELECT repo_id, agent_type, input FROM unattributed"
        ).fetchall()
    assert rows == [(pr_metrics.lookup_repo_id(SLUG), "main", 40)]


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
        keys = {r[0] for r in conn.execute("SELECT pr_key FROM prs")}
    assert keys == {_key(1), _key(2)}


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


_LEGACY_SCHEMA = """
CREATE TABLE prs (pr INTEGER PRIMARY KEY, title TEXT, head_branch TEXT,
    merged_at TEXT, files INTEGER, additions INTEGER, deletions INTEGER,
    commits INTEGER, review_rounds INTEGER, wrapups_posted INTEGER,
    precommit_runs INTEGER, test_runs INTEGER, active_s REAL, wall_s REAL,
    recorded_at TEXT, forge_version TEXT);
CREATE TABLE pr_tokens (pr INTEGER NOT NULL, agent_type TEXT NOT NULL,
    requests INTEGER, input INTEGER, cache_write INTEGER, cache_read INTEGER,
    output INTEGER, PRIMARY KEY (pr, agent_type));
CREATE TABLE unattributed (date TEXT NOT NULL, agent_type TEXT NOT NULL,
    requests INTEGER, input INTEGER, cache_write INTEGER, cache_read INTEGER,
    output INTEGER, PRIMARY KEY (date, agent_type));
INSERT INTO prs VALUES (9876543, 'Secret Title', 'feat/secret-branch',
    '2026-08-01T10:00:00Z', 2, 30, 10, 3, 1, 1, 2, 1, 60.0, 120.0,
    '2026-08-01T11:00:00+00:00', '11.0.0');
INSERT INTO pr_tokens VALUES (9876543, 'main', 1, 100, 0, 0, 5);
INSERT INTO pr_tokens VALUES (9876543, 'reviewer-acme', 1, 7, 0, 0, 0);
INSERT INTO unattributed VALUES ('2026-08-01', 'main', 1, 40, 0, 0, 0);
"""


def test_legacy_per_repo_database_moves_into_machine_database(
    tmp_path: Path, github: FakeGitHub
) -> None:
    """Behavior: an old per-repo file is imported without names, then deleted.

    Importing the same file again — a crash after import, before the
    delete — counts nothing twice; a run with nothing left changes nothing.
    """
    del github  # wires XDG, slug and repo root
    legacy = tmp_path / "data" / "forge" / "acme" / "widget" / "metrics.sqlite"
    legacy.parent.mkdir(parents=True)
    with sqlite3.connect(legacy) as conn:
        conn.executescript(_LEGACY_SCHEMA)
    conn.close()
    saved = legacy.read_bytes()

    assert pr_metrics.main(["report"]) == 0
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(saved)  # as if the delete never happened
    assert pr_metrics.main(["report"]) == 0
    assert pr_metrics.main(["report"]) == 0

    assert not (tmp_path / "data" / "forge" / "acme").exists()
    db = tmp_path / "data" / "forge" / "metrics.sqlite"
    assert _tokens(db, 9876543) == {"main": 100, "other": 7}
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT merged_on, tool FROM prs").fetchall() == [
            ("2026-08-01", "claude-code")
        ]
        assert conn.execute("SELECT input FROM unattributed").fetchall() == [(40,)]
    data = db.read_bytes()
    for leak in (b"Secret", b"secret-branch", b"acme", b"widget", b"9876543"):
        assert leak not in data


def test_export_equals_stored_rows(
    tmp_path: Path, github: FakeGitHub, transcripts: Path
) -> None:
    """Behavior: export writes each stored row as it is, with the schema version."""
    github.add(1, "feat/a")
    db = tmp_path / "m.sqlite"
    out = tmp_path / "export.json"
    _run(db, transcripts, "record", "--pr", "1")

    assert _run(db, transcripts, "export", "--out", str(out)) == 0

    assert pr_metrics.machine_secret() not in out.read_text()
    payload = json.loads(out.read_text())
    assert payload["schema"] == pr_metrics.SCHEMA_VERSION
    [pr] = payload["prs"]
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        stored = dict(conn.execute("SELECT * FROM prs").fetchone())
    assert {k: v for k, v in pr.items() if k != "tokens"} == stored
    assert {t: c["input"] for t, c in pr["tokens"].items()} == _tokens(db, 1)


def test_report_repo_filter_resolves_current_repo_and_rejects_unknown(
    tmp_path: Path,
    github: FakeGitHub,
    transcripts: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: ``--repo .`` shows this repo; a repo never recorded exits 2."""
    github.add(1, "feat/a")
    db = tmp_path / "m.sqlite"
    _run(db, transcripts, "record", "--pr", "1")
    capsys.readouterr()

    assert _run(db, transcripts, "report", "--repo", ".") == 0
    short = (pr_metrics.lookup_repo_id(SLUG) or "")[:6]
    assert f"2026-09-30 {short}#1" in capsys.readouterr().out
    assert _run(db, transcripts, "report", "--repo", "acme/other") == 2


def test_pr_key_is_keyed_by_the_machine_secret_and_stable(
    github: FakeGitHub,
) -> None:
    """Behavior: pr_key is an HMAC, not a plain sha256 anyone could recompute."""
    del github  # wires XDG
    secret = pr_metrics.machine_secret()

    key = pr_metrics.pr_key(secret, "repo-1", 42)

    assert key == pr_metrics.pr_key(pr_metrics.machine_secret(), "repo-1", 42)
    assert key != hashlib.sha256(b"repo-1:42").hexdigest()
    assert len(secret) == 64


def test_plain_sha256_database_is_rekeyed_on_open(
    tmp_path: Path, github: FakeGitHub
) -> None:
    """Behavior: a schema-2 database's plain keys become HMAC keys, tokens too."""
    del github  # wires XDG
    db = tmp_path / "v2.sqlite"
    plain = hashlib.sha256(b"repo-1:7").hexdigest()
    with sqlite3.connect(db) as conn:
        conn.executescript(pr_metrics._SCHEMA)
        conn.execute("PRAGMA user_version = 2")
        conn.execute("INSERT INTO prs (pr_key, repo_id) VALUES (?, 'repo-1')", (plain,))
        conn.execute(
            "INSERT INTO pr_tokens VALUES (?, 'main', 1, 5, 0, 0, 0)", (plain,)
        )
    conn.close()

    conn = pr_metrics.open_db(db, pr_metrics.machine_secret())

    expected = pr_metrics.pr_key(pr_metrics.machine_secret(), "repo-1", 7)
    assert conn.execute("SELECT pr_key FROM prs").fetchall() == [(expected,)]
    assert conn.execute("SELECT pr_key FROM pr_tokens").fetchall() == [(expected,)]
    assert conn.execute("PRAGMA user_version").fetchone() == (
        pr_metrics.SCHEMA_VERSION,
    )
    conn.close()


@pytest.mark.parametrize("version", [99, 0])
def test_unknown_or_legacy_database_is_refused(
    tmp_path: Path,
    github: FakeGitHub,
    transcripts: Path,
    caplog: pytest.LogCaptureFixture,
    version: int,
) -> None:
    """Behavior: --db on an unknown schema or an old per-repo file exits 2, named.

    Version 0 with tables is a per-repo file of an earlier version.

    Args:
        version: Value written to the database's PRAGMA user_version.
    """
    del github  # wires XDG
    db = tmp_path / "odd.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE prs (pr INTEGER)")
        conn.execute(f"PRAGMA user_version = {version}")
    conn.close()

    assert _run(db, transcripts, "report") == 2

    assert f"schema {version}" in caplog.text or f"version {version}" in caplog.text


def test_fold_keeps_public_agent_types_and_sums_the_rest_as_other() -> None:
    """Behavior: only main, shipped forge agents and Claude Code built-ins keep names.

    A ``forge:`` prefix is not enough: a repo's own agent often carries
    its name.
    """
    usage = {
        name: TokenUsage(input_tokens=n, requests=1)
        for name, n in (
            ("main", 10),
            ("forge:design-checker", 5),
            ("Explore", 4),
            ("forge:acme-reviewer", 3),
            ("design-checker-acme", 2),
        )
    }

    folded = pr_metrics.fold_agent_types(usage)

    assert set(folded) == {"main", "forge:design-checker", "Explore", "other"}
    assert folded["other"].input_tokens == 5


def test_fold_refuses_an_empty_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavior: an unreadable roster raises instead of folding all to other."""
    monkeypatch.setattr(pr_metrics, "shipped_agent_names", frozenset)

    with pytest.raises(pr_metrics.MetricsError, match="roster"):
        pr_metrics.fold_agent_types({"main": TokenUsage()})


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("claude-opus-4-1", "claude-opus-4-1"),
        ("us.anthropic.claude:v2", "us.anthropic.claude:v2"),
        ("acme corp model", "other"),
        ("a/b", "other"),
        ("x" * 65, "other"),
        (None, None),
    ],
)
def test_model_is_stored_only_when_it_looks_like_a_model_id(
    raw: str | None, stored: str | None
) -> None:
    """Behavior: a free-form model string from a transcript is stored as other.

    Args:
        raw: Model string as read from the transcript, or None.
        stored: Expected value after sanitising, or None.
    """
    assert pr_metrics.safe_model(raw) == stored
