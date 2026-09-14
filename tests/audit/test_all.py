"""Tests for ``forge.audit.all`` — the audit orchestrator."""

# MOCKING STRATEGY: no sub-audit CLI actually runs — the orchestration logic is
# exercised in isolation.
#   - subprocess.run: replaced by `fake_run` closures returning the canonical
#     `FakeProc` (returncode/stdout/stderr) so no child process spawns. Since
#     this patches the shared `subprocess` module (not just `audit_all`'s own
#     reference), it also intercepts the real `git` calls `main()` makes via
#     `git_utils.produced_at_stamp` when stamping the summary log (#538) — the
#     subprocess-faking `main()` tests below discriminate on `cmd[0]` so those
#     `git` calls degrade to a failing fake (stamp reads `tree=unknown`)
#     instead of being mistaken for a sub-audit CLI invocation.
#   - _run_one: replaced directly in the real-stamp test
#     (test_main_summary_log_stamps_real_tree) so subprocess.run stays real
#     and produced_at_stamp reaches actual git instead of degrading.
#   - repo_root / require_cli: stubbed to a tmp_path and a no-op so the run
#     neither touches the real repo nor enforces CLI presence.
#   - git_utils.repo_root.cache_clear: no-op'd to avoid clearing the real cache.
#   - patch(sys.argv): drives main()'s argument parsing (--only, defaults).

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from forge import git_utils
from forge.audit import all as audit_all
from forge.audit import common as audit_common
from forge.audit.common import Finding, Severity, write_log
from tests.conftest import PRODUCED_AT_RE, FakeProc, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_read_finding_count_parses_header() -> None:
    """A ``# findings: N`` header line yields the integer."""
    text = "# audit\n# findings: 7\n# generated: ...\n"
    assert audit_all._read_finding_count(text) == 7


def test_read_finding_count_survives_real_write_log_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_read_finding_count` still parses a log stamped by the real `write_log`.

    BEHAVIOR: pins that the `# findings: N` header this orchestrator
    depends on survives `write_log` prepending the `# produced-at:`
    provenance stamp (FOUNDATION §13) as line 1 — displacing the
    `# forge-audit-<name>` header the count used to follow directly.
    `_read_finding_count` scans the first 10 lines rather than a fixed
    offset, so this is a real-git regression pin, not a rewrite of the
    parser's own contract (already covered by the header-parsing tests
    above).
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(audit_common, "repo_root", lambda: tmp_path)
    finding = Finding(
        audit="dup", severity=Severity.HIGH, path="a.py", line=1, message="m"
    )
    path = write_log("dup", [finding], summary="one duplicate")

    assert audit_all._read_finding_count(path.read_text(encoding="utf-8")) == 1


def test_read_finding_count_missing_returns_minus_one() -> None:
    """Missing header returns ``-1`` (sentinel for 'unknown')."""
    assert audit_all._read_finding_count("no header here\n") == -1


def test_read_finding_count_invalid_returns_minus_one() -> None:
    """Non-integer findings value returns ``-1``."""
    assert audit_all._read_finding_count("# findings: oops\n") == -1


def test_render_summary_contains_each_subaudit(tmp_path: Path) -> None:
    """The rendered summary lists every result with its exit code and log path."""
    del tmp_path
    results = [
        audit_all.SubResult(
            name="dup",
            exit_code=0,
            log_path="code_health/audit_dup.log",
            finding_count=3,
        ),
        audit_all.SubResult(
            name="deps",
            exit_code=1,
            log_path="code_health/audit_deps.log",
            finding_count=-1,
        ),
    ]
    text = audit_all._render_summary(results)
    assert "dup" in text
    assert "deps" in text
    assert "3" in text  # finding count
    assert "n/a" in text  # for the -1 sentinel
    assert "code_health/audit_dup.log" in text
    assert "# subaudits: 2" in text


