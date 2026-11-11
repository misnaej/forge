"""Tests for ``forge-tree-verified`` — may CI skip a suite its PR already ran."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge import ci_tree_gate as mod
from tests.conftest import commit_all, git_stdout, init_git_repo


if TYPE_CHECKING:
    from collections.abc import Callable


# MOCKING STRATEGY: GitHub is the only fake, shaped after
# ``tests/test_pr_metrics.py``. ``gh_api`` on ``forge.ci_tree_gate`` is
# replaced by :class:`FakeGitHub`, which answers the five endpoints the gate
# calls (pulls, commit, compare, Actions runs, run jobs) from an in-memory
# dict and raises ``AssertionError`` for any other, so a new unvetted call
# (the old check-runs endpoint included) cannot hide. It returns raw strings
# (or ``None`` for unreachable), so ``json.loads`` and its failure branch run
# for real. The repo is real git (HEAD's tree, SHA and parent are genuine);
# only the origin slug is fixed to ``acme/widget``. The repo fixture is
# module-scoped, the fake function-scoped.
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
SLUG = "acme/widget"
PR_HEAD = "b" * 40
OTHER_TREE = "f" * 40
PR_NUMBER = 7
PR_BRANCH = "feat/widget-change"
OTHER_BRANCH = "feat/another-change"
FORK_SLUG = "fork-owner/widget"
# Run ids differ from PR_NUMBER so an id/number mix-up shows in the evidence.
RUN_ID = 4242
OLDER_RUN_ID = 9000  # largest id on purpose: max-by-id must not win
FOREIGN_RUN_ID = 100
LATE_LISTED_RUN_ID = (
    50  # listed last but oldest by created_at: a last-pick must not win
)
# A pull_request run's `path` is the bare file path, with no `@ref` suffix.
WORKFLOW_PATH = ".github/workflows/ci.yml"
OTHER_WORKFLOW_PATH = ".github/workflows/other.yml"
WORKFLOW_REF = f"{SLUG}/{WORKFLOW_PATH}@refs/heads/main"
API_DOWN = mod.API_UNAVAILABLE
NO_RUN_REASON = f"no {WORKFLOW_PATH} run for this pull request's head"
ORIGIN_UNKNOWN = "pull request head origin unknown"
RUNS_ROUTE = f"actions/runs?head_sha={PR_HEAD}&event=pull_request&per_page=100"


@dataclass(frozen=True)
class RepoFacts:
    """Real SHAs of the squash-commit repo's HEAD."""

    head: str
    parent: str
    tree: str


@dataclass(frozen=True)
class Raw:
    """A response body returned verbatim instead of JSON-encoded."""

    text: str


def _workflow_run(
    run_id: int | str,
    *,
    path: str = WORKFLOW_PATH,
    created_at: str = "2026-01-01T00:00:00Z",
    head_branch: str = PR_BRANCH,
    head_repo: str = SLUG,
) -> dict[str, object]:
    """Build one workflow-run object as the runs endpoint returns it.

    Args:
        run_id: The run's id.
        path: The workflow file path the run came from.
        created_at: ISO-8601 creation time.
        head_branch: The branch the run's head commit was on.
        head_repo: Full name of the repository holding that branch.

    Returns:
        The run object.
    """
    return {
        "id": run_id,
        "path": path,
        "created_at": created_at,
        "head_branch": head_branch,
        "head_repository": {"full_name": head_repo},
    }


def _run_without(field: str) -> dict[str, object]:
    """Build the happy run with one field absent.

    Args:
        field: The run key to drop.

    Returns:
        The run object, minus *field*.
    """
    return {k: v for k, v in _workflow_run(RUN_ID).items() if k != field}


def _job(
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, object]:
    """Build one job object as the run-jobs endpoint returns it.

    Args:
        name: The job's displayed name.
        status: The job's status.
        conclusion: The job's conclusion, or None while it runs.

    Returns:
        The job object.
    """
    return {"name": name, "status": status, "conclusion": conclusion}


def _jobs_route(run_id: int) -> str:
    """Return the run-jobs endpoint tail for *run_id*.

    Args:
        run_id: The run whose jobs endpoint is meant.

    Returns:
        The endpoint path after ``repos/<slug>/``.
    """
    return f"actions/runs/{run_id}/jobs?per_page=100"


