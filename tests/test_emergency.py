"""Tests for forge.emergency — one-shot deferred-verification bypass CLI.

# MOCKING STRATEGY: every ``gh`` round-trip goes through ``emergency._gh``,
# monkeypatched here with a plain callable (``_fake_gh``) that records each
# call's args and pops canned ``FakeProc`` replies in call order — never
# ``unittest.mock.Mock``, since the surface under test IS the sequencing
# and parsing of successive ``gh`` calls (comment-then-close ordering,
# "no re-comment on refusal", etc.), not a single canned return value.
# ``_cmd_consume``'s bare ``git rev-parse HEAD`` shells out directly (not
# through ``_gh``), so that one case runs against a real ephemeral repo
# built by ``tests.conftest.init_git_repo`` instead of being faked. No test
# freezes time: TTL/expiry assertions compare a freshly computed
# ``datetime.now(UTC)`` delta with a generous tolerance instead.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from forge import emergency
from tests.conftest import GIT_ENV, FakeProc, init_git_repo


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _iso_in(hours: float) -> str:
    """Return an ISO-8601 UTC instant *hours* from now (negative = past).

    Args:
        hours: Offset from "now" in hours; negative for a past instant.

    Returns:
        The ``datetime.isoformat()`` string ``EmergencyState.expires_at``
        expects.
    """
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def _fake_gh(
    responses: list[FakeProc],
) -> tuple[Callable[..., FakeProc], list[tuple[str, ...]]]:
    """Return a ``_gh``-shaped fake that pops *responses* in call order.

    Args:
        responses: Canned replies, consumed front-to-back — one entry per
            expected ``_gh(...)`` call along the exercised code path. A
            call beyond the queue gets a default success ``FakeProc()``.

    Returns:
        A ``(fake, calls)`` pair: ``fake`` is the monkeypatch replacement
        for ``emergency._gh``; ``calls`` accumulates each invocation's
        args tuple, in call order, for assertion (an empty list after the
        run pins "gh was never called").
    """
    calls: list[tuple[str, ...]] = []
    queue = list(responses)

    def _fake(*args: str) -> FakeProc:
        calls.append(args)
        return queue.pop(0) if queue else FakeProc()

    return _fake, calls


# --- read_state --------------------------------------------------------


def test_read_state_returns_parsed_state_for_valid_sentinel(tmp_path: Path) -> None:
    """A well-formed sentinel JSON round-trips into an `EmergencyState`."""
    (tmp_path / emergency.SENTINEL_RELPATH).write_text(
        json.dumps(
            {
                "ledger_issue": 42,
                "reason": "prod down",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "spent": False,
            }
        )
    )

    state = emergency.read_state(tmp_path)

    assert state == emergency.EmergencyState(
        ledger_issue=42,
        reason="prod down",
        expires_at="2099-01-01T00:00:00+00:00",
        spent=False,
    )


@pytest.mark.parametrize(
    "sentinel_content",
    [
        None,
        "{not json",
        json.dumps({"reason": "x", "expires_at": "2099-01-01T00:00:00+00:00"}),
        json.dumps(
            {
                "ledger_issue": "not-a-number",
                "reason": "x",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        ),
    ],
    ids=["missing_file", "malformed_json", "missing_key", "non_numeric_ledger_issue"],
)
def test_read_state_degrades_to_none_on_corrupt_sentinel(
    tmp_path: Path, sentinel_content: str | None
) -> None:
    """Every corrupt sentinel shape fails closed to `None`, never a raise.

    Args:
        sentinel_content: Sentinel file text to write, or ``None`` for a
            missing file.
    """
    if sentinel_content is not None:
        (tmp_path / emergency.SENTINEL_RELPATH).write_text(sentinel_content)

    assert emergency.read_state(tmp_path) is None


# --- armed_state ---------------------------------------------------------


def test_armed_state_returns_none_when_expired(tmp_path: Path) -> None:
    """An expiry instant in the past disarms the sentinel."""
    emergency.write_state(
        tmp_path,
        emergency.EmergencyState(ledger_issue=1, reason="x", expires_at=_iso_in(-1)),
    )

    assert emergency.armed_state(tmp_path) is None


def test_armed_state_returns_none_when_spent(tmp_path: Path) -> None:
    """A `spent` sentinel is never usable again, even before its expiry."""
    emergency.write_state(
        tmp_path,
        emergency.EmergencyState(
            ledger_issue=1, reason="x", expires_at=_iso_in(1), spent=True
        ),
    )

    assert emergency.armed_state(tmp_path) is None


def test_armed_state_returns_none_on_unparseable_expiry(tmp_path: Path) -> None:
    """An `expires_at` that is not valid ISO-8601 fails closed to `None`."""
    emergency.write_state(
        tmp_path,
        emergency.EmergencyState(ledger_issue=1, reason="x", expires_at="not-a-date"),
    )

    assert emergency.armed_state(tmp_path) is None


# --- write_state / gitignore ----------------------------------------------


def test_write_state_creates_sentinel_and_gitignore_entry(tmp_path: Path) -> None:
    """`write_state` persists the sentinel JSON and adds it to `.gitignore`."""
    state = emergency.EmergencyState(ledger_issue=7, reason="x", expires_at=_iso_in(1))

    emergency.write_state(tmp_path, state)

    assert emergency.read_state(tmp_path) == state
    assert (
        str(emergency.SENTINEL_RELPATH)
        in (tmp_path / ".gitignore").read_text().splitlines()
    )


def test_write_state_gitignore_entry_is_idempotent(tmp_path: Path) -> None:
    """A second `write_state` does not duplicate the `.gitignore` entry."""
    state = emergency.EmergencyState(ledger_issue=7, reason="x", expires_at=_iso_in(1))

    emergency.write_state(tmp_path, state)
    emergency.write_state(
        tmp_path,
        emergency.EmergencyState(
            ledger_issue=7, reason="x", expires_at=_iso_in(1), spent=True
        ),
    )

    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines.count(str(emergency.SENTINEL_RELPATH)) == 1


# --- _cmd_start -------------------------------------------------------------


def test_cmd_start_arms_on_ledger_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful ledger-issue creation writes the sentinel and returns 0."""
    fake, calls = _fake_gh([FakeProc(stdout="https://github.com/o/r/issues/17\n")])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_start(tmp_path, "prod is down", 4.0)

    assert rc == 0
    state = emergency.read_state(tmp_path)
    assert state is not None
    assert state.ledger_issue == 17
    assert state.reason == "prod is down"
    assert state.spent is False
    hours = (
        datetime.fromisoformat(state.expires_at) - datetime.now(UTC)
    ).total_seconds() / 3600
    assert hours == pytest.approx(4.0, abs=0.01)
    assert "ARMED" in capsys.readouterr().out
    assert calls[0][:2] == ("issue", "create")


