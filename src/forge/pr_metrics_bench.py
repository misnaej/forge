"""``forge-metrics bench``: fixed tasks, run by hand, to compare forge versions.

Real PRs differ too much to tell whether a forge version made the same
work cheaper. The bench gives five fixed tasks (package data under
``forge/data/bench``) and a private *sandbox* repo to run them in:

- ``init`` fills an empty private GitHub repo (created by the owner) with
  a sample Python project, sets it up as a forge consumer, pushes ``main``
  and tags it ``bench-base-1``;
- ``start <task>`` puts a local clone of the sandbox on a fresh branch at
  the base tag and prints the task's prompt; the owner runs it in a normal
  Claude Code session in that clone;
- ``finish`` collects what the run did, hands it to the database writer,
  closes the PR unmerged and deletes the branch on GitHub.

The forge version measured is the one installed in the clone's ``.venv``
(else the clone's pin). Rows are keyed by a random run id — no sandbox
name, branch or path is stored. The sandbox slug lives only in
``bench.json`` and the run in progress in ``bench-run.json``, both beside
the database. Python data files ship with a ``.tmpl`` suffix so forge's
own linters and test collection leave the sample alone; ``dot-<name>``
ships ``.<name>``.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from forge.agent_profile import TranscriptFilter, branch_usage, default_transcripts_dir
from forge.agent_profile_tokens import parse_ts
from forge.git_utils import (
    PUSH_TIMEOUT_S,
    create_annotated_tag,
    create_commit,
    delete_remote_branch,
    forge_cli_argv,
    forge_data_dir,
    github_slug,
    network_git_env,
    parse_semver,
    push_branch,
    push_tag,
    ref_exists,
    run_git,
)
from forge.upgrade import pinned_ref


if TYPE_CHECKING:
    import contextlib
    from collections.abc import Callable, Mapping, Sequence

    from forge.agent_profile_tokens import BranchSummary


logger = logging.getLogger(__name__)

#: Local file naming the sandbox; never exported, never in the database.
BENCH_CONFIG_NAME: Final = "bench.json"
#: Local file describing the run between ``start`` and ``finish``.
MARKER_NAME: Final = "bench-run.json"
BASE_TAG_PREFIX: Final = "bench-base-"
FIRST_BASE_TAG: Final = f"{BASE_TAG_PREFIX}1"
#: The sandbox's base branch.
SANDBOX_BASE: Final = "main"
#: Bootstrap steps ``init`` skips: ``plugin`` would register a Claude Code
#: install for the temporary directory ``init`` deletes.
SKIPPED_BOOTSTRAP_STEPS: Final = ("plugin",)
#: Work on a run starts after ``start``; a little slack for clock skew.
_START_SLACK: Final = timedelta(minutes=1)
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._][A-Za-z0-9._-]*")
_TEMPLATE_SUFFIX: Final = ".tmpl"
_DOT_PREFIX: Final = "dot-"
_SETUP_TIMEOUT_S: Final = 900
_CHECK_TIMEOUT_S: Final = 600
_GH_TIMEOUT_S: Final = 60
_TAIL_CHARS: Final = 400
#: The dist-info directory name ``pip`` gives forge-scripts.
_DIST_INFO = re.compile(r"forge_scripts-(?P<version>[^-]+)\.dist-info")


class BenchError(RuntimeError):
    """The sandbox, the clone or the run marker is not usable; the user can act."""


# --- running commands ------------------------------------------------------


@dataclass(frozen=True)
class Completed:
    """What one external command did."""

    #: Exit code; ``None`` when it timed out or could not start.
    returncode: int | None
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """Whether it exited 0."""
        return self.returncode == 0


class Runner(Protocol):
    """Runs one external command; the seam tests replace."""

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout: float | None = None
    ) -> Completed:
        """Run *argv*.

        Args:
            argv: The command.
            cwd: Its working directory.
            timeout: Seconds before it is stopped; unbounded when ``None``.
        """
        ...


def run_command(
    argv: Sequence[str], *, cwd: Path, timeout: float | None = None
) -> Completed:
    """Run *argv* without a terminal, bounded by *timeout*.

    Args:
        argv: The command.
        cwd: Its working directory.
        timeout: Seconds before it is stopped; unbounded when ``None``.

    Returns:
        The result; ``returncode`` ``None`` when it timed out or did not start.
    """
    try:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Completed(None, "", f"timed out after {timeout}s")
    except OSError as exc:
        return Completed(None, "", str(exc))
    return Completed(proc.returncode, proc.stdout, proc.stderr)


def _tail(result: Completed) -> str:
    """The end of a failed command's output, for a message.

    Args:
        result: The command's result.

    Returns:
        Its last characters of stderr (stdout when stderr is empty).
    """
    text = (result.stderr or result.stdout or "").strip()
    return text[-_TAIL_CHARS:]


# --- package data ----------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One outcome check of a task, run in the clone at ``finish``."""

    #: ``pytest``, ``symbol`` or ``contains``.
    kind: str
    #: ``pytest``: test paths or node ids to run.
    nodes: tuple[str, ...] = ()
    #: ``pytest``: a data file copied into ``tests/`` for the check only.
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
    #: Clone path → data file, written by ``start``.
    setup: dict[str, str] = field(default_factory=dict)
    checks: tuple[Check, ...] = ()

    def data_files(self) -> list[str]:
        """Every ``files/`` data path the task names.

        Returns:
            Paths relative to the ``files`` data directory.
        """
        hidden = [c.hidden for c in self.checks if c.hidden]
        return [*self.setup.values(), *hidden]


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


