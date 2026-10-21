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
   merge commit and push the tag — in fragments mode that is the
   assembly merge, the only commit whose tree names the new version.
   When it does not tag it logs why. On a repo with no plugin manifest
   the flag warns and skips: nothing names the version to tag, and
   ``forge-release`` cuts release tags there.
4. **Prune stale branches** (``--prune-branches``, default ON): delete
   local branches whose remote shows ``[origin/...: gone]``. Uses
   ``git branch -d`` (safe) — never ``-D``.
5. **Prune merged remote branches** (``--prune-remote``, default off;
   ``/next`` always passes it): delete an origin branch only when a
   same-repo PR from it merged into the base branch, no PR from it is
   open, and origin's live tip still equals that PR's head commit. The
   delete is lease-pinned to that commit, so a branch that moved since
   the merge is reported and left alone; the base branch and the branch
   ``origin/HEAD`` names are never touched. When ``gh`` cannot answer,
   the step is skipped with a warning — never read as "nothing to prune".

Exits 0 on success, 1 if the target branch can't fast-forward
(divergent state).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NamedTuple

from forge.changelog_fragments import discover_fragments
from forge.config import (
    is_fragments_mode,
    load_config,
)
from forge.git_utils import (
    GH_TIMEOUT_S,
    PUSH_TIMEOUT_S,
    configure_cli_logging,
    create_annotated_tag,
    delete_remote_branch,
    fetch_tags_best_effort,
    latest_v_tag,
    network_git_env,
    parse_semver,
    read_local_plugin_version,
    run_git,
    tag_on_remote,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


# Applied via fullmatch — a bare `$` would tolerate one trailing newline.
_SEMVER_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_GONE_BRANCH_RE = re.compile(r"^\*?\s*(\S+)\s+[0-9a-f]+\s+\[origin/\S+: gone\]")


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


def tag_staleness_warning(repo_root: Path) -> str | None:
    """Return a warning when the integration branch owes a rolling-next tag.

    Fires only on the configured ``base_branch`` and only when
    ``plugin.json``'s version is strictly newer than the latest ``v*``
    tag — i.e. a merge bumped the rolling-next version but
    ``forge-next-prep --tag`` was never run, so the tag silently lags and
    the pre-commit guard keeps passing without forcing the next bump. This
    is the advisory the ``forge-post-merge`` hook surfaces on ``git pull``.
    Detection only — it never tags or pushes (an irreversible release must
    stay an explicit human action).

    Args:
        repo_root: Git repo root.

    Returns:
        A one-line warning string, or ``None`` when not on ``base_branch``,
        when there is no plugin manifest, or when the tag is already
        current.
    """
    current = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_root, check=False)
    if not current or current != load_config(repo_root).base_branch:
        return None
    plugin_ver = read_local_plugin_version(repo_root)
    if plugin_ver is None:
        return None
    latest = latest_v_tag(repo_root)
    if not _is_newer(plugin_ver, latest):
        return None
    return (
        f"plugin.json {plugin_ver} is ahead of the latest tag "
        f"{latest or '(none)'} — run `forge-next-prep --tag` to tag this release."
    )


def _tag_misuse_warning(repo_root: Path) -> str | None:
    """Return a warning when ``--tag`` is used in a repo with no manifest.

    ``--tag`` cuts the version ``.claude-plugin/plugin.json`` names. A
    repo with no manifest releases via ``forge-release``
    instead, so ``--tag`` there is almost always a command copied from
    forge's own workflow — warn loudly rather than no-op silently.

    Args:
        repo_root: Repo root.

    Returns:
        A one-line warning when the repo has no plugin manifest;
        ``None`` when ``--tag`` is applicable.
    """
    if read_local_plugin_version(repo_root) is not None:
        return None
    return (
        "--tag skipped: no .claude-plugin/plugin.json, so nothing names the "
        "version to tag. Cut release tags with `forge-release` instead "
        "(docs/consumer-release.md)."
    )


class TagDecision(NamedTuple):
    """Outcome of ``--tag``: the tag cut, or why none was.

    The reason is always set, so the CI log of a merge that cut no tag
    says which of the no-op cases applied instead of a bare "nothing to
    do" that reads the same whether or not a release was owed.

    Attributes:
        tag: The tag created and pushed, or ``None`` when none was.
        reason: One line explaining the decision.
    """

    tag: str | None
    reason: str


