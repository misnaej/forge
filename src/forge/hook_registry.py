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

import re
import tomllib
from importlib import resources
from typing import TYPE_CHECKING, Final, NamedTuple

from forge.instruction_files import generated_name, shipped_path


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


HOOKS_RESOURCE: Final = "data/hooks.toml"

# Vibe-only hook extras, merged into the generated Vibe wiring alongside
# the canonical wiring — their scripts are guard surface too.
VIBE_EXTRA_RESOURCE: Final = "data/vibe-extra-hooks.toml"

# A script referenced relative to the calling script's own directory —
# ``source "$(dirname "$0")/x.sh"`` or ``ANCHOR_LIB="$(dirname "$0")/x.sh"``.
# Deliberately not any ``*.sh`` token: comments name scripts that are not
# sourced, and requiring those would fail validation on a missing mention.
# The name may carry directory segments (``lib/command_utils.sh``): the
# shared readers live in a subdirectory. A basename-only pattern matches
# none of those references, so the helper would never enter the required
# set, the installer would not copy it, and every guard sourcing it would
# fail closed with no missing file to blame.
_SH_REF: Final = re.compile(r'\$\(dirname "\$0"\)/((?:[\w.-]+/)*[\w.-]+\.sh)')

#: A generated agent name safe to interpolate into a harness matcher
#: without escaping. Deliberately narrow: anything outside this set is
#: refused rather than escaped, because an escape a harness will not
#: compile yields a matcher that matches nothing, in silence.
_PLAIN_NAME: Final = re.compile(r"[a-z0-9-]+")

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
        agent: Canonical stem of the one agent this group is scoped to,
            or ``None`` for a group that runs whatever the agent. The
            stem is never the name the agent is addressed by: it is
            resolved at render time, so a rename cannot strand it.
        entries: The scripts to run, in declaration order. Order is part
            of the contract: a guard that blocks is cheapest first.
    """

    matcher: str | None
    agent: str | None
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
        wiring[event] = tuple(_group(raw, event) for raw in raw_groups)
    return wiring


def _group(raw: object, event: str) -> HookGroup:
    """Validate one declared group and return it.

    Split from :func:`read_wiring` so each function validates one level
    of the file: the caller owns events, this owns a group, and
    :func:`_entry` owns a hook.

    Args:
        raw: The group as parsed from TOML, not yet known to be a table.
        event: Event name the group was declared under, for messages.

    Returns:
        The validated group.

    Raises:
        HookWiringError: When the group is not a table, its ``matcher``
            or ``agent`` is not a string, or it lists no hooks.
    """
    if not isinstance(raw, dict):
        msg = f"{HOOKS_RESOURCE}: {event} group is not a table"
        raise HookWiringError(msg)
    matcher = raw.get("matcher")
    if matcher is not None and not isinstance(matcher, str):
        msg = f"{HOOKS_RESOURCE}: {event} matcher is not a string"
        raise HookWiringError(msg)
    # `agent` and `matcher` are orthogonal and may both appear. A
    # SubagentStop matcher selects an agent, so a group carrying only
    # `agent` has its matcher built from it. A PreToolUse matcher selects
    # a *tool*, so such a group keeps its own matcher and `agent` only
    # tells the script which agent it restricts — which is why those
    # scripts check the agent themselves.
    agent = raw.get("agent")
    if agent is not None and not isinstance(agent, str):
        msg = f"{HOOKS_RESOURCE}: {event} agent is not a string"
        raise HookWiringError(msg)
    raw_hooks = raw.get("hooks")
    if not isinstance(raw_hooks, list) or not raw_hooks:
        msg = f"{HOOKS_RESOURCE}: {event} group lists no hooks"
        raise HookWiringError(msg)
    return HookGroup(
        matcher=matcher,
        agent=agent,
        entries=tuple(_entry(h, event) for h in raw_hooks),
    )


def _resolved_agent(stem: str | None, event: str) -> str | None:
    """Resolve a group's canonical agent stem to its generated name.

    Raises rather than falling back, because every fallback here is a
    silent fail-open: an unknown stem that rendered no matcher would
    scope the group to *every* agent, and one that rendered an empty
    matcher would scope it to none. Both leave a guard that reports
    success while guarding nothing — the failure this indirection
    exists to prevent.

    Args:
        stem: Canonical stem from the wiring, or ``None`` when the group
            is not agent-scoped.
        event: Event name, for the error message.

    Returns:
        The name the agent is generated under, or ``None`` when *stem*
        is ``None``.

    Raises:
        HookWiringError: When *stem* names no shipped agent, or no
            shipped agents can be enumerated at all.
    """
    if stem is None:
        return None
    source = shipped_path("agents")
    shipped = {p.stem for p in source.glob("*.md") if not p.name.startswith("_")}
    if not shipped:
        msg = (
            f"{HOOKS_RESOURCE}: {event} is scoped to agent {stem!r}, but no "
            f"shipped agents could be read from {source}. Reinstall "
            "forge-scripts."
        )
        raise HookWiringError(msg)
    if stem not in shipped:
        msg = (
            f"{HOOKS_RESOURCE}: {event} is scoped to agent {stem!r}, which "
            f"forge does not ship. Shipped: {', '.join(sorted(shipped))}."
        )
        raise HookWiringError(msg)
    name = generated_name(stem)
    # The name goes into a harness matcher as a regex. Validate it holds
    # nothing a regex could read specially and interpolate it raw, rather
    # than escaping: `re.escape` would emit `\-`, which is valid in
    # Python and in a plain JavaScript regex but a syntax error under its
    # unicode flag — and a matcher the harness refuses to compile matches
    # *nothing*, which is the silent failure this whole change exists to
    # remove. Fail here instead, where it is visible.
    if not _PLAIN_NAME.fullmatch(name):
        msg = (
            f"{HOOKS_RESOURCE}: {event} is scoped to agent {stem!r}, whose "
            f"generated name {name!r} is not plain enough to put in a "
            "harness matcher (expected lowercase letters, digits and "
            "hyphens)."
        )
        raise HookWiringError(msg)
    return name


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
        entries, matching declaration order. A group scoped to
        an agent also carries ``agent``, the resolved generated name, and
        a matcher derived from it when the wiring declares none; each
        converter delivers that name to its guards.

    Raises:
        HookWiringError: Propagated from :func:`read_wiring`.
    """
    rendered: dict[str, list[dict[str, object]]] = {}
    for event, groups in read_wiring().items():
        blocks: list[dict[str, object]] = []
        for group in groups:
            agent_name = _resolved_agent(group.agent, event)
            matcher = group.matcher
            if agent_name is not None and matcher is None:
                # Only an agent-selecting event (SubagentStop) derives its
                # matcher from the scope. A tool-selecting matcher stays
                # as declared; there the scope only reaches the script.
                matcher = f"^{agent_name}$"
            hooks: list[dict[str, object]] = []
            for entry in group.entries:
                hook: dict[str, object] = {
                    "type": "command",
                    "command": command_for(entry.script),
                }
                if entry.timeout is not None:
                    hook["timeout"] = entry.timeout
                hooks.append(hook)
            block: dict[str, object] = (
                {"matcher": matcher, "hooks": hooks}
                if matcher is not None
                else {"hooks": hooks}
            )
            if agent_name is not None:
                # Carried as data, never baked into the command: the
                # Claude settings file runs a command through a shell, so
                # it can prefix an assignment, while the Vibe dispatcher
                # execs the command as a bare path and would read any
                # prefix as part of the filename. Each converter delivers
                # it the way its harness can receive it.
                block["agent"] = agent_name
            blocks.append(block)
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
    anchor, the once-marker, the wrap-up anchor, the shared
    command-segment reader under ``lib/`` — which the wiring never names
    because nothing fires it directly. References are discovered
    from the scripts' **text**, not by intersecting with what is on disk:
    a helper that failed to arrive is still referenced, so it still
    enters the required set and fails validation — discovering by glob
    would make exactly the missing-file case invisible.

    Args:
        source: Directory holding the shipped guard scripts. Passed in
            rather than resolved here so the traversal can be exercised
            on a synthetic chain, and so the one place that decides where
            shipped sources come from stays
            :func:`forge.instruction_files.shipped_path`.
        wired: Script names the canonical wiring references.

    Returns:
        The wired scripts plus every script their text references,
        transitively.
    """
    needed = set(wired)
    pending = list(wired)
    while pending:
        path = source / pending.pop()
        if not path.is_file():
            continue
        for ref in _SH_REF.findall(path.read_text(encoding="utf-8")):
            if ref not in needed:
                needed.add(ref)
                pending.append(ref)
    return frozenset(needed)


def _vibe_extra_script_names() -> frozenset[str]:
    """Return the script names the Vibe-only hook extras run.

    Returns:
        Basenames of every ``command`` in :data:`VIBE_EXTRA_RESOURCE`.

    Raises:
        HookWiringError: If the extras resource cannot be read or parsed.
    """
    try:
        text = (
            resources.files("forge")
            .joinpath(VIBE_EXTRA_RESOURCE)
            .read_text(encoding="utf-8")
        )
        table = tomllib.loads(text)
    except (OSError, ModuleNotFoundError, ValueError) as exc:
        msg = f"{VIBE_EXTRA_RESOURCE} is unreadable: {exc}"
        raise HookWiringError(msg) from exc
    hooks = table.get("hooks", [])
    return frozenset(
        str(hook["command"]).rsplit("/", 1)[-1]
        for hook in hooks
        if isinstance(hook, dict) and hook.get("command")
    )


def validate_shipped_scripts(source: Path | None = None) -> frozenset[str]:
    """Return the full guard script set, failing closed if any is missing.

    The Vibe installer never copies guard scripts — it writes absolute
    paths into the roster sidecar — so unlike the Claude installer there
    is no copy step that would notice a missing file. This check is what
    stands between a wiring entry and a silently disarmed guard: a hook
    whose command does not exist exits 127, and 127 does not block the
    tool call. The required set is the canonical wiring's scripts, the
    scripts their text references (sourced helpers — a *missing* helper
    is still required), and the Vibe-only extras' scripts.

    Args:
        source: Directory holding the shipped guard scripts; defaults to
            the packaged :data:`HOOKS_DIR`. Passed in so tests can
            exercise a synthetic tree.

    Returns:
        The wired scripts plus every referenced or extra script, all
        verified present on disk.

    Raises:
        HookWiringError: When any required script does not ship.
    """
    if source is None:
        source = shipped_path(HOOKS_DIR)
    needed = required_scripts(source, script_names() | _vibe_extra_script_names())
    for name in sorted(needed):
        if not (source / name).is_file():
            msg = (
                f"wiring references {name}, which forge does not ship at "
                f"{source}. Reinstall forge-scripts."
            )
            raise HookWiringError(msg)
    return needed
