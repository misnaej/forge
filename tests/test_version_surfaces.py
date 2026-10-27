"""Tests for forge.version_surfaces — the install-version and install-record readers."""

# MOCKING STRATEGY: every reader touches either the filesystem (tmp_path) or
# importlib.metadata; metadata calls are stubbed so no real distribution
# needs to be installed.
#   - version_surfaces.metadata.version / .distribution: stubbed per test.
#   - Claude Code's install records and user settings: written into the fake
#     ``~/.claude`` the autouse ``claude_home`` fixture provides.

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NamedTuple

import pytest

from forge import version_surfaces
from tests.conftest import write_install


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
# install_version
# ---------------------------------------------------------------------------


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
# safe_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("forge@forge-v1-2-3", "forge@forge-v1-2-3"),
        ("forge; rm -rf ~", version_surfaces.UNPRINTABLE),
        ("main$(id)", version_surfaces.UNPRINTABLE),
    ],
    ids=["none", "plain-key", "shell-metachars", "command-substitution"],
)
def test_safe_text_replaces_anything_not_name_shaped(
    value: object, expected: str | None
) -> None:
    """Values read from another tool's file are echoed only when name-shaped.

    Args:
        value: The raw value.
        expected: What may be printed.
    """
    assert version_surfaces.safe_text(value) == expected


# ---------------------------------------------------------------------------
# per-checkout plugin install — target release and install records
# ---------------------------------------------------------------------------


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


