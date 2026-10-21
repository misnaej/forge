"""Tests for ``forge.next_prep`` — helpers + CLI smoke."""

# MOCKING STRATEGY: the CLI path tests stub two seams. The high-level
# ``next_prep.run_git`` helper is replaced with a fake that records its
# argv and returns empty output, and ``next_prep.subprocess.run`` is
# replaced with a fake returning the canonical ``FakeProc`` whose
# ``returncode`` is branch-dependent (``git switch`` reports the
# configurable ``switch_rc`` to exercise the ``git checkout`` fallback;
# every other command reports 0).

from __future__ import annotations

import argparse
import functools
import json
import logging
import operator
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import next_prep
from forge.config import ForgeConfig
from forge.git_utils import PushResult
from tests.conftest import FakeProc


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# _is_newer
# ---------------------------------------------------------------------------


def test_is_newer_true_when_no_tags() -> None:
    """First release ever — any plugin version qualifies."""
    assert next_prep._is_newer("1.0.0", None) is True


def test_is_newer_true_when_strictly_ahead() -> None:
    """1.2.10 > v1.2.9."""
    assert next_prep._is_newer("1.2.10", "v1.2.9") is True


def test_is_newer_false_when_equal() -> None:
    """1.2.9 vs v1.2.9 — no tag bump needed."""
    assert next_prep._is_newer("1.2.9", "v1.2.9") is False


def test_is_newer_false_when_behind() -> None:
    """1.2.8 < v1.2.9 — don't auto-tag backwards."""
    assert next_prep._is_newer("1.2.8", "v1.2.9") is False


def test_is_newer_handles_minor_jump() -> None:
    """1.3.0 > v1.2.99 (sort-V handles double-digit comparison)."""
    assert next_prep._is_newer("1.3.0", "v1.2.99") is True


# Section: ``_gone_branches`` regex coverage.


def test_gone_branch_regex_matches_canonical_line() -> None:
    """`git branch -vv` output for a gone branch is parsed."""
    line = "  fix/foo abc1234 [origin/fix/foo: gone] message"
    match = next_prep._GONE_BRANCH_RE.match(line)
    assert match is not None
    assert match.group(1) == "fix/foo"


def test_gone_branch_regex_matches_current_starred() -> None:
    """Current branch with the ``* `` prefix is still matched."""
    line = "* fix/foo abc1234 [origin/fix/foo: gone] message"
    match = next_prep._GONE_BRANCH_RE.match(line)
    assert match is not None
    assert match.group(1) == "fix/foo"


def test_gone_branch_regex_skips_live_branch() -> None:
    """Branches whose remote is alive (no ``: gone``) do not match."""
    line = "  feat/x abc1234 [origin/feat/x] message"
    assert next_prep._GONE_BRANCH_RE.match(line) is None


def test_gone_branch_regex_skips_branch_without_remote() -> None:
    """Local-only branches (no tracking remote) do not match."""
    line = "  local-only abc1234 commit message"
    assert next_prep._GONE_BRANCH_RE.match(line) is None


# ---------------------------------------------------------------------------
# _maybe_tag_release
# ---------------------------------------------------------------------------