class FakeGitHub:
    """Answers the gate's five ``gh api`` endpoints from an in-memory table."""

    def __init__(self, facts: RepoFacts) -> None:
        """Build the happy-path table from *facts*.

        Args:
            facts: Real SHAs of the repo under test.
        """
        self.calls: list[str] = []
        self.routes: dict[str, str] = {
            f"commits/{facts.head}/pulls": "pulls",
            f"commits/{PR_HEAD}": "commit",
            f"compare/{PR_HEAD}...{facts.parent}": "compare",
            RUNS_ROUTE: "runs",
            _jobs_route(RUN_ID): "jobs",
        }
        self.payloads: dict[str, object] = {
            "pulls": [
                {
                    "number": PR_NUMBER,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "merge_commit_sha": facts.head,
                    "head": {
                        "sha": PR_HEAD,
                        "ref": PR_BRANCH,
                        "repo": {"full_name": SLUG},
                    },
                },
            ],
            "commit": {"commit": {"tree": {"sha": facts.tree}}},
            "compare": {"status": "behind"},
            "runs": self.runs_for([_workflow_run(RUN_ID)]),
            "jobs": self.jobs_for([_job("ci"), _job("lint")]),
        }

    @staticmethod
    def runs_for(runs: list[dict[str, object]]) -> dict[str, object]:
        """Wrap *runs* as a complete workflow-runs response.

        Args:
            runs: Workflow-run objects.

        Returns:
            The payload, with a ``total_count`` matching *runs*.
        """
        return {"total_count": len(runs), "workflow_runs": runs}

    @staticmethod
    def jobs_for(jobs: list[dict[str, object]]) -> dict[str, object]:
        """Wrap *jobs* as a complete run-jobs response.

        Args:
            jobs: Job objects.

        Returns:
            The payload, with a ``total_count`` matching *jobs*.
        """
        return {"total_count": len(jobs), "jobs": jobs}

    def add_jobs(self, run_id: int, jobs: list[dict[str, object]]) -> None:
        """Register a jobs endpoint for a run other than the happy one.

        Args:
            run_id: The run whose jobs to serve.
            jobs: Job objects for that run.
        """
        self.routes[_jobs_route(run_id)] = f"jobs-{run_id}"
        self.payloads[f"jobs-{run_id}"] = self.jobs_for(jobs)

    @property
    def pull(self) -> dict[str, object]:
        """The single merged PR of the happy table."""
        pulls = self.payloads["pulls"]
        assert isinstance(pulls, list)
        return pulls[0]

    def __call__(self, *args: str, **_kw: object) -> str | None:
        """Stand in for :func:`forge.git_utils.gh_api`.

        Args:
            *args: The ``gh api`` argv tail.
            **_kw: Ignored (timeout).

        Returns:
            The raw body, or ``None`` for an unreachable endpoint.
        """
        endpoint = next(a for a in args if a.startswith("repos/"))
        tail = endpoint.removeprefix(f"repos/{SLUG}/")
        assert tail in self.routes, f"unvetted gh api call: {tail}"
        self.calls.append(tail)
        payload = self.payloads[self.routes[tail]]
        if payload is None:
            return None
        if isinstance(payload, Raw):
            return payload.text
        return json.dumps(payload)


def _replace(key: str, value: object) -> Callable[[FakeGitHub], None]:
    """Return a mutation that swaps the payload stored under *key*.

    Args:
        key: The payload table key to overwrite.
        value: The replacement payload.

    Returns:
        The mutation, applied to a fake.
    """

    def mutate(fake: FakeGitHub) -> None:
        fake.payloads[key] = value

    return mutate


def _edit_pull(**fields: object) -> Callable[[FakeGitHub], None]:
    """Return a mutation that updates fields of the happy merged PR.

    Args:
        **fields: Pull-request fields to overwrite.

    Returns:
        The mutation, applied to a fake.
    """

    def mutate(fake: FakeGitHub) -> None:
        fake.pull.update(fields)

    return mutate


def _set_jobs(jobs: list[dict[str, object]]) -> Callable[[FakeGitHub], None]:
    """Return a mutation that replaces the happy run's jobs.

    Args:
        jobs: Job objects for the happy run.

    Returns:
        The mutation, applied to a fake.
    """
    return _replace("jobs", FakeGitHub.jobs_for(jobs))