def test_main_invokes_every_selected_subaudit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main()`` calls one sub-audit per name in ``SUB_AUDITS`` by default."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("forge.git_utils.repo_root.cache_clear", lambda: None)
    monkeypatch.setattr(audit_all, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        audit_all,
        "require_cli",
        lambda *_a, **_kw: None,
    )

    invoked: list[str] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        """Record sub-audit invocations; degrade everything else to failure.

        Monkeypatching ``audit_all.subprocess.run`` replaces the shared
        ``subprocess`` module's ``run`` attribute, so it also intercepts
        the real ``git`` calls ``main()`` now makes via
        ``git_utils.produced_at_stamp`` when stamping the summary log
        (#538) — those must not be mistaken for a sub-audit CLI call.

        Args:
            cmd: Command argv (first element is recorded when it names a
                ``forge-audit-*`` sub-audit CLI).
            **_kwargs: Ignored keyword arguments.

        Returns:
            A fake process with returncode 0 for a recorded sub-audit
            invocation; a failing fake for anything else (the stamp's
            ``git`` calls), so the stamp degrades to ``tree=unknown``
            instead of the fake being read as a real git response.
        """
        if cmd[0].startswith("forge-audit-"):
            invoked.append(cmd[0])
            return FakeProc(returncode=0)
        return FakeProc(returncode=1)

    monkeypatch.setattr(audit_all.subprocess, "run", _fake_run)
    with patch("sys.argv", ["forge-audit-all"]):
        rc = audit_all.main()

    assert rc == 0
    expected_calls = [f"forge-audit-{n}" for n in audit_all.SUB_AUDITS]
    assert invoked == expected_calls
    summary = (tmp_path / "code_health" / "audit_summary.log").read_text()
    assert "# forge-audit-all" in summary
    assert "# subaudits:" in summary


def test_main_only_filters_subaudits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--only dup deps`` runs only those two sub-audits."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(audit_all, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(audit_all, "require_cli", lambda *_a, **_kw: None)

    invoked: list[str] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        """Record sub-audit invocations; degrade everything else to failure.

        Same passthrough concern as `test_main_invokes_every_selected_subaudit`
        above — `produced_at_stamp`'s `git` calls must not be recorded as
        sub-audit invocations.

        Args:
            cmd: Command argv (first element is recorded when it names a
                ``forge-audit-*`` sub-audit CLI).
            **_kwargs: Ignored keyword arguments.

        Returns:
            A fake process with returncode 0 for a recorded sub-audit
            invocation; a failing fake for anything else.
        """
        if cmd[0].startswith("forge-audit-"):
            invoked.append(cmd[0])
            return FakeProc(returncode=0)
        return FakeProc(returncode=1)

    monkeypatch.setattr(audit_all.subprocess, "run", _fake_run)
    with patch("sys.argv", ["forge-audit-all", "--only", "dup", "deps"]):
        rc = audit_all.main()

    assert rc == 0
    assert invoked == ["forge-audit-dup", "forge-audit-deps"]


def test_main_returns_max_subaudit_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main()`` returns the maximum exit code across sub-audits."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(audit_all, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(audit_all, "require_cli", lambda *_a, **_kw: None)

    codes = iter([0, 2, 1])

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        """Return the next exit code for a sub-audit call; fail everything else.

        Same passthrough concern as the other `main()` tests above:
        `produced_at_stamp`'s `git` calls must not consume an entry from
        `codes` — only the three sub-audit invocations this test drives
        should.

        Args:
            cmd: Command argv (first element decides whether this call
                consumes the next canned exit code).
            **_kwargs: Ignored keyword arguments.

        Returns:
            A fake process with the next returncode from the iterator for
            a ``forge-audit-*`` call; a failing fake for anything else.
        """
        if cmd[0].startswith("forge-audit-"):
            return FakeProc(returncode=next(codes))
        return FakeProc(returncode=1)

    monkeypatch.setattr(audit_all.subprocess, "run", _fake_run)
    with patch("sys.argv", ["forge-audit-all", "--only", "dup", "deps", "orphans"]):
        rc = audit_all.main()

    assert rc == 2


def test_main_summary_log_stamps_real_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`audit_summary.log`'s line 1 names the repo's real clean working tree.

    Unlike the `main()` tests above — which fake `subprocess.run` and so
    read `tree=unknown` from the stamp's own degraded `git` calls — this
    one leaves `subprocess.run` real and replaces `_run_one` directly, so
    `produced_at_stamp` reaches actual git and the stamp can be checked
    against the repo's true tree, not just its shape.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(audit_all, "repo_root", lambda: tmp_path)

    def _fake_run_one(
        name: str, _scope: str, _roots: list[str] | None
    ) -> audit_all.SubResult:
        """Return a canned success without invoking any sub-audit CLI.

        Args:
            name: Audit step name.
            _scope: Audit scope (unused).
            _roots: Root directories to search (unused).

        Returns:
            A SubResult with zero findings.
        """
        return audit_all.SubResult(
            name=name,
            exit_code=0,
            log_path=f"code_health/audit_{name}.log",
            finding_count=0,
        )

    monkeypatch.setattr(audit_all, "_run_one", _fake_run_one)
    with patch("sys.argv", ["forge-audit-all"]):
        rc = audit_all.main()

    assert rc == 0
    lines = (
        (tmp_path / "code_health" / "audit_summary.log")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    match = PRODUCED_AT_RE.fullmatch(lines[0])
    assert match is not None
    assert match["tree"] == git_utils.get_tree_sha(tmp_path, "HEAD")
    assert lines[1] == "# forge-audit-all"