def test_maybe_tag_release_skips_when_no_plugin_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No plugin.json → no tag action, no git invocations."""
    git_calls: list[list[str]] = []

    def _fake_git(*args: str, **_kw: object) -> str:
        git_calls.append(list(args))
        return ""

    monkeypatch.setattr(next_prep, "run_git", _fake_git)
    decision = next_prep._maybe_tag_release(tmp_path)
    assert decision.tag is None
    assert "no .claude-plugin/plugin.json version" in decision.reason
    assert git_calls == []


def test_maybe_tag_release_skips_when_version_equals_latest_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin.json version equals latest tag → no tag, reason names release-pr."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.0.0"})
    )

    monkeypatch.setattr(next_prep, "latest_v_tag", lambda _root: "v1.0.0")
    monkeypatch.setattr(next_prep, "run_git", lambda *_a, **_kw: "")
    decision = next_prep._maybe_tag_release(tmp_path)
    assert decision.tag is None
    assert "is already tagged as v1.0.0" in decision.reason
    assert "forge-changelog release-pr" in decision.reason


def test_maybe_tag_release_skips_when_version_behind_latest_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin.json version below the latest tag → no tag, reason says behind."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.0.0"})
    )

    monkeypatch.setattr(next_prep, "latest_v_tag", lambda _root: "v1.0.1")
    monkeypatch.setattr(next_prep, "run_git", lambda *_a, **_kw: "")
    decision = next_prep._maybe_tag_release(tmp_path)
    assert decision.tag is None
    assert "is behind the latest tag" in decision.reason


def test_maybe_tag_release_creates_and_pushes_new_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin.json ahead of latest tag → ``git tag`` + ``git push``."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2.10"})
    )
    invoked: list[list[str]] = []
    created: list[tuple[Path, str, str, bool]] = []

    def _fake_git(*args: str, **_kw: object) -> str:
        invoked.append(list(args))
        return ""

    def _fake_create_annotated_tag(
        repo_root: Path, tag: str, *, commit: str = "HEAD", force: bool = False
    ) -> None:
        created.append((repo_root, tag, commit, force))

    monkeypatch.setattr(next_prep, "latest_v_tag", lambda _root: "v1.2.9")
    monkeypatch.setattr(next_prep, "run_git", _fake_git)
    monkeypatch.setattr(next_prep, "create_annotated_tag", _fake_create_annotated_tag)
    result = next_prep._maybe_tag_release(tmp_path)
    assert result.tag == "v1.2.10"
    assert created == [(tmp_path, "v1.2.10", "HEAD", False)]
    # Push was invoked via run_git.
    assert any(c[:2] == ["push", "origin"] and "v1.2.10" in c for c in invoked)


def _run_main_with_timeout_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verb: str,
    argv: list[str],
    *,
    remote_has_tag: bool | None = None,
) -> int:
    """Run ``next_prep.main`` with the git call for *verb* timing out.

    ``fetch`` and ``push`` go through ``run_git``; ``pull`` is a direct
    ``subprocess.run``. Either way the timed-out call must have been
    bounded by ``PUSH_TIMEOUT_S`` and barred from prompting.

    Args:
        monkeypatch: pytest fixture for patching.
        tmp_path: Sandbox dir treated as the repo root.
        verb: The git verb (``"fetch"`` / ``"pull"`` / ``"push"``) whose
            call times out.
        argv: argv list passed via ``sys.argv``.
        remote_has_tag: What the remote tag probe answers after a
            timed-out push.

    Returns:
        ``main``'s exit code.
    """

    def _time_out(cmd: list[str], timeout: float | None, env: object) -> None:
        assert timeout == next_prep.PUSH_TIMEOUT_S
        assert isinstance(env, dict)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    def _fake_git(
        *args: str, timeout: float | None = None, env: object = None, **_kw: object
    ) -> str:
        if args[0] == verb:
            _time_out(["git", *args], timeout, env)
        return ""

    def _fake_run(
        cmd: list[str],
        *,
        timeout: float | None = None,
        env: object = None,
        **_kw: object,
    ) -> FakeProc:
        if cmd[1] == verb:
            _time_out(cmd, timeout, env)
        return FakeProc()

    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2.10"})
    )
    monkeypatch.setattr(next_prep, "run_git", _fake_git)
    monkeypatch.setattr(next_prep, "tag_on_remote", lambda *_a: remote_has_tag)
    monkeypatch.setattr(next_prep, "latest_v_tag", lambda _root: "v1.2.9")
    monkeypatch.setattr(next_prep, "create_annotated_tag", lambda *_a, **_kw: None)
    monkeypatch.setattr(next_prep, "fetch_tags_best_effort", lambda _root: [])
    monkeypatch.setattr(next_prep.subprocess, "run", _fake_run)
    monkeypatch.setattr(next_prep.Path, "cwd", classmethod(lambda _: tmp_path))
    monkeypatch.setattr(next_prep.sys, "argv", argv)
    return next_prep.main()


_TAG_ARGV = ["forge-next-prep", "--tag", "--no-sync", "--no-prune-branches"]


@pytest.mark.parametrize(
    ("remote_has_tag", "state"),
    [(False, "origin does not have it"), (None, "whether origin has it is unknown")],
)
def test_main_timed_out_tag_push_exits_one_with_manual_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    remote_has_tag: bool | None,
    state: str,
) -> None:
    """SCENARIO: the release-tag push stalls and origin lacks it or is mute.

    MOCK SETUP: ``run_git`` raises ``TimeoutExpired`` on ``push``; the
        remote probe answers *remote_has_tag*; tag creation is stubbed.
    EXPECTED BEHAVIOR: exit 1, no traceback, and one error that states
        origin's state truthfully and names the manual push — a re-run in
        this checkout would see the local tag and not retry.

    Args:
        remote_has_tag: The probe's answer.
        state: The phrase the error must use for that answer.
    """
    with caplog.at_level(logging.ERROR, logger="forge.next_prep"):
        rc = _run_main_with_timeout_on(
            monkeypatch, tmp_path, "push", _TAG_ARGV, remote_has_tag=remote_has_tag
        )
    assert rc == 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert state in message
    assert "git push origin v1.2.10" in message
    assert "in this checkout" in message
    assert errors[0].exc_info is None


def test_main_timed_out_tag_push_that_landed_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: the tag push times out but origin did receive the tag.

    MOCK SETUP: ``run_git`` raises ``TimeoutExpired`` on ``push``; the
        remote probe reports the tag present.
    EXPECTED BEHAVIOR: exit 0 and the usual "Tagged and pushed" line,
        noting that the timed-out push landed; no error.
    """
    with caplog.at_level(logging.INFO, logger="forge.next_prep"):
        rc = _run_main_with_timeout_on(
            monkeypatch, tmp_path, "push", _TAG_ARGV, remote_has_tag=True
        )
    assert rc == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "Tagged and pushed v1.2.10" in r.getMessage()
        and "timed-out push landed" in r.getMessage()
        for r in caplog.records
    )


