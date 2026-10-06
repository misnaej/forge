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


def test_hook_sidecar_version_none_when_sidecar_undecodable(tmp_path: Path) -> None:
    """A sidecar holding non-UTF-8 bytes reads as absent, not a crash."""
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / version_surfaces.HOOK_VERSION_SIDECAR).write_bytes(b"\xff\xfe")
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


def _write_registry(
    path: Path, install_location: Path | str, *, ref: str | None = None
) -> None:
    """Write a ``known_marketplaces.json`` pointing forge at *install_location*.

    Args:
        path: File to write.
        install_location: Value for the entry's ``installLocation``.
        ref: The registration's ``source.ref``; omitted when ``None``.
    """
    source = {"source": "github", "repo": "misnaej/forge"}
    if ref is not None:
        source["ref"] = ref
    path.write_text(
        json.dumps(
            {
                "forge": {
                    "source": source,
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
    assert status.stale_areas == ()
    # No install record names this repo, so the newest copy was judged.
    assert status.fallback is True


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


def test_plugin_cache_status_consumer_names_stale_content_areas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer's stale slot is caught by content, not by version.

    SCENARIO: the failure the version comparison cannot see — the slot
    declares a version no higher than the clone's, yet ships fewer hooks.
    MOCK SETUP: no repo manifest; a pin at v6.11.0; a marketplace clone
    carrying three hooks; a cache slot carrying one of them.
    EXPECTED BEHAVIOR: ``"stale-content"`` naming the hooks area.
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
    assert status.stale_areas == ("claude-hooks",)


def test_plugin_cache_status_consumer_current_when_content_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer slot carrying the pinned content byte for byte is current.

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
    assert status.stale_areas == ()


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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git+https://github.com/misnaej/forge", "misnaej/forge"),
        ("git+https://github.com/misnaej/forge.git", "misnaej/forge"),
        ("git+ssh://git@github.com/misnaej/forge", "misnaej/forge"),
        ("git@github.com:misnaej/forge.git", "misnaej/forge"),
        ("git+https://token:x@github.com/misnaej/forge", "misnaej/forge"),
        ("git+https://gitlab.com/group/sub/repo", "group/sub/repo"),
        ("nonsense", None),
    ],
)
def test_repo_slug_reads_every_pin_url_shape(url: str, expected: str | None) -> None:
    """Each pin URL form resolves to its slug, or to None when it names none.

    Two cases carry the weight. A URL with embedded credentials must not
    leak them into the slug — they belong to the host half, which the
    ``://`` split discards. And a path of more than two segments is
    returned whole: trimming it to the last two would silently name a
    different repository, where returning it intact simply fails to match
    and leaves the check reading "unknown".

    Args:
        url: Pin URL to test.
        expected: Expected slug result or None.
    """
    assert version_surfaces._repo_slug(url) == expected


# ---------------------------------------------------------------------------
# installed_plugins.json — which copy this repo uses
# ---------------------------------------------------------------------------


def _write_installed(claude_home: Path, records: object) -> None:
    """Write a fake ``installed_plugins.json`` listing *records* for forge.

    Args:
        claude_home: The fake ``~/.claude`` from the ``claude_home`` fixture.
        records: The ``forge@forge`` value — a list of records or a single
            record, the two shapes seen on disk.
    """
    plugins = claude_home / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"forge@forge": records}}),
        encoding="utf-8",
    )


def _record(
    install_dir: Path, *, scope: str, project: Path | None = None
) -> dict[str, str]:
    """Build one install record the way Claude Code writes it.

    Args:
        install_dir: The cache directory the install loads.
        scope: ``"user"`` or ``"project"``.
        project: The repo a project-scope record belongs to.

    Returns:
        The record dict.
    """
    record = {
        "scope": scope,
        "installPath": str(install_dir),
        "version": install_dir.name,
    }
    if project is not None:
        record["projectPath"] = str(project)
    return record


