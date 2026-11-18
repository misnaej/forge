"""install-forge-claude — generate forge's Claude Code integration in-repo.

Forge's agents, skills and guard hooks reach a Claude Code session the
same way the Mistral Vibe integration already reaches Vibe: generated
into the checkout from forge's packaged files, with no plugin to publish,
version, install or keep from going stale.

What it writes:

1. ``.claude/agents/forge-<name>.md`` — every shipped agent, with each
   reference to another shipped agent or skill rewritten into this
   harness's namespace. ``_TEMPLATE.md`` documents the agent contract
   rather than defining an agent, and is never published.
2. ``.claude/skills/forge-<name>/SKILL.md`` — every shipped skill,
   rewritten the same way.
3. ``.claude/hooks/<script>`` — every guard script the wiring
   references, plus the helpers those scripts source, executable.
4. ``.claude/settings.json`` — forge's hook groups, registered under
   ``${CLAUDE_PROJECT_DIR}`` per FOUNDATION §11, merged into whatever
   else the file holds, and with the keys that named the retired plugin
   removed.
5. ``.claude/.gitignore`` — the list of what forge generated, which is
   both why those files stay untracked and the record of what forge
   owns.

Items 1 to 3 are gitignored. ``settings.json`` stays **tracked**: it
carries the repo's own permissions beside forge's registrations, so a
change to the guards is reviewed like any other diff.

**Agents and skills carry a ``forge-`` prefix**, the same on every
harness, so forge's are distinguishable from the repo's at a glance and
cannot collide with them. Shipped content names them with a ``forge:``
marker, which :func:`forge.agent_harness.map_plugin_references` rewrites
to that prefix at generation.

**The repo's own files are never touched.** A repo may keep its own
agents, skills and hooks in these same directories (FOUNDATION §16's
wrapper patterns). Ownership is therefore recorded rather than guessed:
a file is forge's only if ``.claude/.gitignore`` says a previous run
wrote it (:func:`previously_generated`), and a settings hook group is
forge's only when every command in it names a script forge wires — by
**name**, not by directory, because the repo's scripts live in that same
directory.

Usage:
    install-forge-claude            # Full installation
    install-forge-claude --check    # Dry-run, report what would change
    install-forge-claude --verify   # Verify an existing installation
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import stat
import sys
from pathlib import Path
from typing import Final

from forge.agent_harness import HarnessName, map_plugin_references
from forge.claude_settings_schema import MARKETPLACE_KEY, PLUGIN_KEY
from forge.git_utils import configure_cli_logging
from forge.hook_registry import (
    HookWiringError,
    rendered_hooks,
    validate_shipped_scripts,
)
from forge.instruction_files import (
    generated_name,
    renamed_frontmatter,
    shipped_path,
)


configure_cli_logging()
logger = logging.getLogger(__name__)

# Where the generated integration lives, relative to the repo root.
CLAUDE_DIR: Final = ".claude"
AGENTS_SUBDIR: Final = "agents"
SKILLS_SUBDIR: Final = "skills"
HOOKS_SUBDIR: Final = "hooks"
SETTINGS_NAME: Final = "settings.json"

# The template beside the shipped agents, which documents the agent
# contract rather than defining a spawnable agent.
AGENT_TEMPLATE: Final = "_TEMPLATE.md"

# The command prefix that marks a hook registration as forge's own. Paths
# are rooted at ${CLAUDE_PROJECT_DIR} because a hook fires with an
# unspecified working directory (FOUNDATION §11).
HOOK_ROOT: Final = "${CLAUDE_PROJECT_DIR}/" + f"{CLAUDE_DIR}/{HOOKS_SUBDIR}/"


def hook_command(script: str) -> str:
    """Return the settings command string that runs *script*.

    Args:
        script: Script file name from the canonical wiring.

    Returns:
        A ``${CLAUDE_PROJECT_DIR}``-rooted command.
    """
    return HOOK_ROOT + script


def is_forge_group(group: object, scripts: frozenset[str]) -> bool:
    """Report whether a settings hook group is one forge owns.

    Ownership is read off the **script names**, not the directory the
    commands point at. Forge and the consumer share
    ``.claude/hooks/``, so a location test would claim a consumer's own
    registration and drop it on the next install. A name test cannot:
    the consumer's scripts are not in forge's wiring.

    The residual case is a consumer script named exactly like one forge
    wires, which collides on the file as well as the registration and is
    the documented hazard of writing into a shared directory.

    Args:
        group: A parsed entry from a settings ``hooks.<event>`` list.
        scripts: The script names forge's canonical wiring references.

    Returns:
        ``True`` when the group has at least one hook and every hook
        command names a script forge wires.
    """
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list) or not hooks:
        return False
    return all(
        isinstance(h, dict)
        and isinstance(h.get("command"), str)
        and h["command"].rsplit("/", 1)[-1] in scripts
        for h in hooks
    )


def forge_hook_groups() -> dict[str, list[dict[str, object]]]:
    """Build forge's hook registrations in Claude settings shape.

    An agent-scoped group arrives carrying the resolved agent name. That
    name is not a Claude settings key, so it is spent here: each command
    in the group gains a leading ``FORGE_HOOK_AGENT=`` assignment, which
    works because Claude runs a hook command through a shell. The guard
    then compares against that value instead of spelling a name of its
    own, so no rename can strand it.

    Returns:
        Event name to its groups, each carrying the matcher (when the
        event has one) and its commands in declaration order.

    Raises:
        HookWiringError: Propagated from :func:`read_wiring`.
    """
    groups = rendered_hooks(hook_command)
    for blocks in groups.values():
        for block in blocks:
            agent = block.pop("agent", None)
            if not isinstance(agent, str):
                continue
            for hook in block["hooks"]:  # type: ignore[union-attr]
                hook["command"] = f"FORGE_HOOK_AGENT={agent} {hook['command']}"
    return groups


def _drop_plugin_era_keys(settings: dict[str, object]) -> None:
    """Remove settings that named forge's retired Claude Code plugin.

    Forge used to reach a session as an installed plugin, which the
    settings file either enabled or explicitly turned off, and which a
    marketplace entry pointed at. The plugin no longer exists, so those
    keys tell Claude Code about something unavailable; the generated
    integration replaces them. Only forge's own entries are touched —
    another plugin's stay, and a key left empty is removed rather than
    kept as an empty object.

    Args:
        settings: Parsed settings, modified in place.
    """
    for key in ("enabledPlugins", "extraKnownMarketplaces"):
        block = settings.get(key)
        if not isinstance(block, dict):
            continue
        for name in (PLUGIN_KEY, MARKETPLACE_KEY):
            block.pop(name, None)
        if not block:
            settings.pop(key, None)


def merge_settings(
    existing: dict[str, object],
    groups: dict[str, list[dict[str, object]]],
    scripts: frozenset[str],
) -> dict[str, object]:
    """Return *existing* settings with forge's hook groups refreshed.

    Forge's own groups are replaced wholesale, so a wiring change lands
    cleanly, and an event forge no longer wires keeps only its non-forge
    groups. Every other event and group is preserved. Two further changes
    are deliberate: the keys that named the retired plugin are removed
    (:func:`_drop_plugin_era_keys`), and a ``hooks`` value that is not an
    object is replaced, since nothing could be merged into it.

    Args:
        existing: Parsed settings file, or an empty mapping.
        groups: Forge's registrations, from :func:`forge_hook_groups`.
        scripts: Script names forge wires, for the ownership test.

    Returns:
        A new settings mapping; *existing* is not modified.
    """
    merged = json.loads(json.dumps(existing)) if existing else {}
    _drop_plugin_era_keys(merged)
    hooks_block = merged.get("hooks")
    hooks: dict[str, object] = hooks_block if isinstance(hooks_block, dict) else {}
    for event in set(hooks) | set(groups):
        current = hooks.get(event)
        prior = current if isinstance(current, list) else []
        kept = [g for g in prior if not is_forge_group(g, scripts)]
        combined = groups.get(event, []) + kept
        if combined:
            hooks[event] = combined
        else:
            hooks.pop(event, None)
    merged["hooks"] = hooks
    return merged


def _write_if_changed(path: Path, content: str) -> bool:
    """Write *content* to *path* only when it differs.

    Args:
        path: Target file.
        content: Desired content.

    Returns:
        ``True`` when the file was created or rewritten.
    """
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def shipped_agent_files() -> list[Path]:
    """Return the agent definition files forge publishes.

    Returns:
        Shipped ``agents/*.md`` paths, sorted, excluding the template.
    """
    source = shipped_path(AGENTS_SUBDIR)
    return sorted(p for p in source.glob("*.md") if p.name != AGENT_TEMPLATE)


def shipped_skill_files() -> list[Path]:
    """Return the skill definition files forge publishes.

    Returns:
        Shipped ``skills/<name>/SKILL.md`` paths, sorted.
    """
    source = shipped_path(SKILLS_SUBDIR)
    return sorted(source.glob("*/SKILL.md"))