def test_main_timed_out_pull_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: ``git pull --ff-only`` stalls past the bound.

    MOCK SETUP: ``subprocess.run`` raises ``TimeoutExpired`` on ``pull``.
    EXPECTED BEHAVIOR: exit 1 with one traceback-free error saying origin
        did not answer the pull — not misreported as divergence.
    """
    argv = ["forge-next-prep", "--no-prune-branches"]
    with caplog.at_level(logging.ERROR, logger="forge.next_prep"):
        assert _run_main_with_timeout_on(monkeypatch, tmp_path, "pull", argv) == 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "did not answer the pull" in errors[0].getMessage()
    assert errors[0].exc_info is None


def test_main_timed_out_fetch_exits_one_before_syncing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SCENARIO: the opening ``git fetch --prune`` stalls past the bound.

    MOCK SETUP: ``run_git`` raises ``TimeoutExpired`` on ``fetch``.
    EXPECTED BEHAVIOR: exit 1 with one traceback-free error saying
        origin did not answer.
    """
    argv = ["forge-next-prep", "--no-prune-branches"]
    with caplog.at_level(logging.ERROR, logger="forge.next_prep"):
        assert _run_main_with_timeout_on(monkeypatch, tmp_path, "fetch", argv) == 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "did not answer the fetch" in errors[0].getMessage()
    assert errors[0].exc_info is None


# ---------------------------------------------------------------------------
# config-driven branch resolution
# ---------------------------------------------------------------------------