def test_cmd_start_refuses_when_already_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-armed sentinel refuses `start` without any `gh` call."""
    emergency.write_state(
        tmp_path,
        emergency.EmergencyState(ledger_issue=1, reason="first", expires_at=_iso_in(1)),
    )
    fake, calls = _fake_gh([])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_start(tmp_path, "second", 4.0)

    assert rc == 1
    assert calls == []


def test_cmd_start_writes_no_sentinel_when_ledger_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing `gh issue create` leaves no sentinel — no ledger, no mode."""
    fake, calls = _fake_gh([FakeProc(returncode=1)])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_start(tmp_path, "reason", 4.0)

    assert rc == 1
    assert emergency.read_state(tmp_path) is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("raw_ttl", "expected_hours"),
    [(0.0, 0.1), (-5.0, 0.1), (100.0, 24.0)],
    ids=["zero", "negative", "over_max"],
)
def test_cmd_start_clamps_ttl_to_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_ttl: float,
    expected_hours: float,
) -> None:
    """TTL is clamped into `(0.1, 24]` hours regardless of the raw `--ttl` value.

    Args:
        raw_ttl: The requested ``--ttl`` value.
        expected_hours: The clamped lifetime the sentinel must record.
    """
    fake, _calls = _fake_gh([FakeProc(stdout="https://github.com/o/r/issues/9\n")])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_start(tmp_path, "reason", raw_ttl)

    assert rc == 0
    state = emergency.read_state(tmp_path)
    assert state is not None
    actual_hours = (
        datetime.fromisoformat(state.expires_at) - datetime.now(UTC)
    ).total_seconds() / 3600
    assert actual_hours == pytest.approx(expected_hours, abs=0.01)


