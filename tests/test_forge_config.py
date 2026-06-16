"""Tests for ``forge.forge_config``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import forge_config


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_lookup_returns_value_and_unset() -> None:
    """`_lookup` returns nested values, or the `_UNSET` sentinel when absent."""
    data = {"tool": {"forge": {"base_branch": "trunk"}}}
    assert forge_config._lookup(data, ("tool", "forge", "base_branch")) == "trunk"
    assert (
        forge_config._lookup(data, ("tool", "forge", "dev_branch"))
        is forge_config._UNSET
    )


def test_report_shows_defaults_when_unset() -> None:
    """Unset keys render their default value flagged `(not set)`."""
    text = "\n".join(forge_config.build_report({}))
    assert "[tool.forge]" in text
    assert "base_branch" in text
    assert "<default: 'main'>" in text
    assert "(not set)" in text


def test_report_shows_configured_values() -> None:
    """Set keys render their actual value, not the default."""
    data = {"tool": {"forge": {"base_branch": "main", "dev_branch": "dev"}}}
    text = "\n".join(forge_config.build_report(data))
    assert "= 'main'" in text
    assert "= 'dev'" in text
    # The [tool.forge] block (before cli_wiring) has both keys set — no defaults there.
    assert "<default:" not in text.split("[tool.forge.cli_wiring]")[0]


def test_report_advises_on_recommended_unset_keys() -> None:
    """A recommended-but-unset key (paths) appears in the suggested-setup block."""
    text = "\n".join(forge_config.build_report({}))
    assert "Suggested setup" in text
    assert "docstring_coverage].paths" in text


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
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\ndev_branch = "dev"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-config", "--list"])
    assert forge_config.main() == 0
