"""Tests for ``forge.plugin_roster``.

# MOCKING STRATEGY: every test fakes the packaged roster resource. Null-object
# resources (``_TextResource``, ``_MissingResource``, ``_NoEncodingResource``)
# stand in for ``importlib.resources.files("forge")``. They are installed by
# rebinding only ``plugin_roster.resources`` (a ``SimpleNamespace``), never the
# stdlib module object, so the patch cannot leak into other readers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forge import plugin_roster


class _TextResource:
    """Null-object resource whose ``read_text`` returns a fixed string."""

    def __init__(self, text: str) -> None:
        """Store the text the fake resource file returns.

        Args:
            text: Content returned by `read_text`.
        """
        self._text = text

    def joinpath(self, _name: str) -> _TextResource:
        """Return self so the chained `.joinpath(...).read_text()` call resolves.

        Args:
            _name: Resource filename to locate (unused).

        Returns:
            Self, enabling chained calls.
        """
        return self

    def read_text(self, encoding: str | None = None) -> str:
        """Return the fixed text supplied at construction.

        Args:
            encoding: Accepted to match the real resource's signature, which
                the reader passes; the stored text needs no decoding.

        Returns:
            The fixed text.
        """
        _ = encoding
        return self._text


class _MissingResource(_TextResource):
    """Null-object resource whose read always raises FileNotFoundError."""

    def __init__(self) -> None:
        """Build with empty text; it is never returned."""
        super().__init__("")

    def read_text(self, encoding: str | None = None) -> str:
        """Simulate an older installed package lacking the data file.

        Args:
            encoding: Accepted to match the real resource's signature.

        Raises:
            FileNotFoundError: Always, standing in for the absent file.
        """
        _ = encoding
        raise FileNotFoundError


class _NoEncodingResource:
    """Null-object resource whose ``read_text`` rejects an ``encoding`` argument."""

    def joinpath(self, _name: str) -> _NoEncodingResource:
        """Return self so the chained `.joinpath(...).read_text()` call resolves.

        Args:
            _name: Resource filename to locate (unused).

        Returns:
            Self, enabling chained calls.
        """
        return self

    def read_text(self) -> str:
        """Take no arguments, so the reader's ``encoding=`` call is a TypeError.

        Returns:
            Empty text; unreachable when called with ``encoding``.
        """
        return ""


def _use_resource(
    monkeypatch: pytest.MonkeyPatch, fake: _TextResource | _NoEncodingResource
) -> None:
    """Make ``plugin_roster`` read its roster from *fake*.

    Args:
        monkeypatch: Fixture used to rebind the leaf's ``resources`` name.
        fake: Resource returned for ``resources.files(...)``.
    """
    monkeypatch.setattr(
        plugin_roster, "resources", SimpleNamespace(files=lambda _pkg: fake)
    )


def test_roster_names_degrades_to_empty_on_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing/unreadable shipped data file degrades to empty, never raises."""
    _use_resource(monkeypatch, _MissingResource())
    assert plugin_roster.roster_names("skills") == frozenset()


def test_roster_names_degrades_to_empty_on_invalid_toml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid shipped TOML degrades to empty, never raises."""
    _use_resource(monkeypatch, _TextResource("not [ valid toml @@@"))
    assert plugin_roster.roster_names("skills") == frozenset()


def test_roster_names_drops_non_string_and_empty_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `names` array keeps only non-empty strings.

    SCENARIO: the roster is well-formed TOML, but a `names` array carries
    stray ints and an empty string (a hand-edit footgun).
    EXPECTED BEHAVIOR: only the non-empty string names survive; nothing raises.
    """
    text = '[skills]\nnames = [1, "commit", "", "x"]\n'
    _use_resource(monkeypatch, _TextResource(text))
    assert plugin_roster.roster_names("skills") == {"commit", "x"}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('agents = "x"\n', id="scalar-under-kind"),
        pytest.param('[skills]\nnames = ["a"]\n', id="kind-absent"),
        pytest.param("[agents]\n", id="no-names-key"),
        pytest.param('[agents]\nnames = "commit"\n', id="names-not-a-list"),
    ],
)
def test_roster_names_returns_empty_on_misshapen_table(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """A roster shaped unexpectedly yields an empty set.

    Args:
        monkeypatch: Patches the packaged roster resource.
        text: The roster TOML body under test.

    SCENARIO: the `agents` kind is a scalar, absent, lacks `names`, or has a
    string `names`. The string case matters most: without the list guard the
    string would be iterated into single characters, which would then be
    rewritten in prose as plugin names.
    EXPECTED BEHAVIOR: `frozenset()` in every case.
    """
    _use_resource(monkeypatch, _TextResource(text))
    assert plugin_roster.roster_names("agents") == frozenset()


def test_roster_names_does_not_swallow_a_wrong_read_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong `read_text` call signature surfaces as TypeError.

    SCENARIO: the resource's `read_text` takes no `encoding`, so the reader's
    call is a programming error.
    EXPECTED BEHAVIOR: `TypeError` propagates; it is never turned into a
    silently empty roster (guards against re-adding `except TypeError`).
    """
    _use_resource(monkeypatch, _NoEncodingResource())
    with pytest.raises(TypeError):
        plugin_roster.roster_names("agents")


def test_shipped_names_exclude_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin names are agents plus skills; hooks never join them.

    SCENARIO: a synthetic roster with distinct names under each kind, so a
    regression that adds hooks is visible as extra members.
    EXPECTED BEHAVIOR: `shipped_plugin_names()` is agents union skills;
    `shipped_agent_names()` is the agents alone.
    """
    text = (
        '[agents]\nnames = ["a1", "a2"]\n\n'
        '[skills]\nnames = ["s1"]\n\n'
        '[hooks]\nnames = ["h1"]\n'
    )
    _use_resource(monkeypatch, _TextResource(text))
    assert plugin_roster.shipped_plugin_names() == {"a1", "a2", "s1"}
    assert plugin_roster.shipped_agent_names() == {"a1", "a2"}