# --- local state -----------------------------------------------------------


@dataclass(frozen=True)
class Sandbox:
    """The private repo benchmark runs use, and the tag they start from."""

    slug: str
    base_tag: str = FIRST_BASE_TAG


def _write_private(path: Path, payload: Mapping[str, object]) -> None:
    """Write *payload* as JSON, readable by the owner only.

    Args:
        path: The file.
        payload: Its content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _read_json(path: Path) -> dict[str, object] | None:
    """A JSON object file, ``None`` when absent or unreadable.

    Args:
        path: The file.

    Returns:
        Its content.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def valid_slug(slug: str) -> bool:
    """Whether *slug* is a plain ``owner/name``.

    Args:
        slug: The candidate.

    Returns:
        ``True`` when it can only name a repo.
    """
    return bool(_SLUG.fullmatch(slug)) and ".." not in slug


def read_sandbox() -> Sandbox | None:
    """The sandbox ``init`` recorded, if any.

    Returns:
        It, or ``None`` when ``init`` never ran or the file is unreadable.
    """
    raw = _read_json(forge_data_dir() / BENCH_CONFIG_NAME) or {}
    slug, tag = raw.get("repo"), raw.get("base_tag", FIRST_BASE_TAG)
    if not isinstance(slug, str) or not valid_slug(slug) or not isinstance(tag, str):
        return None
    return Sandbox(slug, tag)


def write_sandbox(sandbox: Sandbox) -> None:
    """Save *sandbox* to ``bench.json``.

    Args:
        sandbox: The sandbox.
    """
    payload = {"repo": sandbox.slug, "base_tag": sandbox.base_tag}
    _write_private(forge_data_dir() / BENCH_CONFIG_NAME, payload)


@dataclass(frozen=True)
class Marker:
    """The run between ``start`` and ``finish`` (local only)."""

    task_id: str
    run_n: int
    base_tag: str
    started_at: str
    branch: str
    clone: str


def marker_path() -> Path:
    """The local ``bench-run.json``.

    Returns:
        Its path beside the database.
    """
    return forge_data_dir() / MARKER_NAME


def read_marker() -> Marker | None:
    """The run in progress, if any.

    Returns:
        It, or ``None`` when no run is started or the file is unreadable.
    """
    raw = _read_json(marker_path()) or {}
    text = {
        key: raw.get(key)
        for key in ("task_id", "base_tag", "started_at", "branch", "clone")
    }
    run_n = raw.get("run_n")
    if not isinstance(run_n, int) or not all(isinstance(v, str) for v in text.values()):
        return None
    return Marker(
        task_id=str(text["task_id"]),
        run_n=run_n,
        base_tag=str(text["base_tag"]),
        started_at=str(text["started_at"]),
        branch=str(text["branch"]),
        clone=str(text["clone"]),
    )


