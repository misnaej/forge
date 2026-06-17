"""Tests for ``forge.forge_config``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from forge import forge_config


if TYPE_CHECKING:
    import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _forge_source_files(*, exclude: str) -> dict[Path, str]:
    """Return ``src/forge`` ``*.py`` sources keyed by path, minus *exclude*.

    Args:
        exclude: Filename to drop (e.g. the declaration site itself).

    Returns:
        Mapping of file path to its text for every other module.
    """
    return {
        path: path.read_text(encoding="utf-8")
        for path in (_REPO_ROOT / "src" / "forge").rglob("*.py")
        if path.name != exclude
    }


def test_every_config_key_leaf_is_read_by_some_module() -> None:
    """Every CONFIG_KEYS leaf appears in a reader module — no stale entries.

    #46: CONFIG_KEYS is forge's single declared enumeration of the
    ``[tool.forge.*]`` keys forge reads. This couples the registry to
    reality — a key whose leaf no longer appears as a string literal in
    any ``src/forge`` module (other than ``forge_config.py``, the
    declaration site) is a stale entry that makes ``forge-config --list``
    advertise a key nothing reads.
    """
    sources = _forge_source_files(exclude="forge_config.py")
    for key in forge_config.CONFIG_KEYS:
        leaf = key.path[-1]
        read_somewhere = any(
            f'"{leaf}"' in text or f"'{leaf}'" in text for text in sources.values()
        )
        assert read_somewhere, (
            f"CONFIG_KEYS leaf {leaf!r} ({'.'.join(key.path)}) is referenced "
            f"by no src/forge module — stale registry entry?"
        )


def test_forge_step_config_sections_are_declared() -> None:
    """Every ``[tool.forge.<section>]`` read via ``_forge_step_config`` is declared.

    #46 reverse direction: a new pre-commit step reading
    ``[tool.forge.<newstep>]`` without a CONFIG_KEYS entry would make
    ``forge-config --list`` silently incomplete. Extract the section names
    passed to ``precommit._forge_step_config`` and assert each is a
    declared third-level token under ``[tool.forge]``.
    """
    precommit_src = (_REPO_ROOT / "src" / "forge" / "precommit.py").read_text(
        encoding="utf-8"
    )
    sections = set(
        re.findall(r'_forge_step_config\([^,]+,\s*"([^"]+)"\)', precommit_src)
    )
    declared = {key.path[2] for key in forge_config.CONFIG_KEYS if len(key.path) >= 3}
    undocumented = sections - declared
    assert not undocumented, (
        f"[tool.forge.{sorted(undocumented)}] read via _forge_step_config but "
        f"absent from CONFIG_KEYS"
    )


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
    lines = forge_config.build_report(data)
    # Set keys render their value (not a <default> placeholder).
    base_line = next(line for line in lines if "base_branch" in line)
    dev_line = next(line for line in lines if "dev_branch" in line)
    assert base_line.endswith("'main'")
    assert dev_line.endswith("'dev'")


def test_report_lists_layout_dirs() -> None:
    """The report enumerates the repo-wide source_dirs / test_dirs keys."""
    text = "\n".join(forge_config.build_report({}))
    assert "source_dirs" in text
    assert "test_dirs" in text
    assert "<default: ['src']>" in text
    assert "<default: ['tests']>" in text


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
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\ndev_branch = "dev"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["forge-config", "--list"])
    assert forge_config.main() == 0
