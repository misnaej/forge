"""Tests for forge.pr_create — publish a PR from the branch it verifies.

# MOCKING STRATEGY: every scenario runs against a real ephemeral git repo
# (``tests.conftest.init_git_repo``) so the branch/HEAD the gate reads are
# real git state, not asserted text — that is the whole point of the
# module (FOUNDATION §12, module docstring). ``subprocess.run`` is
# replaced with a thin dispatcher that forwards anything but a ``gh``
# argv to the real implementation and records ``gh`` argvs instead of
# executing them — the seam is which binary the argv names, never the
# whole subprocess surface, so `git` calls the gate depends on
# (``rev-parse``, ``branch --show-current``) still run for real.
# `classify` is patched at the `forge.pr_create` namespace (FOUNDATION §5)
# with a plain lambda returning a canned verdict — the classifier has its
# own test module; only the gate's dispatch on its result is under test
# here. The emergency sentinel is NOT faked: tests write a real sentinel
# (claims live in the sentinel) in the ephemeral repo, so
# claim/commit/release run for real and
# only the `gh` binary is scripted. The CVE-scan gate is not faked either:
# `_run_main` writes a real `pip_audit.json` sidecar stamped now (after
# every commit the test made), and the scan-gate cases write their own
# stamp or none, so the fork-point lookup and the sidecar read run for real.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from forge import emergency, pip_audit_json, pr_create
from forge.emergency_state import armed_state
from forge.git_utils import code_health_dir
from tests.conftest import GIT_ENV, commit_all, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


def _head_sha(repo: Path) -> str:
    """Return *repo*'s current HEAD commit's full sha.

    Args:
        repo: Git checkout to read HEAD from.

    Returns:
        The full HEAD sha.
    """
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write_wrapup(repo: Path, sha: str, *, mode_line: str | None = None) -> None:
    """Write a `code_health/pr_wrapup.md` naming *sha*, optionally with a mode line.

    Args:
        repo: Repo root to write under — matches `WRAPUP_NAME`'s
            repo-relative resolution.
        sha: Commit sha (full or short) to embed in the `verified-at:` line.
        mode_line: Optional `wrapup-mode: ...` header line to append.
    """
    code_health = code_health_dir(repo)
    code_health.mkdir(parents=True, exist_ok=True)
    lines = ["# PR Wrap-up", "", f"verified-at: {sha}"]
    if mode_line is not None:
        lines.append(mode_line)
    (code_health / "pr_wrapup.md").write_text("\n".join(lines) + "\n")


def _stub_gh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Route `gh` argvs to a recorder while leaving every other command real.

    Patches the shared `subprocess` module object (FOUNDATION §5 — the
    namespace `forge.pr_create` looks `run` up in), so both `pr_create`'s
    own `gh pr create` call and every `git` call the gate makes through
    `forge.git_utils` pass through the same dispatcher. Only a `gh` argv
    is intercepted; a stray real invocation would mean the gate reached
    publication when a test expected it to refuse first.

    Args:
            to the calling test.

    Returns:
        The list `gh` argvs are appended to, in call order.
    """
    real_run = subprocess.run
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "gh":
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pr_create.subprocess, "run", _fake_run)
    return calls


def _write_scan(repo: Path, scanned_at: float) -> None:
    """Write a real pip-audit sidecar recording a scan at *scanned_at*.

    Args:
        repo: Repo whose `code_health/` receives the sidecar.
        scanned_at: Epoch seconds the scan is recorded as having run.
    """
    sidecar = code_health_dir(repo) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"dependencies": [], pip_audit_json.SCANNED_AT_KEY: scanned_at})
    )


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    *extra_args: str,
    fresh_scan: bool = True,
) -> int:
    """Point `forge-pr-create` at *repo* and run it with a minimal argv.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        repo: Checkout `repo_root()` should resolve to.
        *extra_args: Additional argv tokens appended after the required
            `--base`/`--title`/`--body-file` trio.
        fresh_scan: Record a CVE scan made now, after every commit the test
            built, so the scan-freshness gate passes and the case under
            test is what decides. ``False`` leaves the sidecar as the test
            wrote it.

    Returns:
        `pr_create.main()`'s exit code.
    """
    if fresh_scan:
        _write_scan(repo, time.time())
    monkeypatch.setattr(pr_create, "repo_root", lambda: repo)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "forge-pr-create",
            "--base",
            "main",
            "--title",
            "t",
            "--body-file",
            "body.md",
            *extra_args,
        ],
    )
    return pr_create.main()