def default_clone_dir() -> Path:
    """Where ``start`` keeps the sandbox clone unless told otherwise.

    Returns:
        ``<data dir>/bench/sandbox``.
    """
    return forge_data_dir() / "bench" / "sandbox"


# --- sandbox checks --------------------------------------------------------


def check_access(slug: str, runner: Runner, cwd: Path) -> None:
    """Refuse a sandbox that is not private or that the user does not own.

    Args:
        slug: ``owner/name``.
        runner: Command runner.
        cwd: Where to run ``gh``.

    Raises:
        BenchError: The repo is unreadable, public, or not administered by
            the ``gh`` user.
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
        msg = f"forge-metrics bench: cannot read {slug} through gh"
        raise BenchError(msg)
    if info.get("private") is not True:
        msg = f"forge-metrics bench: {slug} is not private; refused"
        raise BenchError(msg)
    permissions = info.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("admin") is not True:
        msg = f"forge-metrics bench: {slug} is not yours (no admin rights); refused"
        raise BenchError(msg)


def check_empty(slug: str, runner: Runner, cwd: Path) -> None:
    """Refuse a repo with any branch.

    Args:
        slug: ``owner/name``.
        runner: Command runner.
        cwd: Where to run ``gh``.

    Raises:
        BenchError: The repo has a branch, or its branches cannot be read.
    """
    branches = runner(
        ["gh", "api", f"repos/{slug}/branches", "--jq", "length"],
        cwd=cwd,
        timeout=_GH_TIMEOUT_S,
    )
    if not branches.ok or branches.stdout.strip() != "0":
        msg = f"forge-metrics bench: {slug} is not empty; init needs a new empty repo"
        raise BenchError(msg)


# --- init ------------------------------------------------------------------


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
    nobody else uses, checked empty, private and owned just before, and the
    push goes through forge's no-force seam, so it can create ``main`` but
    never rewrite one.

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
        check_access(slug, runner, tmp)
        check_empty(slug, runner, tmp)
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


# --- start -----------------------------------------------------------------


def _now() -> str:
    """The current UTC time, to the second.

    Returns:
        ISO 8601 text.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def _require_sandbox() -> Sandbox:
    """The recorded sandbox.

    Returns:
        It.

    Raises:
        BenchError: ``init`` never ran.
    """
    sandbox = read_sandbox()
    if sandbox is None:
        msg = "forge-metrics bench: no sandbox; run `bench init --repo` first"
        raise BenchError(msg)
    return sandbox


def _ready_clone(
    sandbox: Sandbox,
    clone: Path,
    runner: Runner,
    origin_slug: Callable[[Path], str | None],
) -> None:
    """Make sure *clone* is a clean, fetched clone of the sandbox.

    Args:
        sandbox: The sandbox.
        clone: The local clone; created when it does not exist.
        runner: Command runner.
        origin_slug: Reads a clone's ``origin`` as ``owner/name``.

    Raises:
        BenchError: Cloning or fetching failed, *clone* is another repo, or
            its working tree has changes (never discarded).
    """
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        cloned = runner(
            ["gh", "repo", "clone", sandbox.slug, str(clone)],
            cwd=clone.parent,
            timeout=PUSH_TIMEOUT_S,
        )
        if not cloned.ok:
            msg = f"forge-metrics bench: clone failed: {_tail(cloned)}"
            raise BenchError(msg)
    elif origin_slug(clone) != sandbox.slug:
        msg = f"forge-metrics bench: {clone} is not a clone of the sandbox"
        raise BenchError(msg)
    if run_git("status", "--porcelain", cwd=clone):
        msg = (
            f"forge-metrics bench: {clone} has uncommitted changes; commit, "
            "move or remove them first (nothing is discarded)"
        )
        raise BenchError(msg)
    run_git(
        "fetch",
        "--quiet",
        "--tags",
        "origin",
        cwd=clone,
        env=network_git_env(clone),
        timeout=PUSH_TIMEOUT_S,
    )


