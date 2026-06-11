"""Tests for ``forge.next_prep`` — helpers + CLI smoke."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from forge import next_prep


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ---------------------------------------------------------------------------
# _read_plugin_version
# ---------------------------------------------------------------------------


def test_read_plugin_version_returns_semver_string(tmp_path: Path) -> None:
    """Valid plugin.json with a semver version returns the string."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2.10"})
    )
    assert next_prep._read_plugin_version(tmp_path) == "1.2.10"


def test_read_plugin_version_returns_none_when_file_missing(tmp_path: Path) -> None:
    """No plugin.json → None."""
    assert next_prep._read_plugin_version(tmp_path) is None


def test_read_plugin_version_returns_none_on_non_semver(tmp_path: Path) -> None:
    """Non-semver version field → None (defence against tag injection)."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2"})
    )
    assert next_prep._read_plugin_version(tmp_path) is None


def test_read_plugin_version_returns_none_on_malformed_json(tmp_path: Path) -> None:
    """Malformed JSON → None (not raise)."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{not valid")
    assert next_prep._read_plugin_version(tmp_path) is None


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

    monkeypatch.setattr(next_prep, "_git", _fake_git)
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

    def _fake_git(*args: str, **_kw: object) -> str:
        if args[:2] == ("tag", "--list"):
            return "v1.0.0"
        return ""

    monkeypatch.setattr(next_prep, "_git", _fake_git)
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

    def _fake_git(*args: str, **_kw: object) -> str:
        invoked.append(list(args))
        if args[:2] == ("tag", "--list"):
            return "v1.2.9"
        return ""

    monkeypatch.setattr(next_prep, "_git", _fake_git)
    result = next_prep._maybe_tag_release(tmp_path)
    assert result == "v1.2.10"
    # Tag-create and push were invoked.
    assert any(c[:2] == ["tag", "-a"] and "v1.2.10" in c for c in invoked)
    assert any(c[:2] == ["push", "origin"] and "v1.2.10" in c for c in invoked)


# ---------------------------------------------------------------------------
# --target / config-driven branch resolution
# ---------------------------------------------------------------------------


def _run_main_capturing_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    *,
    switch_rc: int = 0,
) -> list[list[str]]:
    """Invoke next_prep.main with stubbed git ops; return the captured commands.

    Captures both the high-level ``_git`` helper calls AND the direct
    ``subprocess.run`` calls (which carry the new ``git switch`` path
    and the ``git pull``). Branch checkout uses ``git switch`` first;
    set ``switch_rc=1`` to force the legacy ``_git("checkout", ...)``
    fallback path.

    Args:
        monkeypatch: pytest fixture for patching.
        tmp_path: Sandbox dir treated as the repo root.
        argv: argv list passed via ``sys.argv``.
        switch_rc: Return code the stubbed ``git switch`` reports.
            ``0`` exercises the happy path; non-zero forces the
            ``checkout`` fallback.

    Returns:
        Captured argv lists from BOTH ``_git`` and ``subprocess.run``,
        in invocation order. Each entry is the argv after the leading
        ``git`` (e.g. ``["switch", "dev"]`` or ``["checkout", "main"]``).
    """
    captured: list[list[str]] = []

    def _fake_git(*args: str, **_kw: object) -> str:
        captured.append(list(args))
        return ""

    class _Proc:
        """Mock subprocess result."""

        def __init__(self, rc: int = 0) -> None:
            self.returncode = rc
            self.stdout = ""
            self.stderr = ""

    def _fake_run(cmd: list[str], **_kw: object) -> _Proc:
        # Strip leading "git" so callers can assert against the same
        # shape as _fake_git's captured argv.
        argv_tail = cmd[1:] if cmd and cmd[0] == "git" else cmd
        captured.append(list(argv_tail))
        if argv_tail[:1] == ["switch"]:
            return _Proc(switch_rc)
        return _Proc(0)

    monkeypatch.setattr(next_prep, "_git", _fake_git)
    monkeypatch.setattr(next_prep.subprocess, "run", _fake_run)
    monkeypatch.setattr(next_prep.Path, "cwd", classmethod(lambda _: tmp_path))
    monkeypatch.setattr(next_prep.sys, "argv", argv)
    rc = next_prep.main()
    assert rc == 0
    return captured