def _for_claude(text: str) -> str:
    """Rewrite shipped content into this harness's agent namespace.

    The canonical files name other agents and skills with a ``forge:``
    marker. It is rewritten to this harness's prefix, so a reference
    names the file the installer actually writes. A token that merely
    begins with a shipped name is left alone.

    Args:
        text: Canonical file content.

    Returns:
        The content as this harness addresses it.
    """
    return map_plugin_references(text, harness=HarnessName.CLAUDE.value)


def _publish_agents(repo_root: Path, *, check: bool) -> list[str]:
    """Copy shipped agents into ``.claude/agents/``.

    Args:
        repo_root: The checkout being installed into.
        check: Report what would change without writing.

    Returns:
        Repo-relative paths that were written, or would be.
    """
    dest_dir = repo_root / CLAUDE_DIR / AGENTS_SUBDIR
    written: list[str] = []
    for src in shipped_agent_files():
        dest = dest_dir / f"{generated_name(src.stem)}.md"
        content = _for_claude(
            renamed_frontmatter(src.read_text(encoding="utf-8"), src.stem)
        )
        if check:
            if not dest.is_file() or dest.read_text(encoding="utf-8") != content:
                written.append(str(dest.relative_to(repo_root)))
        elif _write_if_changed(dest, content):
            written.append(str(dest.relative_to(repo_root)))
    return written


