"""Answer one question for CI: has this exact tree already passed its suite?

A squash-merge rewrites the commit SHA, the parent, the message and the
metadata — everything except the **tree**. For a suite whose result
depends only on the tree, running it again at an identical tree can only
reproduce the answer already in hand. This module decides whether the
tree is in fact identical, so a workflow can skip that execution and
still report green on the commit.

**"Only the tree" is an assumption about the suite, not a fact about
tests.** A suite can read what a squash changed: a version setuptools-scm
derives from tag distance and commit SHA, tags, the clock, the 48-hour
full-run stamp, the 30-day lifecycle window, the network. None of that is
tree-determined. A repo whose tests assert on any of it should not adopt
the skip, and ``forge-docs/ci-recipe.md`` says so where a consumer will
read it. Why this repository adopts it anyway, and what that rests on:
the gate step's comment in ``.github/workflows/ci.yml``.

Tree identity as the join is not new here:
:func:`forge.git_utils.get_tree_sha` already uses it to recognise the
squash commit that reproduces a release tag. This is the same join,
between a pull request's verified head and the squash commit that landed
it.

**Fail-closed everywhere.** Every unknown is a reason to run the suite: a
missing token, an unreachable API, a truncated listing, an ambiguous pull
request, a check still running, even an anticipated fault (``FAULTS``). One
redundant test run is the cost of a false negative; a false positive
ships untested code behind a green tick, so only a completely evidenced
match skips.

Three traps this has to avoid, each of which looks fine until it ships:

- **A pull-request run tests the merge ref, not the head.** GitHub checks
  out ``refs/pull/N/merge`` — the merge of the base at that moment with
  the head — while the run is recorded against the head SHA. Tree equality
  between the squash commit and the head therefore proves what was
  *tested* only when the head already contained the base.
  :func:`_parent_is_contained` establishes that; without it, a behind
  branch whose merge result happened to match could skip on a tree
  nothing ever ran.
- **A skipped job is not a passing job.** A workflow that excludes some
  pull requests (bot authors, drafts) leaves its own check skipped while
  unrelated checks stay green, so "every check succeeded" can be true of
  a commit whose suite never ran. The gate therefore requires one named
  job — its own, by default — to exist and to have concluded success.
- **A check-run's name proves nothing about what ran.** Anything holding
  ``checks: write`` can post a check-run under any name, and a step using
  ``GITHUB_TOKEN`` posts one attributed to the GitHub Actions app itself,
  so neither the name nor the app identifies a job. Two workflows sharing
  a job name collide the same way without anyone attacking. The gate
  therefore reads the Actions API: the pull-request run **of this
  workflow file** for that head, and the job of that run. A workflow path
  is forgeable only by someone who can already change the workflow.

The decision line is the contract a reader scans in a CI log, so it names
the evidence on a skip and the specific reason on a run. How a workflow
consumes it: ``forge-docs/ci-recipe.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from forge.git_utils import (
    configure_cli_logging,
    emit,
    get_tree_sha,
    gh_api,
    github_slug,
    repo_root,
    run_git,
)
from forge.run_context import is_ci


configure_cli_logging()
logger = logging.getLogger(__name__)

#: How much of a SHA the decision line prints. Enough to re-check by hand
#: against ``git rev-parse``, short enough to stay on one line.
SHA_PREFIX: Final = 12

#: Fields of a ``$GITHUB_WORKFLOW_REF`` value before the workflow path:
#: ``owner``, ``repo``, then the path itself.
WORKFLOW_REF_PARTS: Final = 3

#: ``compare`` statuses proving the squash commit's parent is an ancestor
#: of the pull request's head — i.e. the head already contained the base.
CONTAINED_STATUSES: Final = frozenset({"behind", "identical"})

#: Faults :func:`main` turns into a not-verified verdict instead of a
#: traceback. Listed explicitly rather than caught as a blind ``Exception``:
#: FOUNDATION §5 allows no lint suppression for that, and naming the types says
#: which failures were anticipated — a malformed payload, an unreadable
#: repo, a ``gh`` that could not run, a bug in this module.
#:
#: A type outside the tuple ends the step instead. That stays safe in
#: both directions only because the workflow pairs the step with
#: ``continue-on-error``: the suite runs because no ``verified=true`` was
#: written, and a gate bug cannot turn the whole job red. A caller that
#: omits ``continue-on-error`` trades a skipped optimisation for a failed
#: job, which is why the shipped recipe carries it.
FAULTS: Final = (
    OSError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    RuntimeError,
)

#: Reason used for every GitHub call that did not come back usable.
#: One string for all of them on purpose: ``gh_api`` collapses a missing
#: ``gh``, a bad token, a denied scope, a timeout and a 404 into the same
#: ``None``, so claiming to know which would be a guess. It names the
#: likely cause because a permanently dead optimisation looks identical to
#: a working one that never matches.
API_UNAVAILABLE: Final = "api unavailable (token/permissions?)"


@dataclass(frozen=True, kw_only=True)
class Decision:
    """One verdict, with the evidence or the reason behind it.

    Attributes:
        verified: True only when every check passed and the suite may be
            skipped.
        reason: Why the suite must run. Empty when `verified`.
        evidence: Named facts behind a `verified` verdict, printed so a
            reader can re-check the skip by hand.
    """

    verified: bool
    reason: str = ""
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Local:
    """What the checkout itself says, before any API call.

    Attributes:
        tree: HEAD's tree SHA.
        head: HEAD's commit SHA.
        parent: HEAD's first parent, the commit `main` was on before.
        slug: `owner/repo` from the origin remote.
    """

    tree: str
    head: str
    parent: str
    slug: str


@dataclass(frozen=True, kw_only=True)
class _Job:
    """The passing job a skip rests on.

    Attributes:
        name: The job's displayed name.
        run_id: The Actions run it belonged to, so a reader can open it.
        workflow: The workflow file that run came from.
    """

    name: str
    run_id: int
    workflow: str


def _run(reason: str) -> Decision:
    """Build a not-verified verdict.

    Args:
        reason: Why the suite must run.

    Returns:
        The verdict.
    """
    return Decision(verified=False, reason=reason)


def _api_json(*args: str) -> object | None:
    """Return a parsed `gh api` response, or None when anything failed.

    Args:
        *args: Trailing arguments after `gh api`.

    Returns:
        The decoded JSON, or None when the call failed or returned
        something that is not JSON. `gh_api` already collapses every
        transport failure to None; this extends that to an unparseable
        body. Callers narrow the type themselves, so the return is
        deliberately untyped rather than trusted.
    """
    raw = gh_api(*args)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("gh api returned non-JSON for %s", args[0] if args else "")
        return None


def _local_facts(root: Path) -> _Local | str:
    """Collect what the checkout can answer on its own.

    Args:
        root: Repo root.

    Returns:
        The facts, or a reason string when any of them is unavailable.
    """
    tree = get_tree_sha(root, "HEAD")
    if not tree:
        return "tree unresolved"
    head = run_git("rev-parse", "--verify", "HEAD", cwd=root, check=False)
    # `--verify` is load-bearing, not decoration: a bare
    # `rev-parse HEAD^1` on a root commit exits 128 *and echoes the
    # literal string `HEAD^1` on stdout, so the caller receives a
    # non-empty value that is not a SHA and sails past an emptiness
    # check. With `--verify` the failure leaves stdout empty.
    parent = run_git("rev-parse", "--verify", "HEAD^1", cwd=root, check=False)
    if not head or not parent:
        return "no merge parent (root commit or shallow clone?)"
    slug = github_slug(root)
    if not slug:
        return "no github remote"
    return _Local(tree=tree, head=head, parent=parent, slug=slug)


def _merged_pr_for(slug: str, head_sha: str) -> dict[str, object] | str:
    """Find the single merged pull request whose merge produced *head_sha*.

    Several pull requests can list one commit — a stacked child, a
    cherry-pick, one whose head happens to be this SHA — so the match is
    narrowed to the one GitHub recorded as having merged *into* this very
    commit. Anything ambiguous is refused rather than guessed.

    Args:
        slug: `owner/repo`.
        head_sha: The pushed commit.

    Returns:
        The pull-request object, or a reason string when there is not
        exactly one.
    """
    payload = _api_json(f"repos/{slug}/commits/{head_sha}/pulls")
    if not isinstance(payload, list):
        return API_UNAVAILABLE
    merged = [
        pr
        for pr in payload
        if isinstance(pr, dict)
        and pr.get("merged_at")
        and pr.get("merge_commit_sha") == head_sha
    ]
    if not merged:
        return "no merged pr for head"
    if len(merged) > 1:
        return "multiple prs"
    return merged[0]


def _pr_head_sha(pull: dict[str, object]) -> str | None:
    """Return the head commit SHA of *pull*, or None when absent.

    Args:
        pull: A pull-request object from the API.

    Returns:
        The SHA, or None when the payload lacked it.
    """
    head = pull.get("head")
    if not isinstance(head, dict):
        return None
    sha = head.get("sha")
    return sha if isinstance(sha, str) and sha else None


def _pr_head_origin(found: dict[str, object]) -> tuple[str, str] | None:
    """The repository and branch a pull request's head commit lived on.

    Needed because a workflow run is found by head SHA, and a SHA does
    not belong to one pull request: two pull requests can share a head
    commit with different bases, and a fork's pull request appears among
    this repository's runs. Each run tested the merge of *its own* base
    with that head, so only the run whose head matches this pull
    request's own branch is evidence about this merge.

    Args:
        found: The merged pull request object.

    Returns:
        ``(repository full name, branch)``, or None when the payload does
        not carry both — a deleted fork leaves ``repo`` null, which
        refuses rather than guesses.
    """
    head = found.get("head")
    if not isinstance(head, dict):
        return None
    ref = head.get("ref")
    repo = head.get("repo")
    if not isinstance(ref, str) or not isinstance(repo, dict):
        return None
    full_name = repo.get("full_name")
    return (full_name, ref) if isinstance(full_name, str) else None


def _head_tree(slug: str, pr_head: str) -> str | None:
    """Return the tree SHA of *pr_head*, read from the API.

    Read remotely on purpose: the pull request's branch is usually deleted
    on merge, so a fresh CI clone does not hold that commit even at full
    depth.

    Args:
        slug: `owner/repo`.
        pr_head: The pull request's head commit SHA.

    Returns:
        The tree SHA, or None when the call failed or the shape was not
        what the API documents.
    """
    payload = _api_json(f"repos/{slug}/commits/{pr_head}")
    if not isinstance(payload, dict):
        return None
    commit = payload.get("commit")
    if not isinstance(commit, dict):
        return None
    tree = commit.get("tree")
    if not isinstance(tree, dict):
        return None
    sha = tree.get("sha")
    return sha if isinstance(sha, str) and sha else None


def _parent_is_contained(slug: str, pr_head: str, parent_sha: str) -> bool | None:
    """Whether *parent_sha* is an ancestor of *pr_head*.

    This is what proves the pull-request run tested the head's own tree
    rather than a merge of it with a newer base: if the squash commit's
    parent is already in the head's history, every earlier base was too,
    so the merge ref GitHub tested carried the head's tree.

    Args:
        slug: `owner/repo`.
        pr_head: The pull request's head commit SHA.
        parent_sha: The squash commit's first parent.

    Returns:
        True when contained, False when not, None when undeterminable.
    """
    payload = _api_json(f"repos/{slug}/compare/{pr_head}...{parent_sha}")
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if not isinstance(status, str):
        return None
    return status in CONTAINED_STATUSES


def _workflow_path(workflow_ref: str | None) -> str | None:
    """Read the workflow file's path out of a ``$GITHUB_WORKFLOW_REF`` value.

    Args:
        workflow_ref: The runner's value, shaped
            ``owner/repo/.github/workflows/ci.yml@refs/heads/main``, or
            None when unset.

    Returns:
        The repository-relative workflow path, or None when the value is
        missing or not in that shape — which refuses rather than guesses.
    """
    if not workflow_ref:
        return None
    parts = workflow_ref.split("@", 1)[0].split("/", WORKFLOW_REF_PARTS - 1)
    if len(parts) != WORKFLOW_REF_PARTS:
        return None
    path = parts[-1]
    return path if path.endswith((".yml", ".yaml")) else None


def _run_is_this_pulls(
    run: dict[str, object],
    origin: tuple[str, str],
    pr_head: str,
) -> bool:
    """Whether *run* belongs to the pull request *origin* describes.

    The commit is re-checked here even though the query already asks the
    server for one ``head_sha``: a branch and a repository do not pin a
    commit, so an earlier push to the same branch matches both halves of
    the origin. Repeating the comparison locally means the guarantee is
    in this function rather than in a query parameter.

    Args:
        run: A workflow-run object from the API.
        origin: The pull request's ``(repository full name, branch)``.
        pr_head: The pull request's head commit SHA.

    Returns:
        True when the run's head commit, repository and branch are that
        pull request's. A run missing any of them is not this one.
    """
    full_name, ref = origin
    if run.get("head_sha") != pr_head or run.get("head_branch") != ref:
        return False
    repo = run.get("head_repository")
    return isinstance(repo, dict) and repo.get("full_name") == full_name


def _matching_run(
    slug: str,
    pr_head: str,
    workflow_path: str,
    origin: tuple[str, str],
) -> dict[str, object] | str:
    """Find this workflow's own pull-request run for *pr_head*.

    Asking the Actions API rather than the commit's check-runs is what
    ties the evidence to a job that really executed, for the reasons in
    the module docstring: a check-run's name and app identify nothing.

    Args:
        slug: `owner/repo`.
        pr_head: The pull request's head commit SHA.
        workflow_path: Repository-relative path of this workflow file.
        origin: The pull request's ``(repository full name, branch)``, so
            a run for the same commit on another pull request or a fork
            is not mistaken for this one.

    Returns:
        The newest matching run, or the reason there is none to trust.
    """
    payload = _api_json(
        f"repos/{slug}/actions/runs?head_sha={pr_head}&event=pull_request&per_page=100",
    )
    if not isinstance(payload, dict):
        return API_UNAVAILABLE
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return API_UNAVAILABLE
    total = payload.get("total_count")
    if not isinstance(total, int):
        # Without the count there is no way to know the page is whole,
        # and "fail-closed everywhere" means an unknown runs the suite.
        return API_UNAVAILABLE
    if total > len(runs):
        return "workflow runs truncated"
    mine = [
        r
        for r in runs
        if isinstance(r, dict)
        and r.get("path") == workflow_path
        and _run_is_this_pulls(r, origin, pr_head)
    ]
    if not mine:
        return f"no {workflow_path} run for this pull request's head"
    # Several entries mean the workflow was dispatched more than once for
    # that head; a re-run raises the attempt on one entry instead. The
    # newest is the verdict that stands, and `created_at` is ISO-8601 Z,
    # which sorts correctly as text.
    return max(mine, key=lambda r: str(r.get("created_at", "")))


def _run_jobs(slug: str, run_id: int) -> list[dict[str, object]] | str:
    """List the job objects of run *run_id*, or the reason the list is unusable.

    Args:
        slug: `owner/repo`.
        run_id: The workflow run whose jobs to read.

    Returns:
        The job objects, or a reason string when the listing is missing,
        malformed or truncated.
    """
    payload = _api_json(f"repos/{slug}/actions/runs/{run_id}/jobs?per_page=100")
    if not isinstance(payload, dict):
        return API_UNAVAILABLE
    jobs = payload.get("jobs")
    total = payload.get("total_count")
    if not isinstance(jobs, list) or not isinstance(total, int):
        return API_UNAVAILABLE
    if total > len(jobs):
        # A second page exists. Paging would be easy; trusting a partial
        # list would not, since the missing page may hold the failure.
        return "jobs truncated"
    return [j for j in jobs if isinstance(j, dict)]


def _job_verdict(slug: str, run_id: int, check_name: str) -> str | None:
    """Judge the job called *check_name* inside run *run_id*.

    Args:
        slug: `owner/repo`.
        run_id: The workflow run whose jobs to read. It already pins the
            commit, so no SHA is needed here.
        check_name: The job's displayed name, which is the job id only
            when the job sets no ``name:`` and has no matrix.

    Returns:
        None when it passed, otherwise the reason it did not.
    """
    jobs = _run_jobs(slug, run_id)
    if isinstance(jobs, str):
        return jobs
    named = [j for j in jobs if j.get("name") == check_name]
    if not named:
        return f"job {check_name} not found"
    for job in named:
        if job.get("status") != "completed":
            return "jobs still running"
        if job.get("conclusion") != "success":
            return f"job {check_name} not success ({job.get('conclusion')})"
    return None


def _job_evidence(
    slug: str,
    pr_head: str,
    *,
    check_name: str | None,
    workflow_path: str | None,
    origin: tuple[str, str],
) -> _Job | str:
    """Establish that a named job of this workflow passed on *pr_head*.

    Args:
        slug: `owner/repo`.
        pr_head: The pull request's head commit SHA.
        check_name: The job that must have passed. A falsy name refuses
            rather than falling back to "some check succeeded": that
            weaker question is satisfied by a workflow which skipped its
            own test job while an unrelated check went green. An empty
            name arrives by accident (`--check-name ""`, or `GITHUB_JOB`
            unset off Actions), so it must not be the permissive case.
        workflow_path: This workflow's path. Falsy refuses for the same
            reason: without it, any workflow's job of that name counts.
        origin: The pull request's ``(repository full name, branch)``,
            passed through to :func:`_matching_run`.

    Returns:
        The passing job, or the reason there is none.
    """
    if not check_name:
        return "no check name"
    if not workflow_path:
        return "no workflow path (GITHUB_WORKFLOW_REF unset?)"
    run = _matching_run(slug, pr_head, workflow_path, origin)
    if isinstance(run, str):
        return run
    run_id = run.get("id")
    if not isinstance(run_id, int):
        return API_UNAVAILABLE
    failed = _job_verdict(slug, run_id, check_name)
    if failed:
        return failed
    return _Job(name=check_name, run_id=run_id, workflow=workflow_path)


def _same_tested_tree(local: _Local, pr_head: str) -> str | None:
    """Whether *pr_head* was tested at this checkout's tree.

    Two conditions, and both are needed. Equal trees say the content is
    the same; containment says the pull-request run actually read that
    content rather than a merge of it with a newer base.

    Args:
        local: Facts from the checkout.
        pr_head: The pull request's head commit SHA.

    Returns:
        None when the tested tree was this one, otherwise the reason it
        was not.
    """
    pr_tree = _head_tree(local.slug, pr_head)
    if not pr_tree:
        return API_UNAVAILABLE
    if pr_tree != local.tree:
        return (
            "pr head tree differs "
            f"({pr_tree[:SHA_PREFIX]} != {local.tree[:SHA_PREFIX]})"
        )
    contained = _parent_is_contained(local.slug, pr_head, local.parent)
    if contained is None:
        return API_UNAVAILABLE
    if not contained:
        return "pr head does not contain merge parent"
    return None


def _against_pull(
    local: _Local,
    *,
    check_name: str | None,
    workflow_path: str | None,
) -> Decision:
    """Compare the checkout with the pull request that landed it.

    Args:
        local: Facts from the checkout.
        check_name: The job that must have passed on the PR head.
        workflow_path: This workflow's repository-relative path.

    Returns:
        The verdict.
    """
    found = _merged_pr_for(local.slug, local.head)
    if isinstance(found, str):
        return _run(found)
    pr_head = _pr_head_sha(found)
    if not pr_head:
        return _run(API_UNAVAILABLE)

    differs = _same_tested_tree(local, pr_head)
    if differs:
        return _run(differs)

    origin = _pr_head_origin(found)
    if origin is None:
        return _run("pull request head origin unknown")

    job = _job_evidence(
        local.slug,
        pr_head,
        check_name=check_name,
        workflow_path=workflow_path,
        origin=origin,
    )
    if isinstance(job, str):
        return _run(job)

    return Decision(
        verified=True,
        evidence={
            "tree": local.tree[:SHA_PREFIX],
            "pr": f"#{found.get('number')}",
            "head": pr_head[:SHA_PREFIX],
            "workflow": job.workflow,
            "run": str(job.run_id),
            "job": job.name,
            "concluded": "success",
            "parent-contained": "yes",
        },
    )


def decide(
    root: Path,
    *,
    check_name: str | None,
    workflow_path: str | None,
    event_name: str | None,
    ci: bool,
) -> Decision:
    """Decide whether HEAD's tree has already passed its suite.

    Every input is injected rather than read from the environment, so the
    decision is testable without one.

    Args:
        root: Repo root.
        check_name: Job that must have concluded success on the pull
            request's head. A falsy name refuses: see
            :func:`_job_evidence`.
        workflow_path: Repository-relative path of the workflow whose job
            that is. A falsy path refuses, for the same reason.
        event_name: The CI event. Only a push is eligible: on a
            pull-request run the checkout is a synthetic merge commit and
            the question is meaningless.
        ci: Whether this is a CI run. A local run never skips — it would
            be trusting a remote pull request's check for a working tree
            nobody verified.

    Returns:
        The verdict, with evidence on a skip and a specific reason
        otherwise.
    """
    if not ci:
        return _run("not ci")
    if event_name != "push":
        return _run("event is not push")
    local = _local_facts(root)
    if isinstance(local, str):
        return _run(local)
    return _against_pull(
        local,
        check_name=check_name,
        workflow_path=workflow_path,
    )


def decision_line(decision: Decision) -> str:
    """Render the one line a reader scans in a CI log.

    It states the verdict, never the consequence. This command decides
    nothing: the workflow does, by reading the output, and it may not be
    reading it at all (``--github-output`` absent, the file unwritable, a
    local run), so a line promising a skip would be false in each of
    those cases.

    Args:
        decision: The verdict.

    Returns:
        The line, without a trailing newline.
    """
    if decision.verified:
        facts = " ".join(f"{k}={v}" for k, v in decision.evidence.items())
        return f"tree-verified: verified=true {facts} (suite may be skipped)"
    return f"tree-verified: verified=false reason={decision.reason}"


def _write_github_output(decision: Decision) -> None:
    """Append ``verified`` to ``$GITHUB_OUTPUT`` when that file is set.

    Args:
        decision: The verdict.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"verified={'true' if decision.verified else 'false'}\n")
    except OSError as exc:
        # The workflow compares against 'true', so a missing output reads
        # as "run the suite" — the safe direction.
        logger.warning("could not write GITHUB_OUTPUT: %s", exc)


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="forge-tree-verified",
        description=(
            "Answer whether HEAD's tree already passed its suite in the pull "
            "request that landed it, so CI can skip a redundant run."
        ),
    )
    parser.add_argument(
        "--check-name",
        default=os.environ.get("GITHUB_JOB"),
        help=(
            "Job that must have concluded success on the PR head "
            "(default: $GITHUB_JOB, i.e. this job's own earlier run). "
            "An empty or unset name refuses rather than accepting any "
            "check. It is matched against the job's DISPLAYED name."
        ),
    )
    parser.add_argument(
        "--workflow",
        default=None,
        help=(
            "Repository-relative path of the workflow that job belongs to, "
            "e.g. .github/workflows/ci.yml (default: parsed from "
            "$GITHUB_WORKFLOW_REF). Refuses when neither is available."
        ),
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="Append verified=true|false to $GITHUB_OUTPUT as well as stdout.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the verdict as JSON instead of the one-line form.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Print whether HEAD's tree has already passed, and why.

    Exits 0 whatever the verdict: this answers a question rather than
    gating, so a non-zero "not verified" would fail every ordinary run.
    That is a deliberate difference from ``forge-precommit --verdict``,
    which does exit non-zero on its negative. A fault in ``FAULTS`` is
    caught and reported as
    not-verified, because a crash must never read as permission to skip
    tests; any other exception ends the step, which the workflow's
    ``continue-on-error`` turns into a suite run.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        0 always, except for an argparse usage error.
    """
    args = _parser().parse_args(argv)
    try:
        decision = decide(
            repo_root(),
            check_name=args.check_name,
            workflow_path=(
                args.workflow or _workflow_path(os.environ.get("GITHUB_WORKFLOW_REF"))
            ),
            event_name=os.environ.get("GITHUB_EVENT_NAME"),
            ci=is_ci(),
        )
    except FAULTS as exc:
        decision = _run(f"internal error: {type(exc).__name__}")
        logger.debug("tree gate raised", exc_info=True)

    if args.as_json:
        emit(
            json.dumps(
                {
                    "verified": decision.verified,
                    "reason": decision.reason,
                    "evidence": decision.evidence,
                },
                indent=2,
            ),
        )
    else:
        emit(decision_line(decision))
    if args.github_output:
        _write_github_output(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