def _write_manifest_version(repo: Path, content: str) -> None:
    """Write a raw ``.claude-plugin/plugin.json`` into *repo*.

    Args:
        repo: Checkout root.
        content: The file's exact text.
    """
    (repo / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(content, encoding="utf-8")


def _write_records(claude_home: Path, content: str) -> None:
    """Write raw ``installed_plugins.json`` text into the fake home.

    Args:
        claude_home: The fake ``~/.claude`` from the ``claude_home`` fixture.
        content: The file's exact text.
    """
    (claude_home / "plugins").mkdir(parents=True, exist_ok=True)
    (claude_home / "plugins" / "installed_plugins.json").write_text(
        content, encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("version", "expected"),
    [("1.2.3", "forge-v1-2-3"), ("10.0.14", "forge-v10-0-14")],
)
def test_pinned_marketplace_turns_dots_into_dashes(version: str, expected: str) -> None:
    """The name is one token and always says which release it serves.

    Args:
        version: Release version string, dotted.
        expected: Marketplace name the version maps to.
    """
    assert version_surfaces.pinned_marketplace(version) == expected


def test_target_plugin_version_is_the_manifest_version(tmp_path: Path) -> None:
    """A plugin repo runs the release its own manifest names, pin or not."""
    _write_consumer_pin(tmp_path, "v9.9.9")
    _write_manifest_version(tmp_path, '{"name": "forge", "version": "1.2.3"}')

    assert version_surfaces.target_plugin_version(tmp_path) == "1.2.3"


@pytest.mark.parametrize(
    "manifest",
    [
        '{"name": "forge", "version": "banana"}',
        '{"name": "forge", "version": "1.2.3/../../x"}',
        '{"name": "forge"}',
        "{not json",
    ],
    ids=["unparsable-version", "suffixed-version", "no-version", "malformed"],
)
def test_target_plugin_version_does_not_fall_back_to_the_pin(
    tmp_path: Path, manifest: str
) -> None:
    """A manifest that cannot name a release is unknown, never the pin's release.

    Args:
        manifest: Raw manifest file content that names no usable version.
    """
    _write_consumer_pin(tmp_path, "v9.9.9")
    _write_manifest_version(tmp_path, manifest)

    assert version_surfaces.target_plugin_version(tmp_path) is None


@pytest.mark.parametrize(
    ("ref", "expected"),
    [("v2.3.4", "2.3.4"), ("main", None)],
    ids=["tag-pin", "branch-pin"],
)
def test_target_plugin_version_of_a_consumer_is_its_tag_pin(
    tmp_path: Path, ref: str, expected: str | None
) -> None:
    """A consumer runs the tag its pin names; a branch names no fixed content.

    Args:
        ref: Pinned git ref (tag or branch).
        expected: Release version the ref resolves to, or None for a branch.
    """
    _write_consumer_pin(tmp_path, ref)

    assert version_surfaces.target_plugin_version(tmp_path) == expected


def test_target_plugin_version_without_manifest_or_pin_is_none(
    tmp_path: Path,
) -> None:
    """Nothing names a release."""
    assert version_surfaces.target_plugin_version(tmp_path) is None


def test_checkout_installs_returns_only_this_checkouts_own_records(
    tmp_path: Path, claude_home: Path
) -> None:
    """Another repo's, the user-scope and vanished-directory records are not ours.

    SCENARIO: one machine-wide record file holds four records; only one is
    this checkout's. The checkout is reached through a symlink, as Claude
    Code may have recorded the resolved path.
    """
    real = tmp_path / "repo"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    own_dir = write_install(claude_home, real, "1.2.3")
    write_install(claude_home, tmp_path / "other", "1.1.0")
    write_install(claude_home, None, "1.0.0", key="forge@forge", scope="user")
    gone = write_install(claude_home, real, "0.5.0")
    gone.rmdir()

    installs = version_surfaces.checkout_installs(link)

    assert installs == [
        version_surfaces.CheckoutInstall(
            "forge@forge-v1-2-3", "local", "1.2.3", own_dir
        )
    ]


def test_checkout_installs_reads_the_named_plugin_and_a_single_record(
    tmp_path: Path, claude_home: Path
) -> None:
    """``plugin`` selects which plugin's records count; a bare record is read too.

    SCENARIO: the record file holds this checkout's forge install and a
    single record (not a list) for another plugin, both for this checkout.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    write_install(claude_home, repo, "1.2.3")
    other_dir = tmp_path / "other-cache" / "0.4.0"
    other_dir.mkdir(parents=True)
    path = claude_home / "plugins" / "installed_plugins.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["plugins"]["other@market"] = {
        "scope": "project",
        "installPath": str(other_dir),
        "projectPath": str(repo),
        "version": "0.4.0",
    }
    path.write_text(json.dumps(data), encoding="utf-8")

    installs = version_surfaces.checkout_installs(repo, plugin="other")

    assert installs == [
        version_surfaces.CheckoutInstall("other@market", "project", "0.4.0", other_dir)
    ]


@pytest.mark.parametrize(
    "content",
    [
        "{not-json",
        '["a", "list"]',
        '{"plugins": ["not", "a", "dict"]}',
        '{"plugins": {"forge@forge": [42, null, {"installPath": 7}]}}',
        (
            '{"plugins": {"forge@forge": [{"scope": "local", '
            '"installPath": "/x", "projectPath": "re\\u0000po"}]}}'
        ),
    ],
    ids=["not-json", "list", "plugins-not-dict", "junk-records", "nul-project"],
)
def test_install_readers_degrade_on_malformed_records(
    tmp_path: Path, claude_home: Path, content: str
) -> None:
    """Malformed install records read as "no install", never an exception.

    Args:
        tmp_path: Pytest temp directory.
        claude_home: Fake ``~/.claude``.
        content: Raw ``installed_plugins.json`` text.
    """
    _write_records(claude_home, content)

    assert version_surfaces.checkout_installs(tmp_path / "repo") == []
    assert version_surfaces.user_installs() == []
    assert version_surfaces.resolve_install(tmp_path / "repo") is None


class InstallHome(NamedTuple):
    """The temp directory and the machine's Claude Code records for one check."""

    tmp_path: Path
    claude_home: Path


@pytest.fixture
def install_home(tmp_path: Path, claude_home: Path) -> InstallHome:
    """Bundle the temp directory and the Claude Code records for one install check."""
    return InstallHome(tmp_path, claude_home)


@pytest.mark.parametrize(
    ("key", "version", "own_repo", "expected"),
    [
        ("forge@forge-v1-2-3", "1.2.3", True, True),
        ("forge@forge", "1.2.3", True, False),
        ("forge@forge-v1-2-3", "1.2.2", True, False),
        ("forge@forge-v1-2-3", "1.2.3", False, False),
    ],
    ids=["own-install", "shared-marketplace-key", "wrong-version", "other-repo"],
)
def test_checkout_plugin_current_needs_this_repos_install_of_the_release(
    install_home: InstallHome,
    key: str,
    version: str,
    *,
    own_repo: bool,
    expected: bool,
) -> None:
    """Current means the per-version key, at that version, for this checkout.

    Args:
        key: Install record key under test.
        version: Release version recorded for the install.
        own_repo: Whether the install record belongs to this checkout.
        expected: Whether the checkout counts as current.
    """
    tmp_path, claude_home = install_home
    repo = tmp_path / "repo"
    repo.mkdir()
    write_install(
        claude_home, repo if own_repo else tmp_path / "other", version, key=key
    )

    assert version_surfaces.checkout_plugin_current(repo, "1.2.3") is expected


# ---------------------------------------------------------------------------
# resolve_install — which install answers for a checkout
# ---------------------------------------------------------------------------


def test_resolve_install_prefers_the_release_marketplace_install(
    tmp_path: Path, claude_home: Path
) -> None:
    """Several forge installs for one checkout: its release's own one wins.

    SCENARIO: a checkout carries an older shared-marketplace record at the
    same version, filed first, beside its per-release install.
    EXPECTED BEHAVIOR: the per-release record, not the first one filed.
    """
    repo = tmp_path / "repo"
    _write_manifest_version(repo, '{"name": "forge", "version": "1.2.3"}')
    write_install(claude_home, repo, "1.2.3", key="forge@forge", scope="project")
    pinned = write_install(claude_home, repo, "1.2.3")

    install = version_surfaces.resolve_install(repo)

    assert install is not None
    assert install.key == "forge@forge-v1-2-3"
    assert install.install_dir == pinned


def test_resolve_install_falls_back_to_the_checkouts_other_install(
    tmp_path: Path, claude_home: Path
) -> None:
    """No install at the target release: the checkout's own record still answers."""
    repo = tmp_path / "repo"
    _write_manifest_version(repo, '{"name": "forge", "version": "1.2.3"}')
    write_install(claude_home, repo, "1.2.0")

    install = version_surfaces.resolve_install(repo)

    assert install is not None
    assert install.version == "1.2.0"


def test_resolve_install_never_answers_forge_with_a_user_scope_install(
    tmp_path: Path, claude_home: Path
) -> None:
    """A user-wide forge is not this checkout's install."""
    repo = tmp_path / "repo"
    repo.mkdir()
    write_install(claude_home, None, "1.2.3", key="forge@forge", scope="user")

    assert version_surfaces.resolve_install(repo) is None


def test_resolve_install_falls_back_to_user_scope_for_another_plugin(
    tmp_path: Path, claude_home: Path
) -> None:
    """Another plugin with no checkout record is judged on its user-wide install."""
    repo = tmp_path / "repo"
    repo.mkdir()
    install_dir = write_install(
        claude_home, None, "0.4.0", key="other@market", scope="user"
    )

    install = version_surfaces.resolve_install(repo, "other")

    assert install == version_surfaces.CheckoutInstall(
        "other@market", "user", "0.4.0", install_dir
    )


# ---------------------------------------------------------------------------
# enabled_forge_keys / global_forge_keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            {
                "enabledPlugins": {
                    "forge@forge-v1-2-3": True,
                    "forge@forge": False,
                    "other@market": True,
                    "forge@x": "yes",
                }
            },
            ["forge@forge-v1-2-3"],
        ),
        ({"enabledPlugins": None}, []),
        ([], []),
    ],
    ids=["mixed", "null-block", "not-an-object"],
)
def test_enabled_forge_keys_reads_only_forge_keys_set_true(
    tmp_path: Path, content: object, expected: list[str]
) -> None:
    """Only a literal ``true`` on a ``forge@…`` key counts.

    Args:
        tmp_path: Pytest temp directory.
        content: The settings file's JSON value.
        expected: The keys reported.
    """
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(content), encoding="utf-8")

    assert version_surfaces.enabled_forge_keys(settings) == expected