def _free_branch(clone: Path, task_id: str, first_n: int) -> tuple[str, int]:
    """The first ``bench/<task>-<n>`` branch name not taken locally or on origin.

    Args:
        clone: The sandbox clone.
        task_id: The task.
        first_n: The lowest run number to try.

    Returns:
        ``(branch, n)``.
    """
    n = first_n
    while True:
        branch = f"bench/{task_id}-{n}"
        taken = ref_exists(clone, f"refs/heads/{branch}") or ref_exists(
            clone, f"refs/remotes/origin/{branch}"
        )
        if not taken:
            return branch, n
        n += 1


@dataclass(frozen=True)
class Seams:
    """The external-command seams a run uses; tests replace them."""

    runner: Runner = run_command
    origin_slug: Callable[[Path], str | None] = github_slug


DEFAULT_SEAMS: Final = Seams()


def start_run(
    task: Task,
    *,
    data: Path,
    first_n: int,
    clone: Path | None = None,
    seams: Seams = DEFAULT_SEAMS,
) -> tuple[Marker, str]:
    """Put the sandbox clone on a fresh branch for *task* and record the marker.

    The clone is moved to the base tag only when its working tree is clean;
    a dirty clone is refused, never reset.

    Args:
        task: The task.
        data: The bench data directory.
        first_n: The lowest run number to use (one past the recorded runs).
        clone: The local clone; :func:`default_clone_dir` when ``None``.
        seams: Command runner and origin reader.

    Returns:
        The marker and the prompt to paste.

    Raises:
        BenchError: No sandbox, a run already started, or the clone refused.
    """
    sandbox = _require_sandbox()
    current = read_marker()
    if current is not None:
        msg = (
            f"forge-metrics bench: run {current.task_id} #{current.run_n} is "
            "started; `bench finish` it first"
        )
        raise BenchError(msg)
    clone = (clone or default_clone_dir()).resolve()
    check_access(
        sandbox.slug, seams.runner, clone.parent if not clone.is_dir() else clone
    )
    _ready_clone(sandbox, clone, seams.runner, seams.origin_slug)
    branch, n = _free_branch(clone, task.id, first_n)
    run_git("checkout", "--quiet", "-b", branch, sandbox.base_tag, cwd=clone)
    for dest, src in task.setup.items():
        install_file(data / "files" / src, clone / dest)
    marker = Marker(task.id, n, sandbox.base_tag, _now(), branch, str(clone))
    _write_private(marker_path(), asdict(marker))
    return marker, task_prompt(task, data, branch)


def session_line(marker: Marker) -> str:
    """The command to paste to start the session.

    Args:
        marker: The run.

    Returns:
        ``cd <clone> && claude``.
    """
    return f"cd {shlex.quote(marker.clone)} && claude"


# --- finish ----------------------------------------------------------------


@dataclass
class BenchOutcome:
    """What one run measured, as the database writer stores it."""

    run_id: str
    #: Forge release ``X.Y.Z`` of the clone's environment; ``None`` if unknown.
    version: str | None
    task_id: str
    run_n: int
    base_tag: str
    started_at: str
    ended_at: str | None = None
    pr_opened: bool = False
    wrapups_posted: int = 0
    #: The sandbox's whole test suite.
    tests_green: bool | None = None
    #: ``forge-precommit --verdict`` in the clone.
    precommit_ok: bool | None = None
    checks_passed: int = 0
    checks_total: int = 0
    files: int = 0
    additions: int = 0
    deletions: int = 0
    commits: int = 0
    summary: BranchSummary | None = None


