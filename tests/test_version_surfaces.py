"""Tests for forge.version_surfaces — the three install-version readers."""

# MOCKING STRATEGY: every reader touches either the filesystem (tmp_path),
# importlib.metadata, or Claude Code's own JSON registries
# (known_marketplaces.json / installed_plugins.json). metadata calls are
# stubbed so no real distribution needs to be installed; the registries are
# stubbed via KNOWN_MARKETPLACES / INSTALLED_PLUGINS so no test reads the
# real ~/.claude/plugins/ state.
#   - version_surfaces.metadata.version / .distribution: stubbed per test.
#   - version_surfaces.find_install_dir: stubbed for plugin_cache_version
#     tests that don't need a real two-level cache layout on disk.
#   - version_surfaces.KNOWN_MARKETPLACES / INSTALLED_PLUGINS: stubbed to
#     tmp_path files for every test reaching marketplace_clone /
#     installed_record — directly, or via plugin_cache_status.
#   - The commit-identity tests (_commit_identity_status / the
#     "stale-content" / "wrong-ref" consumer states) build REAL git repos
#     via tests.conftest's init_git_repo / init_single_track_repo /
#     commit_all: resolve_commit() and is_ancestor() shell out to git and
#     cannot be faked with a plain directory tree.

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import version_surfaces
from tests.conftest import GIT_ENV, commit_all, init_git_repo, init_single_track_repo


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