def test_pr_create_publishes_when_wrapup_names_own_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wrap-up naming the checkout's own HEAD lets the CLI publish it.

    Regression bound for the module's whole reason to exist: the branch
    published is read from the checkout the process stands in, never
    inferred from anything textual. Asserting the recorded `gh` call's
    `--head` equals the checkout's real branch name is the load-bearing
    check — a regression back to text-inference could still exit 0 while
    naming the wrong branch.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/publish-me"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "file.txt").write_text("x")
    commit_all(repo, "add file")
    _write_wrapup(repo, _head_sha(repo))
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 0
    assert len(calls) == 1
    head_index = calls[0].index("--head")
    assert calls[0][head_index + 1] == "feature/publish-me"


def _publishable_repo(tmp_path: Path) -> Path:
    """Build a checkout whose wrap-up verifies its own HEAD.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The checkout root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo))
    return repo


def test_pr_create_warns_when_the_body_closes_an_unlisted_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A close keyword inside a sentence warns, and publication still proceeds."""
    repo = _publishable_repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / "body.md").write_text("Closes #508, closes #504's doctor half.\n")
    calls = _stub_gh(monkeypatch)

    with caplog.at_level("WARNING", logger=pr_create.__name__):
        assert _run_main(monkeypatch, repo) == 0

    assert len(calls) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "#508, #504" in warnings[0]
    assert "Part of #N" in warnings[0]


def test_pr_create_publishes_quietly_when_the_body_file_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A body file it cannot read is left to `gh`: no warning, no refusal."""
    repo = _publishable_repo(tmp_path)
    monkeypatch.chdir(repo)
    calls = _stub_gh(monkeypatch)

    with caplog.at_level("WARNING", logger=pr_create.__name__):
        assert _run_main(monkeypatch, repo) == 0

    assert len(calls) == 1
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_pr_create_refuses_without_a_wrapup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No authored wrap-up at all refuses publication."""
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


def test_pr_create_refuses_wrapup_naming_a_different_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wrap-up whose `verified-at:` names a commit other than HEAD refuses.

    Distinguishes "wrong tree verified" from "nothing verified" — the
    wrap-up exists here, so a gate that only checked file presence would
    wrongly allow this.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, "0" * 40)
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


def test_pr_create_refuses_light_wrapup_when_classifier_disagrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `wrapup-mode: light` wrap-up refuses when the re-run classifier disagrees.

    The light escape is earned at publish time, not asserted by the
    wrap-up's author — a diff that grew past light-code between authoring
    and publishing must not slip through on a stale self-declaration.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line="wrapup-mode: light")
    monkeypatch.setattr(
        pr_create, "classify", lambda _root, _base, _pr: SimpleNamespace(mode="full")
    )
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


@pytest.mark.parametrize(
    ("declared", "classified"),
    [("light", "light-code"), ("light-release", "light-release")],
)
def test_pr_create_publishes_light_wrapup_when_classifier_agrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, declared: str, classified: str
) -> None:
    """A reporter-skipping wrap-up publishes when the classifier still agrees.

    SCENARIO: complement of the disagreement cases — the re-check can say
        yes, so it is a real gate rather than one that only ever refuses.
    MOCK SETUP: `pr_create.classify` returns *classified*.
    EXPECTED BEHAVIOR: exit 0 and exactly one `gh` call.

    Args:
        declared: The `wrapup-mode:` the wrap-up carries.
        classified: The mode the classifier reports.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line=f"wrapup-mode: {declared}")
    monkeypatch.setattr(
        pr_create,
        "classify",
        lambda _root, _base, _pr: SimpleNamespace(mode=classified),
    )
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 0
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("declared", "classified"),
    [("light", "light-release"), ("light-release", "full")],
)
def test_pr_create_refuses_wrapup_mode_earned_by_the_other_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    declared: str,
    classified: str,
) -> None:
    """Each declared mode needs its own classifier verdict, not any light one.

    SCENARIO: the classifier reads a different reporter-skipping mode than
        the wrap-up declares.
    MOCK SETUP: `pr_create.classify` returns *classified*.
    EXPECTED BEHAVIOR: exit 2, nothing published.

    Args:
        declared: The wrap-up's declared mode.
        classified: The mode the classifier returns instead.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line=f"wrapup-mode: {declared}")
    monkeypatch.setattr(
        pr_create,
        "classify",
        lambda _root, _base, _pr: SimpleNamespace(mode=classified),
    )
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


