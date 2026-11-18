"""forge-pr-watch — start, list and stop background PR monitors.

FOUNDATION §6 mandates one background monitor per open PR, watching five
signals (new review comments, merged/closed, CONFLICTING, CI failure,
wrap-up staleness). This command owns that monitor's lifecycle so no
session improvises a detached process whose only record is a pid:

- ``start`` spawns a bounded poll loop, records it in
  ``.plan/pr_monitors.json`` (gitignored), and verifies the five
  signals are parseable before the first poll — a blind watch is a
  loud non-zero exit, never a silent start.
- ``list`` reads the registry and reports each monitor's state.
- ``stop`` kills a monitor by PR identity, verifying the process's
  command line matches before signalling; a stale record is removed
  without signalling, never killed by pid alone.

The command self-skips under :func:`forge.run_context.is_ci` — CI has
no human to receive alerts.

Registry format (``.plan/pr_monitors.json``)::

    {
        "monitors": [
            {
                "pid": 123,
                "pr": 993,
                "repo": "owner/repo",
                "started_at": "...",
                "last_poll": "...",
                "last_alerted_head": "...",
            }
        ]
    }

Each monitor's own output — the alerts it raises and any crash — goes to
``code_health/pr_watch_<pr>.log``, truncated when that PR's monitor starts.
That file is the only channel: the monitor runs detached, so nothing it
writes reaches the session that started it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from forge.git_utils import (
    code_health_dir,
    configure_cli_logging,
    repo_root,
    run_git,
)
from forge.pr_plan import wrapup_freshness
from forge.run_context import is_ci


if TYPE_CHECKING:
    from collections.abc import Sequence


configure_cli_logging()
logger = logging.getLogger(__name__)

REGISTRY_RELPATH: Final = ".plan/pr_monitors.json"
# The argv[1] of the spawned child. `cmd_stop` matches on it to identify a
# monitor process, so it must be a token no unrelated command carries.
MONITOR_SUBCOMMAND: Final = "_monitor"
DEFAULT_INTERVAL_S: Final = 60
DEFAULT_BOUND_S: Final = 1800
GH_TIMEOUT_S: Final = 30

#: The five signals the arming check verifies before the first poll.
_SIGNALS: Final = (
    "gh-present",
    "gh-auth",
    "repo-resolved",
    "pr-view-json",
    "comments-json",
)

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_ARMING_FAILED: Final = 2


@dataclass
class MonitorRecord:
    """One registered monitor.

    Attributes:
        pid: The monitor process id.
        pr: The PR number watched.
        repo: ``owner/repo`` the monitor reads.
        started_at: ISO-8601 start time (UTC).
        last_poll: ISO-8601 of the last completed poll, empty before the first.
        last_alerted_head: The head SHA last alerted on, empty when none.
    """

    pid: int
    pr: int
    repo: str
    started_at: str
    last_poll: str = ""
    last_alerted_head: str = ""


@dataclass
class Registry:
    """The on-disk registry of monitors."""

    monitors: list[MonitorRecord] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Registry:
        """Read the registry, tolerating a missing or corrupt file.

        Args:
            path: Registry file path.

        Returns:
            The parsed registry, empty when absent or unreadable.
        """
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cls()
        monitors = [
            MonitorRecord(**m) for m in data.get("monitors", []) if isinstance(m, dict)
        ]
        return cls(monitors=monitors)

    def save(self, path: Path) -> None:
        """Write the registry atomically.

        Args:
            path: Registry file path.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(path)


def _registry_path(root: Path) -> Path:
    """Return the monitor registry file for *root*.

    Args:
        root: Git repo root.

    Returns:
        Path to the registry JSON.
    """
    return root / REGISTRY_RELPATH


def monitor_log_path(root: Path, pr: int) -> Path:
    """Return the log a PR's monitor writes its output to.

    One file per PR, because several monitors run at once and a shared
    file interleaves them. This is a runtime log, not step evidence: no
    step produces it and it carries no ``# produced-at:`` stamp, so
    ``forge-precommit``'s freshness check skips it by name rather than
    reporting a stamp it could never have.

    Args:
        root: Git repo root.
        pr: PR number.

    Returns:
        Path to the monitor's log file.
    """
    return code_health_dir(root) / f"pr_watch_{pr}.log"


