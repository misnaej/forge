"""``forge-metrics bench``: the same small tasks, run against a chosen forge version.

Real PRs differ too much to tell whether a forge version made the same
work cheaper. The bench runs five fixed tasks (package data under
``forge/data/bench``) the same way each time:

- ``init`` fills an empty private GitHub repo (the *sandbox*, created by
  the owner) with a sample Python project, sets it up as a forge
  consumer, pushes ``main`` and tags it ``bench-base-1``;
- ``run`` gives each task-run pair a throwaway directory: a clone of the
  sandbox at its base tag, a venv holding the forge version under test, a
  headless ``claude -p`` asked to take the task to a posted PR wrap-up;
  then it collects facts, closes the PR unmerged, deletes the branch and
  removes the directory.

What a run measured goes to :mod:`forge.pr_metrics` through a callback
(this module writes no database row itself), keyed by a random run id —
no sandbox name, branch or path is stored. The sandbox slug lives only in
``bench.json`` beside ``repo-ids.json``. Python files ship with a
``.tmpl`` suffix so forge's own linters and test collection leave the
sample alone; ``dot-<name>`` ships ``.<name>``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from forge.agent_profile import branch_usage, default_transcripts_dir
from forge.git_utils import (
    _FORGE_GITHUB_REPO,
    PUSH_TIMEOUT_S,
    create_annotated_tag,
    create_commit,
    delete_remote_branch,
    forge_cli_argv,
    forge_data_dir,
    network_git_env,
    push_branch,
    push_tag,
    run_git,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from forge.agent_profile_tokens import BranchSummary


logger = logging.getLogger(__name__)

#: Local file naming the sandbox; never exported, never in the database.
BENCH_CONFIG_NAME: Final = "bench.json"
BASE_TAG_PREFIX: Final = "bench-base-"
FIRST_BASE_TAG: Final = f"{BASE_TAG_PREFIX}1"
#: The sandbox's base branch.
SANDBOX_BASE: Final = "main"
DEFAULT_RUNS: Final = 2
DEFAULT_TIMEOUT_MIN: Final = 45
#: ``dontAsk`` runs without a person: a tool the settings file does not
#: allow is denied instead of prompted, and hooks — forge's guards among
#: them — still run on every call. ``bypassPermissions`` would skip them.
PERMISSION_MODE: Final = "dontAsk"
#: Tools the run's settings file allows; forge's hooks still gate each call.
ALLOWED_TOOLS: Final = (
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Task",
    "Agent",
    "Skill",
    "TodoWrite",
)
#: Bootstrap steps a run skips. ``plugin`` would register a Claude Code
#: install for a directory deleted minutes later; the run loads that
#: version's plugin with ``--plugin-dir`` instead.
SKIPPED_BOOTSTRAP_STEPS: Final = ("plugin",)
_VERSION = re.compile(r"v\d+\.\d+\.\d+")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._][A-Za-z0-9._-]*")
_TEMPLATE_SUFFIX: Final = ".tmpl"
_DOT_PREFIX: Final = "dot-"
_SETUP_TIMEOUT_S: Final = 900
_CHECK_TIMEOUT_S: Final = 600
_GH_TIMEOUT_S: Final = 60
_TAIL_CHARS: Final = 400


class BenchError(RuntimeError):
    """The sandbox or a requested version is not usable; the user can act on it."""


# --- running commands ------------------------------------------------------


@dataclass(frozen=True)
class Completed:
    """What one external command did."""

    #: Exit code; ``None`` when it timed out or could not start.
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """Whether it exited 0."""
        return self.returncode == 0


class Runner(Protocol):
    """Runs one external command; the seam tests replace."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Completed:
        """Run *argv*.

        Args:
            argv: The command.
            cwd: Its working directory.
            env: Its whole environment; the current one when ``None``.
            timeout: Seconds before it is killed; unbounded when ``None``.
        """
        ...


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> Completed:
    """Run *argv* without a terminal, killing its whole process group on timeout.

    ``claude`` starts hooks, agents and tools of its own; killing only the
    parent would leave them running after the bound.

    Args:
        argv: The command.
        cwd: Its working directory.
        env: Its whole environment; the current one when ``None``.
        timeout: Seconds before it is killed; unbounded when ``None``.

    Returns:
        The result; ``timed_out`` set when the bound was hit.
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return Completed(None, "", str(exc))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return Completed(None, out, err, timed_out=True)
    return Completed(proc.returncode, out, err)


def _tail(result: Completed) -> str:
    """The end of a failed command's output, for a log line.

    Args:
        result: The command's result.

    Returns:
        Its last characters of stderr (stdout when stderr is empty).
    """
    text = (result.stderr or result.stdout or "").strip()
    return text[-_TAIL_CHARS:]


@contextlib.contextmanager
def _environ(env: Mapping[str, str]) -> Iterator[None]:
    """Run the block with ``os.environ`` replaced by *env*, then restore it.

    :func:`forge.git_utils.create_commit` runs the pre-commit hook with the
    process environment; the hook must find the venv's forge, not the
    caller's.

    Args:
        env: The environment for the block.

    Yields:
        Nothing.
    """
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --- package data ----------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One outcome check of a task, run in the clone after the agent stops."""

    #: ``pytest``, ``symbol`` or ``contains``.
    kind: str
    #: ``pytest``: test paths or node ids to run.
    nodes: tuple[str, ...] = ()
    #: ``pytest``: a data file copied into ``tests/`` after the run, then run.
    hidden: str | None = None
    #: ``symbol`` / ``contains``: file in the clone.
    path: str | None = None
    #: ``symbol``: a top-level ``def`` or ``class`` name.
    name: str | None = None
    #: ``contains``: a regular expression.
    pattern: str | None = None