def _set_workflow_runs(
    runs: list[dict[str, object]],
) -> Callable[[FakeGitHub], None]:
    """Return a mutation that replaces the workflow runs for the head.

    Args:
        runs: Workflow-run objects for the head.

    Returns:
        The mutation, applied to a fake.
    """
    return _replace("runs", FakeGitHub.runs_for(runs))


def _add_second_merged_pr(fake: FakeGitHub) -> None:
    """Append a second merged PR listing the same merge commit.

    Args:
        fake: The fake whose pulls table to extend.
    """
    pulls = fake.payloads["pulls"]
    assert isinstance(pulls, list)
    pulls.append({**fake.pull, "number": PR_NUMBER + 1})


def _only_a_foreign_workflow_ran(fake: FakeGitHub) -> None:
    """Leave only a green run of another workflow file for the head.

    Args:
        fake: The fake whose runs and jobs to replace.
    """
    # Its green jobs are served too, so a dropped path filter yields
    # verified=true rather than an unrelated AssertionError.
    _set_workflow_runs(
        [_workflow_run(FOREIGN_RUN_ID, path=OTHER_WORKFLOW_PATH)],
    )(fake)
    fake.add_jobs(FOREIGN_RUN_ID, [_job("ci")])


@pytest.fixture(scope="module")
def squash_commit_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real two-commit repo: HEAD is the "squash", its parent the old main.

    Returns:
        The repo root.
    """
    repo = tmp_path_factory.mktemp("squash_commit_repo")
    init_git_repo(repo)
    (repo / "feature.txt").write_text("landed\n", encoding="utf-8")
    commit_all(repo, "feat: landed")
    return repo


@pytest.fixture(scope="module")
def squash_commit_facts(squash_commit_repo: Path) -> RepoFacts:
    """HEAD, HEAD's parent and HEAD's tree, read once from real git.

    Returns:
        The facts.
    """
    return RepoFacts(
        head=git_stdout(squash_commit_repo, "rev-parse", "HEAD").strip(),
        parent=git_stdout(squash_commit_repo, "rev-parse", "HEAD^1").strip(),
        tree=git_stdout(squash_commit_repo, "rev-parse", "HEAD^{tree}").strip(),
    )


@pytest.fixture
def merged_pr_with_green_ci_run(
    squash_commit_facts: RepoFacts,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeGitHub:
    """A fake GitHub where one merged PR with a green ``ci`` job landed HEAD.

    Returns:
        The fake, installed as ``gh_api``; mutate it to break one fact.
    """
    fake = FakeGitHub(squash_commit_facts)
    monkeypatch.setattr(mod, "gh_api", fake)
    monkeypatch.setattr(mod, "github_slug", lambda _root: SLUG)
    return fake


@pytest.fixture
def ci_push_checkout(
    squash_commit_repo: Path,
    merged_pr_with_green_ci_run: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeGitHub:
    """Environment of a CI push run in the squash repo, nothing leaking in.

    This suite can itself run inside Actions, where the real ``GITHUB_JOB``
    and ``GITHUB_WORKFLOW_REF`` are set; both are replaced or cleared so no
    test depends on the host's values.

    Returns:
        The happy fake, for ``main`` tests to mutate.
    """
    for name in ("GITHUB_JOB", "GITHUB_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", WORKFLOW_REF)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setattr(mod, "repo_root", lambda: squash_commit_repo)
    return merged_pr_with_green_ci_run


def _decide(
    root: Path,
    check_name: str | None = "ci",
    workflow_path: str | None = WORKFLOW_PATH,
) -> mod.Decision:
    """Run the gate as a CI push run of this workflow.

    Args:
        root: Repo root to decide for.
        check_name: The job that must have passed.
        workflow_path: Repository-relative path of this workflow.

    Returns:
        The gate's verdict.
    """
    return mod.decide(
        root,
        check_name=check_name,
        workflow_path=workflow_path,
        event_name="push",
        ci=True,
    )


@pytest.mark.parametrize(
    ("ci", "event_name", "expected_reason"),
    [
        pytest.param(False, "push", "not ci", id="not-ci"),
        pytest.param(True, "pull_request", "event is not push", id="not-push"),
    ],
)
def test_ineligible_run_exits_before_any_network(
    squash_commit_repo: Path,
    merged_pr_with_green_ci_run: FakeGitHub,
    *,
    ci: bool,
    event_name: str,
    expected_reason: str,
) -> None:
    """SCENARIO: A local run or a non-push event never reaches the API.

    Args:
        ci: Injected CI flag.
        event_name: Injected event.
        expected_reason: The reason the suite must run.
    """
    decision = mod.decide(
        squash_commit_repo,
        check_name="ci",
        workflow_path=WORKFLOW_PATH,
        event_name=event_name,
        ci=ci,
    )

    assert not decision.verified
    assert decision.reason == expected_reason
    assert merged_pr_with_green_ci_run.calls == []


def test_root_commit_has_no_merge_parent(
    tmp_path: Path,
    merged_pr_with_green_ci_run: FakeGitHub,
) -> None:
    """SCENARIO: A one-commit repo has no parent to compare against."""
    init_git_repo(tmp_path)

    decision = _decide(tmp_path)

    assert decision.reason == "no merge parent (root commit or shallow clone?)"
    assert merged_pr_with_green_ci_run.calls == []


@pytest.mark.parametrize("compare_status", ["behind", "identical"])
def test_verified_tree_skips_suite_with_full_evidence(
    squash_commit_repo: Path,
    squash_commit_facts: RepoFacts,
    merged_pr_with_green_ci_run: FakeGitHub,
    compare_status: str,
) -> None:
    """SCENARIO: Every fact lines up, so the suite is skipped.

    Both contained statuses must verify, so a "fix" rejecting everything
    fails here. The run id is pinned as a string, as the decision line
    prints it.

    Args:
        compare_status: GitHub's compare status for the parent ancestry.
    """
    merged_pr_with_green_ci_run.payloads["compare"] = {"status": compare_status}

    decision = _decide(squash_commit_repo)

    assert decision.verified
    assert decision.reason == ""
    assert decision.evidence == {
        "tree": squash_commit_facts.tree[:12],
        "pr": f"#{PR_NUMBER}",
        "head": PR_HEAD[:12],
        "workflow": WORKFLOW_PATH,
        "run": str(RUN_ID),
        "job": "ci",
        "concluded": "success",
        "parent-contained": "yes",
    }
    assert mod.decision_line(decision).endswith("(suite may be skipped)")


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        pytest.param(_replace("pulls", None), API_DOWN, id="pulls-unreachable"),
        pytest.param(_replace("pulls", Raw("<html>")), API_DOWN, id="pulls-non-json"),
        pytest.param(_replace("pulls", {"message": "x"}), API_DOWN, id="pulls-dict"),
        pytest.param(_replace("pulls", []), "no merged pr for head", id="pulls-empty"),
        pytest.param(
            _edit_pull(merged_at=None),
            "no merged pr for head",
            id="pr-not-merged",
        ),
        pytest.param(
            _edit_pull(merge_commit_sha="c" * 40),
            "no merged pr for head",
            id="pr-merged-elsewhere",
        ),
        pytest.param(_add_second_merged_pr, "multiple prs", id="two-merged-prs"),
        pytest.param(_edit_pull(head={}), API_DOWN, id="pr-without-head-sha"),
        pytest.param(_replace("commit", None), API_DOWN, id="head-commit-unreachable"),
        pytest.param(
            _replace("commit", {"commit": {"tree": {"sha": OTHER_TREE}}}),
            "pr head tree differs ({other} != {tree})",
            id="tree-differs",
        ),
        pytest.param(_replace("compare", None), API_DOWN, id="compare-unreachable"),
        pytest.param(_replace("compare", {}), API_DOWN, id="compare-no-status"),
        pytest.param(
            _replace("compare", {"status": "ahead"}),
            "pr head does not contain merge parent",
            id="compare-ahead",
        ),
        pytest.param(
            _replace("compare", {"status": "diverged"}),
            "pr head does not contain merge parent",
            id="compare-diverged",
        ),
        pytest.param(_replace("runs", None), API_DOWN, id="runs-unreachable"),
        pytest.param(
            _replace("runs", {"total_count": 0, "workflow_runs": "nope"}),
            API_DOWN,
            id="runs-not-list",
        ),
        pytest.param(
            _replace(
                "runs",
                {"total_count": 9, "workflow_runs": [_workflow_run(RUN_ID)]},
            ),
            "workflow runs truncated",
            id="runs-truncated",
        ),
        pytest.param(
            _replace("runs", {"workflow_runs": [_workflow_run(RUN_ID)]}),
            API_DOWN,
            id="runs-without-total-count",
        ),
        pytest.param(
            _only_a_foreign_workflow_ran,
            NO_RUN_REASON,
            id="runs-only-foreign-workflow",
        ),
        pytest.param(
            _set_workflow_runs([_workflow_run(RUN_ID, head_branch=OTHER_BRANCH)]),
            NO_RUN_REASON,
            id="run-of-another-pr-sharing-the-head-commit",
        ),
        pytest.param(
            _set_workflow_runs([_workflow_run(RUN_ID, head_repo=FORK_SLUG)]),
            NO_RUN_REASON,
            id="run-from-a-fork",
        ),
        pytest.param(
            _set_workflow_runs([_run_without("head_repository")]),
            NO_RUN_REASON,
            id="run-without-head-repository",
        ),
        pytest.param(
            _set_workflow_runs([_run_without("head_branch")]),
            NO_RUN_REASON,
            id="run-without-head-branch",
        ),
        pytest.param(
            _set_workflow_runs([_workflow_run("4242")]),
            API_DOWN,
            id="run-id-not-int",
        ),
        pytest.param(_replace("jobs", None), API_DOWN, id="jobs-unreachable"),
        pytest.param(
            _replace("jobs", {"total_count": 0, "jobs": "nope"}),
            API_DOWN,
            id="jobs-not-list",
        ),
        pytest.param(
            _replace("jobs", {"total_count": 9, "jobs": [_job("ci")]}),
            "jobs truncated",
            id="jobs-truncated",
        ),
        pytest.param(
            _replace("jobs", {"jobs": [_job("ci")]}),
            API_DOWN,
            id="jobs-without-total-count",
        ),
        pytest.param(
            _set_jobs([_job("lint")]),
            "job ci not found",
            id="named-job-absent",
        ),
        pytest.param(
            _set_jobs([_job("ci", status="in_progress", conclusion=None)]),
            "jobs still running",
            id="named-job-running",
        ),
        pytest.param(
            _set_jobs([_job("ci", conclusion="failure"), _job("lint")]),
            "job ci not success (failure)",
            id="named-job-failed",
        ),
        pytest.param(
            _set_jobs([_job("ci"), _job("ci", conclusion="failure")]),
            "job ci not success (failure)",
            id="named-job-duplicate-name-one-failed",
        ),
    ],
)
def test_decide_runs_suite_when(
    squash_commit_repo: Path,
    squash_commit_facts: RepoFacts,
    merged_pr_with_green_ci_run: FakeGitHub,
    mutation: Callable[[FakeGitHub], None],
    expected_reason: str,
) -> None:
    """SCENARIO: Breaking exactly one fact of the happy table runs the suite.

    MOCK SETUP: The happy fake, with *mutation* altering one field.
    EXPECTED BEHAVIOR: Not verified, with the one reason for that fact.

    Args:
        mutation: Edit applied to the happy fake.
        expected_reason: The reason the suite must run.
    """
    mutation(merged_pr_with_green_ci_run)

    decision = _decide(squash_commit_repo)

    assert not decision.verified
    assert decision.reason == expected_reason.format(
        other=OTHER_TREE[:12],
        tree=squash_commit_facts.tree[:12],
    )


def test_newest_run_of_this_workflow_is_the_verdict(
    squash_commit_repo: Path,
    merged_pr_with_green_ci_run: FakeGitHub,
) -> None:
    """SCENARIO: Several runs are listed; only this workflow's newest counts.

    MOCK SETUP: Four runs in this order: a foreign workflow's, newest by
    ``created_at``, green; this workflow's, oldest of the early two and with
    the largest id, green; this workflow's, middle by ``created_at``,
    failing; this workflow's, listed last but oldest overall, green.
    EXPECTED BEHAVIOR: Not verified with the failing run's reason. A missing
    path filter, a max taken before the filter, a first pick, a last pick, a
    max-by-id or a min each lands on a green run and verifies.
    """
    fake = merged_pr_with_green_ci_run
    _set_workflow_runs(
        [
            _workflow_run(
                FOREIGN_RUN_ID,
                path=OTHER_WORKFLOW_PATH,
                created_at="2026-03-01T00:00:00Z",
                head_branch=OTHER_BRANCH,
            ),
            _workflow_run(OLDER_RUN_ID, created_at="2026-01-01T00:00:00Z"),
            _workflow_run(RUN_ID, created_at="2026-02-01T00:00:00Z"),
            _workflow_run(LATE_LISTED_RUN_ID, created_at="2025-12-01T00:00:00Z"),
        ],
    )(fake)
    fake.add_jobs(FOREIGN_RUN_ID, [_job("ci")])
    fake.add_jobs(OLDER_RUN_ID, [_job("ci")])
    fake.add_jobs(LATE_LISTED_RUN_ID, [_job("ci")])
    fake.add_jobs(RUN_ID, [_job("ci", conclusion="failure")])

    decision = _decide(squash_commit_repo)

    assert not decision.verified
    assert decision.reason == "job ci not success (failure)"


@pytest.mark.parametrize(
    "head",
    [
        pytest.param({"sha": PR_HEAD, "ref": PR_BRANCH}, id="head-without-repo"),
        pytest.param(
            {"sha": PR_HEAD, "ref": PR_BRANCH, "repo": None},
            id="head-repo-deleted-fork",
        ),
        pytest.param(
            {"sha": PR_HEAD, "ref": PR_BRANCH, "repo": {}},
            id="head-repo-without-full-name",
        ),
        pytest.param(
            {"sha": PR_HEAD, "repo": {"full_name": SLUG}},
            id="head-without-ref",
        ),
    ],
)
def test_unknown_pull_request_origin_refuses_without_asking_actions(
    squash_commit_repo: Path,
    merged_pr_with_green_ci_run: FakeGitHub,
    head: dict[str, object],
) -> None:
    """SCENARIO: A PR payload missing its head repo or branch cannot verify.

    MOCK SETUP: The default green runs and jobs; the PR's ``head`` altered.
    EXPECTED BEHAVIOR: Not verified with the origin reason, and no Actions
    endpoint is called: without an origin no run can be tied to this PR.

    Args:
        head: The PR payload's ``head`` object.
    """
    _edit_pull(head=head)(merged_pr_with_green_ci_run)

    decision = _decide(squash_commit_repo)

    assert not decision.verified
    assert decision.reason == ORIGIN_UNKNOWN
    assert not any(c.startswith("actions/") for c in merged_pr_with_green_ci_run.calls)


@pytest.mark.parametrize(
    ("check_name", "workflow_path", "expected_reason"),
    [
        pytest.param("", WORKFLOW_PATH, "no check name", id="empty-check-name"),
        pytest.param(None, WORKFLOW_PATH, "no check name", id="none-check-name"),
        pytest.param(
            "ci",
            "",
            "no workflow path (GITHUB_WORKFLOW_REF unset?)",
            id="empty-workflow-path",
        ),
        pytest.param(
            "ci",
            None,
            "no workflow path (GITHUB_WORKFLOW_REF unset?)",
            id="none-workflow-path",
        ),
    ],
)
def test_missing_check_name_or_workflow_refuses_without_asking_actions(
    squash_commit_repo: Path,
    merged_pr_with_green_ci_run: FakeGitHub,
    check_name: str | None,
    workflow_path: str | None,
    expected_reason: str,
) -> None:
    """SCENARIO: An unset job name or workflow path can never verify.

    Both falsy spellings of each are rows, so an ``is None`` test (which
    misses ``""``) fails here.

    MOCK SETUP: The default green runs and jobs; one input empty or ``None``.
    EXPECTED BEHAVIOR: Not verified with that input's reason, and no Actions
    endpoint is called.

    Args:
        check_name: The check name passed to ``decide``.
        workflow_path: The workflow path passed to ``decide``.
        expected_reason: The reason the suite must run.
    """
    decision = _decide(
        squash_commit_repo,
        check_name=check_name,
        workflow_path=workflow_path,
    )

    assert not decision.verified
    assert decision.reason == expected_reason
    assert not any(c.startswith("actions/") for c in merged_pr_with_green_ci_run.calls)


@pytest.mark.parametrize(
    ("workflow_ref", "expected"),
    [
        pytest.param(
            "acme/widget/.github/workflows/ci.yml@refs/heads/feat/x",
            WORKFLOW_PATH,
            id="yml-with-slashed-ref",
        ),
        pytest.param(
            "acme/widget/.github/workflows/ci.yaml@refs/heads/main",
            ".github/workflows/ci.yaml",
            id="yaml-kept",
        ),
        pytest.param(None, None, id="unset"),
        pytest.param("", None, id="empty"),
        pytest.param("acme/widget@refs/heads/x", None, id="no-path"),
        pytest.param(
            "acme/widget/.github/workflows/ci.txt@refs/heads/main",
            None,
            id="not-a-workflow-file",
        ),
    ],
)
def test_workflow_path_is_read_from_the_workflow_ref(
    workflow_ref: str | None,
    expected: str | None,
) -> None:
    """SCENARIO: ``$GITHUB_WORKFLOW_REF`` yields a path, or nothing.

    Args:
        workflow_ref: The runner's value.
        expected: The repository-relative path, or ``None`` to refuse.
    """
    assert mod._workflow_path(workflow_ref) == expected


def test_skipped_job_is_not_a_passing_job(
    squash_commit_repo: Path,
    merged_pr_with_green_ci_run: FakeGitHub,
) -> None:
    """SCENARIO: A bot or draft PR skips ``ci`` while unrelated jobs pass.

    MOCK SETUP: ``ci`` completed with conclusion ``skipped``; ``lint``
    success. Its status stays ``completed``, else "jobs still running" fires
    first.
    EXPECTED BEHAVIOR: Not verified; the named job is judged on its own.
    """
    _set_jobs([_job("ci", conclusion="skipped"), _job("lint")])(
        merged_pr_with_green_ci_run,
    )

    decision = _decide(squash_commit_repo)

    assert not decision.verified
    assert decision.reason == "job ci not success (skipped)"


def test_unset_github_job_refuses_through_main(
    ci_push_checkout: FakeGitHub,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: No flag and no ``$GITHUB_JOB`` — the accidental CI path.

    MOCK SETUP: ``ci_push_checkout`` already clears ``GITHUB_JOB``; default
    green runs.
    EXPECTED BEHAVIOR: ``main`` prints ``reason=no check name``, never
    ``verified=true``, pinning the argv/env wiring into ``decide``.
    """
    assert mod.main([]) == 0

    out = capsys.readouterr().out
    assert "reason=no check name" in out
    assert "verified=true" not in out


