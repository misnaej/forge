"""Read the canonical hook wiring every harness converter generates from.

``data/hooks.toml`` is the single source of forge's Claude Code hook
wiring: which guard script runs on which event, behind which tool
matcher, with which timeout. It names scripts, never paths — each
converter builds the path its harness needs, so no harness's directory
layout leaks into the others.

**Fail loud, never empty.** :func:`forge.plugin_roster.roster_names`
degrades to an empty set when its resource cannot be read, which is
right for a name list used in drift checks: a reader that cannot see the
roster reports nothing rather than claiming a mismatch. Wiring is the
opposite. A converter handed an empty wiring writes a settings file with
no guards in it and reports success, so every protection in FOUNDATION
§2 silently stops existing. An unreadable wiring therefore raises.
"""

from __future__ import annotations

import tomllib
from importlib import resources
from typing import TYPE_CHECKING, Final, NamedTuple

from forge.instruction_files import shipped_path


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


HOOKS_RESOURCE: Final = "data/hooks.toml"

# The directory forge's packaged guard scripts live in, under the package
# data root. A converter joins this with an entry's script name.
HOOKS_DIR: Final = "claude-hooks"


class HookEntry(NamedTuple):
    """One guard script to run, as a harness-neutral name plus timeout.

    Attributes:
        script: File name inside :data:`HOOKS_DIR`, e.g.
            ``block_force_push.sh``. Never a path — the converter owns
            where the script lives for its harness.
        timeout: Seconds the harness should allow the script, or ``None``
            to leave the harness default in place.
    """

    script: str
    timeout: int | None


class HookGroup(NamedTuple):
    """Entries sharing one event and matcher.

    Attributes:
        matcher: Tool-name pattern the harness matches before running the
            group, or ``None`` for an event that carries no matcher (a
            session or compaction event fires regardless of tool).
        entries: The scripts to run, in declaration order. Order is part
            of the contract: a guard that blocks is cheapest first.
    """

    matcher: str | None
    entries: tuple[HookEntry, ...]


class HookWiringError(RuntimeError):
    """The canonical wiring is missing, unparseable, or malformed.

    Raised instead of returning an empty wiring, because a converter
    cannot tell "no hooks are configured" from "the hooks could not be
    read", and the second one silently ships a repo with no guards.
    """


def _entry(raw: object, where: str) -> HookEntry:
    """Build one :class:`HookEntry` from a parsed table.

    Args:
        raw: The parsed inline table for a single hook.
        where: Event name, for the error message.

    Returns:
        The validated entry.

    Raises:
        HookWiringError: If *raw* is not a table, names no script, or
            carries a non-integer timeout.
    """
    if not isinstance(raw, dict):
        msg = f"{HOOKS_RESOURCE}: {where} hook entry is not a table"
        raise HookWiringError(msg)
    script = raw.get("script")
    if not isinstance(script, str) or not script:
        msg = f"{HOOKS_RESOURCE}: {where} hook entry has no script name"
        raise HookWiringError(msg)
    timeout = raw.get("timeout")
    # `type(...) is not int` rather than isinstance: bool subclasses int in
    # Python, so `timeout = true` would otherwise pass validation and be
    # rendered into a harness config as the integer 1.
    if timeout is not None and type(timeout) is not int:
        msg = f"{HOOKS_RESOURCE}: {where} script {script} has a non-integer timeout"
        raise HookWiringError(msg)
    return HookEntry(script=script, timeout=timeout)


def read_wiring() -> dict[str, tuple[HookGroup, ...]]:
    """Return the canonical wiring, keyed by harness-neutral event name.

    Returns:
        Event name to its groups, in declaration order.

    Raises:
        HookWiringError: If the resource cannot be read or parsed, or any
            event, group or entry has the wrong shape. Never returns an
            empty mapping in place of an error.
    """
    try:
        text = (
            resources.files("forge")
            .joinpath(HOOKS_RESOURCE)
            .read_text(encoding="utf-8")
        )
    except (OSError, ModuleNotFoundError) as exc:
        msg = (
            f"{HOOKS_RESOURCE} is unreadable, so forge's guard wiring cannot be "
            "generated. Reinstall forge-scripts."
        )
        raise HookWiringError(msg) from exc
    try:
        table = tomllib.loads(text)
    except ValueError as exc:
        msg = f"{HOOKS_RESOURCE} is not valid TOML: {exc}"
        raise HookWiringError(msg) from exc
    if not table:
        msg = f"{HOOKS_RESOURCE} declares no events"
        raise HookWiringError(msg)
    wiring: dict[str, tuple[HookGroup, ...]] = {}
    for event, raw_groups in table.items():
        if not isinstance(raw_groups, list) or not raw_groups:
            msg = f"{HOOKS_RESOURCE}: event {event} declares no groups"
            raise HookWiringError(msg)
        groups: list[HookGroup] = []
        for raw in raw_groups:
            if not isinstance(raw, dict):
                msg = f"{HOOKS_RESOURCE}: {event} group is not a table"
                raise HookWiringError(msg)
            matcher = raw.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                msg = f"{HOOKS_RESOURCE}: {event} matcher is not a string"
                raise HookWiringError(msg)
            raw_hooks = raw.get("hooks")
            if not isinstance(raw_hooks, list) or not raw_hooks:
                msg = f"{HOOKS_RESOURCE}: {event} group lists no hooks"
                raise HookWiringError(msg)
            groups.append(
                HookGroup(
                    matcher=matcher,
                    entries=tuple(_entry(h, event) for h in raw_hooks),
                )
            )
        wiring[event] = tuple(groups)
    return wiring