@dataclass(frozen=True)
class Task:
    """One benchmark task."""

    id: str
    summary: str
    prompt: str
    #: Clone path → data file, written before the agent starts.
    setup: dict[str, str] = field(default_factory=dict)
    checks: tuple[Check, ...] = ()


def bench_data() -> contextlib.AbstractContextManager[Path]:
    """The bench package data as a directory on disk.

    Returns:
        A context manager yielding ``forge/data/bench``.
    """
    return resources.as_file(resources.files("forge").joinpath("data/bench"))


def load_tasks(data: Path) -> dict[str, Task]:
    """The shipped tasks, by id.

    Args:
        data: The bench data directory.

    Returns:
        Task id → task, in id order.
    """
    tasks: dict[str, Task] = {}
    for path in sorted((data / "tasks").glob("*.toml")):
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        checks = tuple(
            Check(
                kind=c["kind"],
                nodes=tuple(c.get("nodes", ())),
                hidden=c.get("hidden"),
                path=c.get("path"),
                name=c.get("name"),
                pattern=c.get("pattern"),
            )
            for c in raw.get("checks", [])
        )
        tasks[raw["id"]] = Task(
            id=raw["id"],
            summary=raw.get("summary", ""),
            prompt=raw["prompt"].strip(),
            setup=dict(raw.get("setup", {})),
            checks=checks,
        )
    return tasks


def task_prompt(task: Task, data: Path, branch: str) -> str:
    """The full prompt of *task*: its request, then the shared workflow rules.

    Args:
        task: The task.
        data: The bench data directory.
        branch: The run's branch.

    Returns:
        The prompt.
    """
    workflow = (data / "workflow.md").read_text(encoding="utf-8")
    return f"{task.prompt}\n\n{workflow.replace('{branch}', branch)}"


def _target_name(name: str) -> str:
    """The name a shipped data file takes in a checkout.

    Args:
        name: The file name in package data.

    Returns:
        *name* without ``.tmpl``, ``dot-`` turned into ``.``.
    """
    name = name.removesuffix(_TEMPLATE_SUFFIX)
    if name.startswith(_DOT_PREFIX):
        name = "." + name.removeprefix(_DOT_PREFIX)
    return name


