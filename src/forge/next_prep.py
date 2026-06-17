"""forge-next-prep — prepare main for the next task (fetch, pull, tag, prune).

Single source of truth for the mechanical git work the ``/next`` skill
runs at the start of a fresh task. Extracted from inline bash in the
skill so the version-compare + tag-bump logic is testable and reusable.

The skill remains responsible for user-interaction: refusing to run on a
dirty tree, confirming destructive steps, presenting the report.

Operations (in order, each idempotent):

1. ``git fetch --prune`` — refresh remote tracking.
2. ``git switch <target>`` (``git checkout`` fallback for git < 2.23),
   then ``git pull --ff-only`` — sync to latest.
3. **Optional auto-tag** (``--tag``): if ``.claude-plugin/plugin.json``
   has a ``version`` strictly ahead of the latest ``v*`` tag, tag the
   merge commit and push the tag. Forge's rolling-next workflow.
4. **Prune stale branches** (``--prune-branches``, default ON): delete
   local branches whose remote shows ``[origin/...: gone]``. Uses
   ``git branch -d`` (safe) — never ``-D``.

``--promotion-status`` is a separate read-only mode: it fetches tags,
prints the base/dev plugin versions and the ordered list of ``v*``
releases still pending promotion, then exits — no checkout, pull, tag,
or prune. The ``/promote`` skill calls it instead of hand-rolling the
git/version comparison.

The ``--target`` flag (forge-internal) lets the CLI refresh a different
branch when the repo's ``[tool.forge]`` block names one. Standard
single-branch repos can ignore it.

Exits 0 on success, 1 if the target branch can't fast-forward
(divergent state).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from forge.config import load_config
from forge.git_utils import configure_cli_logging, latest_v_tag, parse_semver


configure_cli_logging()
logger = logging.getLogger(__name__)


_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_GONE_BRANCH_RE = re.compile(r"^\*?\s*(\S+)\s+[0-9a-f]+\s+\[origin/\S+: gone\]")


def _read_plugin_version_at_ref(repo_root: Path, ref: str) -> str | None:
    """Return ``plugin.json["version"]`` at the given git ref, or ``None`` when absent.

    Args:
        repo_root: Working directory for git operations.
        ref: Any git refspec (``origin/dev``, a tag, a SHA).

    Returns:
        Bare version string when ``.claude-plugin/plugin.json`` exists at
        *ref* and parses cleanly, ``None`` otherwise. Missing manifests
        are common in non-plugin repos (the gate that uses this returns
        no-promotion-pending in that case).
    """
    proc = subprocess.run(
        ["git", "show", f"{ref}:.claude-plugin/plugin.json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return str(json.loads(proc.stdout)["version"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _check_promote_pending_message(
    repo_root: Path,
    dev_branch: str,
    base_branch: str,
) -> str | None:
    """Return a one-line user-facing prompt when promotion is pending, else ``None``.

    A promotion is "pending" when ``origin/<dev_branch>``'s plugin
    manifest carries a MINOR or MAJOR version bump over
    ``origin/<base_branch>``'s. Patch-only differences (`Z+1`)
    accumulate on dev between releases per the rolling-next convention
    and do NOT count as pending.

    Args:
        repo_root: Working directory for git operations.
        dev_branch: Name of the fast-channel branch (e.g. ``"dev"``).
        base_branch: Name of the slow-channel branch (e.g. ``"main"``).

    Returns:
        Pre-formatted one-line prompt string when promotion is pending,
        ``None`` otherwise. The string includes the bump type and is
        ready to log directly. ``None`` is returned when: the repo is
        single-branch (``dev_branch == base_branch``); either branch
        lacks ``.claude-plugin/plugin.json``; the diff is patch-only;
        or either version string is not semver-shaped.
    """
    if dev_branch == base_branch:
        return None
    dev_ver = _read_plugin_version_at_ref(repo_root, f"origin/{dev_branch}")
    base_ver = _read_plugin_version_at_ref(repo_root, f"origin/{base_branch}")
    if dev_ver is None or base_ver is None:
        return None
    if not (_SEMVER_RE.match(dev_ver) and _SEMVER_RE.match(base_ver)):
        return None
    dev_major, dev_minor, _ = dev_ver.split(".")
    base_major, base_minor, _ = base_ver.split(".")
    if dev_major != base_major:
        bump = "MAJOR"
    elif dev_minor != base_minor:
        bump = "MINOR"
    else:
        return None
    return (
        f"Pending promotion: {dev_branch} at v{dev_ver}; "
        f"{base_branch} at v{base_ver} ({bump} bump). "
        f"Run /promote (or your repo's equivalent) to open the "
        f"{dev_branch}→{base_branch} release PR."
    )


def _changelog_lacks_entry(changelog_text: str, minor_tag: str) -> bool:
    """Return True when *changelog_text* has no ``## <minor_tag>`` heading.

    Matches a heading whose version token equals ``minor_tag`` — either
    ``## v1.6.0 — <date>`` (the Keep-a-Changelog form forge uses) or a
    bare ``## v1.6.0`` — so the optional date suffix does not defeat the
    lookup. Drives the non-blocking promotion advisory; see
    ``docs/release-process.md`` §5.

    Args:
        changelog_text: Full ``CHANGELOG.md`` contents.
        minor_tag: Release tag to look for, e.g. ``"v1.6.0"``.

    Returns:
        ``True`` when no heading for ``minor_tag`` is present.
    """
    return not any(
        line.startswith(f"## {minor_tag} ") or line.strip() == f"## {minor_tag}"
        for line in changelog_text.splitlines()
    )


def _promotion_status_lines(
    repo_root: Path,
    dev_branch: str,
    base_branch: str,
) -> list[str]:
    """Build the read-only promotion-status report.

    Reports the base/dev plugin versions and, when dev is a MINOR/MAJOR
    ahead, the ordered list of ``X.Y.0`` releases that ``base`` must be
    promoted up to — one minor per line, ascending. ``base`` is
    minor-only: interleaved patch tags are excluded because they fold
    into the next minor's promotion. Shares version-read and
    pending-detection helpers with the ``/next`` advisory, giving the
    ``/promote`` skill a single authoritative source for the git/version
    comparison.

    Args:
        repo_root: Working directory for git operations.
        dev_branch: Fast-channel branch name (e.g. ``"dev"``).
        base_branch: Slow-channel branch name (e.g. ``"main"``).

    Returns:
        Human-readable report lines (never empty). Reads ``origin/*``
        tracking refs — the caller is responsible for fetching first.
    """
    if dev_branch == base_branch:
        return ["Single-branch repo — no dev→base promotion model."]
    base_ver = _read_plugin_version_at_ref(repo_root, f"origin/{base_branch}")
    dev_ver = _read_plugin_version_at_ref(repo_root, f"origin/{dev_branch}")
    if base_ver is None or dev_ver is None:
        return [f"No plugin manifest on origin/{base_branch} or origin/{dev_branch}."]
    lines = [
        f"{base_branch} (origin/{base_branch}): v{base_ver}",
        f"{dev_branch} (origin/{dev_branch}): v{dev_ver}",
    ]
    base_tuple = parse_semver(base_ver)
    dev_tuple = parse_semver(dev_ver)
    # Pending only on a MINOR/MAJOR gap with dev ahead — patches
    # accumulate on dev between releases (rolling-next). Compared from the
    # already-read versions; no second round of git reads.
    if base_tuple is None or dev_tuple is None or base_tuple[:2] >= dev_tuple[:2]:
        lines.append("Up to date — nothing to promote.")
        return lines
    # Only MINOR/MAJOR releases (``X.Y.0``) are promotion targets — base
    # is minor-only, and accumulated patches fold into the next minor's
    # promotion (e.g. 1.5.1 / 1.5.2 ride along when 1.6.0 is promoted).
    # Filtering on ``pv[2] == 0`` drops the interleaved patch tags the
    # version range would otherwise list as separate promotions.
    staged = sorted(
        (pv, tag)
        for tag in _git("tag", "--list", "v*", cwd=repo_root, check=False).split()
        if (pv := parse_semver(tag)) is not None
        and pv[2] == 0
        and base_tuple < pv <= dev_tuple
    )
    if not staged:
        # MINOR/MAJOR gap detected but no ``X.Y.0`` tag in range — the
        # minor was never tagged (fresh or mid-flight repo). Don't print a
        # misleading "promote these (0):" header with an empty list.
        lines.append(
            "Promotion pending, but no X.Y.0 release tag found in range "
            "— check that the target minor was tagged."
        )
        return lines
    lines.append(f"Promotion pending — promote these in order ({len(staged)}):")
    lines.extend(f"  {tag}" for _, tag in staged)
    # Non-blocking CHANGELOG advisory (docs/release-process.md §5): each
    # promoted minor should already carry its entry, authored on dev.
    # Stays silent for repos that keep no CHANGELOG (git show → empty).
    changelog = _git(
        "show", f"origin/{dev_branch}:CHANGELOG.md", cwd=repo_root, check=False
    )
    if changelog:
        missing = [tag for _, tag in staged if _changelog_lacks_entry(changelog, tag)]
        if missing:
            lines.append(
                f"⚠️  CHANGELOG.md (origin/{dev_branch}) has no entry for "
                f"{', '.join(missing)} — author it on {dev_branch} before "
                "promoting (docs/release-process.md §5)."
            )
    return lines


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """Run ``git`` with *args*, return stripped stdout.

    Args:
        *args: Argv tail (without the leading ``git``).
        cwd: Working directory; defaults to current.
        check: When ``True``, raise ``CalledProcessError`` on non-zero exit.

    Returns:
        Trimmed stdout.

    Raises:
        subprocess.CalledProcessError: When ``check=True`` and git exits
            non-zero.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )
    return proc.stdout.strip()


def _read_plugin_version(repo_root: Path) -> str | None:
    """Return ``.claude-plugin/plugin.json["version"]`` or ``None`` if absent.

    Args:
        repo_root: Repo root.

    Returns:
        Bare semver string (e.g. ``"1.2.10"``) or ``None`` when the
        manifest is missing or the version field is absent / non-semver.
    """
    plugin = repo_root / ".claude-plugin" / "plugin.json"
    if not plugin.is_file():
        return None
    try:
        data = json.loads(plugin.read_text())
    except json.JSONDecodeError:
        return None
    version = data.get("version")
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        return None
    return version


def _is_newer(plugin_ver: str, latest_tag: str | None) -> bool:
    """Return True when ``v<plugin_ver>`` would sort *after* ``latest_tag``.

    Compares parsed semver tuples (shared parser from
    ``forge.git_utils.parse_semver``) — no subprocess.

    Args:
        plugin_ver: Bare semver string from plugin.json.
        latest_tag: Latest existing ``v*`` tag or ``None``.

    Returns:
        ``True`` when ``plugin_ver`` is strictly newer than ``latest_tag``
        (or when no tags exist yet, or when ``latest_tag`` is unparseable
        as semver).
    """
    if latest_tag is None:
        return True
    plugin_tuple = parse_semver(plugin_ver)
    tag_tuple = parse_semver(latest_tag)
    if plugin_tuple is None:
        return False  # malformed plugin.json — guarded upstream, defensive here
    if tag_tuple is None:
        return True  # non-semver tag (e.g. "v0.1-pre") — treat plugin as newer
    return plugin_tuple > tag_tuple


def _maybe_tag_release(repo_root: Path) -> str | None:
    """Tag and push ``v<plugin.json.version>`` when newer than the latest tag.

    Idempotent: no-op when plugin.json is missing, the version field is
    non-semver, or the version is not strictly ahead of the latest tag.

    Args:
        repo_root: Repo root.

    Returns:
        The tag name on success (e.g. ``"v1.2.10"``), or ``None`` when
        no tagging was needed / possible.
    """
    plugin_ver = _read_plugin_version(repo_root)
    if plugin_ver is None:
        return None
    latest = latest_v_tag(repo_root)
    if not _is_newer(plugin_ver, latest):
        return None
    tag = f"v{plugin_ver}"
    _git("tag", "-a", tag, "-m", tag, "HEAD", cwd=repo_root)
    _git("push", "origin", tag, cwd=repo_root)
    return tag


def _gone_branches(repo_root: Path) -> list[str]:
    """Return local branch names whose tracking remote is ``[origin/...: gone]``.

    Args:
        repo_root: Repo root.

    Returns:
        Branch names (no leading ``* `` star, no whitespace). Empty list
        when nothing is gone or no branches exist.
    """
    raw = _git("branch", "-vv", cwd=repo_root, check=False)
    out: list[str] = []
    for line in raw.splitlines():
        match = _GONE_BRANCH_RE.match(line)
        if match:
            out.append(match.group(1))
    return out


def _prune_gone_branches(repo_root: Path) -> tuple[list[str], list[str]]:
    """``git branch -d`` every branch whose remote is gone.

    Safe ``-d`` only — never ``-D``. Branches with unmerged commits are
    reported but not deleted.

    Args:
        repo_root: Repo root.

    Returns:
        Tuple ``(deleted, skipped)`` of branch names.
    """
    deleted: list[str] = []
    skipped: list[str] = []
    for branch in _gone_branches(repo_root):
        proc = subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            deleted.append(branch)
        else:
            skipped.append(branch)
    return deleted, skipped


def _emit_promotion_status(
    repo_root: Path,
    dev_branch: str,
    base_branch: str,
) -> int:
    """Fetch tags and log the read-only promotion-status report.

    Args:
        repo_root: Working directory for git operations.
        dev_branch: Fast-channel branch name.
        base_branch: Slow-channel branch name.

    Returns:
        Always ``0`` — this is a pure read-only report.
    """
    _git("fetch", "origin", "--tags", "--quiet", cwd=repo_root, check=False)
    for line in _promotion_status_lines(repo_root, dev_branch, base_branch):
        logger.info("%s", line)
    return 0


def _log_prune_result(repo_root: Path) -> None:
    """Prune stale local branches and log the outcome.

    Args:
        repo_root: Working directory for git operations.
    """
    deleted, skipped = _prune_gone_branches(repo_root)
    if deleted:
        logger.info("Pruned stale branches: %s", ", ".join(deleted))
    if skipped:
        logger.warning(
            "Skipped branches with unmerged commits (use -D manually if "
            "you really want to drop them): %s",
            ", ".join(skipped),
        )
    if not deleted and not skipped:
        logger.info("No stale branches to prune.")


def main() -> int:
    """Refresh main, optionally tag the release, prune stale local branches.

    The target branch defaults to ``main`` for repos without
    ``[tool.forge]``. Forge's own repo overrides this internally to
    support its release workflow — that's not a pattern consumers need
    to replicate.

    Returns:
        ``0`` on success, ``1`` when the target branch cannot
        fast-forward (divergent state — user intervention needed).
    """
    parser = argparse.ArgumentParser(
        prog="forge-next-prep",
        description=(
            "Prepare main for the next task: fetch + pull --ff-only, "
            "optionally tag the rolling-next release, prune stale local "
            "branches. Used by the /next skill."
        ),
    )
    parser.add_argument(
        "--tag",
        action="store_true",
        help=(
            "Tag plugin.json's version when it's ahead of the latest v* tag "
            "and push the tag (forge's rolling-next workflow). Off by default."
        ),
    )
    parser.add_argument(
        "--no-prune-branches",
        action="store_true",
        help="Skip the stale-branch prune step.",
    )
    parser.add_argument(
        "--promotion-status",
        action="store_true",
        help=(
            "Read-only: fetch tags, then print the base/dev plugin versions "
            "and the ordered list of v* releases pending promotion, and exit. "
            "No checkout, pull, tag, or prune. Used by the /promote skill."
        ),
    )
    parser.add_argument(
        "--target",
        choices=("dev", "base"),
        default="dev",
        help=(
            "Branch to refresh. Resolved through [tool.forge] in "
            "pyproject.toml; falls back to 'main' if the config is absent. "
            "Most repos can leave this at the default."
        ),
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    cfg = load_config(repo_root)

    if args.promotion_status:
        return _emit_promotion_status(repo_root, cfg.dev_branch, cfg.base_branch)

    target_branch = cfg.dev_branch if args.target == "dev" else cfg.base_branch

    logger.info("Fetching from origin...")
    _git("fetch", "--prune", cwd=repo_root)

    logger.info("Checking out %s and pulling...", target_branch)
    # Prefer ``git switch``: it operates only on branches, so it's
    # unambiguous when the branch name collides with a working-tree path
    # (e.g. a directory named ``dev/``, which forge itself has). Falls
    # back to ``git checkout`` for git < 2.23 where ``switch`` does not
    # exist. The fallback may still hit the collision on those older
    # gits — a contributor seeing it should upgrade.
    # ``--`` end-of-options separator: defense-in-depth so a malformed
    # ``[tool.forge].dev_branch`` value starting with ``-`` (e.g.
    # ``"--detach"``) is treated as a branch name, not a flag. Branch
    # names come from a repo-owned file so this is self-inflicted only,
    # but the guard is free for ``switch`` (no "branch vs path" overload
    # to confuse).
    proc = subprocess.run(
        ["git", "switch", "--", target_branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        _git("checkout", target_branch, cwd=repo_root)
    proc = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        logger.error(
            "%s cannot fast-forward — divergent state.\n%s",
            target_branch,
            (proc.stdout + proc.stderr).strip(),
        )
        return 1

    if args.tag:
        tag = _maybe_tag_release(repo_root)
        if tag:
            logger.info("Tagged and pushed %s", tag)
        else:
            logger.info("No release tag needed.")

    if not args.no_prune_branches:
        _log_prune_result(repo_root)

    # Promotion-pending advisory. Self-gating: only emits when
    # ``[tool.forge]`` declares a separate ``dev_branch`` (dual-track
    # repos like forge itself); silent for the standard single-branch
    # case. See FOUNDATION §16 "Extending shipped agents, skills, and CLIs".
    pending = _check_promote_pending_message(repo_root, cfg.dev_branch, cfg.base_branch)
    if pending:
        logger.info(pending)

    return 0


if __name__ == "__main__":
    sys.exit(main())