def test_pr_create_refuses_light_release_wrapup_when_classifier_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A classifier crash must not let a light-release wrap-up through.

    SCENARIO: the re-check itself fails; the gate fails closed.
    MOCK SETUP: `pr_create.classify` raises; `gh` is recorded.
    EXPECTED BEHAVIOR: exit 2, nothing published.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line="wrapup-mode: light-release")

    def _boom(*_a: object, **_k: object) -> None:
        msg = "classifier exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(pr_create, "classify", _boom)
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


@pytest.mark.parametrize("configured_base", [None, "other"])
def test_pr_create_refuses_light_release_wrapup_without_agreeing_base_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured_base: str | None
) -> None:
    """The escape is judged against the configured base, so it must resolve and match.

    SCENARIO: `[tool.forge] base_branch` is unset, or differs from `--base`.
    MOCK SETUP: `pr_create.load_config` returns *configured_base*; the
        classifier would say `light-release`, so only the base check refuses.
    EXPECTED BEHAVIOR: exit 2, nothing published.

    Args:
        configured_base: The configured base branch, or `None` when unset.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line="wrapup-mode: light-release")
    monkeypatch.setattr(
        pr_create,
        "load_config",
        lambda _root: SimpleNamespace(base_branch=configured_base),
    )
    monkeypatch.setattr(
        pr_create,
        "classify",
        lambda _root, _base, _pr: SimpleNamespace(mode="light-release"),
    )
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


def test_pr_create_refuses_emergency_wrapup_when_sentinel_not_armed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `wrapup-mode: emergency` wrap-up refuses when no bypass is armed.

    Traceability (the HEAD match) is never deferred, only verification —
    an emergency declaration still needs a spendable sentinel behind it,
    not just the header line. No sentinel exists in the repo, so the real
    claim refuses.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line="wrapup-mode: emergency")
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


def _armed_emergency_repo(tmp_path: Path) -> Path:
    """Build a checkout with an emergency wrap-up and an armed sentinel.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The checkout root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line="wrapup-mode: emergency")
    emergency.write_state(
        repo,
        emergency.EmergencyState(
            ledger_issue=5,
            reason="x",
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        ),
    )
    return repo


def _script_gh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create: subprocess.CompletedProcess[str] | OSError,
    listing: subprocess.CompletedProcess[str] | None = None,
) -> list[list[str]]:
    """Script `gh pr create` and `gh pr list` replies; other `gh` calls succeed.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        create: Reply to `gh pr create` — a completed process, or an
            ``OSError`` raised as if the binary could not run.
        listing: Reply to `gh pr list`; ``None`` fails the test if called.

    Returns:
        Every `gh` argv, in call order.
    """
    real_run = subprocess.run
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] != "gh":
            return real_run(cmd, **kwargs)  # type: ignore[arg-type]
        calls.append(cmd)
        if cmd[1:3] == ["pr", "create"]:
            if isinstance(create, OSError):
                raise create
            return create
        if cmd[1:3] == ["pr", "list"]:
            assert listing is not None, "gh pr list must not be called"
            return listing
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(pr_create.subprocess, "run", _fake_run)
    return calls