def install_file(src: Path, dest: Path) -> None:
    """Copy one data file into a checkout, creating its directory.

    Args:
        src: The data file.
        dest: Where it goes.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def materialise_sample(sample: Path, dest: Path) -> list[Path]:
    """Copy the sample project into *dest*, data names turned into real ones.

    Args:
        sample: The ``sample`` data directory.
        dest: The checkout.

    Returns:
        The files written.
    """
    written = []
    for path in sorted(sample.rglob("*")):
        rel = path.relative_to(sample)
        if path.is_dir() or "__pycache__" in rel.parts:
            continue
        target = dest / rel.parent / _target_name(rel.name)
        install_file(path, target)
        written.append(target)
    return written


# --- sandbox config --------------------------------------------------------


@dataclass(frozen=True)
class Sandbox:
    """The private repo benchmark runs clone, and the tag they start from."""

    slug: str
    base_tag: str = FIRST_BASE_TAG


def config_path() -> Path:
    """The local ``bench.json``.

    Returns:
        Its path in the data directory, beside ``repo-ids.json``.
    """
    return forge_data_dir() / BENCH_CONFIG_NAME


def read_sandbox() -> Sandbox | None:
    """The sandbox ``init`` recorded, if any.

    Returns:
        It, or ``None`` when ``init`` never ran or the file is unreadable.
    """
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    slug = raw.get("repo") if isinstance(raw, dict) else None
    tag = raw.get("base_tag", FIRST_BASE_TAG) if isinstance(raw, dict) else None
    if not isinstance(slug, str) or not valid_slug(slug) or not isinstance(tag, str):
        return None
    return Sandbox(slug, tag)


def write_sandbox(sandbox: Sandbox) -> None:
    """Save *sandbox* to ``bench.json``, readable by the owner only.

    Args:
        sandbox: The sandbox.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"repo": sandbox.slug, "base_tag": sandbox.base_tag}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def valid_slug(slug: str) -> bool:
    """Whether *slug* is a plain ``owner/name``.

    Args:
        slug: The candidate.

    Returns:
        ``True`` when it can only name a repo.
    """
    return bool(_SLUG.fullmatch(slug)) and ".." not in slug


def valid_version(version: str) -> bool:
    """Whether *version* is a forge release tag (``vX.Y.Z``).

    Args:
        version: The candidate.

    Returns:
        ``True`` for ``vX.Y.Z``.
    """
    return bool(_VERSION.fullmatch(version))


# --- init ------------------------------------------------------------------


def check_sandbox(slug: str, runner: Runner, cwd: Path) -> None:
    """Refuse a repo that is not an existing, empty, private one.

    Args:
        slug: ``owner/name``.
        runner: Command runner.
        cwd: Where to run ``gh``.

    Raises:
        BenchError: The repo is unreadable, public, or not empty.
    """
    if not valid_slug(slug):
        msg = f"forge-metrics bench: {slug!r} is not an owner/name repo"
        raise BenchError(msg)
    repo = runner(["gh", "api", f"repos/{slug}"], cwd=cwd, timeout=_GH_TIMEOUT_S)
    try:
        info = json.loads(repo.stdout) if repo.ok else None
    except ValueError:
        info = None
    if not isinstance(info, dict):
        msg = (
            f"forge-metrics bench: cannot read {slug} through gh; create it "
            "first (private, no README, no commits)"
        )
        raise BenchError(msg)
    if info.get("private") is not True:
        msg = f"forge-metrics bench: {slug} is not private; refused"
        raise BenchError(msg)
    branches = runner(
        ["gh", "api", f"repos/{slug}/branches", "--jq", "length"],
        cwd=cwd,
        timeout=_GH_TIMEOUT_S,
    )
    if not branches.ok or branches.stdout.strip() != "0":
        msg = f"forge-metrics bench: {slug} is not empty; init needs a new empty repo"
        raise BenchError(msg)


def _seed(clone: Path, data: Path, runner: Runner) -> None:
    """Fill *clone* with the sample project set up by the running forge, commit.

    Args:
        clone: The empty sandbox clone.
        data: The bench data directory.
        runner: Command runner.

    Raises:
        BenchError: The seeding commit failed (its hook output is logged).
    """
    run_git("symbolic-ref", "HEAD", f"refs/heads/{SANDBOX_BASE}", cwd=clone)
    materialise_sample(data / "sample", clone)
    # One forge for both: the running install sets the sandbox up.
    skip = [arg for step in SKIPPED_BOOTSTRAP_STEPS for arg in ("--skip", step)]
    boot = runner(
        [*forge_cli_argv("install-forge-bootstrap", caller="forge-metrics"), *skip],
        cwd=clone,
        timeout=_SETUP_TIMEOUT_S,
    )
    if not boot.ok:
        logger.warning("forge-metrics bench: bootstrap failures: %s", _tail(boot))
    run_git("add", "-A", cwd=clone)
    try:
        create_commit(clone, "chore: seed forge benchmark sandbox")
    except subprocess.CalledProcessError as exc:
        msg = f"forge-metrics bench: seeding commit failed (exit {exc.returncode})"
        raise BenchError(msg) from exc