def clone_forge_version(clone: Path) -> str | None:
    """The forge release the clone runs: its ``.venv``'s, else its pin.

    Args:
        clone: The sandbox clone.

    Returns:
        ``X.Y.Z``, or ``None`` when neither tells.
    """
    for info in sorted(clone.glob(".venv/lib/python*/site-packages/*.dist-info")):
        match = _DIST_INFO.fullmatch(info.name)
        parsed = parse_semver(match.group("version")) if match else None
        if parsed:
            return ".".join(map(str, parsed))
    try:
        text = (clone / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    ref = pinned_ref(text)
    parsed = parse_semver(ref) if ref else None
    return ".".join(map(str, parsed)) if parsed else None


def _clone_tool(clone: Path, name: str) -> list[str]:
    """Argv running *name* from the clone's ``.venv``, else the running forge.

    Args:
        clone: The sandbox clone.
        name: ``python`` or a forge console script.

    Returns:
        The argv prefix.
    """
    local = clone / ".venv" / "bin" / name
    if local.exists():
        return [str(local)]
    if name == "python":
        return [sys.executable]
    return forge_cli_argv(name, caller="forge-metrics")


def _pytest(clone: Path) -> list[str]:
    """Argv running the clone's tests quietly, writing no cache into it.

    Args:
        clone: The sandbox clone.

    Returns:
        The argv; test paths may be appended.
    """
    return [
        *_clone_tool(clone, "python"),
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    ]


def _numstat(clone: Path, base: str) -> tuple[int, int, int]:
    """Files, lines added and removed since *base*, uncommitted work included.

    Args:
        clone: The sandbox clone.
        base: The base tag.

    Returns:
        ``(files, additions, deletions)``.
    """
    files = added = removed = 0
    diff = run_git("diff", "--numstat", f"{base}^{{commit}}", cwd=clone)
    for line in diff.splitlines():
        plus, minus, *_ = line.split("\t")
        files += 1
        added += int(plus) if plus.isdigit() else 0
        removed += int(minus) if minus.isdigit() else 0
    return files, added, removed


def run_check(
    check: Check, index: int, clone: Path, data: Path, runner: Runner
) -> bool:
    """Run one outcome check in the clone; a hidden test file is removed after.

    Args:
        check: The check.
        index: Its position in the task, naming a hidden test file.
        clone: The sandbox clone.
        data: The bench data directory.
        runner: Command runner.

    Returns:
        Whether it held.
    """
    if check.kind == "pytest":
        nodes = list(check.nodes)
        hidden = clone / f"tests/test_bench_hidden_{index}.py" if check.hidden else None
        if hidden is not None and check.hidden:
            install_file(data / "files" / check.hidden, hidden)
            nodes.append(str(hidden.relative_to(clone)))
        argv = _pytest(clone)
        try:
            return runner([*argv, *nodes], cwd=clone, timeout=_CHECK_TIMEOUT_S).ok
        finally:
            if hidden is not None:
                hidden.unlink(missing_ok=True)
    try:
        text = (clone / (check.path or "")).read_text(encoding="utf-8")
    except OSError:
        return False
    if check.kind == "symbol" and check.name:
        name = re.escape(check.name)
        return bool(re.search(rf"(?m)^(?:def|class)\s+{name}\b", text))
    if check.kind == "contains" and check.pattern:
        return bool(re.search(check.pattern, text))
    return False


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
    words = found.stdout.split() if found.ok else []
    return int(words[0]) if words and words[0].isdigit() else None


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
    return sum(int(n) for n in pages.stdout.split() if n.isdigit()) if pages.ok else 0


def collect(
    outcome: BenchOutcome, marker: Marker, task: Task, data: Path, runner: Runner
) -> None:
    """Fill *outcome* with what the clone and its transcripts show.

    Tokens and time are the run's branch in the clone's transcripts since
    ``start``. The suite and pre-commit verdict are read before the hidden
    checks add (then remove) their test files.

    Args:
        outcome: The run's record, filled in place.
        marker: The run.
        task: Its task.
        data: The bench data directory.
        runner: Command runner.
    """
    clone = Path(marker.clone)
    started = parse_ts(marker.started_at)
    since = started - _START_SLACK if started else None
    transcripts = default_transcripts_dir(clone)
    if transcripts is not None:
        keep = TranscriptFilter(since=since, branches=frozenset({marker.branch}))
        outcome.summary = branch_usage([transcripts], keep).summary(
            marker.branch, after=since
        )
    outcome.files, outcome.additions, outcome.deletions = _numstat(
        clone, marker.base_tag
    )
    count = run_git("rev-list", "--count", f"{marker.base_tag}..HEAD", cwd=clone)
    outcome.commits = int(count) if count.isdigit() else 0
    suite = _pytest(clone)
    outcome.tests_green = runner(suite, cwd=clone, timeout=_CHECK_TIMEOUT_S).ok
    verdict = [*_clone_tool(clone, "forge-precommit"), "--verdict"]
    outcome.precommit_ok = runner(verdict, cwd=clone, timeout=_CHECK_TIMEOUT_S).ok
    outcome.checks_total = len(task.checks)
    outcome.checks_passed = sum(
        run_check(check, i, clone, data, runner) for i, check in enumerate(task.checks)
    )


def close_out(outcome: BenchOutcome, marker: Marker, slug: str, runner: Runner) -> None:
    """Record the PR facts, close the PR unmerged and delete the run's branch.

    The branch is deleted on GitHub through forge's lease-pinned seam, which
    refuses the sandbox's base branch; the local branch is kept.

    Args:
        outcome: The run's record, filled in place.
        marker: The run.
        slug: The sandbox.
        runner: Command runner.
    """
    clone = Path(marker.clone)
    number = _find_pr(slug, marker.branch, runner, clone)
    if number is not None:
        outcome.pr_opened = True
        outcome.wrapups_posted = _wrapups(slug, number, runner, clone)
        closed = runner(
            ["gh", "pr", "close", str(number), "--repo", slug],
            cwd=clone,
            timeout=_GH_TIMEOUT_S,
        )
        if not closed.ok:
            logger.warning("forge-metrics bench: PR not closed: %s", _tail(closed))
    remote = run_git(
        "ls-remote",
        "origin",
        f"refs/heads/{marker.branch}",
        cwd=clone,
        check=False,
        env=network_git_env(clone),
        timeout=PUSH_TIMEOUT_S,
    )
    sha = remote.split()[0] if remote else ""
    if sha:
        deleted = delete_remote_branch(
            clone, marker.branch, expected_sha=sha, base_branch=SANDBOX_BASE
        )
        if not deleted.ok:
            logger.warning("forge-metrics bench: branch kept: %s", deleted.stderr)


def finish_run(
    *,
    data: Path,
    record: Callable[[BenchOutcome], None],
    clone: Path | None = None,
    runner: Runner = run_command,
) -> BenchOutcome:
    """Record the started run, close its PR, delete its branch, clear the marker.

    Args:
        data: The bench data directory.
        record: Stores the outcome (the database writer).
        clone: The local clone; the marker's when ``None``.
        runner: Command runner.

    Returns:
        What the run measured.

    Raises:
        BenchError: No run started, its task unknown, or the sandbox refused.
    """
    marker = read_marker()
    if marker is None:
        msg = "forge-metrics bench: no run started; `bench start <task>` first"
        raise BenchError(msg)
    if clone is not None:
        marker = replace(marker, clone=str(clone.resolve()))
    task = load_tasks(data).get(marker.task_id)
    if task is None:
        msg = f"forge-metrics bench: unknown task {marker.task_id!r} in the marker"
        raise BenchError(msg)
    sandbox = _require_sandbox()
    check_access(sandbox.slug, runner, Path(marker.clone))
    outcome = BenchOutcome(
        run_id=uuid.uuid4().hex,
        version=clone_forge_version(Path(marker.clone)),
        task_id=task.id,
        run_n=marker.run_n,
        base_tag=marker.base_tag,
        started_at=marker.started_at,
    )
    collect(outcome, marker, task, data, runner)
    close_out(outcome, marker, sandbox.slug, runner)
    outcome.ended_at = _now()
    record(outcome)
    marker_path().unlink(missing_ok=True)
    return outcome
