"""Tests for forge.version_surfaces — the three install-version readers."""

# MOCKING STRATEGY: every reader touches either the filesystem (tmp_path) or
# importlib.metadata; metadata calls are stubbed so no real distribution
# needs to be installed.
#   - version_surfaces.metadata.version / .distribution: stubbed per test.
#   - version_surfaces.find_install_dir: stubbed for plugin_cache_version
#     tests that don't need a real two-level cache layout on disk.

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from forge import version_surfaces


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# read_json
# ---------------------------------------------------------------------------


def test_read_json_missing_file(tmp_path: Path) -> None:
    """Missing manifest produces an error string."""
    data, err = version_surfaces.read_json(tmp_path / "nope.json")
    assert data == {}
    assert err is not None
    assert "missing" in err


def test_read_json_invalid(tmp_path: Path) -> None:
    """Invalid JSON produces an error string."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json")
    data, err = version_surfaces.read_json(bad)
    assert data == {}
    assert err is not None
    assert "invalid JSON" in err


def test_read_json_valid(tmp_path: Path) -> None:
    """Valid JSON loads cleanly with no error."""
    good = tmp_path / "good.json"
    good.write_text('{"name": "forge"}')
    data, err = version_surfaces.read_json(good)
    assert err is None
    assert data == {"name": "forge"}


# ---------------------------------------------------------------------------
# pip_version
# ---------------------------------------------------------------------------


def test_pip_version_reads_installed_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns the version string reported by importlib.metadata."""
    monkeypatch.setattr(version_surfaces.metadata, "version", lambda _dist: "2.23.1")
    assert version_surfaces.pip_version() == "2.23.1"


def test_pip_version_none_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns None when the forge-scripts distribution isn't installed."""

    def _raise(_dist: str) -> str:
        raise version_surfaces.metadata.PackageNotFoundError

    monkeypatch.setattr(version_surfaces.metadata, "version", _raise)
    assert version_surfaces.pip_version() is None


# ---------------------------------------------------------------------------
# hook_sidecar_version
# ---------------------------------------------------------------------------


def test_hook_sidecar_version_none_when_sidecar_absent(tmp_path: Path) -> None:
    """No .githooks/.forge-hook-version sidecar → None, not an error."""
    assert version_surfaces.hook_sidecar_version(tmp_path) is None


def test_hook_sidecar_version_reads_stripped_sidecar(tmp_path: Path) -> None:
    """The sidecar's version string is returned with trailing whitespace stripped."""
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / version_surfaces.HOOK_VERSION_SIDECAR).write_text(
        "2.23.1\n", encoding="utf-8"
    )
    assert version_surfaces.hook_sidecar_version(tmp_path) == "2.23.1"


def test_hook_sidecar_version_none_when_sidecar_empty(tmp_path: Path) -> None:
    """An empty (or whitespace-only) sidecar counts as absent."""
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / version_surfaces.HOOK_VERSION_SIDECAR).write_text(
        "   \n", encoding="utf-8"
    )
    assert version_surfaces.hook_sidecar_version(tmp_path) is None


# ---------------------------------------------------------------------------
# plugin_cache_version
# ---------------------------------------------------------------------------


def test_plugin_cache_version_none_when_plugin_root_none() -> None:
    """No cached plugin install → None, short-circuiting before any lookup."""
    assert version_surfaces.plugin_cache_version(None) is None


def test_plugin_cache_version_reads_plugin_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefers the plugin.json "version" field over the install dir name."""
    install_dir = tmp_path / "forge" / "2.23.1"
    (install_dir / ".claude-plugin").mkdir(parents=True)
    (install_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "2.23.1"}), encoding="utf-8"
    )
    monkeypatch.setattr(version_surfaces, "find_install_dir", lambda _root: install_dir)
    assert version_surfaces.plugin_cache_version(tmp_path) == "2.23.1"


def test_plugin_cache_version_falls_back_to_dir_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When plugin.json is missing/unversioned, the install dir name is used."""
    install_dir = tmp_path / "forge" / "2.23.1"
    install_dir.mkdir(parents=True)
    monkeypatch.setattr(version_surfaces, "find_install_dir", lambda _root: install_dir)
    assert version_surfaces.plugin_cache_version(tmp_path) == "2.23.1"