def test_main_env_defaults_alone_verify(
    ci_push_checkout: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: ``$GITHUB_JOB`` and ``$GITHUB_WORKFLOW_REF`` need no flags.

    MOCK SETUP: ``GITHUB_JOB=ci`` on top of the fixture's workflow ref.
    EXPECTED BEHAVIOR: ``main([])`` verifies and names the workflow.
    """
    monkeypatch.setenv("GITHUB_JOB", "ci")

    assert mod.main([]) == 0

    out = capsys.readouterr().out
    assert "verified=true" in out
    assert f"workflow={WORKFLOW_PATH}" in out


def test_main_workflow_flag_overrides_the_env(
    ci_push_checkout: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: ``--workflow`` wins over ``$GITHUB_WORKFLOW_REF``.

    MOCK SETUP: The env names ``other.yml``, which has no run in the fake.
    EXPECTED BEHAVIOR: With ``--workflow`` naming ``ci.yml``, ``main`` verifies.
    """
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        f"{SLUG}/{OTHER_WORKFLOW_PATH}@refs/heads/main",
    )

    assert mod.main(["--workflow", WORKFLOW_PATH, "--check-name", "ci"]) == 0

    assert "verified=true" in capsys.readouterr().out


def test_unset_workflow_ref_refuses_through_main(
    ci_push_checkout: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: A check name but no workflow path — the accidental path.

    MOCK SETUP: ``GITHUB_WORKFLOW_REF`` removed; default green runs.
    EXPECTED BEHAVIOR: ``main`` prints ``reason=no workflow path``, never
    ``verified=true``.
    """
    monkeypatch.delenv("GITHUB_WORKFLOW_REF")

    assert mod.main(["--check-name", "ci"]) == 0

    out = capsys.readouterr().out
    assert "reason=no workflow path" in out
    assert "verified=true" not in out


@pytest.mark.parametrize(
    "fault",
    [
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RuntimeError,
    ],
)
def test_main_turns_a_crash_into_not_verified(
    ci_push_checkout: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: type[Exception],
) -> None:
    """SCENARIO: An unexpected exception must never read as permission to skip.

    The list is literal, not ``mod.FAULTS``: iterating the tuple would pin
    that every member crashes safe, not which types are members.

    MOCK SETUP: ``decide`` replaced by a function raising *fault*.
    EXPECTED BEHAVIOR: Exit 0, ``verified=false``, the exception type named.

    Args:
        fault: Exception type the replaced ``decide`` raises.
    """

    def explode(*_args: object, **_kw: object) -> mod.Decision:
        msg = "boom"
        raise fault(msg)

    monkeypatch.setattr(mod, "decide", explode)

    assert mod.main([]) == 0

    out = capsys.readouterr().out
    assert "verified=false" in out
    assert f"internal error: {fault.__name__}" in out


def test_main_json_output_has_verdict_reason_and_evidence(
    ci_push_checkout: FakeGitHub,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: ``--json`` prints the verdict as an object."""
    assert mod.main(["--check-name", "ci", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    assert payload["reason"] == ""
    evidence = payload["evidence"]
    assert evidence["job"] == "ci"
    assert evidence["workflow"] == WORKFLOW_PATH
    assert evidence["run"] == str(RUN_ID)


def test_github_output_written_when_flag_and_variable_set(
    ci_push_checkout: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """SCENARIO: ``--github-output`` writes the verdict for later steps."""
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    ci_push_checkout.payloads["pulls"] = []

    mod.main(["--github-output"])

    assert out.read_text(encoding="utf-8") == "verified=false\n"


def test_github_output_appends_rather_than_overwrites(
    ci_push_checkout: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """SCENARIO: Other steps' outputs in ``$GITHUB_OUTPUT`` survive."""
    out = tmp_path / "out"
    out.write_text("x=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    mod.main(["--check-name", "ci", "--github-output"])

    assert out.read_text(encoding="utf-8") == "x=1\nverified=true\n"


def test_github_output_not_created_when_variable_unset(
    ci_push_checkout: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """SCENARIO: The flag without ``$GITHUB_OUTPUT`` writes nothing.

    The cwd is the asserted directory, so a stray relative-path write (a
    default filename) would land where it is checked.
    """
    monkeypatch.chdir(tmp_path)

    mod.main(["--github-output"])

    assert list(tmp_path.iterdir()) == []


def test_github_output_untouched_without_flag(
    ci_push_checkout: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """SCENARIO: ``$GITHUB_OUTPUT`` is only written when asked."""
    out = tmp_path / "out"
    out.write_text("x=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    mod.main([])

    assert out.read_text(encoding="utf-8") == "x=1\n"


def test_workflow_gates_pytest_steps_on_tree_gate_and_tolerates_its_crash() -> None:
    """SCENARIO: The workflow wiring the narrow ``FAULTS`` tuple relies on.

    A type outside ``FAULTS`` ends the gate step instead of returning a
    verdict; that is safe only because of these three clauses.

    EXPECTED BEHAVIOR: The ``tree_gate`` step has ``continue-on-error`` true
    (else a gate bug turns ``main`` red). The pytest step runs when the
    output is ``!= 'true'`` (an unset or malformed output must read as "run
    the suite", which ``== 'false'`` would not). The slow-tests report, being
    ``always()`` and reading the pytest log, carries the same comparison, or
    a skipped suite leaves it reporting on a log never written.
    """
    # Imported here, not at module scope: PyYAML arrives via the `audit`
    # extra (through the `dev` umbrella CI installs), so a `.[test]`-only
    # install would otherwise fail to collect this whole file instead of
    # skipping this one workflow-contract test.
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["ci"]["steps"]
    gate = next(s for s in steps if s.get("id") == "tree_gate")
    pytest_step = next(s for s in steps if "pytest -q" in s.get("run", ""))
    report = next(s for s in steps if "forge-slow-tests-report" in s.get("run", ""))
    unverified = "steps.tree_gate.outputs.verified != 'true'"

    assert gate["continue-on-error"] is True
    assert unverified in pytest_step["if"]
    assert unverified in report["if"]