def init_sandbox(
    slug: str,
    *,
    data: Path,
    runner: Runner = run_command,
    workdir: Path | None = None,
) -> Sandbox:
    """Fill the empty private repo *slug* and tag its starting point.

    Pushing ``main`` from a CLI is allowed here although forge never pushes
    to a base branch otherwise: this is the first commit of a sandbox
    nobody else uses, checked empty and private just before, and the push
    goes through forge's no-force seam, so it can create ``main`` but never
    rewrite one.

    Args:
        slug: ``owner/name`` of the sandbox.
        data: The bench data directory.
        runner: Command runner.
        workdir: Parent of the temporary clone; the system's when ``None``.

    Returns:
        The sandbox, also saved to ``bench.json``.

    Raises:
        BenchError: The repo was refused, or cloning or pushing failed.
    """
    tmp = Path(tempfile.mkdtemp(prefix="forge-bench-init-", dir=workdir))
    try:
        check_sandbox(slug, runner, tmp)
        clone = tmp / "sandbox"
        cloned = runner(
            ["gh", "repo", "clone", slug, str(clone)], cwd=tmp, timeout=PUSH_TIMEOUT_S
        )
        if not cloned.ok:
            msg = f"forge-metrics bench: clone failed: {_tail(cloned)}"
            raise BenchError(msg)
        _seed(clone, data, runner)
        pushed = push_branch(clone, SANDBOX_BASE, set_upstream=True)
        if not pushed.ok:
            msg = f"forge-metrics bench: push of {SANDBOX_BASE} failed: {pushed.stderr}"
            raise BenchError(msg)
        create_annotated_tag(clone, FIRST_BASE_TAG)
        tagged = push_tag(clone, FIRST_BASE_TAG)
        if not tagged.ok:
            msg = f"forge-metrics bench: tag push failed: {tagged.stderr}"
            raise BenchError(msg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    sandbox = Sandbox(slug, FIRST_BASE_TAG)
    write_sandbox(sandbox)
    return sandbox


# --- run -------------------------------------------------------------------


@dataclass(frozen=True)
class RunOptions:
    """Per-set run settings shared by every plan.

    Attributes:
        model: Model for ``claude``; its default when ``None``.
        timeout_s: Bound on each agent run, in seconds.
    """

    model: str | None
    timeout_s: float


@dataclass(frozen=True)
class RunPlan:
    """One task-run pair of a set."""

    version: str
    task: Task
    run_n: int
    model: str | None
    timeout_s: float
    sandbox: Sandbox

    @property
    def branch(self) -> str:
        """The run's branch in the sandbox."""
        return f"bench/{self.task.id}-{self.version}-{self.run_n}"


@dataclass
class BenchOutcome:
    """What one run measured; filled as the run goes, recorded even if it fails."""

    run_id: str
    version: str
    task_id: str
    run_n: int
    model: str | None
    base_tag: str
    started_at: str
    ended_at: str | None = None
    #: Clone, venv, bootstrap and task files all ready.
    setup_ok: bool = False
    timed_out: bool = False
    claude_exit: int | None = None
    pr_opened: bool = False
    wrapups_posted: int = 0
    #: The sandbox's whole test suite; ``None`` when not reached.
    tests_green: bool | None = None
    #: ``forge-precommit --verdict``; ``None`` when not reached.
    precommit_ok: bool | None = None
    checks_passed: int = 0
    checks_total: int = 0
    files: int = 0
    additions: int = 0
    deletions: int = 0
    commits: int = 0
    summary: BranchSummary | None = None


@dataclass(frozen=True)
class Workspace:
    """A prepared run directory."""

    clone: Path
    venv: Path
    forge_src: Path
    env: dict[str, str]
    #: The commit the agent starts from; lines changed are counted from it.
    base_sha: str

    def tool(self, name: str) -> str:
        """A console script of the run's venv.

        Args:
            name: Script name.

        Returns:
            Its path.
        """
        return str(self.venv / "bin" / name)


def _now() -> str:
    """The current UTC time, to the second.

    Returns:
        ISO 8601 text.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def forge_repo_url() -> str:
    """The forge upstream repository's clone URL.

    Returns:
        ``https://github.com/<upstream>.git``.
    """
    return f"https://github.com/{_FORGE_GITHUB_REPO}.git"


def run_env(venv: Path) -> dict[str, str]:
    """The environment a run's tools and agent see.

    The venv's scripts come first on ``PATH``. ``FORGE_*`` overrides of
    the caller (a relocated ``code_health/``, an armed bypass) and
    ``CLAUDECODE`` (set when ``forge-metrics`` itself runs inside Claude
    Code) are dropped so the run behaves like a fresh session.

    Args:
        venv: The run's venv.

    Returns:
        The whole environment.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("FORGE_") and k not in {"CLAUDECODE", "VIRTUAL_ENV"}
    }
    env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["VIRTUAL_ENV"] = str(venv)
    return env