def _publish_skills(repo_root: Path, *, check: bool) -> list[str]:
    """Copy shipped skills into ``.claude/skills/``.

    Args:
        repo_root: The checkout being installed into.
        check: Report what would change without writing.

    Returns:
        Repo-relative paths that were written, or would be.
    """
    dest_dir = repo_root / CLAUDE_DIR / SKILLS_SUBDIR
    written: list[str] = []
    for src in shipped_skill_files():
        dest = dest_dir / generated_name(src.parent.name) / src.name
        content = _for_claude(
            renamed_frontmatter(src.read_text(encoding="utf-8"), src.parent.name)
        )
        if check:
            if not dest.is_file() or dest.read_text(encoding="utf-8") != content:
                written.append(str(dest.relative_to(repo_root)))
        elif _write_if_changed(dest, content):
            written.append(str(dest.relative_to(repo_root)))
    return written


def _publish_hooks(
    repo_root: Path, scripts: frozenset[str], *, check: bool
) -> list[str]:
    """Copy the guard scripts into ``.claude/hooks/``, executable.

    The caller validates the set first
    (:func:`forge.hook_registry.validate_shipped_scripts`), so copying
    nothing on a missing script is that one check's job — kept single
    there, not re-implemented here.

    Args:
        repo_root: The checkout being installed into.
        scripts: Script names to copy — the wired set plus the helpers
            they reference (:func:`forge.hook_registry.required_scripts`).
        check: Report what would change without writing.

    Returns:
        Repo-relative paths that were written, or would be.
    """
    source = shipped_path("claude-hooks")
    dest_dir = repo_root / CLAUDE_DIR / HOOKS_SUBDIR
    written: list[str] = []
    for name in sorted(scripts):
        src = source / name
        dest = dest_dir / name
        content = src.read_text(encoding="utf-8")
        changed = not dest.is_file() or dest.read_text(encoding="utf-8") != content
        if check:
            if changed:
                written.append(str(dest.relative_to(repo_root)))
            continue
        if changed:
            _write_if_changed(dest, content)
            written.append(str(dest.relative_to(repo_root)))
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return written


def _render_ignore(scripts: frozenset[str]) -> str:
    """Build the ``.claude/.gitignore`` that lists what forge generates.

    Forge shares these directories with the repo's own agents, skills and
    hooks (FOUNDATION §16's wrapper patterns), so the ignore rules name
    forge's files one by one instead of ignoring the directories. A
    blanket rule would untrack the consumer's wrappers, which are source.

    ``settings.json`` is deliberately absent: it carries the repo's own
    permissions beside forge's hook registrations, so it stays tracked
    and its guard changes are reviewed like any other.

    Args:
        scripts: The script names forge installs -- the wired set plus
            the helpers they source (:func:`required_scripts`).

    Returns:
        The file's full content.
    """
    lines = [
        "# Generated by install-forge-claude — forge's own files only.",
        "# Your agents, skills and hooks in these directories stay tracked;",
        "# settings.json stays tracked too, since it holds your permissions.",
        "",
        ".gitignore",
    ]
    lines += [
        f"{AGENTS_SUBDIR}/{generated_name(p.stem)}.md" for p in shipped_agent_files()
    ]
    lines += [
        f"{SKILLS_SUBDIR}/{generated_name(p.parent.name)}/"
        for p in shipped_skill_files()
    ]
    lines += [f"{HOOKS_SUBDIR}/{name}" for name in sorted(scripts)]
    return "\n".join(lines) + "\n"


