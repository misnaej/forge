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


# ---------------------------------------------------------------------------
# plugin_cache_status
# ---------------------------------------------------------------------------


def _write_plugin_tree(
    root: Path,
    *,
    version: str,
    hooks: tuple[str, ...] = (),
    name: str = "forge",
) -> Path:
    """Materialize a plugin tree — manifest plus a ``claude-hooks/`` set.

    Args:
        root: Directory to build the tree in (created if absent).
        version: Value for the manifest's ``version`` field.
        hooks: File names to create under ``claude-hooks/``.
        name: Value for the manifest's ``name`` field.

    Returns:
        *root*, so callers can chain.
    """
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )
    if hooks:
        (root / "claude-hooks").mkdir(exist_ok=True)
        for hook in hooks:
            (root / "claude-hooks" / hook).write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def _write_consumer_pin(repo_root: Path, ref: str) -> None:
    """Give *repo_root* a ``pyproject.toml`` pinning forge-scripts at *ref*.

    Args:
        repo_root: Consumer repo root (created if absent).
        ref: Pin target — a tag like ``v6.11.0``.
    """
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "pyproject.toml").write_text(
        "[project]\ndependencies = [\n"
        f'  "forge-scripts @ git+https://github.com/misnaej/forge@{ref}",\n]\n',
        encoding="utf-8",
    )


def _write_registry(path: Path, install_location: Path | str) -> None:
    """Write a ``known_marketplaces.json`` pointing forge at *install_location*.

    Args:
        path: File to write.
        install_location: Value for the entry's ``installLocation``.
    """
    path.write_text(
        json.dumps(
            {
                "forge": {
                    "source": {"source": "github", "repo": "misnaej/forge"},
                    "installLocation": str(install_location),
                }
            }
        ),
        encoding="utf-8",
    )


def test_plugin_cache_status_current_when_cache_matches_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo shipping a manifest still compares declared versions.

    SCENARIO: the pre-existing manifest branch — the one ``plugin_sync``
    depends on — must be untouched by the consumer branch.
    MOCK SETUP: repo manifest at 2.23.1, cache slot at 2.23.1.
    EXPECTED BEHAVIOR: ``"current"``, with no hook comparison involved.
    """
    _write_plugin_tree(tmp_path / "repo", version="2.23.1")
    cache_root = _write_plugin_tree(
        tmp_path / "cache" / "forge" / "forge" / "2.23.1", version="2.23.1"
    ).parent.parent
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: cache_root)

    status = version_surfaces.plugin_cache_status(tmp_path / "repo")

    assert status.state == "current"
    assert status.cached == "2.23.1"
    assert status.declared == "2.23.1"
    assert status.missing_hooks == ()


def test_plugin_cache_status_behind_when_cache_lags_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache slot older than the repo's manifest is still ``"behind"``.

    MOCK SETUP: repo manifest at 2.23.1, cache slot at 2.22.0.
    EXPECTED BEHAVIOR: ``"behind"`` — the version-string verdict the
    ``plugin_sync`` pre-commit step blocks on.
    """
    _write_plugin_tree(tmp_path / "repo", version="2.23.1")
    cache_root = _write_plugin_tree(
        tmp_path / "cache" / "forge" / "forge" / "2.22.0", version="2.22.0"
    ).parent.parent
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: cache_root)

    status = version_surfaces.plugin_cache_status(tmp_path / "repo")

    assert status.state == "behind"
    assert (status.cached, status.declared) == ("2.22.0", "2.23.1")