def _step(name: str, result: Completed) -> bool:
    """Log a failed setup step.

    Args:
        name: What the step did.
        result: Its result.

    Returns:
        Whether it succeeded.
    """
    if not result.ok:
        logger.warning("forge-metrics bench: %s failed: %s", name, _tail(result))
    return result.ok


def _fetch(plan: RunPlan, tmp: Path, runner: Runner) -> tuple[Path, Path] | None:
    """Clone the sandbox at its base tag and forge at the version under test.

    Args:
        plan: The run.
        tmp: The run directory.
        runner: Command runner.

    Returns:
        ``(sandbox clone, forge source)``, or ``None`` when either failed.
    """
    clone, forge_src = tmp / "sandbox", tmp / "forge"
    net = {**os.environ, **network_git_env(None)}
    sandbox = runner(
        ["gh", "repo", "clone", plan.sandbox.slug, str(clone), "--", "--quiet"],
        cwd=tmp,
        timeout=PUSH_TIMEOUT_S,
    )
    if not _step("sandbox clone", sandbox):
        return None
    forge = runner(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            plan.version,
            "--",
            forge_repo_url(),
            str(forge_src),
        ],
        cwd=tmp,
        env=net,
        timeout=PUSH_TIMEOUT_S,
    )
    if not _step(f"forge {plan.version} clone", forge):
        return None
    run_git("checkout", "--quiet", "-b", plan.branch, plan.sandbox.base_tag, cwd=clone)
    return clone, forge_src