# --- _create_ledger_issue ---------------------------------------------------


def test_create_ledger_issue_parses_number_from_url_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue number is parsed from the trailing digits of the `gh` URL stdout."""
    fake, calls = _fake_gh([FakeProc(stdout="https://github.com/o/r/issues/123\n")])
    monkeypatch.setattr(emergency, "_gh", fake)

    assert emergency._create_ledger_issue("reason", _iso_in(1)) == 123
    assert calls[0][:2] == ("issue", "create")


# --- main() ------------------------------------------------------------


def test_main_start_without_reason_exits_via_argparse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing `--reason` raises `SystemExit(2)` — argparse's own gate."""
    with pytest.raises(SystemExit) as exc_info:
        emergency.main(["start"])

    assert exc_info.value.code == 2
    assert "--reason" in capsys.readouterr().err


def test_main_dispatches_status_against_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["status"])` reaches `_cmd_status` via the `repo_root()` seam."""
    monkeypatch.setattr(emergency, "repo_root", lambda: tmp_path)
    emergency.write_state(tmp_path, emergency.EmergencyState(7, "dispatch", _iso_in(1)))

    assert emergency.main(["status"]) == 0
    assert "ARMED" in capsys.readouterr().out


def test_main_dispatches_end_against_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["end"])` with no sentinel reaches `_cmd_end`'s nothing-to-do path."""
    monkeypatch.setattr(emergency, "repo_root", lambda: tmp_path)

    assert emergency.main(["end"]) == 0
    assert "nothing to end" in capsys.readouterr().out


# --- _cmd_status -------------------------------------------------------


@pytest.mark.parametrize(
    ("state_factory", "expected_verdict", "expected_rc"),
    [
        (lambda: emergency.EmergencyState(1, "x", _iso_in(1)), "ARMED", 0),
        (
            lambda: emergency.EmergencyState(1, "x", _iso_in(1), spent=True),
            "SPENT",
            1,
        ),
        (lambda: emergency.EmergencyState(1, "x", _iso_in(-1)), "EXPIRED", 1),
        (None, "not armed", 1),
    ],
    ids=["armed", "spent", "expired", "absent"],
)
def test_cmd_status_reports_verdict_and_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state_factory: Callable[[], emergency.EmergencyState] | None,
    expected_verdict: str,
    expected_rc: int,
) -> None:
    """`status` reports the exact verdict word and exit code for each sentinel shape.

    Args:
        state_factory: Builds the sentinel state to write, or ``None`` for
            no sentinel.
        expected_verdict: Verdict word the status line must carry.
        expected_rc: Exit code the verdict maps to.
    """
    if state_factory is not None:
        emergency.write_state(tmp_path, state_factory())

    rc = emergency._cmd_status(tmp_path)

    assert rc == expected_rc
    assert expected_verdict in capsys.readouterr().out


# --- _cmd_consume ------------------------------------------------------


