"""Tests for forge.install_githooks."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from forge import install_githooks
from tests.conftest import CapturedCalls, make_fake_run


if TYPE_CHECKING:
    from pathlib import Path


def test_hook_specs_cover_lifecycle_events() -> None:
    """The installer ships hooks for the 3 lifecycle events we care about."""
    names = {spec.name for spec in install_githooks.HOOKS}
    assert names == {"pre-commit", "post-merge", "post-checkout"}


# ---------------------------------------------------------------------------
# Wrapper-pattern contract — all three hook bodies are one-liners that call the
# matching forge-shipped CLI. Logic that used to live inside the generated
# shell file now lives in the entrypoint Python modules
# (``forge.precommit``, ``forge.post_merge``, ``forge.post_checkout``).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hook_name", "expected_body"),
    [
        ("pre-commit", 'forge-precommit "$@"'),
        ("post-merge", 'forge-post-merge "$@"'),
        ("post-checkout", 'forge-post-checkout "$@"'),
    ],
)
def test_hook_body_is_one_line_wrapper(hook_name: str, expected_body: str) -> None:
    """Every shipped hook body is a single-line wrapper around a forge CLI.

    Args:
        hook_name: Hook name.
        expected_body: Canonical one-liner shape.
    """
    spec = next(s for s in install_githooks.HOOKS if s.name == hook_name)
    assert spec.body == expected_body


def test_no_hook_body_embeds_ci_bypass_or_drift_logic() -> None:
    """Hook bodies hand off to Python; no shell-side CI markers or drift checks remain.

    The FOUNDATION §15 contract lives in ``forge.run_context``. Any
    shell-side duplicate would drift the next time a CI marker is
    added to ``_CI_MARKERS``.
    """
    for spec in install_githooks.HOOKS:
        assert "_CI_MARKERS" not in spec.body
        assert "CI-aware bypass" not in spec.body
        assert "install-forge-claude-md --check" not in spec.body


def test_write_hook_creates_file_with_marker(tmp_path: Path) -> None:
    """_write_hook materializes a hook file and embeds the managed marker + body sha."""
    spec = install_githooks.HOOKS[0]  # pre-commit
    hook = tmp_path / ".githooks" / spec.name
    install_githooks._write_hook(hook, spec, "1.2.13", force=False)
    assert hook.is_file()
    content = hook.read_text()
    body_sha = install_githooks._compute_body_sha(spec.body)
    assert install_githooks.managed_marker("1.2.13", body_sha) in content
    assert spec.body.splitlines()[0] in content
    assert hook.stat().st_mode & 0o111


def test_write_hook_leaves_user_customized_file_alone(tmp_path: Path) -> None:
    """A hook without the managed marker is not overwritten without --force."""
    spec = install_githooks.HOOKS[0]
    hook = tmp_path / ".githooks" / spec.name
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\necho user wrote this\n")
    written = install_githooks._write_hook(hook, spec, "1.2.13", force=False)
    assert not written
    assert "user wrote this" in hook.read_text()


def test_write_hook_force_overwrites_user_customized_file(tmp_path: Path) -> None:
    """--force replaces a user-customized hook with the managed template."""
    spec = install_githooks.HOOKS[0]
    hook = tmp_path / ".githooks" / spec.name
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\necho user wrote this\n")
    written = install_githooks._write_hook(hook, spec, "1.2.13", force=True)
    assert written
    assert install_githooks.managed_marker("1.2.13") in hook.read_text()


def test_write_hook_idempotent_when_already_in_sync(tmp_path: Path) -> None:
    """Re-running on an up-to-date managed hook is a no-op."""
    spec = install_githooks.HOOKS[0]
    hook = tmp_path / ".githooks" / spec.name
    install_githooks._write_hook(hook, spec, "1.2.13", force=False)
    written = install_githooks._write_hook(hook, spec, "1.2.13", force=False)
    assert not written


def test_write_hook_skips_consumer_modified_wrapper(tmp_path: Path) -> None:
    """A managed file whose body diverges from the marker's body-sha survives refresh.

    The wrapper-pattern contract: consumers extend a hook by
    editing the file (e.g. adding a repo-specific shell line after
    the forge CLI call). The body-sha embedded in the marker tells
    refresh "do not touch this file" — only --force overrides.
    """
    spec = install_githooks.HOOKS[0]
    hook = tmp_path / ".githooks" / spec.name
    install_githooks._write_hook(hook, spec, "1.2.13", force=False)
    # Append a consumer step after the forge CLI call.
    hook.write_text(hook.read_text() + "./scripts/install-editable.sh\n")
    written = install_githooks._write_hook(
        hook, spec, "1.2.13", force=False, refresh=True
    )
    assert not written
    assert "./scripts/install-editable.sh" in hook.read_text()


def test_write_hook_force_overwrites_consumer_modified_with_backup(
    tmp_path: Path,
) -> None:
    """--force overrides a consumer-modified wrapper but saves a versioned backup."""
    spec = install_githooks.HOOKS[0]
    hook = tmp_path / ".githooks" / spec.name
    install_githooks._write_hook(hook, spec, "1.2.13", force=False)
    original = hook.read_text() + "./scripts/install-editable.sh\n"
    hook.write_text(original)
    written = install_githooks._write_hook(
        hook, spec, "1.2.13", force=True, refresh=True
    )
    assert written
    assert "./scripts/install-editable.sh" not in hook.read_text()
    backup = hook.with_name(f"{hook.name}.before-forge-v1.2.13.bak")
    assert backup.is_file()
    assert "./scripts/install-editable.sh" in backup.read_text()


def test_write_hook_upgrades_outdated_forge_version(tmp_path: Path) -> None:
    """Stale forge-version marker (body unchanged) → rewrite on plain install."""
    spec = install_githooks.HOOKS[0]
    hook = tmp_path / ".githooks" / spec.name
    install_githooks._write_hook(hook, spec, "1.2.13", force=False)
    # Re-invoke with a newer forge version. Body is unchanged, but the
    # marker carries a different forge-version field, so content differs.
    written = install_githooks._write_hook(hook, spec, "1.3.0", force=False)
    assert written
    assert "forge-version=1.3.0" in hook.read_text()


def test_write_hook_refresh_rewrites_unconditionally(tmp_path: Path) -> None:
    """--refresh rewrites managed hooks even when content matches exactly."""
    spec = install_githooks.HOOKS[0]
    hook = tmp_path / ".githooks" / spec.name
    install_githooks._write_hook(hook, spec, "1.2.13", force=False)
    mtime_before = hook.stat().st_mtime_ns
    # Same args + refresh=True → rewrite even though content is identical.
    written = install_githooks._write_hook(
        hook, spec, "1.2.13", force=False, refresh=True
    )
    assert written
    assert hook.stat().st_mtime_ns >= mtime_before


def test_write_hook_refresh_still_respects_user_customization(
    tmp_path: Path,
) -> None:
    """--refresh does NOT override user-customized files — that needs --force."""
    spec = install_githooks.HOOKS[0]
    hook = tmp_path / ".githooks" / spec.name
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\necho mine\n")
    written = install_githooks._write_hook(
        hook, spec, "1.2.13", force=False, refresh=True
    )
    assert not written
    assert "echo mine" in hook.read_text()


def test_marker_embeds_forge_version_and_body_sha() -> None:
    """managed_marker() embeds forge-version= and (when provided) body-sha=."""
    marker_no_sha = install_githooks.managed_marker("1.2.13")
    assert "forge-version=1.2.13" in marker_no_sha
    assert marker_no_sha.startswith(install_githooks.MANAGED_MARKER_PREFIX)
    marker_with_sha = install_githooks.managed_marker("1.2.13", "abc123def456")
    assert "body-sha=abc123def456" in marker_with_sha


def test_parse_marker_extracts_v2_fields() -> None:
    """_parse_marker pulls hook-version + forge-version + body-sha from a v2 marker."""
    content = (
        "#!/usr/bin/env bash\n"
        "# forge:githook-managed v2 forge-version=1.11.0 body-sha=abc123def456\n"
        "set -euo pipefail\n"
    )
    parsed = install_githooks._parse_marker(content)
    assert parsed is not None
    assert parsed["hook_version"] == "2"
    assert parsed["forge-version"] == "1.11.0"
    assert parsed["body-sha"] == "abc123def456"


def test_parse_marker_handles_legacy_v1_marker() -> None:
    """_parse_marker tolerates v1 markers (no body-sha field)."""
    content = (
        "#!/usr/bin/env bash\n"
        "# forge:githook-managed v1 forge-version=1.10.0\n"
        "set -euo pipefail\n"
    )
    parsed = install_githooks._parse_marker(content)
    assert parsed is not None
    assert parsed["hook_version"] == "1"
    assert parsed["forge-version"] == "1.10.0"
    assert "body-sha" not in parsed


def test_v1_to_v2_migration_backs_up_consumer_customized(tmp_path: Path) -> None:
    """A pristine v1 post-merge gets migrated to v2 with a backup, no consumer loss.

    Triggered when a consumer upgrades a hook-v1 forge install to v1.12.0+.
    The .bak file preserves whatever was on disk so the consumer can
    review and re-port repo-specific lines into the new v2 wrapper.
    """
    spec = next(s for s in install_githooks.HOOKS if s.name == "post-merge")
    hook = tmp_path / ".githooks" / "post-merge"
    hook.parent.mkdir()
    # Build a pristine v1 file: marker + preamble + v1 body.
    v1_body = install_githooks._V1_HOOK_BODIES["post-merge"]
    preamble = install_githooks._STALENESS_PREAMBLE_TEMPLATE.replace(
        "__FORGE_VERSION__", "1.10.0"
    )
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "# forge:githook-managed v1 forge-version=1.10.0\n"
        "set -euo pipefail\n"
        f"{preamble}\n"
        f"{v1_body}\n"
    )
    written = install_githooks._write_hook(
        hook, spec, "1.12.0", force=False, refresh=True
    )
    assert written
    # The new content is the v2 one-liner.
    assert 'forge-post-merge "$@"' in hook.read_text()
    # And the original is preserved as a versioned backup.
    backup = hook.with_name("post-merge.before-forge-v1.12.0.bak")
    assert backup.is_file()
    assert "install-forge-claude-md --check --quiet" in backup.read_text()


def test_is_managed_recognises_legacy_marker_without_forge_version(
    tmp_path: Path,
) -> None:
    """Hooks written by older forge (no forge-version=) are still 'managed'.

    This is the back-compat path: a consumer upgrading from a pre-#40
    forge has hooks carrying `# forge:githook-managed v1` without the
    embedded version. The next install must recognise them as managed
    so they get upgraded silently, not as user-customized.
    """
    hook = tmp_path / "pre-commit"
    hook.write_text(
        "#!/usr/bin/env bash\n# forge:githook-managed v1\nset -e\nforge-precommit\n"
    )
    assert install_githooks._is_managed(hook) is True


def test_hook_content_includes_staleness_preamble() -> None:
    """Generated hooks carry the forge_hook_version + sort -V comparison block."""
    content = install_githooks._hook_content(install_githooks.HOOKS[0], "1.2.13")
    assert 'forge_hook_version="1.2.13"' in content
    assert "sort -V" in content
    assert "install-forge-githooks --refresh" in content


def test_post_merge_body_calls_forge_post_merge_cli() -> None:
    """post-merge body hands off to the `forge-post-merge` CLI (no embedded logic)."""
    post_merge = next(s for s in install_githooks.HOOKS if s.name == "post-merge")
    assert post_merge.body == 'forge-post-merge "$@"'


def test_set_hooks_path_runs_git_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_set_hooks_path runs `git config core.hooksPath .githooks` when unset."""
    captured = CapturedCalls()
    monkeypatch.setattr(
        install_githooks.subprocess,
        "run",
        make_fake_run(stdout="", captured=captured),
    )
    install_githooks._set_hooks_path(tmp_path, force=False)
    assert any(c[-1] == ".githooks" and "config" in c for c in captured.calls)