def test_plugin_cache_version_none_when_no_install_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recognisable install layout under plugin_root → None."""
    monkeypatch.setattr(version_surfaces, "find_install_dir", lambda _root: None)
    assert version_surfaces.plugin_cache_version(tmp_path) is None


# ---------------------------------------------------------------------------
# find_install_dir / find_plugin_cache — cheap wins beyond plugin_cache_version
# ---------------------------------------------------------------------------


def test_find_install_dir_picks_highest_semver_not_lexicographic(
    tmp_path: Path,
) -> None:
    """Two cached versions under a two-level layout: the higher semver wins.

    Regression guard: a lexicographic sort would rank "1.9.0" above
    "1.13.0" ("9" > "1"); version_key's tuple comparison must not.
    """
    for version in ("1.9.0", "1.13.0"):
        install_dir = tmp_path / "forge" / version
        (install_dir / ".claude-plugin").mkdir(parents=True)
        (install_dir / ".claude-plugin" / "plugin.json").write_text(
            "{}", encoding="utf-8"
        )
    result = version_surfaces.find_install_dir(tmp_path)
    assert result is not None
    assert result.name == "1.13.0"


def test_find_plugin_cache_none_when_cache_dir_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ~/.claude/plugins/cache/<plugin> directory → None."""
    monkeypatch.setattr(
        version_surfaces.Path, "home", classmethod(lambda _cls: tmp_path)
    )
    assert version_surfaces.find_plugin_cache("forge") is None


def test_find_plugin_cache_finds_existing_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing cache slot for the plugin name is returned."""
    monkeypatch.setattr(
        version_surfaces.Path, "home", classmethod(lambda _cls: tmp_path)
    )
    cache = tmp_path / ".claude" / "plugins" / "cache" / "forge"
    cache.mkdir(parents=True)
    assert version_surfaces.find_plugin_cache("forge") == cache


# ---------------------------------------------------------------------------
# editable_install_origin
# ---------------------------------------------------------------------------


class _FakeDistribution:
    """Null Object exposing only the ``.read_text(name)`` surface `_direct_url()` reads.

    Attributes:
        text: The canned ``direct_url.json`` content returned for any name.
    """

    def __init__(self, text: str | None) -> None:
        """Store the canned file content.

        Args:
            text: Content returned by ``read_text``, or None for "missing".
        """
        self.text = text

    def read_text(self, _name: str) -> str | None:
        """Return the canned content regardless of requested filename.

        Args:
            _name: The filename being requested (unused; content is fixed).

        Returns:
            The canned content or None.
        """
        return self.text


def _direct_url_json(*, editable: bool, url: str) -> str:
    """Build a PEP 610 ``direct_url.json`` body for editable_install_origin tests.

    Args:
        editable: Value of ``dir_info.editable``.
        url: Value of the top-level ``url`` field.

    Returns:
        A JSON string shaped like a real ``direct_url.json``.
    """
    return json.dumps({"dir_info": {"editable": editable}, "url": url})


def test_editable_install_origin_resolves_file_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editable install's file:// url resolves to a Path."""
    dist_with_editable_direct_url = _FakeDistribution(
        _direct_url_json(editable=True, url=f"file://{tmp_path}")
    )
    monkeypatch.setattr(
        version_surfaces.metadata,
        "distribution",
        lambda _name: dist_with_editable_direct_url,
    )
    assert version_surfaces.editable_install_origin() == tmp_path.resolve()


def test_editable_install_origin_none_when_distribution_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PackageNotFoundError (distribution not installed) → None."""

    def _raise(_name: str) -> _FakeDistribution:
        raise version_surfaces.metadata.PackageNotFoundError

    monkeypatch.setattr(version_surfaces.metadata, "distribution", _raise)
    assert version_surfaces.editable_install_origin() is None


def test_editable_install_origin_none_when_read_text_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty (or absent) direct_url.json read → None."""
    dist_with_no_direct_url = _FakeDistribution("")
    monkeypatch.setattr(
        version_surfaces.metadata, "distribution", lambda _name: dist_with_no_direct_url
    )
    assert version_surfaces.editable_install_origin() is None


def test_editable_install_origin_none_when_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed JSON content → None, not a raised exception."""
    dist_with_malformed_direct_url = _FakeDistribution("{not-json")
    monkeypatch.setattr(
        version_surfaces.metadata,
        "distribution",
        lambda _name: dist_with_malformed_direct_url,
    )
    assert version_surfaces.editable_install_origin() is None


@pytest.mark.parametrize(
    "direct_url_json",
    [
        pytest.param(json.dumps({"url": "file:///repo"}), id="dir_info-absent"),
        pytest.param(
            _direct_url_json(editable=False, url="file:///repo"), id="editable-false"
        ),
    ],
)
def test_editable_install_origin_none_when_not_editable(
    direct_url_json: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-editable install (git/index) has no clone to compare against.

    Args:
        direct_url_json: A non-editable ``direct_url.json`` body (absent or false).
        monkeypatch: Pytest fixture for mocking.
    """
    dist = _FakeDistribution(direct_url_json)
    monkeypatch.setattr(version_surfaces.metadata, "distribution", lambda _name: dist)
    assert version_surfaces.editable_install_origin() is None


def test_editable_install_origin_none_when_url_not_file_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git/https install url (editable flag notwithstanding) → None."""
    dist_with_non_file_url = _FakeDistribution(
        _direct_url_json(editable=True, url="git+https://example.com/forge.git")
    )
    monkeypatch.setattr(
        version_surfaces.metadata,
        "distribution",
        lambda _name: dist_with_non_file_url,
    )
    assert version_surfaces.editable_install_origin() is None