def test_cmd_consume_spends_armed_state_and_comments_head_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consuming an armed sentinel marks it spent and comments the ledger with HEAD.

    Uses a real ephemeral repo (`init_git_repo`) for the bare
    `git rev-parse HEAD` shelled out directly inside `_cmd_consume` — that
    call bypasses the `_gh` seam entirely, so a fake `gh` alone can't cover
    it.
    """
    init_git_repo(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    emergency.write_state(
        tmp_path,
        emergency.EmergencyState(ledger_issue=5, reason="x", expires_at=_iso_in(1)),
    )
    fake, calls = _fake_gh([FakeProc()])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_consume(tmp_path)

    assert rc == 0
    state = emergency.read_state(tmp_path)
    assert state is not None
    assert state.spent is True
    assert calls[0][:3] == ("issue", "comment", "5")
    assert head in calls[0][-1]


def test_cmd_consume_refuses_when_nothing_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sentinel at all refuses `consume` without touching `gh`."""
    fake, calls = _fake_gh([])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_consume(tmp_path)

    assert rc == 1
    assert calls == []


def test_cmd_consume_refuses_expired_leaves_sentinel_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired sentinel refuses `consume` and is not rewritten."""
    state = emergency.EmergencyState(ledger_issue=5, reason="x", expires_at=_iso_in(-1))
    emergency.write_state(tmp_path, state)
    fake, calls = _fake_gh([])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_consume(tmp_path)

    assert rc == 1
    assert calls == []
    assert emergency.read_state(tmp_path) == state


def test_cmd_consume_refuses_already_spent_without_recommenting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-spent sentinel refuses `consume` — no second ledger comment."""
    state = emergency.EmergencyState(
        ledger_issue=5, reason="x", expires_at=_iso_in(1), spent=True
    )
    emergency.write_state(tmp_path, state)
    fake, calls = _fake_gh([])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_consume(tmp_path)

    assert rc == 1
    assert calls == []
    assert emergency.read_state(tmp_path) == state


# --- _repayment_evidence ----------------------------------------------------


def test_repayment_evidence_returns_true_when_head_prefixed_by_verified_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR whose head is prefixed by its newest `verified-at:` SHA is repaid."""
    issue_stdout = "Bypass consumed at `abc123`\nPR #99 opened\n"
    pr_stdout = json.dumps(
        {
            "headRefOid": "deadbeefcafe",
            "comments": [{"body": "verified-at: deadbee wrap-up"}],
        }
    )
    fake, _calls = _fake_gh([FakeProc(stdout=issue_stdout), FakeProc(stdout=pr_stdout)])
    monkeypatch.setattr(emergency, "_gh", fake)

    assert emergency._repayment_evidence(5) == (99, True)


def test_repayment_evidence_returns_none_false_on_issue_view_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing `gh issue view` degrades to `(None, False)`, no `pr view` attempted."""
    fake, calls = _fake_gh([FakeProc(returncode=1)])
    monkeypatch.setattr(emergency, "_gh", fake)

    assert emergency._repayment_evidence(5) == (None, False)
    assert len(calls) == 1


def test_repayment_evidence_returns_none_false_when_no_pr_token_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PR #N` token in the ledger comments degrades to `(None, False)`."""
    fake, calls = _fake_gh([FakeProc(stdout="just chatting, no pr reference\n")])
    monkeypatch.setattr(emergency, "_gh", fake)

    assert emergency._repayment_evidence(5) == (None, False)
    assert len(calls) == 1


def test_repayment_evidence_keeps_pr_number_on_pr_view_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved PR number survives a failing `gh pr view` as `(pr, False)`."""
    fake, _calls = _fake_gh(
        [FakeProc(stdout="PR #42 opened\n"), FakeProc(returncode=1)]
    )
    monkeypatch.setattr(emergency, "_gh", fake)

    assert emergency._repayment_evidence(5) == (42, False)


def test_repayment_evidence_keeps_pr_number_on_invalid_pr_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unparseable `gh pr view` JSON keeps the PR number, still unrepaid."""
    fake, _calls = _fake_gh(
        [FakeProc(stdout="PR #42 opened\n"), FakeProc(stdout="not json")]
    )
    monkeypatch.setattr(emergency, "_gh", fake)

    assert emergency._repayment_evidence(5) == (42, False)


