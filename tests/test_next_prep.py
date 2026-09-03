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
import json
from typing import TYPE_CHECKING

import pytest

from forge import next_prep
from forge.config import ForgeConfig
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
    assert next_prep._maybe_tag_release(tmp_path) is None
    assert git_calls == []


def test_maybe_tag_release_skips_when_version_equals_latest_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin.json version equals latest tag → no action."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.0.0"})
    )

    monkeypatch.setattr(next_prep, "latest_v_tag", lambda _root: "v1.0.0")
    monkeypatch.setattr(next_prep, "run_git", lambda *_a, **_kw: "")
    assert next_prep._maybe_tag_release(tmp_path) is None


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
    assert result == "v1.2.10"
    assert created == [(tmp_path, "v1.2.10", "HEAD", False)]
    # Push was invoked via run_git.
    assert any(c[:2] == ["push", "origin"] and "v1.2.10" in c for c in invoked)


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
    args = argparse.Namespace(tag=False, no_prune_branches=True)
    with caplog.at_level("INFO"):
        assert next_prep._tag_and_report(tmp_path, args) == 0
    assert "1 pending changelog fragment(s)" in caplog.text
    assert "forge-changelog release" in caplog.text


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
    args = argparse.Namespace(tag=False, no_prune_branches=True)
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