def _run_main_capturing_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    *,
    switch_rc: int = 0,
) -> list[list[str]]:
    """Invoke next_prep.main with stubbed git ops; return the captured commands.

    Captures both the high-level ``run_git`` helper calls AND the direct
    ``subprocess.run`` calls (which carry the new ``git switch`` path
    and the ``git pull``). Branch checkout uses ``git switch`` first;
    set ``switch_rc=1`` to force the legacy ``run_git("checkout", ...)``
    fallback path.

    Args:
        monkeypatch: pytest fixture for patching.
        tmp_path: Sandbox dir treated as the repo root.
        argv: argv list passed via ``sys.argv``.
        switch_rc: Return code the stubbed ``git switch`` reports.
            ``0`` exercises the happy path; non-zero forces the
            ``checkout`` fallback.

    Returns:
        Captured argv lists from BOTH ``run_git`` and ``subprocess.run``,
        in invocation order. Each entry is the argv after the leading
        ``git`` (e.g. ``["switch", "main"]`` or ``["checkout", "main"]``).
    """
    captured: list[list[str]] = []

    def _fake_git(*args: str, **_kw: object) -> str:
        captured.append(list(args))
        return ""

    def _fake_run(cmd: list[str], **_kw: object) -> FakeProc:
        # Strip leading "git" so callers can assert against the same
        # shape as _fake_git's captured argv.
        argv_tail = cmd[1:] if cmd and cmd[0] == "git" else cmd
        captured.append(list(argv_tail))
        if argv_tail[:1] == ["switch"]:
            return FakeProc(returncode=switch_rc)
        return FakeProc(returncode=0)

    monkeypatch.setattr(next_prep, "run_git", _fake_git)
    monkeypatch.setattr(next_prep.subprocess, "run", _fake_run)
    monkeypatch.setattr(next_prep.Path, "cwd", classmethod(lambda _: tmp_path))
    monkeypatch.setattr(next_prep.sys, "argv", argv)
    rc = next_prep.main()
    assert rc == 0
    return captured


def test_main_switches_to_configured_base_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``forge-next-prep`` switches to the configured ``base_branch``."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "master"\n',
    )
    captured = _run_main_capturing_git(
        monkeypatch, tmp_path, ["forge-next-prep", "--no-prune-branches"]
    )
    switches = [c for c in captured if c[:1] == ["switch"]]
    assert switches
    assert switches[0][-1] == "master"


def test_main_falls_back_to_checkout_when_switch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``git switch`` returns non-zero, the CLI retries via ``git checkout``.

    Verifies the fallback fires (the second captured argv begins with
    ``checkout``) and targets the configured branch. Exercises the
    behaviour relevant on git < 2.23 where ``switch`` is unavailable.
    """
    captured = _run_main_capturing_git(
        monkeypatch,
        tmp_path,
        ["forge-next-prep", "--no-prune-branches"],
        switch_rc=1,
    )
    switches = [c for c in captured if c[:1] == ["switch"]]
    checkouts = [c for c in captured if c[:1] == ["checkout"]]
    assert switches
    assert switches[0][-1] == "main"
    assert checkouts
    assert checkouts[0][1] == "main"


def test_main_collapses_to_main_when_no_tool_forge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``[tool.forge]`` → the target resolves to ``main`` (back-compat)."""
    captured = _run_main_capturing_git(
        monkeypatch,
        tmp_path,
        ["forge-next-prep", "--no-prune-branches"],
    )
    switches = [c for c in captured if c[:1] == ["switch"]]
    assert switches
    assert switches[0][-1] == "main"


def test_tag_staleness_warning_fires_on_base_when_tag_lags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warn on the base_branch when plugin.json is ahead of the latest tag.

    MOCK SETUP: current branch = main; config base_branch = main (default);
    plugin.json 1.25.0 with latest tag v1.24.1 (a bump that was never
    tagged).
    """
    monkeypatch.setattr(next_prep, "run_git", lambda *_a, **_k: "main")
    monkeypatch.setattr(next_prep, "load_config", lambda _r: ForgeConfig())
    monkeypatch.setattr(next_prep, "read_local_plugin_version", lambda _r: "1.25.0")
    monkeypatch.setattr(next_prep, "latest_v_tag", lambda _r: "v1.24.1")
    warning = next_prep.tag_staleness_warning(tmp_path)
    assert warning is not None
    assert "1.25.0" in warning
    assert "v1.24.1" in warning


def test_tag_staleness_warning_silent_off_base_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No warning when the current branch is not the base_branch."""
    monkeypatch.setattr(next_prep, "run_git", lambda *_a, **_k: "feature/x")
    monkeypatch.setattr(next_prep, "load_config", lambda _r: ForgeConfig())
    assert next_prep.tag_staleness_warning(tmp_path) is None


