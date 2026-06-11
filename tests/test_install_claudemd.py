"""Tests for forge.install_claudemd (v1.1.3+ split-layout)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

from forge import install_claudemd


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_FAKE_FOUNDATION = "# FOUNDATION.md — Fake\n\nFake foundation content for testing.\n"


def _patch_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    foundation: str = _FAKE_FOUNDATION,
    version: str = "1.2.3",
) -> None:
    """Stub the FOUNDATION text and forge version readers.

    Args:
        monkeypatch: Pytest fixture.
        foundation: Text to return from ``_foundation_text``.
        version: Version string to return from ``_forge_version``.
    """
    monkeypatch.setattr(install_claudemd, "_foundation_text", lambda: foundation)
    monkeypatch.setattr(install_claudemd, "_forge_version", lambda: version)
    # No real upstream queries during tests. Tests that exercise
    # `check_upstream` directly pass their own `fetch=` and isolated
    # cache path; the global stub here guards every other test from
    # hitting the network if the orchestrator wires `check_upstream`
    # into main() (it does).
    monkeypatch.setattr(install_claudemd, "check_upstream", lambda **_kw: None)


# ---------------------------------------------------------------------------
# sync_foundation: writes/updates FOUNDATION.md with markers + version stamp.
# ---------------------------------------------------------------------------


def test_creates_fresh_foundation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sync_foundation() creates FOUNDATION.md when it does not exist."""
    _patch_inputs(monkeypatch)
    target = tmp_path / "FOUNDATION.md"
    changed = install_claudemd.sync_foundation(target)
    assert changed
    text = target.read_text()
    assert "<!-- forge:foundation-managed v1 START -->" in text
    assert "<!-- forge:foundation-managed v1 END -->" in text
    assert "Fake foundation content" in text
    assert "Synced from forge-scripts 1.2.3" in text


def test_idempotent_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running sync_foundation() on an in-sync file makes no changes."""
    _patch_inputs(monkeypatch)
    target = tmp_path / "FOUNDATION.md"
    install_claudemd.sync_foundation(target)
    changed = install_claudemd.sync_foundation(target)
    assert not changed


def test_version_drift_is_not_foundation_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Version-string change alone is not flagged as drift.

    A dev install embeds a ``dev<N>+g<hash>`` version that differs every
    commit, but the FOUNDATION text is identical. ``--check`` must
    return 'in sync'.
    """
    target = tmp_path / "FOUNDATION.md"
    _patch_inputs(monkeypatch, version="1.0.1.dev3+gabc1234")
    install_claudemd.sync_foundation(target)

    _patch_inputs(monkeypatch, version="1.0.1.dev9+gdef9876")
    drift = install_claudemd.sync_foundation(target, check_only=True)
    assert not drift, "version-only change must not trigger drift"


def test_detects_drift_on_foundation_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing the foundation source produces drift on next run."""
    _patch_inputs(monkeypatch, foundation="# Old\n")
    target = tmp_path / "FOUNDATION.md"
    install_claudemd.sync_foundation(target)

    _patch_inputs(monkeypatch, foundation="# New\n")
    assert install_claudemd.sync_foundation(target, check_only=True) is True
    install_claudemd.sync_foundation(target)
    assert "# New" in target.read_text()
    assert "# Old" not in target.read_text()


def test_refuses_unmanaged_foundation_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing FOUNDATION.md without markers is left alone unless --force."""
    _patch_inputs(monkeypatch)
    target = tmp_path / "FOUNDATION.md"
    target.write_text("# My own FOUNDATION.md\n\nNo markers here.\n")
    changed = install_claudemd.sync_foundation(target)
    assert not changed
    assert "My own FOUNDATION.md" in target.read_text()