def test_enabled_forge_keys_lets_a_later_file_turn_a_key_off(tmp_path: Path) -> None:
    """A checkout's local settings override its tracked ones, key by key."""
    project = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    project.write_text(
        json.dumps({"enabledPlugins": {"forge@forge": True, "forge@x": True}}),
        encoding="utf-8",
    )
    local.write_text(
        json.dumps({"enabledPlugins": {"forge@forge": False}}), encoding="utf-8"
    )

    assert version_surfaces.enabled_forge_keys(project, local) == ["forge@x"]


def test_global_forge_keys_merges_user_records_and_user_settings(
    tmp_path: Path, claude_home: Path
) -> None:
    """User-scope records of any marketplace and user-level enables both count.

    SCENARIO: a user-scope per-release record whose cache directory is
    gone, a checkout's own local record, and user settings enabling the
    shared key plus the same per-release key again.
    EXPECTED BEHAVIOR: both user-wide keys once each; the checkout's own
    record is not global.
    """
    gone = write_install(claude_home, None, "1.2.3", scope="user")
    gone.rmdir()
    write_install(claude_home, tmp_path / "repo", "1.2.4")
    claude_home.mkdir(parents=True, exist_ok=True)
    (claude_home / "settings.json").write_text(
        json.dumps(
            {"enabledPlugins": {"forge@forge": True, "forge@forge-v1-2-3": True}}
        ),
        encoding="utf-8",
    )

    assert version_surfaces.global_forge_keys() == [
        "forge@forge",
        "forge@forge-v1-2-3",
    ]


def test_global_forge_keys_empty_without_user_scope_forge() -> None:
    """No records and no user settings: nothing is global."""
    assert version_surfaces.global_forge_keys() == []