def test_tag_staleness_warning_silent_when_tag_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No warning when the latest tag already matches plugin.json."""
    monkeypatch.setattr(next_prep, "run_git", lambda *_a, **_k: "main")
    monkeypatch.setattr(next_prep, "load_config", lambda _r: ForgeConfig())
    monkeypatch.setattr(next_prep, "read_local_plugin_version", lambda _r: "1.24.1")
    monkeypatch.setattr(next_prep, "latest_v_tag", lambda _r: "v1.24.1")
    assert next_prep.tag_staleness_warning(tmp_path) is None


def test_tag_misuse_warning_fires_without_manifest(tmp_path: Path) -> None:
    """A repo without plugin.json → warning pointing at forge-release."""
    warning = next_prep._tag_misuse_warning(tmp_path)
    assert warning is not None
    assert "forge-release" in warning


def test_tag_misuse_warning_silent_with_manifest(tmp_path: Path) -> None:
    """Plugin manifest present → --tag is the intended pattern, no warning."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.0.0"})
    )
    assert next_prep._tag_misuse_warning(tmp_path) is None


def test_tag_and_report_advises_on_pending_fragments(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fragments mode with pending fragments → the release advisory is logged.

    ``_tag_and_report`` is called directly with tagging and pruning
    disabled, so only the advisory tail runs — no git needed.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.changelog]\nmode = "fragments"\n'
    )
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "a.added.md").write_text("bump: minor\n- x\n")
    args = argparse.Namespace(tag=False, no_prune_branches=True, prune_remote=False)
    with caplog.at_level("INFO"):
        assert next_prep._tag_and_report(tmp_path, args) == 0
    assert "1 pending changelog fragment(s)" in caplog.text
    assert "forge-changelog release" in caplog.text


def test_tag_and_report_logs_reason_when_no_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--tag`` with no tag decided → ``No release tag: <reason>`` is logged.

    SCENARIO: the tag decision carries a reason; the operator must see it.
    MOCK SETUP: misuse check and tag decision stubbed; no git needed.
    EXPECTED BEHAVIOR: the reason text appears in the INFO log.
    """
    monkeypatch.setattr(next_prep, "_tag_misuse_warning", lambda _root: None)
    monkeypatch.setattr(
        next_prep,
        "_maybe_tag_release",
        lambda _root: next_prep.TagDecision(None, "manifest is parked"),
    )
    args = argparse.Namespace(tag=True, no_prune_branches=True, prune_remote=False)
    with caplog.at_level("INFO"):
        assert next_prep._tag_and_report(tmp_path, args) == 0
    assert "No release tag: manifest is parked" in caplog.text


@pytest.mark.parametrize(
    "pyproject_text",
    [
        '[tool.forge.changelog]\nmode = "fragments"\n',  # mode on, none pending
        "",  # shared-heading repo (no config at all)
    ],
)
def test_tag_and_report_fragment_advisory_silent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    pyproject_text: str,
) -> None:
    """No advisory without pending fragments, and none outside fragments mode.

    Args:
        pyproject_text: The ``pyproject.toml`` contents for the case.
    """
    if pyproject_text:
        (tmp_path / "pyproject.toml").write_text(pyproject_text)
    args = argparse.Namespace(tag=False, no_prune_branches=True, prune_remote=False)
    with caplog.at_level("INFO"):
        assert next_prep._tag_and_report(tmp_path, args) == 0
    assert "pending changelog fragment" not in caplog.text


def test_main_no_sync_skips_checkout_and_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-sync tags on the current HEAD without fetch/switch/pull.

    SCENARIO: CI tag job checked out the exact validated SHA; syncing to
    the branch tip would re-race. MOCK SETUP: run_git + subprocess.run
    recorded. EXPECTED BEHAVIOR: no "switch"/"pull" invocations; tags
    fetched; exit 0.
    """
    captured = _run_main_capturing_git(
        monkeypatch,
        tmp_path,
        ["forge-next-prep", "--tag", "--no-sync", "--no-prune-branches"],
    )
    assert not any(c[:1] == ["switch"] for c in captured)
    assert not any(c[:1] == ["pull"] for c in captured)
    assert any(c[:2] == ["fetch", "--tags"] for c in captured)


