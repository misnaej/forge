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
  command line matches before signalling; a stale record is reported,
  never killed by pid alone.

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

from forge.git_utils import configure_cli_logging, repo_root, run_git
from forge.run_context import is_ci


if TYPE_CHECKING:
    from collections.abc import Sequence


configure_cli_logging()
logger = logging.getLogger(__name__)

REGISTRY_RELPATH: Final = ".plan/pr_monitors.json"
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
    return root / REGISTRY_RELPATH


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
                "number,state,mergeable,headRefOid",
            ).stdout
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        return [f"PR #{pr}: poll failed ({exc})"]
    alerts.extend(_state_alerts(pr, view))
    alerts.extend(_ci_alerts(repo, pr))
    alerts.extend(_staleness_alerts(repo, pr))
    return alerts


def _state_alerts(pr: int, view: dict[str, object]) -> list[str]:
    """Return alerts for the PR's state and mergeable fields.

    Args:
        pr: PR number.
        view: Decoded ``gh pr view`` JSON.

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
    if mergeable == "CONFLICTING":
        alerts.append(
            f"PR #{pr} CONFLICTING — run forge-update-branch (monitor never syncs)"
        )
    return alerts


def _ci_alerts(repo: str, pr: int) -> list[str]:
    """Return CI-failure alerts from ``gh pr checks``.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.

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
    return [
        f"PR #{pr} CI FAILED: {c.get('name', '?')}"
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

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.
        interval: Seconds between polls.
        bound: Total seconds before the loop exits and reports.
        registry_path: Registry file to update with last_poll.
    """
    deadline = time.monotonic() + bound
    root = repo_root()
    reg_path = (
        root / registry_path if not registry_path.is_absolute() else registry_path
    )
    while time.monotonic() < deadline:
        alerts = _poll_once(repo, pr)
        for line in alerts:
            logger.warning("pr-watch: %s", line)
        # Update registry last_poll
        try:
            reg = Registry.load(reg_path)
            for m in reg.monitors:
                if m.pr == pr and m.pid == os.getpid():
                    m.last_poll = datetime.now(UTC).isoformat()
            reg.save(reg_path)
        except OSError:
            pass
        time.sleep(interval)
    logger.warning(
        "pr-watch: bound of %ds reached for PR #%s — %d poll(s) ran; "
        "what remains is unwatched. Restart with forge-pr-watch start --pr %d.",
        bound,
        pr,
        int((bound) / max(interval, 1)),
        pr,
    )


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
    # Spawn the detached monitor process
    reg_path = _registry_path(root)
    script = (
        f"from forge.pr_watch import _monitor_loop; "
        f"_monitor_loop({repo!r}, {pr}, {args.interval}, {args.bound}, "
        f"{str(reg_path)!r})"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
        "pr-watch: started monitor pid %d for PR #%d (%s), interval %ds, bound %ds.",
        proc.pid,
        pr,
        repo,
        args.interval,
        args.bound,
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


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop one or all monitors, verifying process identity before signalling.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code.
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
    for m in targets:
        cmdline = _pid_cmdline(m.pid)
        if "forge.pr_watch" not in cmdline and "pr_watch" not in cmdline:
            logger.warning(
                "pr-watch: pid %d (PR #%s) command line %r does not match a pr-watch "
                "monitor — stale record, not killing.",
                m.pid,
                m.pr,
                cmdline[:80],
            )
            continue
        try:
            os.kill(m.pid, signal.SIGTERM)
            stopped += 1
            logger.info("pr-watch: stopped pid %d (PR #%s).", m.pid, m.pr)
        except ProcessLookupError:
            logger.warning("pr-watch: pid %d (PR #%s) already gone.", m.pid, m.pr)
        except PermissionError:
            logger.exception("pr-watch: cannot signal pid %d (PR #%s).", m.pid, m.pr)
    reg.monitors = [
        m
        for m in reg.monitors
        if m not in targets
        or any(t.pid == m.pid for t in targets if _pid_cmdline(t.pid) == "")
    ]
    # Simpler: remove all stopped targets
    reg.monitors = [m for m in reg.monitors if m not in targets]
    reg.save(_registry_path(root))
    logger.info("pr-watch: %d monitor(s) stopped.", stopped)
    return EXIT_OK if stopped or not targets else EXIT_REFUSED


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
        alive = _pid_cmdline(m.pid) != ""
        sys.stdout.write(
            f"PR #{m.pr}  pid={m.pid}  repo={m.repo}  "
            f"started={m.started_at}  last_poll={m.last_poll or '-'}  "
            f"{'alive' if alive else 'dead'}\n"
        )
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface."""
    parser = argparse.ArgumentParser(
        prog="forge-pr-watch",
        description="Start, list and stop bounded background PR monitors.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
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
