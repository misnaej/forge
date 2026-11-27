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


def test_report_shows_defaults_when_unset(tmp_path: Path) -> None:
    """Unset keys render their default value flagged `(not set)`."""
    text = "\n".join(forge_config.build_report({}, tmp_path))
    assert "[tool.forge]" in text
    assert "base_branch" in text
    assert "<default: 'main'>" in text
    assert "(not set)" in text
    headers = [line for line in text.splitlines() if line.startswith("[tool.forge")]
    assert len(headers) == len(set(headers))


def test_report_shows_configured_values(tmp_path: Path) -> None:
    """Set keys render their actual value, not the default."""
    data = {"tool": {"forge": {"base_branch": "trunk"}}}
    lines = forge_config.build_report(data, tmp_path)
    # Set keys render their value (not a <default> placeholder).
    base_line = next(line for line in lines if "base_branch" in line)
    assert base_line.endswith("'trunk'")


def test_report_reads_c4_key_from_model_file(tmp_path: Path) -> None:
    """A c4 key set in the model file is reported as set, not "(not set)".

    BEHAVIOR: forge reads the resolved model table, so the report must too.
    """
    (tmp_path / "pyproject.toml").write_text('[tool.forge.c4]\nconfig = "c4.toml"\n')
    (tmp_path / "c4.toml").write_text('system = "S"\nstrict_coverage = true\n')
    lines = _c4_section(forge_config.build_report({}, tmp_path))
    line = next(line for line in lines if "strict_coverage" in line)
    assert "True" in line
    assert "(not set)" not in line


def _c4_section(lines: list[str]) -> list[str]:
    """Return the report lines under the ``[tool.forge.c4]`` header only.

    Args:
        lines: Full report lines.

    Returns:
        Lines after that header up to the next ``[`` section header.
    """
    start = lines.index("[tool.forge.c4]") + 1
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("[")),
        len(lines),
    )
    return lines[start:end]


def test_report_c4_config_key_still_read_from_pyproject(tmp_path: Path) -> None:
    """``config`` is left out of the merged model, so it is read from pyproject."""
    data = {"tool": {"forge": {"c4": {"config": "c4.toml"}}}}
    (tmp_path / "c4.toml").write_text('system = "S"\n')
    lines = _c4_section(forge_config.build_report(data, tmp_path))
    line = next(line for line in lines if line.lstrip().startswith("config "))
    assert "c4.toml" in line
    assert "(not set)" not in line


def test_report_lists_layout_dirs(tmp_path: Path) -> None:
    """The report enumerates the repo-wide source_dirs / test_dirs keys.

    Their default is now the smart-detect description (not a fixed list),
    since an unset key resolves against the actual on-disk layout.
    """
    text = "\n".join(forge_config.build_report({}, tmp_path))
    assert "source_dirs" in text
    assert "test_dirs" in text
    assert "smart-detect" in text


def test_report_omits_suggested_setup_when_nothing_recommended(tmp_path: Path) -> None:
    """No [tool.forge.*] key is recommended by default, so no nudge block.

    Override-only keys like `docstring_coverage.paths` are deliberately
    NOT recommended — nudging them would have a consumer override the
    repo-wide layout they already set.
    """
    text = "\n".join(forge_config.build_report({}, tmp_path))
    assert "Suggested setup" not in text


def test_report_suggests_recommended_unset_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advisor mechanism: a recommended-but-unset key is nudged."""
    key = forge_config.ConfigKey(
        ("tool", "forge", "demo"), "x", "Demo key.", recommended=True
    )
    monkeypatch.setattr(forge_config, "CONFIG_KEYS", (key,))
    text = "\n".join(forge_config.build_report({}, tmp_path))
    assert "Suggested setup" in text
    assert "tool.forge].demo" in text


def test_report_names_interrogate_as_native_section(tmp_path: Path) -> None:
    """The report points at `[tool.interrogate]` as forge-read native config."""
    text = "\n".join(forge_config.build_report({}, tmp_path))
    assert "[tool.interrogate]" in text
    assert "native tool section" in text


def test_report_marks_interrogate_set_when_present(tmp_path: Path) -> None:
    """When `[tool.interrogate]` exists, the native pointer flags it `set`."""
    data = {"tool": {"interrogate": {"fail-under": 100}}}
    text = "\n".join(forge_config.build_report(data, tmp_path))
    assert "[tool.interrogate]  (set" in text


def test_main_prints_report_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`forge-config` reads pyproject.toml and exits 0 (read-only advisory)."""
    (tmp_path / "pyproject.toml").write_text('[tool.forge]\nbase_branch = "main"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-config", "--list"])
    assert forge_config.main() == 0


def test_forge_config_list_includes_exclude_key(tmp_path: Path) -> None:
    """Verify ``exclude`` key appears in forge-config --list output.

    The ``exclude`` key is the single repo-wide mechanism for omitting
    vendored / generated Python from whole-tree steps.  Two checks:
    (1) the key appears in the report output so consumers see it via
    ``forge-config --list``; (2) the CONFIG_KEYS description for it
    mentions vendored / generated paths so the purpose is self-evident
    in the code — the description is what downstream docs / the
    pre-install nudge would surface.
    """
    report = "\n".join(forge_config.build_report({}, tmp_path))
    assert "exclude" in report

    exclude_key = next(
        k for k in forge_config.CONFIG_KEYS if k.path == ("tool", "forge", "exclude")
    )
    assert "vendor" in exclude_key.description or "generated" in exclude_key.description


def test_forge_config_registers_smart_test_safe_fallback_key(tmp_path: Path) -> None:
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
    assert "safe_fallback" in "\n".join(forge_config.build_report({}, tmp_path))