def _reply(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Build a canned completed `gh` process.

    Args:
        returncode: Exit status.
        stdout: Captured standard output.

    Returns:
        The completed process.
    """
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_emergency_publish_success_spends_with_pr_number_from_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A created PR commits the bypass with the number parsed from its URL.

    SCENARIO: armed sentinel, emergency wrap-up, `gh pr create` prints a PR URL.
    MOCK SETUP: scripted `gh`; `pr list` is forbidden (must not be probed).
    EXPECTED BEHAVIOR: exit 0, sentinel spent with the PR number, one
    ledger comment, no `gh pr list`.
    """
    repo = _armed_emergency_repo(tmp_path)
    calls = _script_gh(
        monkeypatch, create=_reply(0, "https://github.com/o/r/pull/31\n")
    )

    assert _run_main(monkeypatch, repo) == 0

    state = emergency.read_state(repo)
    assert state is not None
    assert state.spent is True
    assert state.pr_number == 31
    assert not [c for c in calls if c[1:3] == ["pr", "list"]]
    assert len([c for c in calls if c[1:3] == ["issue", "comment"]]) == 1


def test_emergency_publish_failure_with_no_pr_releases_the_bypass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed create with no PR for the branch hands the bypass back for a retry.

    SCENARIO: `gh pr create` fails; `gh pr list` returns `[]`.
    MOCK SETUP: scripted `gh` replies.
    EXPECTED BEHAVIOR: exit 2, still armed, no claim recorded, the probe
    asked for open PRs only, and a retry claim succeeds.
    """
    repo = _armed_emergency_repo(tmp_path)
    calls = _script_gh(monkeypatch, create=_reply(1), listing=_reply(0, "[]"))

    assert _run_main(monkeypatch, repo) == 2

    assert armed_state(repo) is not None
    probe = next(c for c in calls if c[1:3] == ["pr", "list"])
    assert probe[probe.index("--state") + 1] == "open"
    assert emergency.claim(repo, "fix/x") == 0


def test_emergency_publish_reported_success_without_url_never_releases_on_empty_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A zero-exit create is never handed back, even when the open list is empty.

    SCENARIO: `gh pr create` exits 0 with no URL; `gh pr list` returns `[]`
    (the listing may simply lag the creation).
    MOCK SETUP: scripted `gh` replies.
    EXPECTED BEHAVIOR: exit 0, the claim is held (not armed, not spent).
    """
    repo = _armed_emergency_repo(tmp_path)
    _script_gh(
        monkeypatch, create=_reply(0, "something odd\n"), listing=_reply(0, "[]")
    )

    assert _run_main(monkeypatch, repo) == 0

    state = emergency.read_state(repo)
    assert state is not None
    assert state.spent is False
    assert state.claimed_branch is not None
    assert armed_state(repo) is None


@pytest.mark.parametrize(
    "listing",
    [_reply(0, '[{"number": 44}]'), _reply(1)],
    ids=["pr-found", "probe-errors"],
)
def test_emergency_publish_with_unclear_outcome_holds_the_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    listing: subprocess.CompletedProcess[str],
) -> None:
    """When a PR may exist the claim is held and recovery is named, never guessed.

    SCENARIO: `gh pr create` fails, then `gh pr list` reports a PR or errors.
    MOCK SETUP: scripted `gh` replies.
    EXPECTED BEHAVIOR: exit 2, claim kept, sentinel unspent, log carries
    the recovery hint, and a second claim is refused.

    Args:
        listing: The `gh pr list` reply.
    """
    repo = _armed_emergency_repo(tmp_path)
    _script_gh(monkeypatch, create=_reply(1), listing=listing)

    with caplog.at_level("ERROR", logger=pr_create.__name__):
        assert _run_main(monkeypatch, repo) == 2

    state = emergency.read_state(repo)
    assert state is not None
    assert state.spent is False
    assert state.claimed_branch is not None
    assert any(emergency.RECOVERY_HINT in r.getMessage() for r in caplog.records)
    assert emergency.claim(repo, "fix/x") == 1


def test_emergency_publish_releases_the_bypass_when_gh_cannot_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If `gh` never ran, no PR can exist: the bypass is released, still armed.

    SCENARIO: running `gh pr create` raises `OSError` (binary missing).
    MOCK SETUP: scripted `gh` raising from `subprocess.run`.
    EXPECTED BEHAVIOR: exit 2, sentinel still armed (claim released).
    """
    repo = _armed_emergency_repo(tmp_path)
    _script_gh(monkeypatch, create=FileNotFoundError("gh"))

    assert _run_main(monkeypatch, repo) == 2

    assert armed_state(repo) is not None


def test_emergency_publish_with_no_parseable_url_holds_the_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A zero-exit create with no PR URL is unclear: the claim is held, not guessed.

    SCENARIO: `gh pr create` exits 0 but prints text with no PR URL.
    MOCK SETUP: scripted `gh`; `pr list` finds one open PR (the probe now
    runs on this path too, to name candidates).
    EXPECTED BEHAVIOR: exit 0, claim kept, sentinel unspent, log carries
    the recovery hint and the candidate PR number.
    """
    repo = _armed_emergency_repo(tmp_path)
    _script_gh(
        monkeypatch,
        create=_reply(0, "something odd\n"),
        listing=_reply(0, '[{"number": 44}]'),
    )

    with caplog.at_level("ERROR", logger=pr_create.__name__):
        assert _run_main(monkeypatch, repo) == 0

    state = emergency.read_state(repo)
    assert state is not None
    assert state.spent is False
    assert state.claimed_branch is not None
    messages = [r.getMessage() for r in caplog.records]
    assert any(emergency.RECOVERY_HINT in m and "#44" in m for m in messages)