def test_pull_ff_only_runs_with_network_env_over_the_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pull cannot prompt (HTTPS or ssh) yet still finds `ssh` on `PATH`.

    SCENARIO: no user ssh choice anywhere, so the batch-mode default applies.
    MOCK SETUP: `subprocess.run` records the pull's kwargs; the config probe
        inside `network_git_env` runs through the same seam and answers
        "not configured".
    EXPECTED BEHAVIOR: the pull env carries the no-prompt and ssh batch
        variables and keeps every inherited variable such as `PATH`.
    """
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    seen: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> FakeProc:
        if cmd[1:3] == ["pull", "--ff-only"]:
            seen.update(kwargs)
        return FakeProc(returncode=0)

    monkeypatch.setattr(next_prep.subprocess, "run", _fake_run)

    assert next_prep._pull_ff_only(tmp_path, "main") is True

    env = seen["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert env["PATH"] == os.environ["PATH"]


# Section: ``--prune-remote``. The live remote listing, origin/HEAD probe
# and the delete seam are replaced as module attributes; the gh lookup is
# the injectable ``pr_lister``.

_TIP = "a" * 40
_OTHER = "b" * 40


def _pr(
    state: str = "MERGED",
    *,
    oid: str = _TIP,
    base: str = "main",
    cross: bool = False,
    head: str = "feature",
) -> dict[str, object]:
    """Build one ``gh pr list --json`` row for the branch ``feature``.

    Args:
        state: PR state (``MERGED`` / ``OPEN`` / ``CLOSED``).
        oid: The PR's head commit.
        base: The PR's base branch.
        cross: Whether the head lives in a fork.
        head: The PR's head branch name.

    Returns:
        The row.
    """
    return {
        "number": 1,
        "state": state,
        "headRefName": head,
        "headRefOid": oid,
        "isCrossRepository": cross,
        "baseRefName": base,
        "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None,
    }


def _lister(*prs: dict[str, object]) -> next_prep.PrLister:
    """Return a PR lister that answers *prs* for every branch.

    Args:
        *prs: The rows to answer.

    Returns:
        The lister.
    """
    return lambda _root, _branch: next_prep.PrLookup(list(prs))


@pytest.fixture
def remote_with_feature(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Origin holds ``main`` and ``feature`` (tip ``_TIP``); deletes are recorded.

    Returns:
        The ``(branch, expected_sha)`` of every delete requested.
    """
    deletes: list[tuple[str, str]] = []

    def _fake_delete(
        _root: Path, branch: str, *, expected_sha: str, base_branch: str
    ) -> PushResult:
        assert base_branch == "main"
        deletes.append((branch, expected_sha))
        return PushResult(ok=True, returncode=0, stderr="")

    monkeypatch.setattr(
        next_prep, "_remote_heads", lambda _root: {"main": _OTHER, "feature": _TIP}
    )
    monkeypatch.setattr(next_prep, "_origin_head_branch", lambda _root: "main")
    monkeypatch.setattr(next_prep, "delete_remote_branch", _fake_delete)
    return deletes


def test_prune_remote_deletes_merged_unchanged_branch(
    tmp_path: Path, remote_with_feature: list[tuple[str, str]]
) -> None:
    """A same-repo PR merged into main at origin's tip → deleted, lease = tip."""
    report = next_prep._prune_merged_remote_branches(
        tmp_path, "main", pr_lister=_lister(_pr())
    )
    assert remote_with_feature == [("feature", _TIP)]
    assert report.deleted == ["feature"]


@pytest.mark.parametrize(
    ("prs", "bucket"),
    [
        ((_pr(), _pr("OPEN")), "open_pr"),
        ((_pr(base="release"),), "unmerged"),
        ((_pr(oid=_OTHER),), "moved"),
        ((_pr(cross=True),), "unmerged"),
        ((_pr("CLOSED"),), "unmerged"),
    ],
    ids=["open-after-merged", "base-mismatch", "moved-tip", "cross-repo", "closed"],
)
def test_prune_remote_leaves_branch_that_is_not_safely_merged(
    tmp_path: Path,
    remote_with_feature: list[tuple[str, str]],
    prs: tuple[dict[str, object], ...],
    bucket: str,
) -> None:
    """Every not-safely-merged shape is reported in its bucket, never deleted.

    Args:
        prs: The PR rows gh answers for ``feature``.
        bucket: The report list ``feature`` must land in.
    """
    report = next_prep._prune_merged_remote_branches(
        tmp_path, "main", pr_lister=_lister(*prs)
    )
    assert remote_with_feature == []
    assert getattr(report, bucket) == ["feature"]