def rendered_hooks(
    command_for: Callable[[str], str],
) -> dict[str, list[dict[str, object]]]:
    """Render the wiring into the event/matcher/command shape harnesses use.

    Both converters need the same structure and differ only in how a
    script name becomes a command — the Claude installer roots it at the
    project directory, the Vibe generator resolves it to an absolute
    path — so the structure is built once here and the difference is the
    caller's one-line function.

    Args:
        command_for: Maps a script name to the command string for this
            harness.

    Returns:
        Event name to its groups, each with ``matcher`` when the event
        has one and a ``hooks`` list of ``type``/``command``/``timeout``
        entries, matching declaration order.

    Raises:
        HookWiringError: Propagated from :func:`read_wiring`.
    """
    rendered: dict[str, list[dict[str, object]]] = {}
    for event, groups in read_wiring().items():
        blocks: list[dict[str, object]] = []
        for group in groups:
            hooks: list[dict[str, object]] = []
            for entry in group.entries:
                hook: dict[str, object] = {
                    "type": "command",
                    "command": command_for(entry.script),
                }
                if entry.timeout is not None:
                    hook["timeout"] = entry.timeout
                hooks.append(hook)
            blocks.append(
                {"matcher": group.matcher, "hooks": hooks}
                if group.matcher is not None
                else {"hooks": hooks}
            )
        rendered[event] = blocks
    return rendered


def script_names() -> frozenset[str]:
    """Return every script the canonical wiring references.

    A converter copies exactly this set into the repo, so a script that
    ships but is wired nowhere is never installed, and a wired script
    that does not ship fails the copy loudly.

    Returns:
        The distinct script file names.

    Raises:
        HookWiringError: Propagated from :func:`read_wiring`.
    """
    return frozenset(
        entry.script
        for groups in read_wiring().values()
        for group in groups
        for entry in group.entries
    )


def required_scripts(source: Path, wired: frozenset[str]) -> frozenset[str]:
    """Return every script needed for the wired ones to run.

    A wired guard may ``source`` a shipped helper — the git-command
    anchor, the once-marker, the wrap-up anchor — which the wiring never
    names because nothing fires it directly. Copying only the wired set
    leaves those helpers behind, and a guard that cannot source its
    helper refuses the call it was protecting: the guards fail closed, so
    the omission disarms nothing but blocks everything.

    Dependencies are read from the scripts themselves rather than listed
    here, so a helper added later needs no change in this module.

    Args:
        source: Directory holding the shipped guard scripts. Passed in
            rather than resolved here so the traversal can be exercised
            on a synthetic chain, and so the one place that decides where
            shipped sources come from stays
            :func:`forge.instruction_files.shipped_path`.
        wired: Script names the canonical wiring references.

    Returns:
        The wired scripts plus every shipped script they reference,
        transitively.
    """
    shipped = {p.name for p in source.glob("*.sh")}
    needed = set(wired)
    pending = list(wired)
    while pending:
        path = source / pending.pop()
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for candidate in shipped - needed:
            if candidate in text:
                needed.add(candidate)
                pending.append(candidate)
    return frozenset(needed)


def validate_shipped_scripts(source: Path | None = None) -> frozenset[str]:
    """Return the full guard script set, failing closed if any is missing.

    The Vibe installer never copies guard scripts — it writes absolute
    paths into the roster sidecar — so unlike the Claude installer there
    is no copy step that would notice a missing file. This check is what
    stands between a wiring entry and a silently disarmed guard: a hook
    whose command does not exist exits 127, and 127 does not block the
    tool call.

    Args:
        source: Directory holding the shipped guard scripts; defaults to
            the packaged :data:`HOOKS_DIR`. Passed in so tests can
            exercise a synthetic tree.

    Returns:
        The wired scripts plus every shipped script they source,
        transitively — all verified present on disk.

    Raises:
        HookWiringError: When any required script does not ship.
    """
    if source is None:
        source = shipped_path(HOOKS_DIR)
    needed = required_scripts(source, script_names())
    for name in sorted(needed):
        if not (source / name).is_file():
            msg = (
                f"wiring references {name}, which forge does not ship at "
                f"{source}. Reinstall forge-scripts."
            )
            raise HookWiringError(msg)
    return needed