@pytest.mark.parametrize(
    "refusal", ["passthrough", "wrapup-names-other-sha", "no-cve-scan"]
)
def test_emergency_refusal_before_publish_never_claims_the_bypass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, refusal: str
) -> None:
    """A refusal that precedes publication leaves the armed bypass untouched.

    SCENARIO: armed sentinel, then a disallowed passthrough flag, a
    wrap-up naming a different commit, or no CVE scan on the branch — an
    emergency defers the review ceremony, never the scan's freshness.
    MOCK SETUP: scripted `gh`; any `gh` call is recorded.
    EXPECTED BEHAVIOR: exit 2, `gh` never called, no claim, still armed.

    Args:
        refusal: Which pre-publication refusal to trigger.
    """
    repo = _armed_emergency_repo(tmp_path)
    calls = _script_gh(monkeypatch, create=_reply(0, "https://github.com/o/r/pull/9\n"))
    extra: list[str] = []
    if refusal == "passthrough":
        extra = ["--", "--fill"]
    elif refusal == "wrapup-names-other-sha":
        _write_wrapup(repo, "0" * 40, mode_line="wrapup-mode: emergency")

    fresh_scan = refusal != "no-cve-scan"
    assert _run_main(monkeypatch, repo, *extra, fresh_scan=fresh_scan) == 2

    assert calls == []
    assert not (repo / emergency._CLAIM_LOCK).exists()
    assert armed_state(repo) is not None


def test_emergency_publish_reports_a_spend_that_could_not_be_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A PR created while the claim vanished is reported, not announced as consumed.

    SCENARIO: `gh pr create` succeeds with a PR URL, but the sentinel no
    longer holds the claim by the time the spend is recorded.
    MOCK SETUP: scripted `gh`; the `pr create` reply releases the claim as
    a side effect, i.e. after it was taken.
    EXPECTED BEHAVIOR: exit 0, error log names `forge-emergency status`, no
    "consumed by PR" warning.
    """
    repo = _armed_emergency_repo(tmp_path)
    _script_gh(monkeypatch, create=_reply(0, "https://github.com/o/r/pull/31\n"))
    scripted_run = pr_create.subprocess.run

    def _run_dropping_claim(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        result = scripted_run(cmd, **kwargs)  # type: ignore[arg-type]
        if cmd[1:3] == ["pr", "create"]:
            emergency.release(repo)
        return result

    monkeypatch.setattr(pr_create.subprocess, "run", _run_dropping_claim)

    with caplog.at_level("WARNING", logger=pr_create.__name__):
        assert _run_main(monkeypatch, repo) == 0

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any(
        "could not be recorded" in m and "forge-emergency status" in m for m in errors
    )
    assert not any("consumed by PR" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "passthrough",
    [
        pytest.param(["--head", "other-branch"], id="separated"),
        pytest.param(["--head=other-branch"], id="equals-joined"),
        pytest.param(["-Hother-branch"], id="attached-short"),
        pytest.param(["--repo", "other/org"], id="owned-flag-repo"),
    ],
)
def test_passthrough_cannot_override_the_verified_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, passthrough: list[str]
) -> None:
    """A passthrough token setting an owned flag refuses, in every form gh accepts.

    The exploit: `gh`'s parser takes the LAST occurrence of a repeated
    flag, so a passthrough `--head other` appended after this command's
    own `--head <verified-branch>` would win — verification would pass
    against the real checkout while the branch actually published is
    whatever the passthrough named. `--base`/`--repo` are owned for the
    same reason (the light escape judged against one base, the PR opened
    against another; the whole publication redirected). Checking both the
    exit code and that `gh` was never invoked matters: a refusal for an
    unrelated reason (e.g. a parser error) could exit 2 without proving
    the passthrough itself was caught.

    Args:
        passthrough: The exploit tokens under test, in every form the
            underlying parser accepts.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/publish-me"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "file.txt").write_text("x")
    commit_all(repo, "add file")
    _write_wrapup(repo, _head_sha(repo))
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo, "--", *passthrough) == 2
    assert calls == []