def _install(tmp: Path, clone: Path, forge_src: Path, runner: Runner) -> Path | None:
    """Build the run's venv: the forge version under test, then the sample.

    A throwaway venv inside the run directory is FOUNDATION §2's
    "disposable per-checkout env": nothing in the user's environment or
    any pin changes, and the directory is deleted after the run. The
    package and the plugin come from the same tag checkout, so the
    agents, skills and hooks the session loads match the CLIs it runs.

    Args:
        tmp: The run directory.
        clone: The sandbox clone.
        forge_src: Forge's source at the version under test.
        runner: Command runner.

    Returns:
        The venv, or ``None`` when a step failed.
    """
    venv = tmp / "venv"
    if not _step("venv", runner([sys.executable, "-m", "venv", str(venv)], cwd=tmp)):
        return None
    pip = [str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet"]
    for name, argv in (
        ("forge install", [*pip, str(forge_src)]),
        ("sample install", [*pip, "-e", f"{clone}[dev]"]),
    ):
        if not _step(name, runner(argv, cwd=tmp, timeout=_SETUP_TIMEOUT_S)):
            return None
    return venv


def _bootstrap(clone: Path, venv: Path, runner: Runner) -> str | None:
    """Set the clone up with the version under test and commit what changed.

    The commit runs that version's pre-commit hook, so the agent starts
    from a tree the version itself accepts.

    Args:
        clone: The sandbox clone, on the run's branch.
        venv: The run's venv.
        runner: Command runner.

    Returns:
        The commit the agent starts from, or ``None`` when setup failed.
    """
    env = run_env(venv)
    skip = [arg for step in SKIPPED_BOOTSTRAP_STEPS for arg in ("--skip", step)]
    boot = runner(
        [str(venv / "bin" / "install-forge-bootstrap"), *skip],
        cwd=clone,
        env=env,
        timeout=_SETUP_TIMEOUT_S,
    )
    if not boot.ok:
        logger.warning("forge-metrics bench: bootstrap failures: %s", _tail(boot))
    if run_git("status", "--porcelain", cwd=clone):
        run_git("add", "-A", cwd=clone)
        try:
            with _environ(env):
                create_commit(clone, "chore: forge setup for this benchmark run")
        except subprocess.CalledProcessError:
            logger.warning("forge-metrics bench: setup commit refused by its hook")
            return None
    return run_git("rev-parse", "HEAD", cwd=clone)


def prepare(plan: RunPlan, tmp: Path, data: Path, runner: Runner) -> Workspace | None:
    """Make the run directory ready for the agent.

    Args:
        plan: The run.
        tmp: The run directory.
        data: The bench data directory.
        runner: Command runner.

    Returns:
        The workspace, or ``None`` when a step failed (logged).
    """
    fetched = _fetch(plan, tmp, runner)
    if fetched is None:
        return None
    clone, forge_src = fetched
    venv = _install(tmp, clone, forge_src, runner)
    if venv is None:
        return None
    base_sha = _bootstrap(clone, venv, runner)
    if base_sha is None:
        return None
    for dest, src in plan.task.setup.items():
        install_file(data / "files" / src, clone / dest)
    return Workspace(clone, venv, forge_src, run_env(venv), base_sha)


def write_settings(path: Path) -> Path:
    """Write the run's Claude Code settings: the allow-list, nothing else.

    Args:
        path: The file to write.

    Returns:
        *path*.
    """
    settings = {
        "permissions": {"defaultMode": PERMISSION_MODE, "allow": list(ALLOWED_TOOLS)}
    }
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return path


def claude_argv(plan: RunPlan, prompt: str, settings: Path, plugin: Path) -> list[str]:
    """The headless ``claude`` command of one run.

    Args:
        plan: The run.
        prompt: The full task prompt.
        settings: The run's settings file.
        plugin: The version's plugin directory (its source checkout).

    Returns:
        The argv.
    """
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        PERMISSION_MODE,
        "--settings",
        str(settings),
        "--plugin-dir",
        str(plugin),
    ]
    if plan.model:
        argv += ["--model", plan.model]
    return argv


# --- collecting ------------------------------------------------------------


def _numstat(clone: Path, base_sha: str) -> tuple[int, int, int]:
    """Files, lines added and removed since *base_sha*, uncommitted work included.

    Args:
        clone: The sandbox clone.
        base_sha: The commit the agent started from.

    Returns:
        ``(files, additions, deletions)``.
    """
    files = added = removed = 0
    for line in run_git("diff", "--numstat", base_sha, cwd=clone).splitlines():
        plus, minus, _path = ([*line.split("\t", 2), "", ""])[:3]
        files += 1
        added += int(plus) if plus.isdigit() else 0
        removed += int(minus) if minus.isdigit() else 0
    return files, added, removed


def run_check(
    check: Check, index: int, ws: Workspace, data: Path, runner: Runner
) -> bool:
    """Run one outcome check in the clone.

    Args:
        check: The check.
        index: Its position in the task, naming a hidden test file.
        ws: The run's workspace.
        data: The bench data directory.
        runner: Command runner.

    Returns:
        Whether it held.
    """
    if check.kind == "pytest":
        nodes = list(check.nodes)
        if check.hidden:
            rel = f"tests/test_bench_hidden_{index}.py"
            install_file(data / "files" / check.hidden, ws.clone / rel)
            nodes.append(rel)
        argv = [ws.tool("python"), "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        result = runner(
            [*argv, *nodes], cwd=ws.clone, env=ws.env, timeout=_CHECK_TIMEOUT_S
        )
        return result.ok
    target = ws.clone / (check.path or "")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return False
    if check.kind == "symbol" and check.name:
        name = re.escape(check.name)
        return bool(re.search(rf"(?m)^(?:def|class)\s+{name}\b", text))
    if check.kind == "contains" and check.pattern:
        return bool(re.search(check.pattern, text))
    return False


