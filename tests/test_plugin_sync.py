"""Tests for forge.plugin_sync — the per-checkout forge plugin installer."""

# MOCKING STRATEGY: the command reaches two outside worlds, GitHub and the
# ``claude`` binary, and both sit behind module seams so no test touches a
# network or a real Claude Code.
#   - plugin_sync.run_git: stands in for ``git ls-remote``.
#   - plugin_sync._claude: a recorder (FakeClaude) for the sync tests; on
#     ``install`` it writes the install record Claude Code would, so the
#     command's own post-verification passes honestly.
#   - plugin_sync.shutil.which: whether ``claude`` is installed.
#   - A real shell script named ``claude`` on a private PATH exercises
#     ``_claude`` itself (argv, cwd, JSON parsing), nothing else does.
#   - ``claude_home`` (conftest) and XDG_CACHE_HOME isolate every file.

from __future__ import annotations

import json
import logging
import subprocess
from typing import TYPE_CHECKING, NamedTuple

import pytest

from forge import plugin_sync
from tests.conftest import write_install


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

VERSION = "1.2.3"
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout whose plugin manifest names release 1.2.3."""
    root = tmp_path / "repo"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "forge", "version": VERSION}), encoding="utf-8"
    )
    return root


@pytest.fixture
def xdg_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user cache directory the shared marketplaces are written under."""
    cache = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return cache


class SyncEnv(NamedTuple):
    """The paths and fixtures a full sync run is set up against."""

    repo: Path
    tmp_path: Path
    claude_home: Path
    xdg_cache: Path


@pytest.fixture
def sync_env(
    repo: Path,
    tmp_path: Path,
    claude_home: Path,
    xdg_cache: Path,
    online: None,
) -> SyncEnv:
    """Bundle the checkout, machine paths and the online switch for a sync run.

    The online switch is requested here so every test that unpacks this bundle
    still runs with GitHub reachable, exactly as when it was requested directly.
    """
    del online
    return SyncEnv(repo, tmp_path, claude_home, xdg_cache)


class FakeClaude:
    """Recorder standing in for ``plugin_sync._claude``.

    Attributes:
        calls: Each call's ``(args, cwd)`` in order.
    """

    def __init__(
        self,
        claude_home: Path,
        repo: Path,
        *,
        fail_on: str | None = None,
        record_install: bool = True,
    ) -> None:
        """Configure which subcommand fails and whether install records.

        Args:
            claude_home: Fake ``~/.claude``.
            repo: Checkout the install record is written for.
            fail_on: First argument (``marketplace``, ``install``,
                ``uninstall``) that reports failure.
            record_install: Whether ``install`` writes its record.
        """
        self.calls: list[tuple[list[str], Path]] = []
        self._home = claude_home
        self._repo = repo
        self._fail_on = fail_on
        self._record_install = record_install

    def __call__(self, args: list[str], cwd: Path) -> tuple[bool, str]:
        """Record the call and answer like Claude Code would.

        Args:
            args: The ``claude`` argument vector.
            cwd: The directory the call would run in.

        Returns:
            ``(ok, message)`` as Claude Code would report it.
        """
        self.calls.append((args, cwd))
        if args[0] == self._fail_on:
            return False, "refused"
        if args[0] == "install" and self._record_install:
            key = args[1]
            version = key.partition("-v")[2].replace("-", ".")
            write_install(self._home, self._repo, version, key=key)
        return True, "ok"

    @property
    def verbs(self) -> list[str]:
        """The first argument of every call, in order."""
        return [args[0] for args, _ in self.calls]


def _fail_loud(*_args: object, **_kwargs: object) -> None:
    """Stand in for a seam that must not be reached.

    Args:
        *_args: Ignored positional arguments.
        **_kwargs: Ignored keyword arguments.

    Raises:
        AssertionError: Always, because the seam was reached.
    """
    msg = "seam must not be called"
    raise AssertionError(msg)