def _maybe_tag_release(repo_root: Path) -> TagDecision:
    """Tag and push ``v<plugin.json.version>`` when newer than the latest tag.

    Idempotent: no-op when plugin.json is missing, the version field is
    non-semver, or the version is not strictly ahead of the latest tag.

    Args:
        repo_root: Repo root.

    Returns:
        The decision — ``tag`` set (e.g. ``"v1.2.10"``) on success,
        ``None`` otherwise, with the reason either way.

    Raises:
        subprocess.TimeoutExpired: When the tag push exceeds
            :data:`~forge.git_utils.PUSH_TIMEOUT_S` and origin cannot be
            shown to hold the tag; what the operator must do next is
            logged first. A timed-out push the remote did receive counts
            as success.
    """
    plugin_ver = read_local_plugin_version(repo_root)
    if plugin_ver is None:
        return TagDecision(None, "no .claude-plugin/plugin.json version to tag")
    if parse_semver(plugin_ver) is None:
        return TagDecision(None, f"plugin.json version {plugin_ver!r} is not semver")
    latest = latest_v_tag(repo_root)
    if not _is_newer(plugin_ver, latest):
        if latest is not None and parse_semver(plugin_ver) == parse_semver(latest):
            return TagDecision(
                None,
                f"plugin.json {plugin_ver} is already tagged as {latest}; the "
                "next tag is cut when a release PR (`forge-changelog "
                "release-pr`) raising plugin.json merges",
            )
        return TagDecision(
            None,
            f"plugin.json {plugin_ver} is behind the latest tag {latest}; "
            "nothing to tag until a release PR (`forge-changelog release-pr`) "
            "raises plugin.json past it",
        )
    tag = f"v{plugin_ver}"
    create_annotated_tag(repo_root, tag)
    reason = f"plugin.json {plugin_ver} is ahead of {latest}"
    try:
        run_git(
            "push",
            "origin",
            tag,
            cwd=repo_root,
            env=network_git_env(repo_root),
            timeout=PUSH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # A timed-out push may still have landed: the remote can accept
        # the ref and stall before git hears back. Ask before failing.
        on_remote = tag_on_remote(repo_root, tag)
        if not on_remote:
            _log_tag_push_timeout(tag, on_remote=on_remote)
            raise
        reason += "; the timed-out push landed"
    return TagDecision(tag, reason)


def _log_tag_push_timeout(tag: str, *, on_remote: bool | None) -> None:
    """Tell the operator how to finish a tag push that timed out.

    Logged without a traceback: the stack would only show forge waiting
    on git, and reads in CI as a crash rather than an unresponsive remote.

    Args:
        tag: The tag that was created locally but whose push timed out.
        on_remote: What the remote probe answered — ``False`` when origin
            lacks the tag, ``None`` when it could not be asked.
    """
    state = (
        "origin does not have it"
        if on_remote is False
        else "whether origin has it is unknown"
    )
    # A re-run in the same checkout would not retry: latest_v_tag now sees
    # the local tag, so plugin.json reads as "already tagged".
    logger.error(
        "%s was created locally but its push timed out after %ss, and %s; "
        "once origin responds, push it with `git push origin %s`. "
        "Re-running forge-next-prep --tag in this checkout will not retry "
        "(the local tag counts as already tagged); a fresh checkout "
        "without the tag does.",
        tag,
        PUSH_TIMEOUT_S,
        state,
        tag,
    )


def _fetch_origin(repo_root: Path) -> bool:
    """Run the bounded ``git fetch --prune`` the sync starts with.

    Args:
        repo_root: Repo root.

    Returns:
        ``True`` when the fetch finished; ``False`` when origin did not
        answer within :data:`~forge.git_utils.PUSH_TIMEOUT_S` (logged).
    """
    try:
        run_git(
            "fetch",
            "--prune",
            cwd=repo_root,
            env=network_git_env(repo_root),
            timeout=PUSH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pass
    else:
        return True
    # Logged outside the handler, without a traceback: the stack would
    # only show forge waiting on git.
    logger.error(
        "origin did not answer the fetch within %ss — nothing was synced; "
        "re-run once it responds.",
        PUSH_TIMEOUT_S,
    )
    return False


def _gone_branches(repo_root: Path) -> list[str]:
    """Return local branch names whose tracking remote is ``[origin/...: gone]``.

    Args:
        repo_root: Repo root.

    Returns:
        Branch names (no leading ``* `` star, no whitespace). Empty list
        when nothing is gone or no branches exist.
    """
    raw = run_git("branch", "-vv", cwd=repo_root, check=False)
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


# A branch reused for several PRs can have more than one; a lookup that
# fills the limit may be hiding an open one, so it counts as unknown.
_GH_PR_LIST_LIMIT = 10
# Bounds one run's gh calls (one per remote branch); the rest wait for
# the next run.
_PRUNE_REMOTE_CAP = 50
_PR_JSON_FIELDS = (
    "number,state,headRefName,headRefOid,isCrossRepository,baseRefName,mergedAt"
)


class PrLookup(NamedTuple):
    """What ``gh`` said about the PRs whose head is one branch.

    Attributes:
        prs: The PRs as ``gh pr list --json`` returns them, or ``None``
            when gh could not answer.
        problem: Why gh could not answer, one line; ``None`` on success.
        fatal: ``True`` when the problem applies to every branch (gh
            missing, unauthenticated, timed out), so the run stops asking.
    """

    prs: list[dict[str, object]] | None
    problem: str | None = None
    fatal: bool = False


PrLister = Callable[[Path, str], PrLookup]


def _gh_prs_for_head(repo_root: Path, branch: str) -> PrLookup:
    """Ask ``gh`` for every PR (any state) whose head branch is *branch*.

    Args:
        repo_root: Repo root (gh resolves the repository from it).
        branch: Head branch name.

    Returns:
        The lookup; failures are classified rather than raised, and never
        collapse to an empty list — "gh could not say" is not "no PR".
    """
    if shutil.which("gh") is None:
        return PrLookup(None, "gh is not installed", fatal=True)
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                f"--head={branch}",
                "--state=all",
                f"--json={_PR_JSON_FIELDS}",
                f"--limit={_GH_PR_LIST_LIMIT}",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return PrLookup(None, f"gh did not answer within {GH_TIMEOUT_S}s", fatal=True)
    except OSError as exc:
        return PrLookup(None, f"gh could not start: {exc}", fatal=True)
    if proc.returncode != 0:
        return _gh_pr_list_failure(proc.stderr)
    return _parse_pr_list(proc.stdout)


def _gh_pr_list_failure(stderr: str) -> PrLookup:
    """Classify a non-zero ``gh pr list`` exit.

    Args:
        stderr: What gh wrote to stderr.

    Returns:
        A failed lookup; fatal when the cause is authentication.
    """
    detail = (stderr.strip().splitlines() or ["no error output"])[0]
    if "auth" in stderr.lower():
        return PrLookup(None, f"gh is not authenticated ({detail})", fatal=True)
    return PrLookup(None, f"gh pr list failed ({detail})")


def _parse_pr_list(stdout: str) -> PrLookup:
    """Parse the JSON array ``gh pr list`` printed on success.

    Args:
        stdout: What gh wrote to stdout.

    Returns:
        The parsed lookup, or a failed one when the output is not a JSON list.
    """
    try:
        prs = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return PrLookup(None, "gh pr list returned output that is not JSON")
    if not isinstance(prs, list):
        return PrLookup(None, "gh pr list returned output that is not a list")
    return PrLookup(prs)


def _remote_heads(repo_root: Path) -> dict[str, str] | None:
    """Return ``{branch: tip sha}`` for every branch on origin, read live.

    Read with ``git ls-remote`` rather than from remote-tracking refs, so
    the tip compared against the merged PR is origin's own, not a cached
    copy.

    Args:
        repo_root: Repo root.

    Returns:
        The mapping, or ``None`` when origin could not be listed.
    """
    try:
        raw = run_git(
            "ls-remote",
            "--heads",
            "origin",
            cwd=repo_root,
            env=network_git_env(repo_root),
            timeout=PUSH_TIMEOUT_S,
            log_errors=False,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    heads: dict[str, str] = {}
    for line in raw.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            heads[ref.removeprefix("refs/heads/")] = sha
    return heads


def _origin_head_branch(repo_root: Path) -> str | None:
    """Return the branch ``origin/HEAD`` points at, or ``None`` if unknown.

    Args:
        repo_root: Repo root.

    Returns:
        The branch name (e.g. ``"main"``), or ``None``.
    """
    ref = run_git(
        "symbolic-ref",
        "--quiet",
        "refs/remotes/origin/HEAD",
        cwd=repo_root,
        check=False,
        log_errors=False,
    )
    return ref.removeprefix("refs/remotes/origin/") or None


class RemotePruneReport(NamedTuple):
    """What ``--prune-remote`` did, per branch.

    Attributes:
        deleted: Branches deleted on origin.
        moved: Branches whose tip differs from their merged PR's head
            (including a lease git rejected), left alone.
        open_pr: Branches with an open PR, left alone.
        unmerged: Branches with no same-repo PR merged into the base
            branch, left alone.
        failed: ``{branch: reason}`` for a delete that failed otherwise.
        unknown: ``{reason: [branches]}`` for branches gh could not judge.
        examined: How many remote branches were examined.
        total: How many remote branches were candidates.
        listed: ``False`` when origin could not be listed at all.
    """

    deleted: list[str]
    moved: list[str]
    open_pr: list[str]
    unmerged: list[str]
    failed: dict[str, str]
    unknown: dict[str, list[str]]
    examined: int
    total: int
    listed: bool = True


def _pr_verdict(
    prs: list[dict[str, object]], branch: str, base: str
) -> tuple[Literal["open", "unmerged", "merged"], frozenset[str]]:
    """Judge *branch* from its PRs.

    Any open PR on the head wins, whatever repository it comes from: the
    branch is still in use. Only a same-repo PR merged into *base* makes
    the branch prunable — a cross-repo PR's head lives in a fork.

    Args:
        prs: The branch's PRs from :func:`_gh_prs_for_head`.
        branch: The branch being judged.
        base: The configured base branch.

    Returns:
        The verdict, and for ``"merged"`` the merged PRs' head commits
        (the caller matches origin's live tip against them).
    """
    if any(pr.get("state") == "OPEN" for pr in prs):
        return "open", frozenset()
    merged = frozenset(
        str(pr.get("headRefOid"))
        for pr in prs
        if pr.get("state") == "MERGED"
        and pr.get("isCrossRepository") is False
        and pr.get("headRefName") == branch
        and pr.get("baseRefName") == base
        and pr.get("headRefOid")
    )
    return ("merged", merged) if merged else ("unmerged", frozenset())


def _prune_merged_remote_branches(
    repo_root: Path, base_branch: str, *, pr_lister: PrLister | None = None
) -> RemotePruneReport:
    """Delete origin branches whose same-repo PR merged and whose tip is unchanged.

    A branch is deleted only when a same-repo PR from it merged into
    *base_branch*, no PR from it is open, and origin's live tip equals
    that PR's head commit — the delete is lease-pinned to that commit, so
    a push landing in between leaves the branch alone. *base_branch* and
    the branch ``origin/HEAD`` names are never candidates.

    Args:
        repo_root: Repo root.
        base_branch: The configured base branch.
        pr_lister: Replaces the gh lookup (tests); defaults to
            :func:`_gh_prs_for_head`, resolved at call time.

    Returns:
        The report.
    """
    lister = pr_lister or _gh_prs_for_head
    report = RemotePruneReport([], [], [], [], {}, {}, 0, 0)
    heads = _remote_heads(repo_root)
    if heads is None:
        return report._replace(listed=False)
    protected = {base_branch, _origin_head_branch(repo_root) or base_branch}
    candidates = sorted(b for b in heads if b not in protected)
    batch = candidates[:_PRUNE_REMOTE_CAP]
    report = report._replace(total=len(candidates), examined=len(batch))
    for index, branch in enumerate(batch):
        lookup = lister(repo_root, branch)
        if lookup.prs is None or len(lookup.prs) >= _GH_PR_LIST_LIMIT:
            reason = lookup.problem or (
                f"{_GH_PR_LIST_LIMIT}+ PRs on this head, too many to judge"
            )
            # A problem that applies to every branch stops the run: asking
            # again would only repeat it (or wait out the timeout again).
            rest = batch[index:] if lookup.fatal else [branch]
            report.unknown.setdefault(reason, []).extend(rest)
            if lookup.fatal:
                break
            continue
        verdict, merged_heads = _pr_verdict(lookup.prs, branch, base_branch)
        if verdict == "open":
            report.open_pr.append(branch)
        elif verdict == "unmerged":
            report.unmerged.append(branch)
        elif heads[branch] not in merged_heads:
            report.moved.append(branch)
        else:
            _delete_one(repo_root, branch, heads[branch], base_branch, report)
    return report


def _delete_one(
    repo_root: Path, branch: str, sha: str, base_branch: str, report: RemotePruneReport
) -> None:
    """Delete *branch* on origin lease-pinned to *sha*, recording the outcome.

    Args:
        repo_root: Repo root.
        branch: Branch to delete.
        sha: The merged head commit the branch must still point at.
        base_branch: The configured base branch (refused by the seam).
        report: The report whose lists receive the outcome.
    """
    result = delete_remote_branch(
        repo_root, branch, expected_sha=sha, base_branch=base_branch
    )
    if result.ok:
        report.deleted.append(branch)
    elif result.stale_lease:
        report.moved.append(branch)
    else:
        report.failed[branch] = result.stderr or "git push failed"


def _log_remote_prune_result(
    repo_root: Path, base_branch: str, *, pr_lister: PrLister | None = None
) -> None:
    """Prune merged remote branches and log the outcome.

    Args:
        repo_root: Repo root.
        base_branch: The configured base branch.
        pr_lister: Replaces the gh lookup (tests).
    """
    report = _prune_merged_remote_branches(repo_root, base_branch, pr_lister=pr_lister)
    if not report.listed:
        logger.warning(
            "Remote prune skipped: origin's branches could not be listed "
            "(git ls-remote failed or timed out) — nothing was deleted."
        )
        return
    if report.deleted:
        logger.info(
            "Deleted merged remote branches (each restorable from its PR page): %s",
            ", ".join(report.deleted),
        )
    if report.moved:
        logger.warning(
            "Left remote branches that moved since their PR merged: %s",
            ", ".join(report.moved),
        )
    if report.open_pr:
        logger.info(
            "Left remote branches with an open PR: %s", ", ".join(report.open_pr)
        )
    if report.unmerged:
        logger.info(
            "Left remote branches with no merged PR into %s: %s",
            base_branch,
            ", ".join(report.unmerged),
        )
    for branch, reason in report.failed.items():
        logger.warning("Could not delete remote branch %s: %s", branch, reason)
    for reason, branches in report.unknown.items():
        logger.warning(
            "Remote prune skipped %d branch(es), PR state unknown (%s): %s",
            len(branches),
            reason,
            ", ".join(branches),
        )
    if report.total > report.examined:
        logger.warning(
            "Remote prune examined %d of %d remote branches (cap %d per run, "
            "in name order); the remaining %d were not examined.",
            report.examined,
            report.total,
            _PRUNE_REMOTE_CAP,
            report.total - report.examined,
        )
    if report.total == 0:
        logger.info("No remote branches besides %s to prune.", base_branch)


def main() -> int:
    """Refresh main, optionally tag the release, prune stale local branches.

    The target branch defaults to ``main`` for repos without
    ``[tool.forge]``. Forge's own repo overrides this internally to
    support its release workflow — that's not a pattern consumers need
    to replicate.

    Returns:
        ``0`` on success, ``1`` when the target branch cannot
        fast-forward (divergent state — user intervention needed) or
        when origin does not answer the fetch, pull or tag push in time
        (a timed-out tag push origin did receive still counts as success).
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
        "--prune-remote",
        action="store_true",
        help=(
            "Delete origin branches whose same-repo PR merged into the base "
            "branch and whose tip is unchanged since (lease-pinned to the "
            "merged commit). Branches with an open PR, no merged PR, or a "
            "moved tip are reported and left alone. Off by default."
        ),
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help=(
            "Skip the fetch/checkout/pull steps and operate on the current "
            "HEAD (tags are still fetched for the version comparison). For "
            "CI tag jobs that check out the exact validated SHA — syncing "
            "to the branch tip would re-introduce the race the pinned "
            "checkout exists to prevent."
        ),
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    cfg = load_config(repo_root)

    target_branch = cfg.base_branch

    if args.no_sync:
        # CI tag path: HEAD is already the exact commit CI validated;
        # only the tag set needs refreshing for the version comparison.
        for note in fetch_tags_best_effort(repo_root):
            logger.warning("%s", note)
        return _tag_and_report(repo_root, args)

    logger.info("Fetching from origin...")
    if not _fetch_origin(repo_root):
        return 1

    logger.info("Checking out %s and pulling...", target_branch)
    # Prefer ``git switch``: it operates only on branches, so it's
    # unambiguous when the branch name collides with a working-tree path
    # (e.g. a directory named ``dev/``, which forge itself has). Falls
    # back to ``git checkout`` for git < 2.23 where ``switch`` does not
    # exist. The fallback may still hit the collision on those older
    # gits — a contributor seeing it should upgrade.
    # ``--`` end-of-options separator: defense-in-depth so a malformed
    # ``[tool.forge].base_branch`` value starting with ``-`` (e.g.
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
        run_git("checkout", target_branch, cwd=repo_root)
    if not _pull_ff_only(repo_root, target_branch):
        return 1

    return _tag_and_report(repo_root, args)


def _pull_ff_only(repo_root: Path, target_branch: str) -> bool:
    """Fast-forward *target_branch* from origin, bounded and prompt-free.

    Args:
        repo_root: Repo root.
        target_branch: The checked-out branch being synced (for messages).

    Returns:
        ``True`` when the pull fast-forwarded (or had nothing to do);
        ``False`` when the branch has diverged or origin did not answer
        within :data:`~forge.git_utils.PUSH_TIMEOUT_S` (both logged).
    """
    proc: subprocess.CompletedProcess[str] | None
    try:
        proc = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **network_git_env(repo_root)},
            timeout=PUSH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        proc = None
    if proc is None:
        # Logged outside the handler, without a traceback: the stack would
        # only show forge waiting on git.
        logger.error(
            "origin did not answer the pull of %s within %ss — %s was not "
            "fast-forwarded; re-run once origin responds.",
            target_branch,
            PUSH_TIMEOUT_S,
            target_branch,
        )
        return False
    if proc.returncode != 0:
        logger.error(
            "%s cannot fast-forward — divergent state.\n%s",
            target_branch,
            (proc.stdout + proc.stderr).strip(),
        )
        return False
    return True


def _tag_and_report(repo_root: Path, args: argparse.Namespace) -> int:
    """Run the post-sync tail: optional tag, optional prunes, advisory.

    Shared by the normal (synced) path and ``--no-sync``, so the tag /
    prune / promotion-advisory behavior cannot drift between them.

    Args:
        repo_root: Repo root.
        args: Parsed CLI namespace (``tag``, ``no_prune_branches``,
            ``prune_remote``).

    Returns:
        ``1`` when the tag push timed out (already logged); otherwise ``0``
        — other failures in these steps raise.
    """
    if args.tag:
        misuse = _tag_misuse_warning(repo_root)
        if misuse:
            logger.warning(misuse)
        else:
            try:
                decision = _maybe_tag_release(repo_root)
            except subprocess.TimeoutExpired:
                return 1
            if decision.tag:
                logger.info("Tagged and pushed %s (%s)", decision.tag, decision.reason)
            else:
                logger.info("No release tag: %s", decision.reason)

    if not args.no_prune_branches:
        _log_prune_result(repo_root)
    if args.prune_remote:
        _log_remote_prune_result(repo_root, load_config(repo_root).base_branch)

    # Fragment-accumulation advisory. Self-gating on fragments mode
    # (FOUNDATION §16, pattern C): pending changelog.d/ entries mean an
    # unreleased bump is waiting — surface the release command; silent
    # for shared-heading repos and when nothing is pending.
    if is_fragments_mode(repo_root):
        pending_fragments = len(discover_fragments(repo_root))
        if pending_fragments:
            logger.info(
                "%d pending changelog fragment(s) — release when ready: "
                "forge-changelog release-pr",
                pending_fragments,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