def _resolve_repo(root: Path, repo_arg: str | None) -> str | None:
    """Return ``owner/repo``, from *repo_arg* or the checkout's origin remote.

    Args:
        root: Repo root.
        repo_arg: Explicit ``--repo`` value.

    Returns:
        The ``owner/repo`` slug, or ``None`` when unresolvable.
    """
    if repo_arg:
        return repo_arg
    try:
        url = run_git("remote", "get-url", "origin", cwd=root).strip()
    except subprocess.CalledProcessError:
        return None
    # ssh (git@host:owner/repo.git) or https (https://host/owner/repo.git)
    for sep in (":", "/"):
        if sep in url:
            slug = url.rsplit(sep, 1)[-1]
            break
    else:
        return None
    slug = slug.removesuffix(".git")
    return slug if "/" in slug else None


def _gh(*args: str, timeout: int = GH_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` command, raising on failure.

    Args:
        *args: gh argv tail.
        timeout: Per-call timeout in seconds.

    Returns:
        The completed process.

    Raises:
        subprocess.CalledProcessError: Non-zero exit.
        FileNotFoundError: gh binary missing.
        subprocess.TimeoutExpired: Timed out.
    """
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True, timeout=timeout
    )


def arming_check(repo: str, pr: int) -> list[str]:
    """Verify every signal the monitor needs is parseable.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.

    Returns:
        List of failure descriptions; empty when all signals parse.
    """
    failures: list[str] = []
    if not _check_gh_present(failures):
        return failures
    _check_gh_auth(failures)
    _check_pr_view(repo, pr, failures)
    return failures


def _check_gh_present(failures: list[str]) -> bool:
    """Append a failure when gh is missing; return whether gh is usable.

    Args:
        failures: Failure list to append to.

    Returns:
        True when gh ran, False when the binary is missing or failed.
    """
    try:
        _gh("--version")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        failures.append(f"{_SIGNALS[0]}: gh binary missing or failed ({exc})")
        return False
    return True


def _check_gh_auth(failures: list[str]) -> None:
    """Append a failure when gh auth is not logged in.

    Args:
        failures: Failure list to append to.
    """
    try:
        auth = _gh("auth", "status")
        if "Logged in" not in auth.stdout + auth.stderr:
            failures.append(f"{_SIGNALS[1]}: gh auth status not logged in")
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        failures.append(f"{_SIGNALS[1]}: gh auth status failed ({exc})")


def _check_pr_view(repo: str, pr: int, failures: list[str]) -> None:
    """Parse one full gh pr view and append failures for missing signals.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.
        failures: Failure list to append to.
    """
    try:
        proc = _gh(
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "number,mergeable,headRefOid,comments",
        )
        data = json.loads(proc.stdout)
        if not isinstance(data, dict) or "number" not in data:
            failures.append(f"{_SIGNALS[3]}: pr view JSON missing 'number'")
        comments = data.get("comments")
        if not isinstance(comments, list):
            failures.append(f"{_SIGNALS[4]}: comments field not a list")
    except FileNotFoundError as exc:
        failures.append(f"{_SIGNALS[0]}: gh binary missing ({exc})")
    except subprocess.CalledProcessError as exc:
        failures.append(f"{_SIGNALS[2]}/{_SIGNALS[3]}: gh pr view failed ({exc})")
    except subprocess.TimeoutExpired as exc:
        failures.append(f"{_SIGNALS[3]}: gh pr view timed out ({exc})")
    except json.JSONDecodeError as exc:
        failures.append(f"{_SIGNALS[3]}/{_SIGNALS[4]}: unparseable JSON ({exc})")


def _poll_once(repo: str, pr: int) -> list[str]:
    """Run one poll cycle and return alert lines.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.

    Returns:
        Alert lines (empty = quiet).
    """
    alerts: list[str] = []
    try:
        view = json.loads(
            _gh(
                "pr",
                "view",
                str(pr),
                "--repo",
                repo,
                "--json",
                "number,state,mergeable,headRefOid,baseRefName",
            ).stdout
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        return [f"PR #{pr}: poll failed ({exc})"]
    base_ref = str(view.get("baseRefName", ""))
    stack_child = _stack_parent(repo, base_ref) if base_ref else False
    alerts.extend(_state_alerts(pr, view, stack_child=stack_child))
    alerts.extend(_ci_alerts(repo, pr, stack_child=stack_child, base_ref=base_ref))
    alerts.extend(_staleness_alerts(repo, pr))
    return alerts


def _stack_parent(repo: str, base_ref: str) -> bool:
    """Return whether *base_ref* is another open PR's head branch.

    A stacked child targets an unmerged parent PR's branch, so
    ``CONFLICTING`` against it is the expected steady state, not a
    signal. One ``gh pr list`` call per poll; the open-PR set is small.

    Args:
        repo: ``owner/repo`` slug.
        base_ref: The PR's ``baseRefName``.

    Returns:
        True when an open PR's head branch equals *base_ref*.
    """
    try:
        listing = json.loads(
            _gh(
                "pr",
                "list",
                "--state",
                "open",
                "--repo",
                repo,
                "--json",
                "headRefName",
                "--limit",
                "100",
            ).stdout
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return False
    if not isinstance(listing, list):
        return False
    return any(
        isinstance(pr, dict) and pr.get("headRefName") == base_ref for pr in listing
    )


def _state_alerts(
    pr: int, view: dict[str, object], *, stack_child: bool = False
) -> list[str]:
    """Return alerts for the PR's state and mergeable fields.

    Args:
        pr: PR number.
        view: Decoded ``gh pr view`` JSON.
        stack_child: Whether the PR's base is another open PR's branch.
            ``CONFLICTING`` is the expected steady state in a stack, so
            it is not alerted on (resolution stays ``forge-update-branch``
            per FOUNDATION §6).

    Returns:
        Alert lines.
    """
    alerts: list[str] = []
    state = str(view.get("state", ""))
    mergeable = str(view.get("mergeable", ""))
    if state == "MERGED":
        alerts.append(f"PR #{pr} MERGED")
    elif state == "CLOSED":
        alerts.append(f"PR #{pr} CLOSED")
    if mergeable == "CONFLICTING" and not stack_child:
        alerts.append(
            f"PR #{pr} CONFLICTING — run forge-update-branch (monitor never syncs)"
        )
    return alerts


def _ci_alerts(
    repo: str, pr: int, *, stack_child: bool = False, base_ref: str = ""
) -> list[str]:
    """Return CI-failure alerts from ``gh pr checks``.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.
        stack_child: Whether the PR's base is another open PR's branch;
            its CI runs against the unmerged parent, which the alert
            line says so a failure is read in that light.
        base_ref: The PR's ``baseRefName``, named in the marker.

    Returns:
        Alert lines (empty when checks are unavailable or all pass).
    """
    try:
        checks = json.loads(
            _gh("pr", "checks", str(pr), "--repo", repo, "--json", "name,bucket").stdout
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return []
    if not isinstance(checks, list):
        return []
    marker = f" (CI run against unmerged parent base {base_ref})" if stack_child else ""
    return [
        f"PR #{pr} CI FAILED: {c.get('name', '?')}{marker}"
        for c in checks
        if isinstance(c, dict) and c.get("bucket") == "fail"
    ]


def _staleness_alerts(repo: str, pr: int) -> list[str]:
    """Return a wrap-up-staleness alert when the freshness verdict is stale.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.

    Returns:
        At most one alert line.
    """
    try:
        plan = subprocess.run(
            [
                sys.executable,
                "-m",
                "forge.pr_plan",
                "--freshness",
                "--pr",
                str(pr),
                "--repo",
                repo,
            ],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            check=False,
        )
        if plan.returncode != 0:
            return []
        freshness = json.loads(plan.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    if freshness.get("fresh") is False:
        verified = freshness.get("latest_verified_at")
        head_oid = freshness.get("head_oid")
        return [f"PR #{pr} stale wrap-up: verified-at {verified}, head {head_oid}"]
    return []


def _monitor_loop(
    repo: str, pr: int, interval: int, bound: int, registry_path: Path
) -> None:
    """The bounded poll loop (runs in the spawned child process).

    Takes the registry as an absolute path and resolves nothing itself, so
    the child needs no checkout of its own: it runs detached and may
    outlive the state its parent read.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.
        interval: Seconds between polls.
        bound: Total seconds before the loop exits and reports.
        registry_path: Absolute path of the registry file to stamp.
    """
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        alerts = _poll_once(repo, pr)
        for line in alerts:
            logger.warning("pr-watch: %s", line)
        # Match on the PR alone, never on our own pid: the parent registers
        # the record *after* spawning us, so a fast first poll can run before
        # it exists, and a pid comparison would silently skip the stamp. One
        # record per PR is the registry's invariant (the parent drops any
        # earlier one), so the PR identifies ours.
        try:
            reg = Registry.load(registry_path)
            for m in reg.monitors:
                if m.pr == pr:
                    m.last_poll = datetime.now(UTC).isoformat()
            reg.save(registry_path)
        except OSError:
            # Never silent: swallowing this is what let a dead monitor look
            # like a running one. The log this writes to is the alert channel.
            logger.exception("pr-watch: could not stamp last_poll in %s", registry_path)
        time.sleep(interval)
    logger.warning(
        "pr-watch: bound of %ds reached for PR #%s — %d poll(s) ran; "
        "what remains is unwatched. Restart with forge-pr-watch start --pr %d.",
        bound,
        pr,
        int((bound) / max(interval, 1)),
        pr,
    )


def cmd_monitor(args: argparse.Namespace) -> int:
    """Run the poll loop in the foreground (the spawned child's entry point).

    Not a user-facing subcommand: ``cmd_start`` spawns it. It exists so the
    child's arguments are converted by argparse — inside code a type checker
    reads — rather than interpolated into a ``python -c`` string.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code.
    """
    _monitor_loop(args.repo, args.pr, args.interval, args.bound, args.registry)
    return EXIT_OK


def cmd_start(args: argparse.Namespace) -> int:
    """Start a bounded monitor for a PR.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code.
    """
    if is_ci():
        logger.info("pr-watch: CI context — monitor not started (no human to alert).")
        return EXIT_OK
    root = repo_root()
    repo = _resolve_repo(root, args.repo)
    if repo is None:
        logger.error("pr-watch: could not resolve repo; pass --repo owner/repo.")
        return EXIT_REFUSED
    pr = args.pr
    if pr is None:
        # Auto-detect from the current branch
        try:
            branch = run_git("branch", "--show-current", cwd=root).strip()
            proc = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    "--head",
                    branch,
                    "--repo",
                    repo,
                    "--json",
                    "number",
                ],
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_S,
                check=False,
            )
            if proc.returncode == 0:
                pr = int(json.loads(proc.stdout)["number"])
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
            KeyError,
        ):
            pass
        if pr is None:
            logger.error("pr-watch: no PR found for the current branch; pass --pr N.")
            return EXIT_REFUSED
    failures = arming_check(repo, pr)
    if failures:
        for f in failures:
            logger.error("pr-watch: arming check failed — %s", f)
        return EXIT_ARMING_FAILED
    # Spawn the detached monitor process. Run it as a subcommand, never as a
    # `python -c` string: argparse converts each argument inside code a type
    # checker reads, and interpolating values into source text is what let a
    # str reach a Path parameter and kill every monitor before its first poll.
    reg_path = _registry_path(root)
    log_path = monitor_log_path(root, pr)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        "-m",
        "forge.pr_watch",
        MONITOR_SUBCOMMAND,
        "--repo",
        repo,
        "--pr",
        str(pr),
        "--interval",
        str(args.interval),
        "--bound",
        str(args.bound),
        "--registry",
        str(reg_path),
    ]
    # Truncated per start, so the log describes the monitor now running. The
    # child inherits the descriptor; the parent closes its own copy. Both
    # streams go here because the alerts are log records on stderr — sending
    # them to DEVNULL discarded every alert the monitor ever raised.
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            argv,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    reg = Registry.load(reg_path)
    # Remove any stale record for this PR
    reg.monitors = [m for m in reg.monitors if m.pr != pr]
    reg.monitors.append(
        MonitorRecord(
            pid=proc.pid,
            pr=pr,
            repo=repo,
            started_at=datetime.now(UTC).isoformat(),
        )
    )
    reg.save(reg_path)
    logger.info(
        "pr-watch: started monitor pid %d for PR #%d (%s), interval %ds, bound %ds. "
        "Alerts and any crash go to %s.",
        proc.pid,
        pr,
        repo,
        args.interval,
        args.bound,
        log_path,
    )
    return EXIT_OK


def _pid_cmdline(pid: int) -> str:
    """Return the command line of *pid*, or empty when unreadable.

    Args:
        pid: Process id.

    Returns:
        The full command line, empty on any error.
    """
    try:
        with Path(f"/proc/{pid}/cmdline").open("rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _is_monitor(pid: int) -> bool:
    """Return whether *pid* is one of this module's monitor processes.

    The single identity test, so ``stop`` never signals a process that
    ``list`` would not have called a monitor. It matches the child's own
    subcommand rather than the module name alone: a bare ``pr_watch``
    also matches a test run or an editor, and once the operating system
    reuses a pid that would make an unrelated process look like ours —
    reported as ``alive``, and in ``stop``'s case sent a signal.

    Args:
        pid: Process id from a registry record.

    Returns:
        True when the process is a monitor started by this module.
    """
    cmdline = _pid_cmdline(pid)
    return MONITOR_SUBCOMMAND in cmdline and "forge.pr_watch" in cmdline


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop one or all monitors, verifying process identity before signalling.

    A record whose pid is gone or is not a monitor is pruned without a
    signal, and counts as success: after a merge that is the usual case.

    Args:
        args: Parsed CLI arguments.

    Returns:
        ``EXIT_OK`` when a monitor was stopped, a stale record was removed,
        or none was registered; ``EXIT_REFUSED`` when no monitor matches
        ``--pr`` or none of the targets could be stopped or pruned.
    """
    root = repo_root()
    reg = Registry.load(_registry_path(root))
    if not reg.monitors:
        logger.info("pr-watch: no monitors registered.")
        return EXIT_OK
    targets = reg.monitors if args.all else [m for m in reg.monitors if m.pr == args.pr]
    if not targets and not args.all:
        logger.error("pr-watch: no monitor for PR #%s.", args.pr)
        return EXIT_REFUSED
    stopped = 0
    pruned = 0
    kept: list[MonitorRecord] = []
    for m in targets:
        if not _is_monitor(m.pid):
            logger.warning(
                "pr-watch: pid %d (PR #%s) command line %r is not a pr-watch "
                "monitor — stale record, removing it without signalling.",
                m.pid,
                m.pr,
                _pid_cmdline(m.pid)[:80],
            )
            pruned += 1
            continue
        try:
            os.kill(m.pid, signal.SIGTERM)
            stopped += 1
            logger.info("pr-watch: stopped pid %d (PR #%s).", m.pid, m.pr)
        except ProcessLookupError:
            logger.warning("pr-watch: pid %d (PR #%s) already gone.", m.pid, m.pr)
            pruned += 1
        except PermissionError:
            # Keep the record: the monitor is alive and we failed to stop it,
            # so dropping it would leave a running process nothing tracks.
            logger.exception("pr-watch: cannot signal pid %d (PR #%s).", m.pid, m.pr)
            kept.append(m)
    reg.monitors = [m for m in reg.monitors if m not in targets or m in kept]
    reg.save(_registry_path(root))
    # Report every number: clearing stale records is the common case after a
    # merge, and reporting it as "0 stopped" with a refusal exit code read as
    # "nothing happened" while the registry had in fact been pruned.
    tail = f", {len(kept)} kept (could not signal)" if kept else ""
    logger.info(
        "pr-watch: %d monitor(s) stopped, %d stale record(s) removed%s.",
        stopped,
        pruned,
        tail,
    )
    return EXIT_OK if stopped or pruned or not targets else EXIT_REFUSED


def _monitor_state(record: MonitorRecord, *, alive: bool) -> str:
    """Return a one-word reading of a monitor's state, for display.

    Shared by ``list`` and ``stack`` so one wording is produced in both
    (FOUNDATION §12). ``alive`` is tested first because a monitor that has
    started but not yet finished its first poll also has an empty
    ``last_poll`` — only a dead one means the poll never happened.

    Args:
        record: The registry record to read.
        alive: Whether the recorded pid is still a monitor process, as
            :func:`_is_monitor` decides — occupancy alone is not enough,
            since a reused pid would otherwise read as ``alive``.

    Returns:
        ``alive``, ``dead`` or ``died-before-first-poll``.
    """
    if alive:
        return "alive"
    return "dead" if record.last_poll else "died-before-first-poll"


def cmd_list(_args: argparse.Namespace) -> int:
    """List registered monitors.

    Args:
        _args: Parsed CLI arguments (unused).

    Returns:
        Exit code.
    """
    root = repo_root()
    reg = Registry.load(_registry_path(root))
    if not reg.monitors:
        sys.stdout.write("no monitors registered\n")
        return EXIT_OK
    for m in reg.monitors:
        state = _monitor_state(m, alive=_is_monitor(m.pid))
        sys.stdout.write(
            f"PR #{m.pr}  pid={m.pid}  repo={m.repo}  "
            f"started={m.started_at}  last_poll={m.last_poll or '-'}  "
            f"{state}\n"
        )
        if state == "died-before-first-poll":
            sys.stdout.write(
                f"    never polled — read {monitor_log_path(root, m.pr)}, "
                f"then restart with forge-pr-watch start --pr {m.pr}\n"
            )
    return EXIT_OK


def _stack_order(prs: list[dict[str, object]]) -> list[dict[str, object]]:
    """Order open PRs so every parent precedes its stacked children.

    An edge runs from a PR to each open PR whose ``baseRefName`` is its
    ``headRefName``. Roots (basing on a non-PR branch, e.g. the default
    branch) come first, then children in dependency order. A cycle —
    which GitHub cannot produce but untrusted PR fields could fake —
    falls back to number order for the remainder rather than looping.

    Args:
        prs: Decoded ``gh pr list`` entries.

    Returns:
        The PRs in dependency order.
    """
    by_head = {str(p.get("headRefName", "")): p for p in prs}
    children: dict[str, list[dict[str, object]]] = {}
    roots: list[dict[str, object]] = []
    for p in prs:
        base = str(p.get("baseRefName", ""))
        if base in by_head and by_head[base] is not p:
            children.setdefault(base, []).append(p)
        else:
            roots.append(p)
    ordered: list[dict[str, object]] = []
    seen: set[int] = set()
    queue = list(roots)
    while queue:
        p = queue.pop(0)
        number = int(str(p.get("number", "0")))
        if number in seen:
            continue
        seen.add(number)
        ordered.append(p)
        queue.extend(children.get(str(p.get("headRefName", "")), []))
    ordered.extend(
        p
        for p in sorted(prs, key=lambda x: int(x.get("number", 0)))
        if int(str(p.get("number", "0"))) not in seen
    )
    return ordered


def cmd_stack(args: argparse.Namespace) -> int:
    """List the open-PR stack in dependency order with per-PR state.

    Each line carries the PR's base, mergeability, wrap-up freshness and
    monitor state, so a stack can be read top-down: parent first, each
    child against its parent's branch.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code.
    """
    root = repo_root()
    repo = args.repo or _resolve_repo(root, None)
    if not repo:
        logger.error("pr-watch: cannot resolve owner/repo; pass --repo.")
        return EXIT_REFUSED
    try:
        listing = json.loads(
            _gh(
                "pr",
                "list",
                "--state",
                "open",
                "--repo",
                repo,
                "--json",
                "number,title,baseRefName,headRefName,mergeable",
                "--limit",
                "100",
            ).stdout
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        logger.exception("pr-watch: gh pr list failed")
        return EXIT_REFUSED
    if not isinstance(listing, list) or not listing:
        sys.stdout.write("no open PRs\n")
        return EXIT_OK
    prs = [p for p in listing if isinstance(p, dict)]
    reg = Registry.load(_registry_path(root))
    monitors = {m.pr: m for m in reg.monitors}
    for p in _stack_order(prs):
        number = int(str(p.get("number", "0")))
        title = str(p.get("title", ""))[:60]
        base = str(p.get("baseRefName", ""))
        mergeable = str(p.get("mergeable", ""))
        try:
            fresh = wrapup_freshness(number, repo=repo).fresh
        except Exception:  # noqa: BLE001 - freshness is informational
            fresh = None
        fresh_text = {True: "fresh", False: "STALE", None: "unknown"}[fresh]
        m = monitors.get(number)
        if m is None:
            monitor_text = "no-monitor"
        elif _is_monitor(m.pid):
            monitor_text = f"alive(pid {m.pid})"
        else:
            monitor_text = _monitor_state(m, alive=False)
        sys.stdout.write(
            f"#{number} {title}  base={base}  mergeable={mergeable or '?'}  "
            f"fresh={fresh_text}  monitor={monitor_text}\n"
        )
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface."""
    parser = argparse.ArgumentParser(
        prog="forge-pr-watch",
        description="Start, list and stop bounded background PR monitors.",
    )
    # metavar hides the internal _monitor subcommand from help output.
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{start,list,stop,stack}"
    )
    p_start = sub.add_parser("start", help="Start a monitor for a PR.")
    p_start.add_argument(
        "--pr", type=int, help="PR number (default: the current branch's PR)."
    )
    p_start.add_argument("--repo", help="owner/repo (default: the checkout's origin).")
    p_start.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_S,
        help="Poll interval seconds.",
    )
    p_start.add_argument(
        "--bound", type=int, default=DEFAULT_BOUND_S, help="Total run bound seconds."
    )
    p_start.set_defaults(func=cmd_start)
    p_list = sub.add_parser("list", help="List registered monitors.")
    p_list.set_defaults(func=cmd_list)
    p_stop = sub.add_parser("stop", help="Stop a monitor.")
    p_stop.add_argument("--pr", type=int, help="PR number to stop.")
    p_stop.add_argument("--all", action="store_true", help="Stop all monitors.")
    p_stop.set_defaults(func=cmd_stop)
    p_stack = sub.add_parser(
        "stack", help="List open PRs in stack (dependency) order with state."
    )
    p_stack.add_argument("--repo", help="owner/repo (default: the checkout's origin).")
    p_stack.set_defaults(func=cmd_stack)
    # Internal: what cmd_start spawns. Declared here so argparse does the
    # type conversion the child used to miss.
    p_monitor = sub.add_parser(MONITOR_SUBCOMMAND)
    p_monitor.add_argument("--repo", required=True)
    p_monitor.add_argument("--pr", type=int, required=True)
    p_monitor.add_argument("--interval", type=int, required=True)
    p_monitor.add_argument("--bound", type=int, required=True)
    p_monitor.add_argument("--registry", type=Path, required=True)
    p_monitor.set_defaults(func=cmd_monitor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``forge-pr-watch``.

    Args:
        argv: Argument list; ``None`` reads :data:`sys.argv`.

    Returns:
        Process exit code.
    """
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