def test_prune_remote_result_at_limit_is_unknown(
    tmp_path: Path, remote_with_feature: list[tuple[str, str]]
) -> None:
    """A lookup that fills the limit may hide an open PR → skipped as unknown."""
    rows = [_pr()] * next_prep._GH_PR_LIST_LIMIT
    report = next_prep._prune_merged_remote_branches(
        tmp_path, "main", pr_lister=_lister(*rows)
    )
    assert remote_with_feature == []
    assert functools.reduce(operator.iadd, report.unknown.values(), []) == ["feature"]


def test_prune_remote_listing_failure_warns_and_deletes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_with_feature: list[tuple[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``git ls-remote`` failing is a warning, never "nothing to prune"."""
    monkeypatch.setattr(next_prep, "_remote_heads", lambda _root: None)
    with caplog.at_level("INFO"):
        next_prep._log_remote_prune_result(tmp_path, "main", pr_lister=_lister(_pr()))
    assert remote_with_feature == []
    assert "could not be listed" in caplog.text
    assert "nothing was deleted" in caplog.text


def test_prune_remote_stale_lease_is_reported_and_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_with_feature: list[tuple[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lease git rejects means the branch moved: reported, run still exits 0.

    SCENARIO: the branch moved between ls-remote and the delete.
    MOCK SETUP: the delete seam answers a stale-lease rejection; the
        module's gh lookup is replaced so ``_tag_and_report`` runs as the
        CLI would.
    EXPECTED BEHAVIOR: exit 0 and a "moved since" warning naming it.
    """
    monkeypatch.setattr(
        next_prep,
        "delete_remote_branch",
        lambda *_a, **_k: PushResult(
            ok=False, returncode=1, stderr="! [rejected] (stale info)", stale_lease=True
        ),
    )
    monkeypatch.setattr(next_prep, "_gh_prs_for_head", _lister(_pr()))
    args = argparse.Namespace(tag=False, no_prune_branches=True, prune_remote=True)
    with caplog.at_level("INFO"):
        assert next_prep._tag_and_report(tmp_path, args) == 0
    assert "moved since their PR merged: feature" in caplog.text


def test_prune_remote_skips_with_one_warning_when_gh_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_with_feature: list[tuple[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ``gh`` → every branch is unknown, one warning, nothing deleted."""
    monkeypatch.setattr(
        next_prep,
        "_remote_heads",
        lambda _root: {"main": _OTHER, "feature": _TIP, "other": _TIP},
    )
    monkeypatch.setattr(next_prep.shutil, "which", lambda _name: None)
    with caplog.at_level("INFO"):
        next_prep._log_remote_prune_result(tmp_path, "main")
    assert remote_with_feature == []
    assert caplog.text.count("gh is not installed") == 1
    assert "feature, other" in caplog.text


def test_prune_remote_never_lists_base_or_origin_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_with_feature: list[tuple[str, str]],
) -> None:
    """The base branch and origin/HEAD's branch are never even looked up."""
    monkeypatch.setattr(
        next_prep,
        "_remote_heads",
        lambda _root: {"main": _OTHER, "trunk": _OTHER, "feature": _TIP},
    )
    monkeypatch.setattr(next_prep, "_origin_head_branch", lambda _root: "trunk")
    asked: list[str] = []

    def _recording(_root: Path, branch: str) -> next_prep.PrLookup:
        asked.append(branch)
        return next_prep.PrLookup([_pr()])

    next_prep._prune_merged_remote_branches(tmp_path, "main", pr_lister=_recording)
    assert asked == ["feature"]


def test_prune_remote_flag_off_makes_no_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``--prune-remote`` the remote is never listed."""

    def _boom(_root: Path) -> None:
        pytest.fail("remote listed without --prune-remote")

    monkeypatch.setattr(next_prep, "_remote_heads", _boom)
    args = argparse.Namespace(tag=False, no_prune_branches=True, prune_remote=False)
    assert next_prep._tag_and_report(tmp_path, args) == 0
