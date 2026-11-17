"""Tests for ``forge.hook_registry`` — the canonical hook wiring reader.

# MOCKING STRATEGY: the failure and shape tests fake the packaged wiring
# resource. A null-object ``_TextResource`` stands in for
# ``importlib.resources.files("forge")``, installed by rebinding only
# ``hook_registry.resources`` (a ``SimpleNamespace``), never the stdlib
# module, so the patch cannot leak into other readers. The real-data tests
# at the bottom use no mock at all.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from forge import hook_registry
from forge.hook_registry import (
    HookWiringError,
    read_wiring,
    rendered_hooks,
    script_names,
)
from forge.install_claude import forge_hook_groups, hook_command
from forge.instruction_files import generated_name, shipped_path


if TYPE_CHECKING:
    from pathlib import Path


class _TextResource:
    """Null-object resource whose ``read_text`` returns a fixed string, or raises."""

    def __init__(self, text: str | None) -> None:
        """Store the text the fake resource file returns.

        Args:
            text: Content returned by `read_text`; ``None`` makes it raise.
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
        """Return the fixed text, or simulate an unreadable file.

        Args:
            encoding: Accepted to match the real resource's signature.

        Returns:
            The fixed text.

        Raises:
            FileNotFoundError: When constructed with ``None``.
        """
        _ = encoding
        if self._text is None:
            raise FileNotFoundError
        return self._text


def _use_wiring(monkeypatch: pytest.MonkeyPatch, text: str | None) -> None:
    """Make ``hook_registry`` read its wiring from *text*.

    Args:
        monkeypatch: Fixture used to rebind the leaf's ``resources`` name.
        text: Wiring TOML, or ``None`` for an unreadable resource.
    """
    fake = _TextResource(text)
    monkeypatch.setattr(
        hook_registry, "resources", SimpleNamespace(files=lambda _pkg: fake)
    )


_GOOD_ENTRY = '{ script = "a.sh", timeout = 5 }'


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(None, id="oserror-on-read"),
        pytest.param("not [ valid toml @@@", id="invalid-toml"),
        pytest.param("", id="empty-file"),
        pytest.param("PreToolUse = 1\n", id="event-is-scalar"),
        pytest.param("PreToolUse = []\n", id="event-is-empty-list"),
        pytest.param("PreToolUse = [1]\n", id="group-not-a-table"),
        pytest.param(
            f"[[PreToolUse]]\nmatcher = 3\nhooks = [{_GOOD_ENTRY}]\n",
            id="matcher-not-a-string",
        ),
        pytest.param('[[PreToolUse]]\nmatcher = "Bash"\n', id="no-hooks-key"),
        pytest.param("[[PreToolUse]]\nhooks = []\n", id="hooks-empty-list"),
        pytest.param('[[PreToolUse]]\nhooks = ["a.sh"]\n', id="entry-not-a-table"),
        pytest.param("[[PreToolUse]]\nhooks = [{ timeout = 5 }]\n", id="no-script"),
        pytest.param('[[PreToolUse]]\nhooks = [{ script = "" }]\n', id="empty-script"),
        pytest.param(
            '[[PreToolUse]]\nhooks = [{ script = "a.sh", timeout = "5" }]\n',
            id="timeout-string",
        ),
        pytest.param(
            '[[PreToolUse]]\nhooks = [{ script = "a.sh", timeout = 5.5 }]\n',
            id="timeout-float",
        ),
        pytest.param(
            '[[PreToolUse]]\nhooks = [{ script = "a.sh", timeout = true }]\n',
            id="timeout-bool",
        ),
    ],
)
def test_unusable_wiring_fails_loud_never_empty(
    monkeypatch: pytest.MonkeyPatch, text: str | None
) -> None:
    """Every malformed wiring raises; none degrades to an empty mapping.

    Args:
        monkeypatch: Patches the packaged wiring resource.
        text: The wiring TOML under test, or ``None`` for an unreadable file.

    SCENARIO: one case per raise site in the reader. The load-bearing
    property is that none returns empty: a converter handed empty wiring
    writes a settings file with no guards and reports success.
    EXPECTED BEHAVIOR: `HookWiringError` from every public entry point.
    """
    _use_wiring(monkeypatch, text)
    with pytest.raises(HookWiringError):
        read_wiring()
    with pytest.raises(HookWiringError):
        rendered_hooks(lambda s: s)
    with pytest.raises(HookWiringError):
        script_names()


_SYNTHETIC = """
[[SessionStart]]
hooks = [{ script = "a.sh" }]

[[PreToolUse]]
matcher = "Bash"
hooks = [{ script = "b.sh", timeout = 5 }, { script = "a.sh", timeout = 7 }]

[[PreCompact]]
hooks = [{ script = "b.sh" }]
"""