def previously_generated(repo_root: Path) -> frozenset[str]:
    """Return every path the last run recorded as forge's own.

    ``.claude/.gitignore`` names exactly what forge generated, so it is
    the ownership ledger across versions. It is the **only** sound
    source for that question:

    - The shipped set answers "does forge ship this now?", which cannot
      recognise something forge has since retired — so a dropped agent,
      skill or guard would linger forever.
    - The roster is worse than useless here. Roster names include
      ``test``, ``commit``, ``pr``, ``fix`` and ``next``, and a repo's
      own ``.claude/skills/test/`` is an entirely plausible file. Pruning
      by roster name would delete it, directory and all.

    So ownership is recorded, never inferred: forge removes what it
    previously wrote and nothing else.

    A missing or unreadable file yields nothing. The consequence is that
    a retired file lingers — never that a file forge does not own is
    removed, which is the direction that must fail safe.

    Args:
        repo_root: The checkout being installed into.

    Returns:
        Entries as the ignore file spells them, relative to
        ``.claude/`` — e.g. ``agents/forge-design-checker.md``,
        ``skills/forge-pr/``, ``hooks/block_force_push.sh``.
    """
    ignore_path = repo_root / CLAUDE_DIR / ".gitignore"
    try:
        lines = ignore_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    prefixes = (f"{AGENTS_SUBDIR}/", f"{SKILLS_SUBDIR}/", f"{HOOKS_SUBDIR}/")
    return frozenset(
        line for line in (raw.strip() for raw in lines) if line.startswith(prefixes)
    )


def _prune_stale(
    repo_root: Path,
    scripts: frozenset[str],
    owned: frozenset[str],
    *,
    check: bool,
) -> list[str]:
    """Remove published files forge no longer ships.

    Only entries a previous run recorded as forge's are considered, so
    the repo's own agent, skill or hook in that directory is never
    removed -- including one whose name matches a shipped skill, which
    pruning by shipped name would have deleted.

    *scripts* must be the same set :func:`_publish_hooks` installs. Given
    the wired set instead, this would delete the helpers that publish had
    just copied, each run undoing the other — and since the guards fail
    closed without their helpers, the result is a checkout where every
    guarded call is refused.

    Args:
        repo_root: The checkout being installed into.
        scripts: The scripts forge installs (:func:`required_scripts`).
        owned: What a previous run recorded as forge's, read before any
            write (:func:`previously_generated`).
        check: Report what would be removed without removing.

    Returns:
        Repo-relative paths that were removed, or would be.
    """
    removed: list[str] = []
    wanted_agents = {
        f"{AGENTS_SUBDIR}/{generated_name(p.stem)}.md" for p in shipped_agent_files()
    }
    wanted_skills = {
        f"{SKILLS_SUBDIR}/{generated_name(p.parent.name)}/"
        for p in shipped_skill_files()
    }
    wanted_hooks = {f"{HOOKS_SUBDIR}/{name}" for name in scripts}
    wanted = wanted_agents | wanted_skills | wanted_hooks

    agent_dir = repo_root / CLAUDE_DIR / AGENTS_SUBDIR
    for path in sorted(agent_dir.glob("*.md")) if agent_dir.is_dir() else []:
        entry = f"{AGENTS_SUBDIR}/{path.name}"
        if entry in owned and entry not in wanted:
            removed.append(str(path.relative_to(repo_root)))
            if not check:
                path.unlink()

    hook_dir = repo_root / CLAUDE_DIR / HOOKS_SUBDIR
    for path in sorted(hook_dir.glob("*.sh")) if hook_dir.is_dir() else []:
        entry = f"{HOOKS_SUBDIR}/{path.name}"
        if entry in owned and entry not in wanted:
            removed.append(str(path.relative_to(repo_root)))
            if not check:
                path.unlink()

    skill_dir = repo_root / CLAUDE_DIR / SKILLS_SUBDIR
    for path in sorted(skill_dir.glob("*/SKILL.md")) if skill_dir.is_dir() else []:
        entry = f"{SKILLS_SUBDIR}/{path.parent.name}/"
        if entry in owned and entry not in wanted:
            removed.append(str(path.relative_to(repo_root)))
            if not check:
                shutil.rmtree(path.parent)
    return removed