def test_force_overwrites_unmanaged_foundation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force overwrites an unmanaged FOUNDATION.md with the managed file."""
    _patch_inputs(monkeypatch)
    target = tmp_path / "FOUNDATION.md"
    target.write_text("# Hand-rolled\nUser wrote this.\n")
    changed = install_claudemd.sync_foundation(target, force=True)
    assert changed
    text = target.read_text()
    assert text.startswith("<!-- forge:foundation-managed v1 START -->")
    assert "Hand-rolled" not in text
    assert "Fake foundation content" in text


# ---------------------------------------------------------------------------
# scaffold_claudemd: writes a minimal CLAUDE.md with @FOUNDATION.md.
# ---------------------------------------------------------------------------


def test_scaffold_creates_minimal_claudemd(tmp_path: Path) -> None:
    """scaffold_claudemd() creates a CLAUDE.md with the include directive."""
    target = tmp_path / "CLAUDE.md"
    created = install_claudemd.scaffold_claudemd(target)
    assert created
    text = target.read_text()
    assert "@FOUNDATION.md" in text
    assert "## Repo-specific rules" in text


def test_scaffold_is_noop_when_claudemd_exists(tmp_path: Path) -> None:
    """scaffold_claudemd() never overwrites an existing CLAUDE.md."""
    target = tmp_path / "CLAUDE.md"
    target.write_text("# existing content\n")
    created = install_claudemd.scaffold_claudemd(target)
    assert not created
    assert target.read_text() == "# existing content\n"


# ---------------------------------------------------------------------------
# warn_claudemd_missing_include: stderr/log warning when @ directive absent.
# ---------------------------------------------------------------------------


def test_warn_when_claudemd_lacks_include(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """warn_claudemd_missing_include logs when CLAUDE.md lacks @FOUNDATION.md."""
    target = tmp_path / "CLAUDE.md"
    target.write_text("# Project\n\nNo include directive.\n")
    install_claudemd.warn_claudemd_missing_include(target)
    assert "does not include `@FOUNDATION.md`" in caplog.text


def test_no_warn_when_claudemd_has_include(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """warn_claudemd_missing_include is silent when the directive is present."""
    target = tmp_path / "CLAUDE.md"
    target.write_text("# Project\n\n@FOUNDATION.md\n\n## Rules\n")
    install_claudemd.warn_claudemd_missing_include(target)
    assert "does not include" not in caplog.text


def test_no_warn_when_claudemd_absent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """warn_claudemd_missing_include is silent when CLAUDE.md does not exist."""
    install_claudemd.warn_claudemd_missing_include(tmp_path / "CLAUDE.md")
    assert "does not include" not in caplog.text


# ---------------------------------------------------------------------------
# migrate_inline_block: convert v1.1.2 inline-block CLAUDE.md to split layout.
# ---------------------------------------------------------------------------


def test_migrate_replaces_inline_block_with_include(tmp_path: Path) -> None:
    """migrate_inline_block strips the inline block and inserts @FOUNDATION.md."""
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        "# CLAUDE.md\n\n"
        "<!-- forge:foundation-managed v1 START -->\n"
        "<!-- DO NOT EDIT between START/END. Synced from forge-scripts 1.1.2 -->\n\n"
        "Foundation content that used to be inline.\n\n"
        "<!-- forge:foundation-managed v1 END -->\n"
        "\n"
        "## Repo-specific rules\n\n"
        "- My team rule.\n",
    )
    migrated = install_claudemd.migrate_inline_block(target)
    assert migrated
    text = target.read_text()
    assert "@FOUNDATION.md" in text
    assert "Foundation content that used to be inline" not in text
    assert "## Repo-specific rules" in text
    assert "My team rule" in text
    # forge-managed markers removed from CLAUDE.md.
    assert "forge:foundation-managed" not in text


def test_migrate_noop_when_already_on_split_layout(tmp_path: Path) -> None:
    """migrate_inline_block is a no-op when @FOUNDATION.md already present."""
    target = tmp_path / "CLAUDE.md"
    body = "# CLAUDE.md\n\n@FOUNDATION.md\n\n## Rules\n"
    target.write_text(body)
    migrated = install_claudemd.migrate_inline_block(target)
    assert not migrated
    assert target.read_text() == body


def test_migrate_noop_when_claudemd_absent(tmp_path: Path) -> None:
    """migrate_inline_block on a missing CLAUDE.md returns False (no error)."""
    migrated = install_claudemd.migrate_inline_block(tmp_path / "CLAUDE.md")
    assert not migrated


# ---------------------------------------------------------------------------
# main: CLI entry point integration.
# ---------------------------------------------------------------------------


def test_main_default_writes_foundation_and_scaffolds_claudemd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default invocation creates FOUNDATION.md, CLAUDE.md, plus .claude/ scaffolds."""
    _patch_inputs(monkeypatch)
    monkeypatch.setattr(install_claudemd, "repo_root", lambda: tmp_path)
    with patch.object(install_claudemd.sys, "argv", ["install-forge-claude-md"]):
        rc = install_claudemd.main()
    assert rc == 0
    assert (tmp_path / "FOUNDATION.md").is_file()
    assert (tmp_path / "CLAUDE.md").is_file()
    assert "@FOUNDATION.md" in (tmp_path / "CLAUDE.md").read_text()
    # Consumer Claude Code hooks scaffold:
    assert (tmp_path / ".claude" / "hooks").is_dir()
    readme = (tmp_path / ".claude" / "hooks" / "README.md").read_text()
    assert "${CLAUDE_PROJECT_DIR}" in readme
    assert (tmp_path / ".claude" / "settings.json").is_file()