def collect(
    outcome: BenchOutcome, plan: RunPlan, ws: Workspace, data: Path, runner: Runner
) -> None:
    """Fill *outcome* with what the clone and the transcripts show.

    The suite and pre-commit verdict are read before the hidden checks
    add their test files.

    Args:
        outcome: The run's record, filled in place.
        plan: The run.
        ws: The run's workspace.
        data: The bench data directory.
        runner: Command runner.
    """
    transcripts = default_transcripts_dir(ws.clone)
    if transcripts is not None:
        outcome.summary = branch_usage([transcripts]).summary(plan.branch)
    outcome.files, outcome.additions, outcome.deletions = _numstat(
        ws.clone, ws.base_sha
    )
    count = run_git("rev-list", "--count", f"{ws.base_sha}..HEAD", cwd=ws.clone)
    outcome.commits = int(count) if count.isdigit() else 0
    suite = [ws.tool("python"), "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    outcome.tests_green = runner(
        suite, cwd=ws.clone, env=ws.env, timeout=_CHECK_TIMEOUT_S
    ).ok
    outcome.precommit_ok = runner(
        [ws.tool("forge-precommit"), "--verdict"],
        cwd=ws.clone,
        env=ws.env,
        timeout=_CHECK_TIMEOUT_S,
    ).ok
    outcome.checks_total = len(plan.task.checks)
    outcome.checks_passed = sum(
        run_check(check, i, ws, data, runner)
        for i, check in enumerate(plan.task.checks)
    )


def _find_pr(slug: str, branch: str, runner: Runner, cwd: Path) -> int | None:
    """The PR opened from *branch* in the sandbox, if any.

    Args:
        slug: The sandbox.
        branch: The run's branch.
        runner: Command runner.
        cwd: Where to run ``gh``.

    Returns:
        Its number, or ``None``.
    """
    found = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number",
            "--jq",
            ".[].number",
        ],
        cwd=cwd,
        timeout=_GH_TIMEOUT_S,
    )
    first = found.stdout.split()[0] if found.ok and found.stdout.split() else ""
    return int(first) if first.isdigit() else None


def _wrapups(slug: str, number: int, runner: Runner, cwd: Path) -> int:
    """Wrap-ups posted on the PR: comments opening with ``verified-at:``.

    Args:
        slug: The sandbox.
        number: The PR.
        runner: Command runner.
        cwd: Where to run ``gh``.

    Returns:
        Their count; ``0`` when GitHub could not be read.
    """
    pages = runner(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{slug}/issues/{number}/comments",
            "--jq",
            '[.[] | select((.body // "") | startswith("verified-at:"))] | length',
        ],
        cwd=cwd,
        timeout=_GH_TIMEOUT_S,
    )
    if not pages.ok:
        return 0
    return sum(int(n) for n in pages.stdout.split() if n.isdigit())


def close_out(
    outcome: BenchOutcome, plan: RunPlan, clone: Path, runner: Runner
) -> None:
    """Record the PR facts, close the PR unmerged and delete the run's branch.

    Runs whatever happened before, so a timed-out agent's PR and branch
    are cleaned up too. The branch is deleted through forge's lease-pinned
    seam, which refuses the sandbox's base branch.

    Args:
        outcome: The run's record, filled in place.
        plan: The run.
        clone: The sandbox clone (may not exist when setup failed early).
        runner: Command runner.
    """
    slug, branch = plan.sandbox.slug, plan.branch
    cwd = clone if clone.is_dir() else clone.parent
    number = _find_pr(slug, branch, runner, cwd)
    if number is not None:
        outcome.pr_opened = True
        outcome.wrapups_posted = _wrapups(slug, number, runner, cwd)
        closed = runner(
            ["gh", "pr", "close", str(number), "--repo", slug],
            cwd=cwd,
            timeout=_GH_TIMEOUT_S,
        )
        _step("closing the PR", closed)
    if not clone.is_dir():
        return
    remote = run_git(
        "ls-remote",
        "origin",
        f"refs/heads/{branch}",
        cwd=clone,
        check=False,
        env=network_git_env(clone),
        timeout=PUSH_TIMEOUT_S,
    )
    sha = remote.split()[0] if remote else ""
    if sha:
        deleted = delete_remote_branch(
            clone, branch, expected_sha=sha, base_branch=SANDBOX_BASE
        )
        if not deleted.ok:
            logger.warning("forge-metrics bench: branch kept: %s", deleted.stderr)


