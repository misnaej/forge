"""Tests for ``forge.forge_config``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import forge_config


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_keys_by_section_groups_keys() -> None:
    """Grouping keeps every key once, under a header equal to its path stem."""
    groups = forge_config.keys_by_section()
    headers = [section for section, _keys in groups]
    assert len(headers) == len(set(headers))
    grouped = [key for _section, keys in groups for key in keys]
    assert sorted(k.path for k in grouped) == sorted(
        k.path for k in forge_config.CONFIG_KEYS
    )
    for section, keys in groups:
        assert all(".".join(k.path[:-1]) == section for k in keys)


def test_lookup_returns_value_and_unset() -> None:
    """`_lookup` returns nested values, or the `_UNSET` sentinel when absent."""
    data = {"tool": {"forge": {"base_branch": "trunk"}}}
    assert forge_config._lookup(data, ("tool", "forge", "base_branch")) == "trunk"
    assert (
        forge_config._lookup(data, ("tool", "forge", "nonexistent"))
        is forge_config._UNSET
    )


def test_report_shows_defaults_when_unset() -> None:
    """Unset keys render their default value flagged `(not set)`."""
    text = "\n".join(forge_config.build_report({}))
    assert "[tool.forge]" in text
    assert "base_branch" in text
    assert "<default: 'main'>" in text
    assert "(not set)" in text
    headers = [line for line in text.splitlines() if line.startswith("[tool.forge")]
    assert len(headers) == len(set(headers))


def test_report_shows_configured_values() -> None:
    """Set keys render their actual value, not the default."""
    data = {"tool": {"forge": {"base_branch": "trunk"}}}
    lines = forge_config.build_report(data)
    # Set keys render their value (not a <default> placeholder).
    base_line = next(line for line in lines if "base_branch" in line)
    assert base_line.endswith("'trunk'")


def test_report_lists_layout_dirs() -> None:
    """The report enumerates the repo-wide source_dirs / test_dirs keys.

    Their default is now the smart-detect description (not a fixed list),
    since an unset key resolves against the actual on-disk layout.
    """
    text = "\n".join(forge_config.build_report({}))
    assert "source_dirs" in text
    assert "test_dirs" in text
    assert "smart-detect" in text


def test_report_omits_suggested_setup_when_nothing_recommended() -> None:
    """No [tool.forge.*] key is recommended by default, so no nudge block.

    Override-only keys like `docstring_coverage.paths` are deliberately
    NOT recommended — nudging them would have a consumer override the
    repo-wide layout they already set.
    """
    text = "\n".join(forge_config.build_report({}))
    assert "Suggested setup" not in text


def test_report_suggests_recommended_unset_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advisor mechanism: a recommended-but-unset key is nudged."""
    key = forge_config.ConfigKey(
        ("tool", "forge", "demo"), "x", "Demo key.", recommended=True
    )
    monkeypatch.setattr(forge_config, "CONFIG_KEYS", (key,))
    text = "\n".join(forge_config.build_report({}))
    assert "Suggested setup" in text
    assert "tool.forge].demo" in text


def test_report_names_interrogate_as_native_section() -> None:
    """The report points at `[tool.interrogate]` as forge-read native config."""
    text = "\n".join(forge_config.build_report({}))
    assert "[tool.interrogate]" in text
    assert "native tool section" in text


def test_report_marks_interrogate_set_when_present() -> None:
    """When `[tool.interrogate]` exists, the native pointer flags it `set`."""
    data = {"tool": {"interrogate": {"fail-under": 100}}}
    text = "\n".join(forge_config.build_report(data))
    assert "[tool.interrogate]  (set" in text


def test_main_prints_report_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`forge-config` reads pyproject.toml and exits 0 (read-only advisory)."""
    (tmp_path / "pyproject.toml").write_text('[tool.forge]\nbase_branch = "main"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-config", "--list"])
    assert forge_config.main() == 0


def test_forge_config_list_includes_exclude_key() -> None:
    """Verify ``exclude`` key appears in forge-config --list output.

    The ``exclude`` key is the single repo-wide mechanism for omitting
    vendored / generated Python from whole-tree steps.  Two checks:
    (1) the key appears in the report output so consumers see it via
    ``forge-config --list``; (2) the CONFIG_KEYS description for it
    mentions vendored / generated paths so the purpose is self-evident
    in the code — the description is what downstream docs / the
    pre-install nudge would surface.
    """
    report = "\n".join(forge_config.build_report({}))
    assert "exclude" in report

    exclude_key = next(
        k for k in forge_config.CONFIG_KEYS if k.path == ("tool", "forge", "exclude")
    )
    assert "vendor" in exclude_key.description or "generated" in exclude_key.description


def test_forge_config_registers_smart_test_safe_fallback_key() -> None:
    """``safe_fallback`` is declared with the ``escalate`` default and is listed.

    An undeclared key would be invisible to ``forge-config --list`` and
    the unknown-key audit, so a consumer could not discover the setting.
    """
    key = next(
        k
        for k in forge_config.CONFIG_KEYS
        if k.path == ("tool", "forge", "smart_test", "safe_fallback")
    )
    assert key.default == "escalate"
    assert "safe_fallback" in "\n".join(forge_config.build_report({}))