def test_plugin_installs_prefers_this_repos_record_over_newest_copy(
    tmp_path: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo is judged on the copy its own record names, not the newest.

    SCENARIO: four repos on four cached copies all reported the newest;
    the record for this repo must win over the newest copy on disk.
    MOCK SETUP: cache holds 8.2.0 and 9.1.1; this repo's record names
    8.2.0, another repo's names 9.1.1.
    EXPECTED BEHAVIOR: 8.2.0 is chosen and no fallback is flagged.
    """
    cache = tmp_path / "cache" / "forge" / "forge"
    old = _write_plugin_tree(cache / "8.2.0", version="8.2.0")
    new = _write_plugin_tree(cache / "9.1.1", version="9.1.1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_installed(
        claude_home,
        [
            _record(new, scope="project", project=tmp_path / "other"),
            _record(old, scope="project", project=repo),
        ],
    )
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: cache.parent)

    installs = version_surfaces.plugin_installs(repo, "forge")

    assert installs.repo is not None
    assert installs.repo.version == "8.2.0"
    assert installs.install_dir == old
    assert installs.fallback is False


def test_plugin_installs_falls_back_to_newest_copy_without_a_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No record for this repo → the newest cached copy, flagged as such.

    MOCK SETUP: no installed_plugins.json; the cache holds 8.2.0 and 9.1.1.
    EXPECTED BEHAVIOR: 9.1.1 is judged and ``fallback`` says so.
    """
    cache = tmp_path / "cache" / "forge" / "forge"
    _write_plugin_tree(cache / "8.2.0", version="8.2.0")
    new = _write_plugin_tree(cache / "9.1.1", version="9.1.1")
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: cache.parent)

    installs = version_surfaces.plugin_installs(tmp_path / "repo", "forge")

    assert installs.repo is None
    assert installs.install_dir == new
    assert installs.fallback is True


@pytest.mark.parametrize("with_user", [True, False])
def test_plugin_installs_reports_a_user_scope_record(
    tmp_path: Path,
    claude_home: Path,
    *,
    with_user: bool,
) -> None:
    """A machine-wide record is reported, never substituted for the repo's.

    Args:
        tmp_path: Pytest temp directory.
        claude_home: Fake ``~/.claude``.
        with_user: Whether a user-scope record exists alongside the repo's.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    own = _write_plugin_tree(tmp_path / "cache" / "9.1.1", version="9.1.1")
    records = [_record(own, scope="project", project=repo)]
    if with_user:
        global_copy = _write_plugin_tree(
            tmp_path / "cache" / "3.30.0", version="3.30.0"
        )
        records.insert(0, _record(global_copy, scope="user"))
    _write_installed(claude_home, records)

    installs = version_surfaces.plugin_installs(repo, "forge")

    assert installs.install_dir == own
    if with_user:
        assert installs.user is not None
        assert installs.user.version == "3.30.0"
    else:
        assert installs.user is None


def test_plugin_records_accepts_a_single_record(
    tmp_path: Path, claude_home: Path
) -> None:
    """A single record in place of a list is read the same way.

    Args:
        tmp_path: Pytest temp directory.
        claude_home: Fake ``~/.claude``.
    """
    install = _write_plugin_tree(tmp_path / "cache" / "9.1.1", version="9.1.1")
    _write_installed(claude_home, _record(install, scope="user"))

    assert [r["version"] for r in version_surfaces.plugin_records("forge")] == ["9.1.1"]


@pytest.mark.parametrize(
    "content",
    [
        "{not-json",
        '["a", "list"]',
        '{"plugins": ["not", "a", "dict"]}',
        '{"plugins": {"forge@forge": [42, null, {"installPath": 7}]}}',
    ],
)
def test_plugin_installs_degrades_on_malformed_records(
    tmp_path: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    """Malformed install records read as "no record", never an exception.

    Args:
        tmp_path: Pytest temp directory.
        claude_home: Fake ``~/.claude``.
        monkeypatch: Pytest monkeypatch fixture.
        content: Raw ``installed_plugins.json`` text.
    """
    (claude_home / "plugins").mkdir(parents=True)
    (claude_home / "plugins" / "installed_plugins.json").write_text(
        content, encoding="utf-8"
    )
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: None)

    installs = version_surfaces.plugin_installs(tmp_path, "forge")

    assert installs == version_surfaces.PluginInstalls(None, None, None, fallback=False)


def test_plugin_cache_status_manifest_repo_judges_its_own_copy(
    tmp_path: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin-shipping repo whose own copy lags is ``"behind"``.

    SCENARIO: the newest cached copy is current, so judging it would say
    "current" for a repo still loading an older one.
    MOCK SETUP: manifest 9.1.1; cache holds 9.0.0 and 9.1.1; this repo's
    record names 9.0.0.
    EXPECTED BEHAVIOR: ``"behind"`` at 9.0.0, no fallback.
    """
    repo = _write_plugin_tree(tmp_path / "repo", version="9.1.1")
    cache = tmp_path / "cache" / "forge" / "forge"
    own = _write_plugin_tree(cache / "9.0.0", version="9.0.0")
    _write_plugin_tree(cache / "9.1.1", version="9.1.1")
    _write_installed(claude_home, [_record(own, scope="project", project=repo)])
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: cache.parent)

    status = version_surfaces.plugin_cache_status(repo)

    assert (status.state, status.cached, status.fallback) == ("behind", "9.0.0", False)