def test_scaffold_claude_settings_creates_when_missing(tmp_path: Path) -> None:
    """`.claude/settings.json` is written with empty hook arrays when absent."""
    settings = tmp_path / ".claude" / "settings.json"
    assert install_claudemd.scaffold_claude_settings(settings) is True
    content = settings.read_text()
    assert '"PreToolUse"' in content
    assert '"PostToolUse"' in content


def test_scaffold_claude_settings_leaves_existing_alone(tmp_path: Path) -> None:
    """An existing `.claude/settings.json` is never overwritten."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": {"PreToolUse": [{"matcher": "Bash"}]}}')
    assert install_claudemd.scaffold_claude_settings(settings) is False
    # File untouched.
    assert "Bash" in settings.read_text()


def test_ensure_claude_hooks_dir_creates_dir_and_readme(tmp_path: Path) -> None:
    """`.claude/hooks/` and its README are created from scratch."""
    hooks_dir = tmp_path / ".claude" / "hooks"
    assert install_claudemd.ensure_claude_hooks_dir(hooks_dir) is True
    assert hooks_dir.is_dir()
    readme = (hooks_dir / "README.md").read_text()
    assert "${CLAUDE_PROJECT_DIR}/.claude/hooks" in readme


def test_ensure_claude_hooks_dir_idempotent_when_present(tmp_path: Path) -> None:
    """Re-running on an existing dir + README is a no-op."""
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "README.md").write_text("custom user content")
    assert install_claudemd.ensure_claude_hooks_dir(hooks_dir) is False
    # Custom README untouched.
    assert (hooks_dir / "README.md").read_text() == "custom user content"


def test_main_check_mode_exits_nonzero_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``install-forge-claude-md --check`` returns 1 on FOUNDATION.md drift."""
    _patch_inputs(monkeypatch)
    monkeypatch.setattr(install_claudemd, "repo_root", lambda: tmp_path)
    with patch.object(
        install_claudemd.sys,
        "argv",
        ["install-forge-claude-md", "--check"],
    ):
        rc = install_claudemd.main()
    assert rc == 1