def test_check_promote_pending_silent_when_single_branch_repo(
    tmp_path: Path,
) -> None:
    """Single-branch repos (`dev_branch == base_branch`) skip the check entirely."""
    result = next_prep._check_promote_pending_message(tmp_path, "main", "main")
    assert result is None


def test_check_promote_pending_silent_when_patch_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch-only differences (`Z+1`) do NOT count as pending promotion."""
    monkeypatch.setattr(
        next_prep,
        "_read_plugin_version_at_ref",
        lambda _root, ref: "1.12.5" if "dev" in ref else "1.12.1",
    )
    result = next_prep._check_promote_pending_message(tmp_path, "dev", "main")
    assert result is None


def test_check_promote_pending_emits_minor_bump_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MINOR delta produces a prompt naming the versions, branches, and bump type."""
    monkeypatch.setattr(
        next_prep,
        "_read_plugin_version_at_ref",
        lambda _root, ref: "1.13.0" if "dev" in ref else "1.12.1",
    )
    result = next_prep._check_promote_pending_message(tmp_path, "dev", "main")
    assert result is not None
    assert "MINOR" in result
    assert "1.13.0" in result
    assert "1.12.1" in result
    assert "/promote" in result


def test_check_promote_pending_emits_major_bump_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MAJOR delta is labelled as such."""
    monkeypatch.setattr(
        next_prep,
        "_read_plugin_version_at_ref",
        lambda _root, ref: "2.0.0" if "dev" in ref else "1.12.1",
    )
    result = next_prep._check_promote_pending_message(tmp_path, "dev", "main")
    assert result is not None
    assert "MAJOR" in result


def test_check_promote_pending_silent_when_either_manifest_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When either branch lacks ``plugin.json``, the check returns None silently."""
    monkeypatch.setattr(
        next_prep,
        "_read_plugin_version_at_ref",
        lambda _root, _ref: None,
    )
    assert next_prep._check_promote_pending_message(tmp_path, "dev", "main") is None


def test_main_defaults_to_dev_when_dual_track_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under dual-track, ``forge-next-prep`` switches to ``dev_branch`` by default."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\ndev_branch = "dev"\n',
    )
    captured = _run_main_capturing_git(
        monkeypatch, tmp_path, ["forge-next-prep", "--no-prune-branches"]
    )
    switches = [c for c in captured if c[:1] == ["switch"]]
    assert switches
    assert switches[0][-1] == "dev"


def test_main_target_base_switches_to_base_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--target base`` selects ``base_branch`` (hotfix / promotion prep)."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\ndev_branch = "dev"\n',
    )
    captured = _run_main_capturing_git(
        monkeypatch,
        tmp_path,
        ["forge-next-prep", "--target", "base", "--no-prune-branches"],
    )
    switches = [c for c in captured if c[:1] == ["switch"]]
    assert switches
    assert switches[0][-1] == "main"


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
    """No ``[tool.forge]`` → both targets resolve to ``main`` (back-compat)."""
    captured_default = _run_main_capturing_git(
        monkeypatch,
        tmp_path,
        ["forge-next-prep", "--no-prune-branches"],
    )
    captured_base = _run_main_capturing_git(
        monkeypatch,
        tmp_path,
        ["forge-next-prep", "--target", "base", "--no-prune-branches"],
    )
    for cap in (captured_default, captured_base):
        switches = [c for c in cap if c[:1] == ["switch"]]
        assert switches
        assert switches[0][-1] == "main"