# ---------------------------------------------------------------------------
# Consumer content comparison and marketplace source
# ---------------------------------------------------------------------------


def _consumer_with_clone_and_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry_ref: str | None = None,
) -> tuple[Path, Path, Path]:
    """Build a pinned consumer, its marketplace clone and an identical slot.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Pytest monkeypatch fixture.
        registry_ref: The registration's ``source.ref``, if any.

    Returns:
        ``(repo, clone, slot)`` — the slot starts byte-identical to the
        clone, so a test changes one side to create the difference it
        exercises.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "v6.11.0")
    trees = []
    for root in (tmp_path / "marketplaces" / "forge", tmp_path / "cache" / "6.11.0"):
        tree = _write_plugin_tree(root, version="6.11.0", hooks=("a.sh",))
        (tree / "agents").mkdir()
        (tree / "agents" / "reviewer.md").write_text("review\n", encoding="utf-8")
        trees.append(tree)
    clone, slot = trees
    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, clone, ref=registry_ref)
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: slot.parent)
    return repo, clone, slot


def test_consumer_same_hook_names_with_changed_content_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook fixed under the same name still counts as stale content.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    repo, clone, _slot = _consumer_with_clone_and_cache(tmp_path, monkeypatch)
    (clone / "claude-hooks" / "a.sh").write_text("#!/bin/sh\nfixed\n", encoding="utf-8")

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "stale-content"
    assert status.stale_areas == ("claude-hooks",)


def test_consumer_rewritten_agent_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewritten agent — no hook involved — is detected and named.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    repo, _clone, slot = _consumer_with_clone_and_cache(tmp_path, monkeypatch)
    (slot / "agents" / "reviewer.md").write_text("older\n", encoding="utf-8")

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "stale-content"
    assert status.stale_areas == ("agents",)


def test_consumer_manifest_version_only_difference_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifests differing only in ``version`` are the same content.

    Fragments mode parks the manifest version at the latest tag, so trees
    with identical content legitimately disagree there.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    repo, clone, _slot = _consumer_with_clone_and_cache(tmp_path, monkeypatch)
    (clone / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "6.12.0", "name": "forge"}), encoding="utf-8"
    )

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "current"
    assert status.stale_areas == ()


@pytest.mark.parametrize(
    ("registry_ref", "settings_ref", "expected"),
    [
        ("main", None, "source-mismatch"),
        ("v6.11.0", None, "current"),
        (None, None, "current"),
        ("main", "main", "current"),
    ],
    ids=["mismatch", "match", "unknown-registry-ref", "settings-ref-wins"],
)
def test_consumer_marketplace_source_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry_ref: str | None,
    settings_ref: str | None,
    expected: str,
) -> None:
    """The machine-wide registration must track the ref this repo pins.

    The repo side is its ``.claude/settings.json`` marketplace ref when
    set, else the pip pin's ref; a missing ref on either side is unknown,
    never a mismatch.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Pytest monkeypatch fixture.
        registry_ref: The registration's ``source.ref``.
        settings_ref: The repo settings' marketplace ref, if any.
        expected: The verdict.
    """
    repo, _clone, _slot = _consumer_with_clone_and_cache(
        tmp_path, monkeypatch, registry_ref=registry_ref
    )
    if settings_ref is not None:
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "extraKnownMarketplaces": {
                        "forge": {"source": {"repo": "x/y", "ref": settings_ref}}
                    }
                }
            ),
            encoding="utf-8",
        )

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == expected
    if expected == "source-mismatch":
        assert (status.source_ref, status.registered_ref) == ("v6.11.0", "main")
        assert status.plugin_name == "forge"
        assert status.source_repo == "misnaej/forge"


# ---------------------------------------------------------------------------
# Hostile or malformed input — degrade, never raise, never echo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'["a", "list"]', "not a JSON object"),
        (b'{"name": "\xff\xfe"}', "unreadable"),
    ],
    ids=["list-typed", "non-utf8"],
)
def test_read_json_rejects_non_object_and_undecodable(
    tmp_path: Path, raw: bytes, message: str
) -> None:
    """Anything but a readable JSON object is an error result, not a raise.

    Args:
        tmp_path: Pytest temp directory.
        raw: File bytes.
        message: Expected error fragment.
    """
    path = tmp_path / "x.json"
    path.write_bytes(raw)

    data, err = version_surfaces.read_json(path)

    assert data == {}
    assert err is not None
    assert message in err


@pytest.mark.parametrize(
    "raw", [b'["a", "list"]', b"\xff\xfe"], ids=["list-typed", "non-utf8"]
)
def test_install_version_falls_back_on_malformed_manifest(
    tmp_path: Path, raw: bytes
) -> None:
    """A malformed cached manifest falls back to the slot's directory name.

    Args:
        tmp_path: Pytest temp directory.
        raw: Manifest bytes.
    """
    install = tmp_path / "9.1.1"
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".claude-plugin" / "plugin.json").write_bytes(raw)

    assert version_surfaces.install_version(install) == "9.1.1"


def test_plugin_installs_survives_nul_in_project_path(
    tmp_path: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record whose projectPath holds a NUL byte matches nothing.

    Args:
        tmp_path: Pytest temp directory.
        claude_home: Fake ``~/.claude``.
        monkeypatch: Pytest monkeypatch fixture.
    """
    install = _write_plugin_tree(tmp_path / "cache" / "9.1.1", version="9.1.1")
    _write_installed(
        claude_home,
        [_record(install, scope="project", project=tmp_path / "re\0po")],
    )
    monkeypatch.setattr(version_surfaces, "find_plugin_cache", lambda _n: None)

    installs = version_surfaces.plugin_installs(tmp_path / "repo", "forge")

    assert installs.repo is None