def test_main_check_mode_exits_zero_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``install-forge-claude-md --check`` returns 0 when FOUNDATION.md is in sync."""
    _patch_inputs(monkeypatch)
    monkeypatch.setattr(install_claudemd, "repo_root", lambda: tmp_path)
    install_claudemd.sync_foundation(tmp_path / "FOUNDATION.md")
    # Scaffold a CLAUDE.md with the include so warn_claudemd_missing_include is quiet.
    (tmp_path / "CLAUDE.md").write_text("# Project\n\n@FOUNDATION.md\n")
    with patch.object(
        install_claudemd.sys,
        "argv",
        ["install-forge-claude-md", "--check"],
    ):
        rc = install_claudemd.main()
    assert rc == 0


def test_main_migrate_converts_inline_block_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--migrate converts a v1.1.2 inline-block layout to the split layout."""
    _patch_inputs(monkeypatch)
    monkeypatch.setattr(install_claudemd, "repo_root", lambda: tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\n"
        "<!-- forge:foundation-managed v1 START -->\n"
        "Stale inline foundation content.\n"
        "<!-- forge:foundation-managed v1 END -->\n\n"
        "## My rules\n",
    )
    with patch.object(
        install_claudemd.sys,
        "argv",
        ["install-forge-claude-md", "--migrate"],
    ):
        rc = install_claudemd.main()
    assert rc == 0
    claudemd_text = (tmp_path / "CLAUDE.md").read_text()
    assert "@FOUNDATION.md" in claudemd_text
    assert "Stale inline foundation content" not in claudemd_text
    assert "## My rules" in claudemd_text
    # FOUNDATION.md was also created by the same run.
    assert (tmp_path / "FOUNDATION.md").is_file()
    assert "Fake foundation content" in (tmp_path / "FOUNDATION.md").read_text()


# ---------------------------------------------------------------------------
# Upstream-version drift warning (issue #34)
# ---------------------------------------------------------------------------


def test_is_behind_true_when_strictly_older() -> None:
    """Older installed → behind = True."""
    assert install_claudemd._is_behind("1.2.2", "v1.2.9") is True


def test_is_behind_false_when_equal_or_newer() -> None:
    """Same or newer installed → behind = False."""
    assert install_claudemd._is_behind("1.2.9", "v1.2.9") is False
    assert install_claudemd._is_behind("1.3.0", "v1.2.9") is False


def test_is_behind_false_when_either_unresolvable() -> None:
    """None on either side → no warning."""
    assert install_claudemd._is_behind(None, "v1.2.9") is False
    assert install_claudemd._is_behind("1.2.2", None) is False
    assert install_claudemd._is_behind("garbage", "v1.2.9") is False


def test_installed_plugin_version_reads_manifest(tmp_path: Path) -> None:
    """The most-recently-installed forge@forge entry's version is returned."""
    manifest = tmp_path / "installed_plugins.json"
    manifest.write_text(
        json.dumps(
            {
                "plugins": {
                    "forge@forge": [
                        {"version": "v1.1.0", "installed_at": "2026-05-01"},
                        {"version": "v1.2.5", "installed_at": "2026-05-20"},
                    ]
                }
            }
        )
    )
    assert install_claudemd._installed_plugin_version(manifest) == "v1.2.5"


def test_installed_plugin_version_returns_none_when_missing(tmp_path: Path) -> None:
    """Absent manifest → None."""
    assert install_claudemd._installed_plugin_version(tmp_path / "no-such.json") is None


def test_installed_plugin_version_returns_none_when_forge_absent(
    tmp_path: Path,
) -> None:
    """Manifest without forge@forge → None."""
    manifest = tmp_path / "installed_plugins.json"
    manifest.write_text(json.dumps({"plugins": {"other@other": [{"version": "1"}]}}))
    assert install_claudemd._installed_plugin_version(manifest) is None


def test_installed_plugin_version_reads_single_dict_shape(tmp_path: Path) -> None:
    """Manifest with forge@forge as a single dict (not a list) is supported."""
    manifest = tmp_path / "installed_plugins.json"
    manifest.write_text(json.dumps({"plugins": {"forge@forge": {"version": "v1.2.5"}}}))
    assert install_claudemd._installed_plugin_version(manifest) == "v1.2.5"