def test_find_install_dir_prefers_recorded_slot_over_semver_name(
    tmp_path: Path,
) -> None:
    """The install-record's named slot wins over the highest-semver fallback.

    SCENARIO: two cache slots exist — an older-named one that happens to
    be the ACTUAL install per Claude Code's own record, and a
    higher-semver-named one that is stale or unrelated. Without a
    ``preferred`` hint the highest-semver fallback would pick the wrong
    slot; a caller that knows what ``installed_plugins.json`` recorded
    must be able to override it.
    """
    older = tmp_path / "forge" / "1.9.0"
    (older / ".claude-plugin").mkdir(parents=True)
    (older / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    newer = tmp_path / "forge" / "2.0.0"
    (newer / ".claude-plugin").mkdir(parents=True)
    (newer / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")

    # Sanity: without a preference, the semver fallback picks the newer slot.
    assert version_surfaces.find_install_dir(tmp_path) == newer

    assert version_surfaces.find_install_dir(tmp_path, preferred=older) == older


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
# installed_record
# ---------------------------------------------------------------------------


def test_installed_record_prefers_this_repos_record(tmp_path: Path) -> None:
    """A project-scoped record for THIS repo wins over a user-scope record.

    SCENARIO: the registry carries two records for the same plugin key —
    a user-wide install and a project-scoped one for THIS repo (Claude
    Code lets a plugin be installed both ways). Judging this repo means
    judging the copy installed for it, so the project record must win
    even though it is not the last entry in the list.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    plugins_file = tmp_path / "installed_plugins.json"
    plugins_file.write_text(
        json.dumps(
            {
                "plugins": {
                    "forge@forge": [
                        {"scope": "user", "version": "1.0.0", "gitCommitSha": "aaaa"},
                        {
                            "scope": "project",
                            "projectPath": str(repo_root),
                            "version": "2.0.0",
                            "gitCommitSha": "bbbb",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    record = version_surfaces.installed_record(
        "forge@forge", repo_root, plugins_file=plugins_file
    )

    assert record is not None
    assert record.commit == "bbbb"
    assert record.version == "2.0.0"


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
        ref: Value for ``source.ref`` (the tracked ref), when given.
    """
    source: dict[str, str] = {"source": "github", "repo": "misnaej/forge"}
    if ref is not None:
        source["ref"] = ref
    path.write_text(
        json.dumps(
            {"forge": {"source": source, "installLocation": str(install_location)}}
        ),
        encoding="utf-8",
    )


def _write_installed_plugins(
    path: Path,
    *,
    commit: str | None = None,
    version: str | None = None,
    project_path: Path | None = None,
    install_path: Path | None = None,
) -> None:
    """Write an ``installed_plugins.json`` carrying one ``forge@forge`` record.

    Shared by every test that reaches :func:`version_surfaces.installed_record`
    — directly or via :func:`version_surfaces.plugin_cache_status` — so none
    of them read the real ``~/.claude/plugins/installed_plugins.json``.

    Args:
        path: File to write.
        commit: Value for the record's ``gitCommitSha``, when given.
        version: Value for the record's ``version``, when given.
        project_path: When given, scopes the record to that project
            (``scope: "project"``, ``projectPath: str(project_path)``);
            otherwise the record is user-scoped.
        install_path: Value for the record's ``installPath`` (the cache
            slot :func:`version_surfaces.InstallRecord.install_dir`
            reads), when given.
    """
    record: dict[str, object] = {"scope": "project" if project_path else "user"}
    if project_path is not None:
        record["projectPath"] = str(project_path)
    if commit is not None:
        record["gitCommitSha"] = commit
    if version is not None:
        record["version"] = version
    if install_path is not None:
        record["installPath"] = str(install_path)
    path.write_text(
        json.dumps({"plugins": {"forge@forge": [record]}}), encoding="utf-8"
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


@pytest.mark.parametrize(
    ("clone_version", "remedy_snippet"),
    [
        pytest.param(None, "/plugin update", id="version-less-clone"),
        pytest.param(
            "5.2.0",
            "delete ~/.claude/plugins/cache/forge/",
            id="declared-version-clone",
        ),
    ],
)
def test_consumer_status_stale_when_installed_commit_differs_from_clone_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clone_version: str | None,
    remedy_snippet: str,
) -> None:
    """A consumer's installed commit lagging the clone HEAD is "stale-content".

    SCENARIO: Claude Code installed one commit of the pinned marketplace
    clone and the clone has since advanced (a new commit pushed to the
    tracked ref) — the commit-based model's counterpart to a version
    string falling behind. Under tag-per-merge distinct commits can share
    one declared version (or none at all), so commit identity — not the
    version string — is what "stale" means here.
    MOCK SETUP: the marketplace clone is a REAL git repo (so
    ``resolve_commit(clone, "HEAD")`` — real git — resolves); the
    registered install commit is the clone's first commit, and the clone
    then advances with a second.
    EXPECTED BEHAVIOR: "stale-content", and the remedy follows the
    CLONE's own manifest: a version-less (commit-keyed) clone can only be
    re-pulled with ``/plugin update`` (an `/plugin update` compares
    declared versions, which a commit-keyed manifest has none of — so
    only re-pulling helps); a clone declaring a version needs the
    slot-deletion remedy (`/plugin update` there reports "already
    current" without moving the stale slot). The installed record also
    names an install slot carrying no hooks at all, so ``missing_hooks``
    must report exactly the one hook the clone's advance added.

    Args:
        clone_version: Value for the CLONE's own manifest ``"version"``
            field, or ``None`` to omit the key entirely.
        remedy_snippet: Substring the resulting ``status.remedy`` must
            contain for this manifest shape.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "main")
    clone = tmp_path / "marketplaces" / "forge"
    clone.mkdir(parents=True)
    init_git_repo(clone)
    manifest_dir = clone / ".claude-plugin"
    manifest_dir.mkdir()
    manifest = (
        {"name": "forge"}
        if clone_version is None
        else {
            "name": "forge",
            "version": clone_version,
        }
    )
    (manifest_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    commit_all(clone, "add manifest")
    installed_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clone,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (clone / "claude-hooks").mkdir()
    (clone / "claude-hooks" / "a.sh").write_text("x", encoding="utf-8")
    commit_all(clone, "advance")

    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, clone, ref="main")
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    # An installed slot with no hooks at all — proves missing_hooks is
    # computed via hook set difference, not merely
    # rendered from a hand-built PluginCacheStatus (test_doctor.py covers
    # rendering separately).
    stale_slot = _write_plugin_tree(tmp_path / "slot", version="0.0.0", hooks=())
    installed_plugins = tmp_path / "installed_plugins.json"
    _write_installed_plugins(
        installed_plugins, commit=installed_sha, install_path=stale_slot
    )
    monkeypatch.setattr(version_surfaces, "INSTALLED_PLUGINS", installed_plugins)

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "stale-content"
    assert remedy_snippet in status.remedy
    assert status.missing_hooks == ("a.sh",)


def test_consumer_status_semver_record_without_commit_is_no_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record's bare semver ``version`` (no ``gitCommitSha``) is never a commit.

    Behavior test: pins down :func:`version_surfaces._installed_commit`'s
    commit-shape guard end to end — a regression here would silently
    compare a version string like "6.11.0" against a real SHA and
    misreport every commit-keyed install lacking a recorded
    ``gitCommitSha`` as "stale-content".

    SCENARIO: same consumer/clone setup as
    ``test_consumer_status_stale_when_installed_commit_differs_from_clone_head``,
    but the install record carries only a semver-shaped ``version`` and
    no ``gitCommitSha`` at all.
    MOCK SETUP: a real git clone declaring ``"version": "6.11.0"`` (the
    realistic shape — Claude Code records whatever version the manifest
    declares, distinct from the sibling stale test's ``None`` /
    ``"5.2.0"`` clone-manifest parametrization; the declared value plays
    no role in this branch) so ``resolve_commit(clone, "HEAD")``
    resolves; the installed record has ``"version": "6.11.0"`` and no
    ``gitCommitSha``.
    EXPECTED BEHAVIOR: ``"unparsed"``, never "stale-content" — a semver
    string must not be compared against the clone's SHA.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "main")
    clone = tmp_path / "marketplaces" / "forge"
    clone.mkdir(parents=True)
    init_git_repo(clone)
    (clone / ".claude-plugin").mkdir()
    (clone / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "forge", "version": "6.11.0"}), encoding="utf-8"
    )
    commit_all(clone, "add manifest")

    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, clone, ref="main")
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    installed_plugins = tmp_path / "installed_plugins.json"
    _write_installed_plugins(installed_plugins, version="6.11.0")
    monkeypatch.setattr(version_surfaces, "INSTALLED_PLUGINS", installed_plugins)

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "unparsed"


def test_consumer_status_wrong_ref_when_registration_serves_another_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The machine's one registration tracking another ref is "wrong-ref".

    SCENARIO: Claude Code keeps ONE ``known_marketplaces.json``
    registration per marketplace name per user — another repo on this
    machine may have registered the forge marketplace at a DIFFERENT ref
    than this repo pins, and no update in THIS repo can change that.
    MOCK SETUP: a real git clone (so ``find_install_dir`` resolves a
    manifest); the registration's ``source.ref`` is ``"dev"``; the
    consumer pins the tag ``"v6.11.0"``, which does not exist in the
    clone — so ``_clone_serves_ref`` cannot fall back to resolving the
    pin by commit either.
    EXPECTED BEHAVIOR: "wrong-ref", naming both the tracked ref
    (``cached``) and the pinned ref (``declared``), with the
    re-registration remedy.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "v6.11.0")
    clone = tmp_path / "marketplaces" / "forge"
    clone.mkdir(parents=True)
    init_git_repo(clone)
    (clone / ".claude-plugin").mkdir()
    (clone / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "forge"}), encoding="utf-8"
    )
    commit_all(clone, "add manifest")

    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, clone, ref="dev")
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    monkeypatch.setattr(version_surfaces, "INSTALLED_PLUGINS", tmp_path / "absent.json")

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "wrong-ref"
    assert status.cached == "dev"
    assert status.declared == "v6.11.0"
    assert "re-point" in status.remedy
    assert "claude plugin marketplace remove forge" in status.remedy


def test_plugin_cache_status_consumer_current_when_installed_commit_matches_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer whose installed commit equals the clone HEAD is "current".

    Commit-based counterpart of the old hook-set comparison: content
    identity is judged by commit, not by a declared version string or a
    hook-name diff.
    MOCK SETUP: a real git clone; the registered install's
    ``gitCommitSha`` is the clone's own HEAD.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "main")
    clone = tmp_path / "marketplaces" / "forge"
    clone.mkdir(parents=True)
    init_git_repo(clone)
    (clone / ".claude-plugin").mkdir()
    (clone / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "forge"}), encoding="utf-8"
    )
    commit_all(clone, "add manifest")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clone,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, clone, ref="main")
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    installed_plugins = tmp_path / "installed_plugins.json"
    _write_installed_plugins(installed_plugins, commit=head)
    monkeypatch.setattr(version_surfaces, "INSTALLED_PLUGINS", installed_plugins)

    status = version_surfaces.plugin_cache_status(repo)

    assert status.state == "current"
    assert status.cached == head[:12]


def test_plugin_cache_status_consumer_uncached_when_no_install_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolvable pin with no install record reports ``"uncached"``.

    MOCK SETUP: the clone's registered ``ref`` matches the pin by NAME,
    short-circuiting ``_clone_serves_ref`` without needing a real git
    repo; ``INSTALLED_PLUGINS`` points at a file that does not exist, so
    ``installed_record`` finds nothing.
    """
    repo = tmp_path / "consumer"
    _write_consumer_pin(repo, "v6.11.0")
    clone = _write_plugin_tree(
        tmp_path / "marketplaces" / "forge", version="5.2.0", hooks=("a.sh",)
    )
    registry = tmp_path / "known_marketplaces.json"
    _write_registry(registry, clone, ref="v6.11.0")
    monkeypatch.setattr(version_surfaces, "KNOWN_MARKETPLACES", registry)
    monkeypatch.setattr(version_surfaces, "INSTALLED_PLUGINS", tmp_path / "absent.json")

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
# _commit_identity_status (via plugin_cache_status, a version-less manifest)
# ---------------------------------------------------------------------------


def _sha_identity_repo(base: Path, *, surface_change: bool) -> tuple[Path, str]:
    """Build a single-track plugin repo and return ``(work_tree, installed_sha)``.

    Seeds ``.claude-plugin/`` (a version-less manifest, forge's own
    shape) plus ``claude-hooks/a.sh`` (the plugin surface) on ``main``,
    records the installed commit, then advances the pushed base with
    either a plugin-surface change (``claude-hooks/a.sh``) or an
    unrelated one (``README.md``) — the fork point every
    ``_commit_identity_status`` test in this module shares.

    Args:
        base: Parent temp directory (``work``/``origin.git`` created
            inside it via :func:`init_single_track_repo`).
        surface_change: When ``True``, the advance touches the plugin
            surface (→ "behind"); otherwise an unrelated file (→
            "current").

    Returns:
        ``(work_tree, installed_commit_sha)``.
    """
    work, _bare = init_single_track_repo(base)
    (work / ".claude-plugin").mkdir()
    (work / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "forge"}), encoding="utf-8"
    )
    (work / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "forge"}), encoding="utf-8"
    )
    (work / "claude-hooks").mkdir()
    (work / "claude-hooks" / "a.sh").write_text("x", encoding="utf-8")
    commit_all(work, "seed")
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=GIT_ENV, check=True
    )
    installed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    target = "claude-hooks/a.sh" if surface_change else "README.md"
    (work / target).write_text("changed", encoding="utf-8")
    commit_all(work, "advance")
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=GIT_ENV, check=True
    )
    return work, installed