def test_set_hooks_path_skips_when_already_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_set_hooks_path is idempotent when already configured."""
    captured = CapturedCalls()
    monkeypatch.setattr(
        install_githooks.subprocess,
        "run",
        make_fake_run(stdout=".githooks\n", captured=captured),
    )
    install_githooks._set_hooks_path(tmp_path, force=False)
    assert len(captured.calls) == 1
    assert "--get" in captured.calls[0]


def test_set_hooks_path_warns_on_existing_other_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Existing non-`.githooks` value (e.g. .husky) is left alone without --force."""
    captured = CapturedCalls()
    monkeypatch.setattr(
        install_githooks.subprocess,
        "run",
        make_fake_run(stdout=".husky\n", captured=captured),
    )
    install_githooks._set_hooks_path(tmp_path, force=False)
    assert len(captured.calls) == 1
    assert any(".husky" in r.message for r in caplog.records)


def test_set_hooks_path_force_overwrites_other_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force overwrites an existing non-`.githooks` value."""
    captured = CapturedCalls()
    monkeypatch.setattr(
        install_githooks.subprocess,
        "run",
        make_fake_run(stdout=".husky\n", captured=captured),
    )
    install_githooks._set_hooks_path(tmp_path, force=True)
    assert len(captured.calls) == 2
    assert captured.calls[1][-1] == ".githooks"


def test_main_writes_all_three_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() writes pre-commit, post-merge, and post-checkout files."""
    monkeypatch.setattr(install_githooks, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(install_githooks, "_set_hooks_path", lambda *_, **__: None)
    with patch.object(install_githooks.sys, "argv", ["install-forge-githooks"]):
        rc = install_githooks.main()
    assert rc == 0
    for name in ("pre-commit", "post-merge", "post-checkout"):
        assert (tmp_path / ".githooks" / name).is_file(), f"{name} missing"


def test_main_refresh_rewrites_unchanged_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--refresh` forces a rewrite even when content is already current."""
    monkeypatch.setattr(install_githooks, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(install_githooks, "_set_hooks_path", lambda *_, **__: None)
    # First run: write hooks.
    with patch.object(install_githooks.sys, "argv", ["install-forge-githooks"]):
        install_githooks.main()
    pre_commit = tmp_path / ".githooks" / "pre-commit"
    mtime_before = pre_commit.stat().st_mtime_ns
    # Second run with --refresh: file should be rewritten (mtime advances or matches,
    # but the call must not skip).
    with patch.object(
        install_githooks.sys,
        "argv",
        ["install-forge-githooks", "--refresh"],
    ):
        rc = install_githooks.main()
    assert rc == 0
    assert pre_commit.stat().st_mtime_ns >= mtime_before


def test_main_quiet_suppresses_info_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`--quiet` lifts the root logger to WARNING so INFO records vanish."""
    monkeypatch.setattr(install_githooks, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(install_githooks, "_set_hooks_path", lambda *_, **__: None)
    with (
        patch.object(
            install_githooks.sys,
            "argv",
            ["install-forge-githooks", "--quiet"],
        ),
        caplog.at_level("INFO"),
    ):
        install_githooks.main()
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert info_records == []