def test_read_upstream_cache_returns_channel_tags_when_fresh(tmp_path: Path) -> None:
    """A fresh new-schema cache entry round-trips to ChannelTags."""
    cache = tmp_path / "upstream_check.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "main_tag": "v1.2.0",
                "dev_tag": "v1.2.11",
            }
        )
    )
    out = install_claudemd._read_upstream_cache(cache, ttl_hours=24)
    assert out == install_claudemd.ChannelTags(main_tag="v1.2.0", dev_tag="v1.2.11")


def test_read_upstream_cache_returns_none_when_stale(tmp_path: Path) -> None:
    """An entry older than TTL is treated as missing."""
    cache = tmp_path / "upstream_check.json"
    stale_time = datetime.now(UTC) - timedelta(hours=48)
    cache.write_text(
        json.dumps(
            {
                "checked_at": stale_time.isoformat(timespec="seconds"),
                "main_tag": "v1.2.0",
                "dev_tag": "v1.2.5",
            }
        )
    )
    assert install_claudemd._read_upstream_cache(cache, ttl_hours=24) is None


def test_read_upstream_cache_treats_old_schema_as_miss(tmp_path: Path) -> None:
    """Pre-rollout cache shape (``latest_tag`` only) is silently ignored.

    Forces a re-fetch under the new schema. Consumers don't need to
    delete the cache file by hand after upgrading.
    """
    cache = tmp_path / "upstream_check.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "latest_tag": "v1.2.11",  # pre-rollout schema
            }
        )
    )
    assert install_claudemd._read_upstream_cache(cache, ttl_hours=24) is None


def _channel_tags(
    main_tag: str | None = None,
    dev_tag: str | None = None,
) -> install_claudemd.ChannelTags:
    return install_claudemd.ChannelTags(main_tag=main_tag, dev_tag=dev_tag)


def _force_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub :func:`forge.run_context.is_non_interactive` to return False.

    ``check_upstream`` self-skips in non-interactive contexts per
    FOUNDATION §15. Pytest runs are non-TTY, so without this stub any
    test that exercises the warning path would be silently skipped by
    the CI bypass.
    """
    monkeypatch.setattr(install_claudemd, "is_non_interactive", lambda: False)


def test_check_upstream_warns_when_behind_both_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Behind on both main and dev → warning shows both channels + cadence note."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: "1.1.0"
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=tmp_path / "no-plugin.json",
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.2.0", dev_tag="v1.2.11"),
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "forge-scripts is behind upstream" in msg
    assert "main (slower, minor-only)" in msg
    assert "dev  (faster, every patch)" in msg
    assert "Both channels publish stable semver" in msg


def test_check_upstream_skipped_in_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``is_non_interactive=True`` short-circuits ``check_upstream``."""
    monkeypatch.setattr(install_claudemd, "is_non_interactive", lambda: True)
    fetch_calls: list[int] = []

    def _no_fetch() -> install_claudemd.ChannelTags:
        fetch_calls.append(1)
        return _channel_tags()

    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=tmp_path / "no-plugin.json",
            cache_ttl_hours=24,
            fetch=_no_fetch,
        )
    assert fetch_calls == []  # short-circuited before network
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_check_upstream_surfaces_freeze_hint_for_branch_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``.devN+gHASH`` install (branch-ref pin) gets a freeze-cache hint.

    Covers issue #57: pip caches branch refs and silently no-ops on
    re-install, so consumers freeze without realising it. The warning
    points them at ``forge-upgrade --apply`` or the manual
    ``--force-reinstall --no-deps`` form.
    """
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd,
        "_installed_forge_scripts_version",
        lambda: "1.2.13.dev5+gabc1234",  # the fingerprint of a branch-ref install
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=tmp_path / "no-plugin.json",
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.3.0", dev_tag="v1.3.1"),
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "branch-ref install" in msg
    assert "forge-upgrade --apply" in msg


def test_check_upstream_warns_dev_only_when_current_on_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Current on main, dev has new patches → 'newer patches available on dev'.

    The consumer is correctly tracking the slower channel; the warning
    should reflect that, not falsely say they're "behind upstream".
    """
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: "1.2.10"
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=tmp_path / "no-plugin.json",
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.2.10", dev_tag="v1.2.13"),
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "newer patches available on the dev channel" in msg
    assert "is behind upstream" not in msg


