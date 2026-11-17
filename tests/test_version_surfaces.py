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
@pytest.mark.parametrize(
    ("ref", "expected"),
    [("v2.3.4", "2.3.4"), ("main", None)],
    ids=["tag-pin", "branch-pin"],
)
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
class InstallHome(NamedTuple):
    """The temp directory and the machine's Claude Code records for one check."""

    tmp_path: Path
    claude_home: Path


@pytest.fixture
def install_home(tmp_path: Path, claude_home: Path) -> InstallHome:
    """Bundle the temp directory and the Claude Code records for one install check."""
    return InstallHome(tmp_path, claude_home)