def test_rendered_hooks_shape_order_and_command_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendering omits absent matcher/timeout, keeps order, applies the mapper.

    SCENARIO: a matcher-less group, a matcher group with two timed scripts,
    and a script with no timeout.
    EXPECTED BEHAVIOR: no `matcher` key (not `None`) on the matcher-less
    group; no `timeout` key on the untimed hook; events and entries in
    declaration order; `command_for` applied to each script.
    """
    _use_wiring(monkeypatch, _SYNTHETIC)
    rendered = rendered_hooks(lambda s: f"run/{s}")
    assert list(rendered) == ["SessionStart", "PreToolUse", "PreCompact"]
    assert rendered["SessionStart"] == [
        {"hooks": [{"type": "command", "command": "run/a.sh"}]}
    ]
    assert rendered["PreToolUse"] == [
        {
            "matcher": "Bash",
            "hooks": [
                {"type": "command", "command": "run/b.sh", "timeout": 5},
                {"type": "command", "command": "run/a.sh", "timeout": 7},
            ],
        }
    ]
    assert "matcher" not in rendered["PreCompact"][0]


def test_script_names_lists_a_repeated_script_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script wired under several events appears once in the name set."""
    _use_wiring(monkeypatch, _SYNTHETIC)
    assert script_names() == frozenset({"a.sh", "b.sh"})


def test_real_wiring_names_only_shipped_scripts(tmp_path: Path) -> None:
    """Every script the packaged wiring names exists in the shipped hooks dir."""
    shipped = {p.name for p in shipped_path("claude-hooks").iterdir()}
    names = script_names()
    assert names
    assert names <= shipped


def test_real_wiring_is_non_empty_and_guards_force_push() -> None:
    """The packaged wiring parses, has events, and wires the force-push guard."""
    wiring = read_wiring()
    assert wiring
    pre_tool_use = {
        entry.script for group in wiring["PreToolUse"] for entry in group.entries
    }
    assert "block_force_push.sh" in pre_tool_use


def _shipped_stems() -> set[str]:
    """Return the canonical stems of the shipped agents, read from disk."""
    return {
        p.stem
        for p in shipped_path("agents").glob("*.md")
        if not p.name.startswith("_")
    }


def test_every_guard_that_reads_hook_agent_is_wired_with_it() -> None:
    """Every guard reading `FORGE_HOOK_AGENT` is registered with it set.

    Behavior test: a guard reading the variable that no wiring provides
    stands aside (or blocks) for the agent it exists to restrict, silently.
    Names come from disk enumeration and `generated_name`, never a literal,
    so a rename cannot leave this test agreeing with a stale matcher.

    SCENARIO: the real packaged wiring rendered through the Claude installer.
    EXPECTED BEHAVIOR: every command of a reading guard is prefixed
    `FORGE_HOOK_AGENT=`; each agent-scoped block names a generated shipped
    agent and, on an agent-selecting event, a matcher that fully matches
    it; the settings output carries no `agent` key.
    """
    readers = {
        p.name
        for p in shipped_path("claude-hooks").glob("*.sh")
        if "FORGE_HOOK_AGENT" in p.read_text(encoding="utf-8")
    }
    assert readers
    seen: set[str] = set()
    settings = forge_hook_groups()
    for blocks in settings.values():
        for block in blocks:
            assert "agent" not in block
            for hook in block["hooks"]:
                script = hook["command"].rsplit("/", 1)[-1]
                if script in readers:
                    seen.add(script)
                    assert hook["command"].startswith("FORGE_HOOK_AGENT="), script
    assert seen == readers

    generated = {generated_name(stem) for stem in _shipped_stems()}
    scoped = [
        (event, block)
        for event, blocks in rendered_hooks(hook_command).items()
        for block in blocks
        if "agent" in block
    ]
    assert scoped
    for event, block in scoped:
        assert block["agent"] in generated
        if event == "SubagentStop":
            assert re.fullmatch(block["matcher"], block["agent"])


_SCOPED = """
[[SubagentStop]]
agent = "{stem}"
hooks = [{{ script = "a.sh" }}]
"""


@pytest.mark.parametrize(
    ("stem", "populate"),
    [
        pytest.param("no-such-agent", True, id="unknown-stem"),
        pytest.param("anything", False, id="empty-enumeration"),
        pytest.param("_TEMPLATE", True, id="underscore-file-not-a-candidate"),
    ],
)
def test_agent_scope_fails_loud_never_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stem: str,
    *,
    populate: bool,
) -> None:
    """An agent scope that cannot be resolved raises instead of rendering open.

    Args:
        monkeypatch: Patches the wiring resource and the agent directory.
        tmp_path: Stands in for the shipped agents directory.
        stem: The stem the synthetic wiring scopes a group to.
        populate: Whether the fake agents directory holds agent files.

    SCENARIO: synthetic wiring scoped to a stem that is unknown, to a
    directory with no agents, and to an `_`-prefixed file.
    EXPECTED BEHAVIOR: `HookWiringError`; an unscoped group would run for
    every agent and an empty matcher for none.
    """
    agents = tmp_path / "agents"
    agents.mkdir()
    if populate:
        (agents / "real-agent.md").write_text("x", encoding="utf-8")
        (agents / "_TEMPLATE.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(hook_registry, "shipped_path", lambda _name: agents)
    _use_wiring(monkeypatch, _SCOPED.format(stem=stem))
    with pytest.raises(HookWiringError):
        rendered_hooks(lambda s: s)