def test_check_upstream_collapses_when_main_equals_dev(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Immediately after promotion, main_tag == dev_tag → single-line output."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: "1.2.10"
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=tmp_path / "no-plugin.json",
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.2.13", dev_tag="v1.2.13"),
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "latest (main = dev): v1.2.13" in msg
    # The dual-channel rows shouldn't appear when collapsed.
    assert "(slower, minor-only)" not in msg


def test_check_upstream_warns_when_plugin_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An older installed plugin → warning labelled with plugin subject."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: "1.2.13"
    )
    plugins_file = tmp_path / "installed_plugins.json"
    plugins_file.write_text(
        json.dumps({"plugins": {"forge@forge": [{"version": "v1.1.3"}]}})
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=plugins_file,
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.2.13", dev_tag="v1.2.13"),
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "Claude plugin 'forge'" in msg
    assert "/plugin" in msg


def test_check_upstream_silent_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Installed >= both channels → no warnings."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: "1.2.13"
    )
    plugins_file = tmp_path / "installed_plugins.json"
    plugins_file.write_text(
        json.dumps({"plugins": {"forge@forge": [{"version": "v1.2.13"}]}})
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=plugins_file,
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.2.13", dev_tag="v1.2.13"),
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("behind" in m for m in msgs)
    assert not any("newer patches" in m for m in msgs)


def test_check_upstream_skipped_on_fetch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fetch returning ChannelTags(None, None) → info skip line, no warning."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    with caplog.at_level("INFO"):
        install_claudemd.check_upstream(
            plugins_file=tmp_path / "no-plugin.json",
            cache_ttl_hours=24,
            fetch=_channel_tags,
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("upstream check skipped" in m for m in msgs)


def test_check_upstream_throttles_via_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh cache entry shortcuts the fetch() call entirely."""
    _force_interactive(monkeypatch)
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "main_tag": "v1.2.13",
                "dev_tag": "v1.2.13",
            }
        )
    )
    monkeypatch.setattr(install_claudemd, "_upstream_cache_path", lambda: cache)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: "1.2.13"
    )

    calls = {"count": 0}

    def _fake_fetch() -> install_claudemd.ChannelTags:
        calls["count"] += 1
        return _channel_tags(
            main_tag="v9.9.9", dev_tag="v9.9.9"
        )  # would warn if reached

    install_claudemd.check_upstream(
        plugins_file=tmp_path / "no-plugin.json",
        cache_ttl_hours=24,
        fetch=_fake_fetch,
    )
    assert calls["count"] == 0


# ---------------------------------------------------------------------------
# _read_configured_channel + channel-mismatch hint (#110)
# ---------------------------------------------------------------------------


def _write_settings(path: Path, ref: str | None) -> None:
    """Write a ``settings.json`` with an optional forge marketplace ref.

    Args:
        path: File to write.
        ref: When set, populates ``extraKnownMarketplaces.forge.source.ref``.
            When ``None``, the ``source`` block is written without a ``ref``
            key — the consumer registered the marketplace but never pinned
            a channel.
    """
    source: dict[str, str] = {"source": "github", "repo": "owner/forge"}
    if ref is not None:
        source["ref"] = ref
    path.write_text(
        json.dumps({"extraKnownMarketplaces": {"forge": {"source": source}}})
    )


def _write_plugin_manifest(path: Path, version: str) -> None:
    """Write a minimal ``installed_plugins.json`` listing forge at *version*.

    Args:
        path: File to write.
        version: Version string to record under ``plugins.forge@forge``.
    """
    path.write_text(json.dumps({"plugins": {"forge@forge": {"version": version}}}))


def test_read_configured_channel_returns_ref_when_set(tmp_path: Path) -> None:
    """Returns the configured ref string when the settings block is well-formed."""
    settings = tmp_path / "settings.json"
    _write_settings(settings, ref="dev")
    assert install_claudemd._read_configured_channel(settings) == "dev"


def test_read_configured_channel_none_when_file_absent(tmp_path: Path) -> None:
    """Returns ``None`` when the settings file does not exist."""
    assert install_claudemd._read_configured_channel(tmp_path / "no.json") is None


def test_read_configured_channel_none_when_malformed_json(tmp_path: Path) -> None:
    """Returns ``None`` when the file is not valid JSON, no exception escapes."""
    settings = tmp_path / "settings.json"
    settings.write_text("{ not valid json")
    assert install_claudemd._read_configured_channel(settings) is None


def test_read_configured_channel_none_when_no_ref_key(tmp_path: Path) -> None:
    """Returns ``None`` when the marketplace block lacks a ``ref`` field."""
    settings = tmp_path / "settings.json"
    _write_settings(settings, ref=None)
    assert install_claudemd._read_configured_channel(settings) is None


def test_read_configured_channel_none_when_marketplaces_is_null(
    tmp_path: Path,
) -> None:
    """``extraKnownMarketplaces: null`` returns ``None``, not an AttributeError."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"extraKnownMarketplaces": None}))
    assert install_claudemd._read_configured_channel(settings) is None


def test_read_configured_channel_none_when_forge_entry_is_null(
    tmp_path: Path,
) -> None:
    """``extraKnownMarketplaces.forge: null`` returns ``None`` cleanly."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"extraKnownMarketplaces": {"forge": None}}))
    assert install_claudemd._read_configured_channel(settings) is None


def test_check_upstream_emits_channel_hint_when_ref_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plugin warning + explicit ref → hint pointing at the docs is appended."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: None
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    plugins = tmp_path / "installed_plugins.json"
    _write_plugin_manifest(plugins, version="v1.2.0")
    settings = tmp_path / "settings.json"
    _write_settings(settings, ref="dev")
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=plugins,
            settings_file=settings,
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.3.0", dev_tag="v1.3.5"),
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "Configured marketplace ref: 'dev'" in msg
    assert "docs/claude-code-plugin.md" in msg


def test_check_upstream_no_hint_when_no_settings_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plugin warning fires but settings.json absent → no hint appended."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: None
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    plugins = tmp_path / "installed_plugins.json"
    _write_plugin_manifest(plugins, version="v1.2.0")
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=plugins,
            settings_file=tmp_path / "no-settings.json",
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.3.0", dev_tag="v1.3.5"),
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "Claude plugin 'forge' is behind" in msg
    assert "Configured marketplace ref" not in msg


def test_check_upstream_no_hint_when_ref_not_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plugin warning fires but consumer has no ``ref`` configured → no hint."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: None
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    plugins = tmp_path / "installed_plugins.json"
    _write_plugin_manifest(plugins, version="v1.2.0")
    settings = tmp_path / "settings.json"
    _write_settings(settings, ref=None)
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=plugins,
            settings_file=settings,
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.3.0", dev_tag="v1.3.5"),
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "Claude plugin 'forge' is behind" in msg
    assert "Configured marketplace ref" not in msg


def test_check_upstream_no_hint_when_plugin_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No plugin warning → no hint, even when ``ref`` is set."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(
        install_claudemd, "_installed_forge_scripts_version", lambda: None
    )
    monkeypatch.setattr(
        install_claudemd, "_upstream_cache_path", lambda: tmp_path / "cache.json"
    )
    plugins = tmp_path / "installed_plugins.json"
    _write_plugin_manifest(plugins, version="v1.3.5")
    settings = tmp_path / "settings.json"
    _write_settings(settings, ref="dev")
    with caplog.at_level("WARNING"):
        install_claudemd.check_upstream(
            plugins_file=plugins,
            settings_file=settings,
            cache_ttl_hours=24,
            fetch=lambda: _channel_tags(main_tag="v1.3.0", dev_tag="v1.3.5"),
        )
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "Configured marketplace ref" not in msg