def run_one(
    plan: RunPlan,
    *,
    data: Path,
    runner: Runner = run_command,
    workdir: Path | None = None,
) -> BenchOutcome:
    """Run one task once, end to end; never raises for a failed step.

    Args:
        plan: The run.
        data: The bench data directory.
        runner: Command runner.
        workdir: Parent of the run directory; the system's when ``None``.

    Returns:
        What the run measured; a failure shows as facts (``setup_ok``,
        ``timed_out``, ``claude_exit``).
    """
    outcome = BenchOutcome(
        run_id=uuid.uuid4().hex,
        version=plan.version,
        task_id=plan.task.id,
        run_n=plan.run_n,
        model=plan.model,
        base_tag=plan.sandbox.base_tag,
        started_at=_now(),
    )
    tmp = Path(tempfile.mkdtemp(prefix="forge-bench-", dir=workdir))
    try:
        ws = prepare(plan, tmp, data, runner)
        if ws is not None:
            outcome.setup_ok = True
            prompt = task_prompt(plan.task, data, plan.branch)
            settings = write_settings(tmp / "settings.json")
            result = runner(
                claude_argv(plan, prompt, settings, ws.forge_src),
                cwd=ws.clone,
                env=ws.env,
                timeout=plan.timeout_s,
            )
            outcome.timed_out, outcome.claude_exit = result.timed_out, result.returncode
            collect(outcome, plan, ws, data, runner)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning(
            "forge-metrics bench: %s run %d stopped: %s", plan.task.id, plan.run_n, exc
        )
    finally:
        try:
            close_out(outcome, plan, tmp / "sandbox", runner)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("forge-metrics bench: cleanup incomplete: %s", exc)
        shutil.rmtree(tmp, ignore_errors=True)
        outcome.ended_at = _now()
    return outcome


def plan_set(
    sandbox: Sandbox,
    tasks: Sequence[Task],
    *,
    version: str,
    runs: int,
    run_options: RunOptions,
) -> list[RunPlan]:
    """Every task-run pair of a set, runs of one task together.

    Args:
        sandbox: The sandbox.
        tasks: The tasks.
        version: The forge version under test.
        runs: Runs per task.
        run_options: Model and timeout shared by every run.

    Returns:
        The plans.
    """
    return [
        RunPlan(version, task, n, run_options.model, run_options.timeout_s, sandbox)
        for task in tasks
        for n in range(1, runs + 1)
    ]


def run_set(
    plans: Sequence[RunPlan],
    *,
    data: Path,
    record: Callable[[BenchOutcome], None],
    runner: Runner = run_command,
    workdir: Path | None = None,
) -> list[BenchOutcome]:
    """Run every plan in turn, recording each as it ends.

    Args:
        plans: The runs.
        data: The bench data directory.
        record: Stores one outcome (the database writer).
        runner: Command runner.
        workdir: Parent of the run directories.

    Returns:
        The outcomes, in plan order.
    """
    outcomes = []
    for plan in plans:
        outcome = run_one(plan, data=data, runner=runner, workdir=workdir)
        record(outcome)
        outcomes.append(outcome)
        logger.info(
            "forge-metrics bench: %s run %d — setup %s, timed out %s, PR %s",
            plan.task.id,
            plan.run_n,
            "yes" if outcome.setup_ok else "no",
            "yes" if outcome.timed_out else "no",
            "opened" if outcome.pr_opened else "none",
        )
    return outcomes


def version_exists(version: str, runner: Runner, cwd: Path) -> bool:
    """Whether forge's upstream has tag *version*.

    Args:
        version: ``vX.Y.Z``.
        runner: Command runner.
        cwd: Where to run git.

    Returns:
        ``True`` when the tag is there.
    """
    result = runner(
        ["git", "ls-remote", "--tags", forge_repo_url(), f"refs/tags/{version}"],
        cwd=cwd,
        env={**os.environ, **network_git_env(None)},
        timeout=PUSH_TIMEOUT_S,
    )
    return result.ok and bool(result.stdout.strip())