def _read_settings(settings_path: Path) -> tuple[dict[str, object], str | None]:
    """Parse the settings file, or say why it cannot be merged into.

    Args:
        settings_path: Path to ``.claude/settings.json``; may be absent.

    Returns:
        ``(existing, error)``: the parsed object (empty when absent) and
        an error message when the file is not a JSON object.
    """
    if not settings_path.is_file():
        return {}, None
    try:
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {}, f"{settings_path} is not valid JSON ({exc}); not overwritten"
    if not isinstance(parsed, dict):
        return {}, f"{settings_path} is not a JSON object; not overwritten"
    return parsed, None


def _publish_text(repo_root: Path, path: Path, text: str, *, check: bool) -> list[str]:
    """Write *text* to *path* when it differs; under *check*, only report.

    Args:
        repo_root: The checkout being installed into, for the relative name.
        path: Target file.
        text: Desired content.
        check: Report drift without writing.

    Returns:
        ``[relative path]`` when the file changed or would change, else an
        empty list.
    """
    if check:
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        drifted = current != text
    else:
        drifted = _write_if_changed(path, text)
    return [str(path.relative_to(repo_root))] if drifted else []


def install_claude(
    repo_root: Path, *, check: bool = False
) -> tuple[list[str], str | None]:
    """Generate the Claude Code integration for *repo_root*.

    Args:
        repo_root: The checkout being installed into.
        check: Report what would change without writing anything.

    Returns:
        ``(changes, error)``: repo-relative paths written or removed, and
        an error message when the wiring could not be read or
        ``.claude/settings.json`` could not be parsed as an object. On an
        error nothing is published.
    """
    try:
        groups = forge_hook_groups()
        # The wired set registers the guards; the required set is what
        # gets copied, since a guard that cannot source its helper
        # refuses every call it was protecting. Validated here so a
        # script that does not ship fails before anything is written.
        scripts = validate_shipped_scripts()
    except HookWiringError as exc:
        return [], str(exc)

    # Read and validate the settings file BEFORE publishing anything.
    # Refusing afterwards would leave a repo with seventy generated files
    # and no guard registration — half-converted, and reported as a
    # failure the author then has to unpick by hand.
    settings_path = repo_root / CLAUDE_DIR / SETTINGS_NAME
    existing, settings_error = _read_settings(settings_path)
    if settings_error is not None:
        return [], settings_error

    # Read the ownership ledger ONCE, before anything is written: the
    # ignore file below is rewritten from the current shipped set, so a
    # later read would no longer name what forge has retired — and
    # retirement is the only question the ledger is for.
    owned = previously_generated(repo_root)

    changes: list[str] = []
    try:
        changes += _publish_agents(repo_root, check=check)
        changes += _publish_skills(repo_root, check=check)
        changes += _publish_hooks(repo_root, scripts, check=check)
        changes += _prune_stale(repo_root, scripts, owned, check=check)
    except HookWiringError as exc:
        return changes, str(exc)

    ignore_path = repo_root / CLAUDE_DIR / ".gitignore"
    changes += _publish_text(
        repo_root,
        ignore_path,
        _render_ignore(scripts),
        check=check,
    )

    # Ownership for the settings merge spans the ledger as well as the
    # current set: a guard forge has retired is no longer in `scripts`,
    # so its group would read as a consumer's and be kept — registering a
    # command whose file this same run deletes.
    owned_scripts = scripts | {
        entry.removeprefix(f"{HOOKS_SUBDIR}/")
        for entry in owned
        if entry.startswith(f"{HOOKS_SUBDIR}/")
    }
    desired = (
        json.dumps(merge_settings(existing, groups, owned_scripts), indent=2) + "\n"
    )
    changes += _publish_text(repo_root, settings_path, desired, check=check)
    return changes, None


def main() -> int:
    """CLI entry point.

    Returns:
        ``0`` when the integration is installed or already current (and
        for ``--check``/``--verify`` finding nothing to change); ``1``
        when a dry run finds drift or the wiring cannot be read.
    """
    parser = argparse.ArgumentParser(
        prog="install-forge-claude",
        description=(
            "Generate forge's Claude Code integration into .claude/ — agents, "
            "skills, guard hooks and their settings registration. Generated "
            "files are gitignored; settings.json stays tracked."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report what would change without writing; exit 1 on drift.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Alias of --check, for symmetry with install-forge-vibe.",
    )
    args = parser.parse_args()
    dry_run = args.check or args.verify

    repo_root = Path.cwd()
    changes, error = install_claude(repo_root, check=dry_run)
    if error:
        logger.error("install-forge-claude: %s", error)
        return 1
    if not changes:
        logger.info("install-forge-claude: already current")
        return 0
    verb = "would update" if dry_run else "updated"
    logger.info("install-forge-claude: %s %d file(s)", verb, len(changes))
    for path in changes:
        logger.info("  %s", path)
    return 1 if dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