@pytest.fixture
def online(monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude` is installed and GitHub answers with a peeled release commit."""
    monkeypatch.setattr(plugin_sync.shutil, "which", lambda _n: "/bin/claude")
    monkeypatch.setattr(
        plugin_sync,
        "run_git",
        lambda *_a, **_k: f"{COMMIT}\trefs/tags/v{VERSION}^{{}}",
    )


# ---------------------------------------------------------------------------
# marketplace_manifest / write_marketplace
# ---------------------------------------------------------------------------


def test_marketplace_manifest_pins_tag_and_commit() -> None:
    """A moved tag cannot change what the name installs: sha is pinned too."""
    manifest = plugin_sync.marketplace_manifest(VERSION, COMMIT)

    source = json.loads(json.dumps(manifest))["plugins"][0]["source"]
    assert manifest["name"] == "forge-v1-2-3"
    assert source["ref"] == "v1.2.3"
    assert source["sha"] == COMMIT


def test_write_marketplace_replaces_content_and_leaves_no_temp_file(
    xdg_cache: Path,
) -> None:
    """Re-writing for the same release swaps the content and leaves no temp file."""
    first = plugin_sync.write_marketplace(VERSION, COMMIT)
    second = plugin_sync.write_marketplace(VERSION, OTHER_COMMIT)

    assert first == second == xdg_cache / "forge" / "marketplaces" / "forge-v1-2-3"
    files = list((second / ".claude-plugin").iterdir())
    assert [f.name for f in files] == ["marketplace.json"]
    written = json.loads(files[0].read_text(encoding="utf-8"))
    assert written["plugins"][0]["source"]["sha"] == OTHER_COMMIT


# ---------------------------------------------------------------------------
# resolve_commit
# ---------------------------------------------------------------------------


def _remote(listing: str | Exception) -> Callable[..., str]:
    """Build a ``run_git`` stand-in answering *listing* or raising it.

    Args:
        listing: The text to return, or the exception to raise.

    Returns:
        A callable accepting any ``run_git`` arguments.
    """

    def _run(*_args: object, **_kwargs: object) -> str:
        if isinstance(listing, Exception):
            raise listing
        return listing

    return _run


def test_resolve_commit_prefers_the_peeled_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An annotated tag's own object is not the commit; its peeled line is."""
    listing = f"{OTHER_COMMIT}\trefs/tags/v1.2.3\n{COMMIT}\trefs/tags/v1.2.3^{{}}"
    monkeypatch.setattr(plugin_sync, "run_git", _remote(listing))

    assert plugin_sync.resolve_commit(VERSION) == COMMIT


def test_resolve_commit_accepts_a_lightweight_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lightweight tag has no peeled line; its own line is the commit."""
    monkeypatch.setattr(plugin_sync, "run_git", _remote(f"{COMMIT}\trefs/tags/v1.2.3"))

    assert plugin_sync.resolve_commit(VERSION) == COMMIT


@pytest.mark.parametrize(
    "answer",
    [
        subprocess.CalledProcessError(128, "git"),
        subprocess.TimeoutExpired("git", 30),
        OSError("no git"),
        "",
        "not-a-sha\trefs/tags/v1.2.3",
    ],
    ids=["git-error", "timeout", "oserror", "empty", "non-hex"],
)
def test_resolve_commit_is_none_when_the_remote_gives_no_commit(
    monkeypatch: pytest.MonkeyPatch, answer: str | Exception
) -> None:
    """Offline, unpublished or garbled answers are "unknown", never a guess.

    Args:
        answer: The remote listing, or the exception the remote raises.
    """
    monkeypatch.setattr(plugin_sync, "run_git", _remote(answer))

    assert plugin_sync.resolve_commit(VERSION) is None


# ---------------------------------------------------------------------------
# _printable / _claude
# ---------------------------------------------------------------------------


def test_printable_escapes_control_characters_and_caps_length() -> None:
    """A message from another tool cannot drive the terminal or flood it."""
    assert plugin_sync._printable("a\x1b[31mb") == "a\\x1b[31mb"
    assert len(plugin_sync._printable("x" * 1000)) == 300


def _fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """Install an executable ``claude`` running *body* as the only PATH entry.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Pytest monkeypatch fixture.
        body: Shell lines the fake runs.

    Returns:
        The directory holding it.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "claude"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    return bindir


def test_claude_reads_the_last_json_line_after_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Progress chatter before the result line does not hide the outcome."""
    _fake_claude(
        tmp_path,
        monkeypatch,
        'echo cloning...\necho \'{"outcome":"ok","message":"done"}\'',
    )

    assert plugin_sync._claude(["install", "x"], tmp_path) == (True, "done")


@pytest.mark.parametrize(
    "body",
    [
        'echo \'{"outcome":"ok","message":"m"}\'; exit 3',
        'echo \'{"outcome":"error","message":"m"}\'',
        "echo 'not json'",
    ],
    ids=["nonzero-exit", "outcome-not-ok", "bad-json"],
)
def test_claude_fails_unless_exit_zero_and_outcome_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """Success needs both a zero exit and an ``ok`` outcome.

    Args:
        body: Shell lines the fake ``claude`` runs.
    """
    _fake_claude(tmp_path, monkeypatch, body)

    ok, _message = plugin_sync._claude(["install", "x"], tmp_path)

    assert ok is False


def test_claude_runs_plugin_subcommand_json_in_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--scope local`` applies to the cwd's project, so cwd must be the checkout."""
    record = tmp_path / "record.txt"
    _fake_claude(
        tmp_path,
        monkeypatch,
        f"pwd > '{record}'\nprintf '%s\\n' \"$@\" >> '{record}'\n"
        'echo \'{"outcome":"ok"}\'',
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    plugin_sync._claude(["install", "forge@x", "--scope", "local"], checkout)

    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(checkout.resolve())
    assert lines[1:] == ["plugin", "install", "forge@x", "--scope", "local", "--json"]


def test_claude_missing_binary_reports_failure_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished ``claude`` is a failed step, not a traceback in a git hook."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    ok, message = plugin_sync._claude(["install", "x"], tmp_path)

    assert ok is False
    assert "claude" in message


def test_claude_timeout_reports_failure_naming_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung clone is bounded and the message names which step hung."""

    def _hang(*_a: object, **_k: object) -> None:
        msg = "claude"
        raise subprocess.TimeoutExpired(msg, 1)

    monkeypatch.setattr(plugin_sync.subprocess, "run", _hang)

    ok, message = plugin_sync._claude(["install", "x"], tmp_path)

    assert ok is False
    assert "plugin install" in message


# ---------------------------------------------------------------------------
# _refusal
# ---------------------------------------------------------------------------


def test_refusal_names_the_user_scope_install(repo: Path, claude_home: Path) -> None:
    """A user-scope forge loads beside the checkout's own, so it must go first."""
    write_install(claude_home, None, "1.0.0", key="forge@forge", scope="user")

    message = plugin_sync._refusal(repo)

    assert message is not None
    assert "--scope user" in message


@pytest.mark.parametrize(
    ("settings", "refused"),
    [
        ('{"enabledPlugins": {"forge@forge": true}}', True),
        ('{"enabledPlugins": {"forge@forge": false}}', False),
        ('{"enabledPlugins": {}}', False),
        ("{not json", False),
        (None, False),
    ],
    ids=["enabled", "disabled", "absent-key", "unreadable", "no-file"],
)
def test_refusal_for_a_shared_settings_enablement(
    repo: Path, settings: str | None, *, refused: bool
) -> None:
    """Only a tracked setting that turns a forge plugin on blocks the install.

    Args:
        settings: Contents of ``.claude/settings.json``, or ``None`` for no file.
        refused: Whether ``_refusal`` should report a refusal.
    """
    if settings is not None:
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(settings, encoding="utf-8")

    message = plugin_sync._refusal(repo)

    assert (message is not None) is refused
    if message is not None:
        assert "forge@forge" in message


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def test_sync_without_a_target_release_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No manifest and no pin names no release to install."""
    monkeypatch.setattr(plugin_sync, "_claude", _fail_loud)
    bare = tmp_path / "bare"
    bare.mkdir()

    assert plugin_sync.sync(bare) == plugin_sync.EXIT_UNKNOWN


@pytest.mark.parametrize("quiet", [False, True], ids=["verbose", "quiet"])
def test_sync_when_already_current_changes_nothing(
    repo: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    quiet: bool,
) -> None:
    """A checkout on its release costs no network or Claude call; quiet says nothing.

    Args:
        quiet: Whether the sync runs in quiet mode.
    """
    write_install(claude_home, repo, VERSION)
    monkeypatch.setattr(plugin_sync, "_claude", _fail_loud)
    monkeypatch.setattr(plugin_sync, "run_git", _fail_loud)

    with caplog.at_level(logging.INFO):
        rc = plugin_sync.sync(repo, quiet=quiet)

    assert rc == plugin_sync.EXIT_OK
    assert bool(caplog.records) is not quiet


def test_sync_check_mode_reports_without_touching_anything(
    repo: Path,
    xdg_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--check`` fails for a missing install and has no side effects."""
    monkeypatch.setattr(plugin_sync, "_claude", _fail_loud)
    monkeypatch.setattr(plugin_sync, "run_git", _fail_loud)

    assert plugin_sync.sync(repo, check=True) == plugin_sync.EXIT_FAILED
    assert not xdg_cache.exists()


def test_sync_refuses_before_changing_anything(
    repo: Path,
    claude_home: Path,
    xdg_cache: Path,
    online: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conflicting user-scope install stops the run before network or Claude."""
    write_install(claude_home, None, "1.0.0", key="forge@forge", scope="user")
    monkeypatch.setattr(plugin_sync, "_claude", _fail_loud)
    monkeypatch.setattr(plugin_sync, "run_git", _fail_loud)

    assert plugin_sync.sync(repo) == plugin_sync.EXIT_FAILED
    assert not xdg_cache.exists()


@pytest.mark.parametrize("quiet", [False, True], ids=["verbose", "quiet"])
def test_sync_without_claude_is_a_quiet_success(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    quiet: bool,
) -> None:
    """No Claude Code means nothing to install; hooks stay silent about it.

    Args:
        quiet: Whether the sync runs in quiet mode.
    """
    monkeypatch.setattr(plugin_sync.shutil, "which", lambda _n: None)
    monkeypatch.setattr(plugin_sync, "run_git", _fail_loud)

    with caplog.at_level(logging.INFO):
        rc = plugin_sync.sync(repo, quiet=quiet)

    assert rc == plugin_sync.EXIT_OK
    assert bool(caplog.records) is not quiet


def test_sync_unresolvable_commit_writes_no_marketplace(
    repo: Path,
    xdg_cache: Path,
    online: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline (or an unpublished tag) is "unknown", before any file is written."""
    monkeypatch.setattr(plugin_sync, "run_git", _remote(OSError("offline")))
    monkeypatch.setattr(plugin_sync, "_claude", _fail_loud)

    assert plugin_sync.sync(repo) == plugin_sync.EXIT_UNKNOWN
    assert not xdg_cache.exists()


def test_sync_installs_and_replaces_only_this_checkouts_old_install(
    sync_env: SyncEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The full run: register, install, drop this repo's older key, warn to restart.

    SCENARIO: the checkout moved from 1.0.0 to 1.2.3 while another project
    still uses 0.9.0 through the same machine-wide records.
    MOCK SETUP: records for both; a recording ``_claude``.
    EXPECTED BEHAVIOR: exactly add/install/uninstall(1.0.0), in that order,
    the other project's install untouched, exit 0, restart advised.
    """
    repo, tmp_path, claude_home, xdg_cache = sync_env
    write_install(claude_home, repo, "1.0.0")
    write_install(claude_home, tmp_path / "other", "0.9.0")
    fake = FakeClaude(claude_home, repo)
    monkeypatch.setattr(plugin_sync, "_claude", fake)

    with caplog.at_level(logging.INFO):
        rc = plugin_sync.sync(repo)

    marketplace = xdg_cache / "forge" / "marketplaces" / "forge-v1-2-3"
    assert rc == plugin_sync.EXIT_OK
    assert [args for args, _ in fake.calls] == [
        ["marketplace", "add", str(marketplace), "--scope", "local"],
        ["install", "forge@forge-v1-2-3", "--scope", "local"],
        ["uninstall", "forge@forge-v1-0-0", "--scope", "local"],
    ]
    assert {cwd for _, cwd in fake.calls} == {repo}
    assert "restart" in caplog.text


@pytest.mark.parametrize(
    ("failing", "ran"),
    [("marketplace", ["marketplace"]), ("install", ["marketplace", "install"])],
)
def test_sync_stops_when_claude_refuses_a_step(
    sync_env: SyncEnv,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
    ran: list[str],
) -> None:
    """A failed step stops the run there and never removes the old install.

    Args:
        failing: The ``claude`` verb that reports failure.
        ran: The verbs expected to run, in order, up to the failure.
    """
    repo, _tmp_path, claude_home, _xdg_cache = sync_env
    write_install(claude_home, repo, "1.0.0")
    fake = FakeClaude(claude_home, repo, fail_on=failing)
    monkeypatch.setattr(plugin_sync, "_claude", fake)

    assert plugin_sync.sync(repo) == plugin_sync.EXIT_FAILED
    assert fake.verbs == ran


def test_sync_survives_a_failed_cleanup_of_the_old_install(
    sync_env: SyncEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The new install is in place, so a stuck old one is a warning, not a failure."""
    repo, _tmp_path, claude_home, _xdg_cache = sync_env
    write_install(claude_home, repo, "1.0.0")
    monkeypatch.setattr(
        plugin_sync, "_claude", FakeClaude(claude_home, repo, fail_on="uninstall")
    )

    with caplog.at_level(logging.WARNING):
        rc = plugin_sync.sync(repo)

    assert rc == plugin_sync.EXIT_OK
    assert "could not remove" in caplog.text


def test_sync_fails_when_claude_succeeds_but_records_nothing(
    repo: Path,
    claude_home: Path,
    xdg_cache: Path,
    online: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success is judged by the install record, not by Claude's say-so."""
    monkeypatch.setattr(
        plugin_sync, "_claude", FakeClaude(claude_home, repo, record_install=False)
    )

    assert plugin_sync.sync(repo) == plugin_sync.EXIT_FAILED


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_is_a_no_op_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI has no Claude Code session to configure."""
    monkeypatch.setattr(plugin_sync, "is_ci", lambda: True)
    monkeypatch.setattr(plugin_sync, "sync", _fail_loud)

    assert plugin_sync.main([]) == plugin_sync.EXIT_OK


def test_main_passes_check_and_quiet_to_sync(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flags reach :func:`sync` and its exit code is returned."""
    seen: list[tuple[Path, bool, bool]] = []

    def _record(root: Path, *, check: bool = False, quiet: bool = False) -> int:
        seen.append((root, check, quiet))
        return 7

    monkeypatch.setattr(plugin_sync, "is_ci", lambda: False)
    monkeypatch.setattr(plugin_sync, "repo_root", lambda: repo)
    monkeypatch.setattr(plugin_sync, "sync", _record)

    assert plugin_sync.main(["--check", "--quiet"]) == 7
    assert seen == [(repo, True, True)]