def test_repayment_evidence_last_pr_token_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Among several `PR #N` tokens (edits, reopens), the last one posted wins."""
    issue_stdout = "PR #10 opened\nsome update\nPR #20 reopened after edits\n"
    pr_stdout = json.dumps({"headRefOid": "abc", "comments": []})
    fake, calls = _fake_gh([FakeProc(stdout=issue_stdout), FakeProc(stdout=pr_stdout)])
    monkeypatch.setattr(emergency, "_gh", fake)

    pr_number, _repaid = emergency._repayment_evidence(5)

    assert pr_number == 20
    assert calls[1][:3] == ("pr", "view", "20")


def test_repayment_evidence_returns_false_when_head_not_prefixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `verified-at:` SHA that does not prefix the PR's head is not repaid."""
    issue_stdout = "PR #7 opened\n"
    pr_stdout = json.dumps(
        {
            "headRefOid": "1234567890",
            "comments": [{"body": "verified-at: ffffff wrap-up"}],
        }
    )
    fake, _calls = _fake_gh([FakeProc(stdout=issue_stdout), FakeProc(stdout=pr_stdout)])
    monkeypatch.setattr(emergency, "_gh", fake)

    assert emergency._repayment_evidence(5) == (7, False)


# --- _cmd_end ------------------------------------------------------------


def test_cmd_end_returns_zero_when_no_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sentinel at all is a no-op success — nothing to end, no `gh` call."""
    fake, calls = _fake_gh([])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_end(tmp_path)

    assert rc == 0
    assert calls == []


def test_cmd_end_reports_debt_and_keeps_sentinel_when_no_pr_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PR recorded on the ledger reports outstanding debt and keeps the sentinel."""
    state = emergency.EmergencyState(ledger_issue=5, reason="x", expires_at=_iso_in(1))
    emergency.write_state(tmp_path, state)
    fake, _calls = _fake_gh([FakeProc(stdout="no pr reference here\n")])
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_end(tmp_path)

    assert rc == 1
    assert emergency.read_state(tmp_path) == state


def test_cmd_end_reports_debt_and_keeps_sentinel_when_pr_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded but unverified PR reports outstanding debt and keeps the sentinel."""
    state = emergency.EmergencyState(ledger_issue=5, reason="x", expires_at=_iso_in(1))
    emergency.write_state(tmp_path, state)
    pr_stdout = json.dumps({"headRefOid": "abcdef1234", "comments": []})
    fake, calls = _fake_gh(
        [FakeProc(stdout="PR #9 opened\n"), FakeProc(stdout=pr_stdout)]
    )
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_end(tmp_path)

    assert rc == 1
    assert emergency.read_state(tmp_path) == state
    assert len(calls) == 2  # no comment/close attempted on outstanding debt


def test_cmd_end_closes_ledger_and_removes_sentinel_when_repaid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repaid PR closes the ledger (comment THEN close) and unlinks the sentinel."""
    state = emergency.EmergencyState(ledger_issue=5, reason="x", expires_at=_iso_in(1))
    emergency.write_state(tmp_path, state)
    pr_stdout = json.dumps(
        {
            "headRefOid": "deadbeefcafe",
            "comments": [{"body": "verified-at: deadbee wrap-up"}],
        }
    )
    fake, calls = _fake_gh(
        [
            FakeProc(stdout="PR #9 opened\n"),
            FakeProc(stdout=pr_stdout),
            FakeProc(),
            FakeProc(),
        ]
    )
    monkeypatch.setattr(emergency, "_gh", fake)

    rc = emergency._cmd_end(tmp_path)

    assert rc == 0
    assert not (tmp_path / emergency.SENTINEL_RELPATH).exists()
    assert calls[2][:2] == ("issue", "comment")
    assert calls[3][:2] == ("issue", "close")