def test_passthrough_still_allows_flags_the_command_does_not_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ordinary passthrough flag the command doesn't own still reaches `gh`.

    Without this, a filter broad enough to catch the owned-flag exploit
    above could quietly also swallow legitimate passthrough use, and
    nothing would notice — this is the complement that proves the filter
    is scoped to the allowlist, not to "anything after `--`".
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/publish-me"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "file.txt").write_text("x")
    commit_all(repo, "add file")
    _write_wrapup(repo, _head_sha(repo))
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo, "--", "--label", "bug") == 0
    assert len(calls) == 1
    assert "--label" in calls[0]
    assert "bug" in calls[0]


@pytest.mark.parametrize(
    "passthrough",
    [
        pytest.param(["--fill"], id="fill-derives-title-and-body-from-commits"),
        pytest.param(["--web"], id="web-stands-for-any-unallowed-flag"),
    ],
)
def test_passthrough_refuses_an_unrecognised_flag_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, passthrough: list[str]
) -> None:
    """A passthrough flag absent from the allowlist refuses, not just owned flags.

    `--fill` is the concrete case a denylist missed: it derives title and
    body from commits, so it would silently overwrite the verified values
    this command sets itself. `--web` is not owned by this command at all —
    it stands for any future `gh` flag nobody has enumerated. A denylist
    would have forwarded both; the allowlist refuses both by default.
    Checking both the exit code and that `gh` was never invoked matters: a
    refusal for an unrelated reason could exit 2 without proving the
    passthrough itself was caught.

    Args:
        monkeypatch: Pytest fixture for mocking.
        tmp_path: Pytest fixture for temporary directory.
        passthrough: Flag(s) to pass through to gh pr create.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/publish-me"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "file.txt").write_text("x")
    commit_all(repo, "add file")
    _write_wrapup(repo, _head_sha(repo))
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo, "--", *passthrough) == 2
    assert calls == []


def test_only_the_wrap_up_header_verifies_not_a_quoted_stamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `verified-at:` quoted below the header must not stand in for the header's own.

    A wrap-up legitimately quotes an older reporter round's stamp further
    down, naming an earlier commit. The exploit: scanning the whole file
    for any `verified-at:` let that quoted, earlier-round stamp — which
    happens to name the checkout's current HEAD from a prior verification
    pass — satisfy the check even though the wrap-up's OWN header names a
    different (stale) commit. This is the exact input the old shell
    implementation refused and the Python rewrite initially did not, so it
    pins a real regression rather than a hypothetical one.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    stale_sha = _head_sha(repo)
    (repo / "file.txt").write_text("x")
    commit_all(repo, "second commit")
    head_sha = _head_sha(repo)
    code_health = code_health_dir(repo)
    code_health.mkdir(parents=True, exist_ok=True)
    (code_health / "pr_wrapup.md").write_text(
        "\n".join(
            [
                "# PR Wrap-up",
                "",
                f"verified-at: {stale_sha}",
                "",
                "Quoted from an earlier reporter round:",
                f"verified-at: {head_sha}   (PR #1, branch feature/publish-me)",
            ]
        )
        + "\n"
    )
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


# ---------------------------------------------------------------------------
# CVE-scan freshness gate
# ---------------------------------------------------------------------------

_FORCE_SCAN_HINT = "FORGE_PIP_AUDIT_FORCE=1 forge-precommit --only pip_audit"


def _feature_branch_repo(tmp_path: Path, *, mode_line: str | None = None) -> Path:
    """Build a checkout one commit ahead of `main`, its wrap-up verifying HEAD.

    Args:
        tmp_path: Pytest's per-test temporary directory.
        mode_line: Optional `wrapup-mode: ...` line for the wrap-up.

    Returns:
        The checkout root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/x"], cwd=repo, env=GIT_ENV, check=True
    )
    (repo / "file.txt").write_text("x")
    commit_all(repo, "feature work")
    _write_wrapup(repo, _head_sha(repo), mode_line=mode_line)
    return repo