def test_sha_identity_behind_when_base_changed_plugin_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base-branch push touching claude-hooks/ makes a commit-keyed plugin "behind".

    SCENARIO: forge's own manifest shape (no declared version) — the
    plugin is keyed on its installed commit, so "behind" means the base
    branch has since changed what Claude Code actually loads
    (:data:`version_surfaces.PLUGIN_SURFACE`), not merely that it moved.
    MOCK SETUP: a real single-track repo; the installed commit is the
    seed commit; ``origin/main`` (the base) advances with a
    ``claude-hooks/a.sh`` edit — squarely inside the plugin surface.
    EXPECTED BEHAVIOR: "behind", with the refresh remedy naming this
    repo's own marketplace + plugin name.
    """
    work, installed = _sha_identity_repo(tmp_path, surface_change=True)
    installed_plugins = tmp_path / "installed_plugins.json"
    _write_installed_plugins(installed_plugins, commit=installed, project_path=work)
    monkeypatch.setattr(version_surfaces, "INSTALLED_PLUGINS", installed_plugins)

    status = version_surfaces.plugin_cache_status(work)

    assert status.state == "behind"
    assert status.cached == installed[:12]
    assert "origin/main" in status.declared
    assert "/plugin marketplace update forge" in status.remedy


def test_sha_identity_current_when_base_changed_only_other_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base-branch push touching only unrelated files stays "current".

    SCENARIO: mirrors ``test_sha_identity_behind_when_base_changed_plugin_surface``
    with ``surface_change=False`` — the advance touches ``README.md``,
    outside :data:`version_surfaces.PLUGIN_SURFACE`, so nothing Claude
    Code loads changed even though the base has moved past the installed
    commit.
    MOCK SETUP: same real single-track repo + installed-commit setup as
    the sibling "behind" test; only the touched path differs.
    EXPECTED BEHAVIOR: "current" — a base-branch advance outside the
    plugin surface must not be reported as skew.
    """
    work, installed = _sha_identity_repo(tmp_path, surface_change=False)
    installed_plugins = tmp_path / "installed_plugins.json"
    _write_installed_plugins(installed_plugins, commit=installed, project_path=work)
    monkeypatch.setattr(version_surfaces, "INSTALLED_PLUGINS", installed_plugins)

    status = version_surfaces.plugin_cache_status(work)

    assert status.state == "current"
    assert status.cached == installed[:12]


def test_sha_identity_unknown_commit_is_no_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed commit this clone doesn't know degrades to "unparsed" (no finding).

    SCENARIO: a newer local install (or a fork) can carry an installed
    commit not reachable in THIS repo's history — the guard never
    guesses; it must not misreport "behind" (or "current") for a commit
    it cannot place.
    MOCK SETUP: same real single-track repo as the sibling identity
    tests, but the installed record's ``gitCommitSha`` is an all-zero
    SHA never committed to the repo.
    EXPECTED BEHAVIOR: "unparsed" — what both ``plugin_sync`` and
    ``forge-doctor`` treat as nothing to report.
    """
    work, _installed = _sha_identity_repo(tmp_path, surface_change=True)
    installed_plugins = tmp_path / "installed_plugins.json"
    _write_installed_plugins(installed_plugins, commit="0" * 40, project_path=work)
    monkeypatch.setattr(version_surfaces, "INSTALLED_PLUGINS", installed_plugins)

    status = version_surfaces.plugin_cache_status(work)

    assert status.state == "unparsed"