def test_consumer_ignores_symlinks_inside_content_areas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlinked files and directories in a slot are never followed.

    SCENARIO: a link in the cache copy pointing elsewhere on the machine
    must neither be hashed nor walked into.
    MOCK SETUP: clone and slot identical; the slot gains a symlinked file
    and a symlinked directory under ``agents/``, both to outside content.
    EXPECTED BEHAVIOR: ``"current"`` — the links contribute nothing.
    """
    repo, _clone, slot = _consumer_with_clone_and_cache(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("elsewhere\n", encoding="utf-8")
    (slot / "agents" / "linked.md").symlink_to(outside / "secret.md")
    (slot / "agents" / "linked-dir").symlink_to(outside, target_is_directory=True)

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "current"


@pytest.mark.parametrize(
    ("cap", "value"),
    [("_MAX_AREA_FILES", 0), ("_MAX_AREA_BYTES", 3)],
    ids=["file-cap", "byte-cap"],
)
def test_consumer_content_over_the_caps_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cap: str,
    value: int,
) -> None:
    """A tree too large to hash yields "unknown", never stale or current.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Pytest monkeypatch fixture.
        cap: Name of the cap constant lowered for the test.
        value: The lowered cap.
    """
    repo, _clone, _slot = _consumer_with_clone_and_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(version_surfaces, cap, value)

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "content-unknown"
    assert status.stale_areas == ()


def test_plugin_cache_status_replaces_unsafe_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names and refs carrying shell or control characters are not echoed.

    SCENARIO: a planted registry ref and clone manifest name would
    otherwise be printed inside a remediation command.
    MOCK SETUP: clone manifest name ``forge; rm -rf ~``; registry ref
    ``main$(id)``.
    EXPECTED BEHAVIOR: both fields read ``<unprintable>``.
    """
    repo, clone, _slot = _consumer_with_clone_and_cache(
        tmp_path, monkeypatch, registry_ref="main$(id)"
    )
    (clone / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "forge; rm -rf ~", "version": "6.11.0"}),
        encoding="utf-8",
    )

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "source-mismatch"
    assert status.plugin_name == version_surfaces.UNPRINTABLE
    assert status.registered_ref == version_surfaces.UNPRINTABLE
    assert status.source_ref == "v6.11.0"