def _fork_time(repo: Path) -> int:
    """Return the commit time of `main`, the feature branch's fork point.

    Args:
        repo: Checkout built by :func:`_feature_branch_repo`.

    Returns:
        Epoch seconds.
    """
    return int(
        subprocess.run(
            ["git", "show", "-s", "--format=%ct", "main"],
            cwd=repo,
            env=GIT_ENV,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


@pytest.mark.parametrize(
    ("case", "expected_text"),
    [
        pytest.param("no-sidecar", _FORCE_SCAN_HINT, id="no-sidecar"),
        pytest.param("scan-before-fork", _FORCE_SCAN_HINT, id="scan-before-fork"),
        pytest.param("scan-in-fork-second", _FORCE_SCAN_HINT, id="scan-in-fork-second"),
        pytest.param("future-dated-scan", _FORCE_SCAN_HINT, id="future-dated-scan"),
        pytest.param("fork-unresolvable", "git fetch origin main", id="no-fork-point"),
        pytest.param("config-typo", "unknown step name", id="config-typo"),
    ],
)
def test_pr_create_refuses_without_a_cve_scan_since_the_fork(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    *,
    case: str,
    expected_text: str,
) -> None:
    """A branch with no trustworthy scan since it left its base is not published.

    SCENARIO: a verified full wrap-up, but the sidecar is missing, holds a
        scan from before (or in the same second as) the fork, is
        future-dated; or the fork point cannot be found; or a step-name
        typo makes the step selection unreadable.
    MOCK SETUP: real git repo and real sidecar file; only `gh` is scripted.
    EXPECTED BEHAVIOR: exit 2, no `gh` call, and the refusal names the fix.

    Args:
        case: Which untrustworthy state to build.
        expected_text: Text the refusal message must carry.
    """
    repo = _feature_branch_repo(tmp_path)
    fork = _fork_time(repo)
    if case == "scan-before-fork":
        _write_scan(repo, fork - 3600)
    elif case == "scan-in-fork-second":
        _write_scan(repo, float(fork))
    elif case == "future-dated-scan":
        _write_scan(repo, time.time() + pip_audit_json.FUTURE_TOLERANCE_S + 600)
    elif case == "fork-unresolvable":
        _write_scan(repo, time.time())
        subprocess.run(
            ["git", "branch", "-q", "-D", "main"], cwd=repo, env=GIT_ENV, check=True
        )
    elif case == "config-typo":
        _write_scan(repo, time.time())
        (repo / "pyproject.toml").write_text(
            '[tool.forge.precommit]\ndisable = ["pip_audt"]\n'
        )
    calls = _stub_gh(monkeypatch)

    with caplog.at_level("ERROR", logger=pr_create.__name__):
        assert _run_main(monkeypatch, repo, fresh_scan=False) == 2

    assert calls == []
    assert any(expected_text in r.getMessage() for r in caplog.records)


def test_pr_create_publishes_with_a_scan_after_the_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A scan recorded after the branch left `main` lets the branch publish."""
    repo = _feature_branch_repo(tmp_path)
    _write_scan(repo, _fork_time(repo) + 60)
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo, fresh_scan=False) == 0
    assert len(calls) == 1


def test_pr_create_exempts_a_repo_that_disabled_pip_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repo whose step selection does not run the scan has none to demand."""
    repo = _feature_branch_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[tool.forge.precommit]\ndisable = ["pip_audit"]\n'
    )
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo, fresh_scan=False) == 0
    assert len(calls) == 1


def test_pr_create_gates_a_light_wrapup_on_the_cve_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An earned light escape skips reporters, not the scan's freshness.

    MOCK SETUP: `pr_create.classify` agrees with the declared light mode,
        so only the missing scan can refuse.
    """
    repo = _feature_branch_repo(tmp_path, mode_line="wrapup-mode: light")
    monkeypatch.setattr(
        pr_create,
        "classify",
        lambda _root, _base, _pr: SimpleNamespace(mode="light-code"),
    )
    calls = _stub_gh(monkeypatch)

    with caplog.at_level("ERROR", logger=pr_create.__name__):
        assert _run_main(monkeypatch, repo, fresh_scan=False) == 2

    assert calls == []
    assert any(_FORCE_SCAN_HINT in r.getMessage() for r in caplog.records)