def test_plugin_cache_status_consumer_reports_hooks_the_cache_lacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer's stale slot is caught by content, not by version.

    SCENARIO: the failure the version comparison cannot see — the slot
    declares a version no higher than the clone's, yet ships fewer hooks.
    MOCK SETUP: no repo manifest; a pin at v6.11.0; a marketplace clone
    carrying three hooks; a cache slot carrying one of them.
    EXPECTED BEHAVIOR: ``"stale-content"`` naming the two absent hooks.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "v6.11.0")
    clone = _write_plugin_tree(
        tmp_path / "marketplaces" / "forge",
        version="5.2.0",
        hooks=("block_no_verify.sh", "warn_generated_conflicts.sh", "a.sh"),
    )
    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, clone)
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    cache_root = _write_plugin_tree(
        tmp_path / "cache" / "forge" / "forge" / "5.2.0",
        version="5.2.0",
        hooks=("a.sh",),
    ).parent.parent
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: cache_root)

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "stale-content"
    assert status.plugin_name == "forge"
    assert status.cached == "5.2.0"
    assert status.declared == "v6.11.0"
    assert status.missing_hooks == (
        "block_no_verify.sh",
        "warn_generated_conflicts.sh",
    )


def test_plugin_cache_status_consumer_current_when_hook_sets_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer slot carrying every pinned hook is current.

    MOCK SETUP: clone and cache slot ship the same two hooks, with the
    slot's declared version *below* the pinned ref.
    EXPECTED BEHAVIOR: ``"current"`` — the lagging version string is not
    the signal, the content is.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "v6.11.0")
    clone = _write_plugin_tree(
        tmp_path / "marketplaces" / "forge", version="5.2.0", hooks=("a.sh", "b.sh")
    )
    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, clone)
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    cache_root = _write_plugin_tree(
        tmp_path / "cache" / "forge" / "forge" / "5.2.0",
        version="5.2.0",
        hooks=("a.sh", "b.sh"),
    ).parent.parent
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: cache_root)

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "current"
    assert status.missing_hooks == ()


def test_plugin_cache_status_consumer_uncached_when_no_slot_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolvable pin with nothing installed reports ``"uncached"``.

    MOCK SETUP: pin and clone resolve; ``find_plugin_cache`` finds no slot.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "v6.11.0")
    clone = _write_plugin_tree(
        tmp_path / "marketplaces" / "forge", version="5.2.0", hooks=("a.sh",)
    )
    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, clone)
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: None)

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "uncached"
    assert status.declared == "v6.11.0"


def test_plugin_cache_status_consumer_without_pin_is_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No manifest and no pin degrades to ``"no-manifest"``, as before.

    MOCK SETUP: an empty repo directory; the registry exists but nothing
    points at it.
    """
    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, tmp_path / "marketplaces" / "forge")
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    repo = tmp_path / "bare"
    repo.mkdir()

    status = version_surfaces.plugin_cache_status(repo)

    assert status == version_surfaces.PluginCacheStatus(
        "no-manifest", "bare", None, None
    )


def test_plugin_cache_status_survives_missing_marketplace_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry that is not there is "unknown", never an exception.

    MOCK SETUP: a pinned consumer whose ``known_marketplaces.json`` path
    does not exist.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "v6.11.0")
    monkeypatch.setattr(
        version_surfaces, "KNOWN_MARKETPLACES", tmp_path / "absent.json"
    )

    assert version_surfaces.plugin_cache_status(repo).state == "no-manifest"


def test_plugin_cache_status_survives_corrupt_marketplace_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unparseable registry JSON degrades cleanly instead of raising.

    MOCK SETUP: a pinned consumer whose registry file holds truncated JSON.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "v6.11.0")
    registry = tmp_path / "known_marketplaces.json"
    registry.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)

    assert version_surfaces.plugin_cache_status(repo).state == "no-manifest"


def test_marketplace_clone_ignores_vanished_install_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry entry naming a deleted clone resolves to ``None``.

    MOCK SETUP: the registry points at a path that was never created.
    """
    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, tmp_path / "gone")
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)

    assert version_surfaces.marketplace_clone("misnaej/forge") is None


def test_marketplace_clone_none_for_unknown_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry with no entry for the pinned repo resolves to ``None``.

    MOCK SETUP: the registry holds only the forge entry; another repo slug
    is looked up.
    """
    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, _write_plugin_tree(tmp_path / "clone", version="1.0.0"))
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)

    assert version_surfaces.marketplace_clone("other/plugin") is None
