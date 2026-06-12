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
    assert install_githooks.managed_marker(body_sha) in content
    assert spec.body.splitlines()[0] in content
    # The forge version is never baked into the tracked hook.
    assert "1.2.13" not in content
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
    assert install_githooks.MANAGED_MARKER_PREFIX in hook.read_text()


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


def test_write_hook_version_change_leaves_tracked_hook_byte_stable(
    tmp_path: Path,
) -> None:
    """A newer forge version alone does NOT rewrite the hook; version in sidecar.

    This is the core anti-churn guarantee: the tracked hook is
    version-free, so bumping the installed forge between two installs
    produces byte-identical content and no rewrite.
    """
    spec = install_githooks.HOOKS[0]
    hook = tmp_path / ".githooks" / spec.name
    install_githooks._write_hook(hook, spec, "1.2.13", force=False)
    first = hook.read_text()
    written = install_githooks._write_hook(hook, spec, "1.3.0", force=False)
    assert not written
    assert hook.read_text() == first
    assert "1.2.13" not in first
    assert "1.3.0" not in hook.read_text()


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


def test_marker_embeds_body_sha_not_forge_version() -> None:
    """managed_marker() embeds body-sha=, never forge-version= (version in sidecar)."""
    bare = install_githooks.managed_marker()
    assert bare.startswith(install_githooks.MANAGED_MARKER_PREFIX)
    assert "body-sha=" not in bare
    assert "forge-version=" not in bare
    with_sha = install_githooks.managed_marker("abc123def456")
    assert "body-sha=abc123def456" in with_sha
    assert "forge-version=" not in with_sha


def test_parse_marker_extracts_current_v2_fields() -> None:
    """_parse_marker parses current v2 marker: hook-version and body-sha."""
    content = (
        "#!/usr/bin/env bash\n"
        "# forge:githook-managed v2 body-sha=abc123def456\n"
        "set -euo pipefail\n"
    )
    parsed = install_githooks._parse_marker(content)
    assert parsed is not None
    assert parsed["hook_version"] == "2"
    assert parsed["body-sha"] == "abc123def456"
    assert "forge-version" not in parsed


def test_parse_marker_tolerates_legacy_v2_with_embedded_version() -> None:
    """A pre-sidecar v2 marker carrying forge-version= is still parsed (back-compat)."""
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
    # Build a pristine v1 file: marker + a legacy preamble (ending in
    # `set -e`, the body-detection terminator) + the canonical v1 body.
    v1_body = install_githooks._V1_HOOK_BODIES["post-merge"]
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "# forge:githook-managed v1 forge-version=1.10.0\n"
        "set -euo pipefail\n"
        'forge_hook_version="1.10.0"\n'
        "set +e\n"
        "set -e\n"
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


def test_hook_content_reads_version_from_sidecar() -> None:
    """Generated hooks read version from sidecar and run sort -V comparison."""
    content = install_githooks._hook_content(install_githooks.HOOKS[0])
    assert install_githooks.SIDECAR_NAME in content
    assert "sort -V" in content
    assert "install-forge-githooks --refresh" in content
    # No baked-in version placeholder or constant survives.
    assert "__FORGE_VERSION__" not in content


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


def test_write_version_sidecar_records_full_version(tmp_path: Path) -> None:
    """The sidecar holds the full version string (dev suffix and all)."""
    githooks = tmp_path / ".githooks"
    version = "1.16.2.dev4+g5b5b916c4.d20260612"
    install_githooks._write_version_sidecar(githooks, version)
    sidecar = githooks / install_githooks.SIDECAR_NAME
    assert sidecar.read_text().strip() == version


def test_ensure_sidecar_gitignored_is_idempotent(tmp_path: Path) -> None:
    """The ignore entry is appended once and never duplicated on re-run."""
    install_githooks._ensure_sidecar_gitignored(tmp_path)
    install_githooks._ensure_sidecar_gitignored(tmp_path)
    text = (tmp_path / ".gitignore").read_text()
    entry = f".githooks/{install_githooks.SIDECAR_NAME}"
    assert text.count(entry) == 1


def test_main_writes_and_gitignores_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() drops the version sidecar and ensures it is gitignored."""
    monkeypatch.setattr(install_githooks, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(install_githooks, "_set_hooks_path", lambda *_, **__: None)
    with patch.object(install_githooks.sys, "argv", ["install-forge-githooks"]):
        install_githooks.main()
    sidecar = tmp_path / ".githooks" / install_githooks.SIDECAR_NAME
    assert sidecar.is_file()
    assert sidecar.read_text().strip() == install_githooks._installed_forge_version()
    assert (
        f".githooks/{install_githooks.SIDECAR_NAME}"
        in (tmp_path / ".gitignore").read_text()
    )


def test_main_refresh_across_versions_keeps_hooks_byte_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version bump between two installs leaves every tracked hook byte-identical.

    The end-to-end anti-churn guarantee: only the gitignored sidecar
    changes when the installed forge version advances.
    """
    monkeypatch.setattr(install_githooks, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(install_githooks, "_set_hooks_path", lambda *_, **__: None)
    monkeypatch.setattr(
        install_githooks, "_installed_forge_version", lambda: "1.16.2.dev1+gaaa"
    )
    with patch.object(install_githooks.sys, "argv", ["install-forge-githooks"]):
        install_githooks.main()
    before = {
        spec.name: (tmp_path / ".githooks" / spec.name).read_text()
        for spec in install_githooks.HOOKS
    }
    monkeypatch.setattr(
        install_githooks, "_installed_forge_version", lambda: "1.16.2.dev9+gbbb"
    )
    with patch.object(
        install_githooks.sys, "argv", ["install-forge-githooks", "--refresh"]
    ):
        install_githooks.main()
    for spec in install_githooks.HOOKS:
        assert (tmp_path / ".githooks" / spec.name).read_text() == before[spec.name]
    sidecar = tmp_path / ".githooks" / install_githooks.SIDECAR_NAME
    assert sidecar.read_text().strip() == "1.16.2.dev9+gbbb"
