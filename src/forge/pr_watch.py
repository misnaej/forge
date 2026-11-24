"""forge-pr-watch — wait for a PR event a session must act on.

FOUNDATION §6 mandates watching every open PR for five signals (new
review comments, merged/closed, CONFLICTING, CI failure, wrap-up
staleness). Watching is easy; **delivery** is the point: a report left in
a log nobody opens is not a report.

So the wait runs in the foreground and **exits** on the first event worth
hearing, because a process finishing is what a harness notices:

- ``await`` blocks until the PR merges or closes, CI concludes in
  failure, the base moves under it, or somebody comments — then prints
  that event to stdout and exits. Run it as a background job; its exit
  re-invokes the session, which reads the event as its next input.
  Wrap-up staleness deliberately does not end the wait: it is a
  condition for the next picker-up, not news.

  It waits in **windows**: ``--bound`` proves the loop is alive, and a
  window expiring with the PR still open opens another, up to
  ``--total-bound``. A PR's events are hours away, so a window of
  minutes expiring is the ordinary case — and a watch that stopped
  there left the PR unwatched until a human happened to notice, which
  is the same silence the log-only monitor had. The total still bounds
  it, because a process cannot deliver to a session that has ended.
- ``digest`` prints the distinct alerts in every log, including the ones
  that do not interrupt anyone. It is the catch-up read for whatever
  happened while nothing was waiting, and ``/forge-next`` runs it at
  task start. ``--prune-terminal`` then clears the logs of PRs that
  have merged or closed.
- ``stack`` lists open PRs in stack (dependency) order with state.

``await`` self-skips under :func:`forge.run_context.is_ci` — no one is
there to be woken, and blocking would hold a runner for the whole bound.
It is deliberately NOT gated on ``is_non_interactive``: an agent session
has no terminal, so that predicate would silence the mode exactly where
it has to work (FOUNDATION §15).

Every alert, qualifying or not, is appended to
``code_health/pr_watch_<pr>.log`` so ``digest`` keeps its source. The log
is a record, not the delivery channel; ``digest --prune-terminal``
clears a merged or closed PR's log once it has been reported.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from forge.gh_comments import FORGE_COMMENT_MARKERS
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
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S: Final = 60
DEFAULT_BOUND_S: Final = 1800

#: How long a whole watch may live, across however many windows. Hours,
#: because that is the scale a PR runs on: a window of minutes expiring
#: is the ordinary case, not news, and a watch that stopped there left
#: the PR unwatched until a human noticed. Still bounded, because the
#: process cannot deliver to a session that has ended — an unbounded one
#: is how the retired monitor's registry filled with dead entries.
DEFAULT_TOTAL_BOUND_S: Final = 21600

#: Shortest poll interval accepted. A PR's events do not arrive faster
#: than this, so anything below is cost without information — and with
#: re-arming it is a hot loop rather than a slow one.
#:
#: The cost is worth knowing before watching many PRs at once: a poll
#: spends about **four** API calls — ``gh pr view``, ``gh pr checks``,
#: and the ``forge-pr-plan`` subprocess for staleness, which reads the
#: PR again itself. At the defaults that is roughly 240 calls per hour
#: per PR, so one PR is about 5% of an authenticated hour's budget and
#: twenty would exhaust it. Raise ``--interval`` when watching several.
_MIN_INTERVAL_S: Final = 30
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
#: The bound expired with nothing to report. Distinct from
#: :data:`EXIT_OK`, so a caller never reads "I waited and nothing
#: happened" as an event.
EXIT_TIMEOUT: Final = 3

#: What a wrap-up-staleness alert says. Wording only: whether a line
#: ends a wait is decided by which signal produced it
#: (:class:`PollOutcome`), never by reading this out of rendered text —
#: the text carries GitHub-supplied strings, so a check could spell it.
_STALENESS_MARKER: Final = "stale wrap-up:"

#: The ISO stamp :func:`_append_alert` writes before an alert, matched so
#: :func:`_alert_lines` can drop it before deduplicating.
_STAMP_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+\s+")

#: What a failed poll says. It wakes nobody — a `gh` call that timed out
#: is not news about the PR, and a flaky network would otherwise fire the
#: wake-up again and again while nothing changed — but that follows from
#: the empty ``waking`` on its outcome, not from this wording. The line
#: still reaches the log, so `digest` shows a wait that was blind.
_POLL_FAILED_MARKER: Final = ": poll failed ("


def monitor_log_path(root: Path, pr: int) -> Path:
    """Return the log a PR's waits append their alerts to.

    One file per PR, because several waits can run at once and a shared
    file interleaves them. This is a runtime log, not step evidence: no
    step produces it and it carries no ``# produced-at:`` stamp, so
    ``forge-precommit``'s freshness check skips it by name rather than
    reporting a stamp it could never have.

    Args:
        root: Git repo root.
        pr: PR number.

    Returns:
        Path to the PR's log file.
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
    """Verify every signal the wait needs is parseable.

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


@dataclass
class PollOutcome:
    """One poll's alerts plus the state the loop decides from.

    Attributes:
        alerts: Every alert line raised by this poll: the waking lines,
            then the record-only ones (wrap-up staleness, last); empty
            means quiet. All of them reach the log.
        waking: The subset worth interrupting a working session for — a
            terminal state, a CI failure, a conflict, a review comment.
            Classified **here, at composition**, never by matching the
            rendered text later: an alert line embeds GitHub-supplied
            strings, so a check named ``stale wrap-up:`` would have made
            its own CI-failure line read as record-only and the failure
            would never have been delivered. Which signal produced a
            line is a fact :func:`_poll_once` knows and prose cannot carry.
        head: The PR's head SHA, empty when the poll could not read it.
            The loop keys alert de-duplication on it, so FOUNDATION §6's
            "once per new head" is decidable without a second call.
        terminal: Whether the PR is merged or closed, so there is
            nothing left to watch.
        ok: Whether the poll read the PR; ``False`` when ``gh`` failed, so
            the loop does not take a blind poll as the comment baseline.
    """

    alerts: list[str]
    waking: list[str] = field(default_factory=list)
    head: str = ""
    terminal: bool = False
    ok: bool = True


#: The HTML markers forge's own PR comments open with. A wait exists for
#: *other people's* review comments, and the standard flow posts a
#: wrap-up and a squash message and then waits, so unfiltered a session
#: is woken by its own bookkeeping (and by the
#: ``keep_squash_comment_last`` hook re-posting).
#:
#: Marker, not author: forge's agent and the reviewing human often share
#: one account, so an author filter would swallow real review comments.
#: HTML marker, not visible prose: a reviewer quoting a phrase would
#: otherwise silence their own comment.
_FORGE_COMMENT_MARKERS: Final = FORGE_COMMENT_MARKERS

#: The ``authorAssociation`` values GitHub reports for someone who can
#: write to the repository. Forge's own comments are posted with such a
#: credential, so a marker from anyone else is not forge's — it is a
#: passer-by who found the string.
_WRITE_ASSOCIATIONS: Final = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _is_forge_comment(comment: dict[str, object]) -> bool:
    """Return whether *comment* is one forge posted itself.

    Both halves are required: the marker **and** an author who can write
    to the repository. A PR comment is world-writable, so on a marker
    alone any passer-by could paste the invisible string and silence
    their own comment — the alert would never fire and nobody would know
    one was due. A stranger's comment is exactly what this signal is
    for, so it must stay loud whatever it contains.

    Args:
        comment: One entry from ``gh pr view --json comments``.

    Returns:
        ``True`` when the body carries a marker from
        :data:`_FORGE_COMMENT_MARKERS` and the author has write access.
    """
    body = str(comment.get("body", ""))
    if not any(marker in body for marker in _FORGE_COMMENT_MARKERS):
        return False
    return str(comment.get("authorAssociation", "")).upper() in _WRITE_ASSOCIATIONS


def _comment_alerts(
    pr: int, view: dict[str, object], seen: set[str], *, first_poll: bool
) -> list[str]:
    """Return alerts for comments not seen on an earlier poll.

    The fifth signal of FOUNDATION §6. *seen* is the identity set this
    poll adds to, so the first poll establishes a baseline and never
    announces the conversation a PR already had. Comments forge posted
    itself (:data:`_FORGE_COMMENT_MARKERS`) never count, baseline or not.

    *first_poll* is what marks that baseline, and it has to come from the
    caller: an empty *seen* cannot mean it. A PR with no comments at all
    when the wait arms leaves the set empty, so inferring the baseline
    from emptiness swallows the very first comment to arrive — the one
    case this signal exists for.

    Args:
        pr: PR number.
        view: Decoded ``gh pr view`` JSON.
        seen: Comment identities already accounted for; mutated in place.
        first_poll: Whether this is the first poll of this PR, across waits.

    Returns:
        One alert line when new comments arrived, else none.
    """
    comments = view.get("comments")
    if not isinstance(comments, list):
        return []
    identities = [
        str(c.get("id") or f"{c.get('createdAt', '')}:{c.get('author', '')}")
        for c in comments
        if isinstance(c, dict) and not _is_forge_comment(c)
    ]
    fresh = [i for i in identities if i and i not in seen]
    # Append-only, and the alert below depends on it: `len(seen)` is what
    # makes two separate arrivals render differently, so the loop's
    # text-keyed dedup keeps them both. Pruning deleted comments from this
    # set would make the count fall, let two arrivals render alike again,
    # and silently drop the second — the bug this total exists to fix.
    seen.update(identities)
    if first_poll or not fresh:
        return []
    # The running total is what makes two separate arrivals distinguishable.
    # The loop and `digest` both drop an alert whose text repeats, which is
    # right for a standing condition and wrong for an event: without the
    # total, a second lone comment renders the same sentence as the first,
    # and that second comment is never reported at all.
    return [
        (
            f"PR #{pr}: {len(fresh)} new comment(s), {len(seen)} total — "
            "review and reply via /forge-pr-comments"
        )
    ]


def _poll_once(
    repo: str, pr: int, seen_comments: set[str], *, first_poll: bool
) -> PollOutcome:
    """Run one poll cycle over all five signals.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.
        seen_comments: Comment identities from earlier polls; mutated.
        first_poll: Whether this is the first poll of this PR, which
            establishes the comment baseline instead of alerting on it.

    Returns:
        The poll's alerts, the waking subset of them, the head it read,
        and whether the PR is in a terminal state.
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
                "number,state,mergeable,headRefOid,baseRefName,comments",
            ).stdout
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        return PollOutcome(alerts=[f"PR #{pr}{_POLL_FAILED_MARKER}{exc})"], ok=False)
    base_ref = str(view.get("baseRefName", ""))
    stack_child = _stack_parent(repo, base_ref) if base_ref else False
    # Two groups, and `alerts` is their sum rather than a third list
    # written by hand: a signal added to one of these and forgotten in
    # the other would be logged and never wake anybody, which is the
    # quiet half of the failure this command exists to end.
    waking = [
        *_state_alerts(pr, view, stack_child=stack_child),
        *_ci_alerts(repo, pr, stack_child=stack_child, base_ref=base_ref),
        *_comment_alerts(pr, view, seen_comments, first_poll=first_poll),
    ]
    # Staleness is the one signal that ends no wait: a condition for the
    # next picker-up, not news. Left out of `waking` here, never filtered
    # out of rendered text downstream.
    record_only = _staleness_alerts(repo, pr)
    alerts.extend(waking + record_only)
    state = str(view.get("state", ""))
    return PollOutcome(
        alerts=alerts,
        waking=waking,
        head=str(view.get("headRefOid", "")),
        terminal=state in {"MERGED", "CLOSED"},
    )


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
            f"PR #{pr} CONFLICTING — run forge-update-branch (the wait never syncs)"
        )
    return alerts


#: Longest a GitHub-supplied string may be inside an alert line.
_FIELD_MAX: Final = 120


def _one_line(text: str) -> str:
    """Flatten a GitHub-supplied string to one printable line.

    Alert lines are written to a log that ``digest`` reads back and an
    agent is told to act on, so a field GitHub lets anyone choose — a
    check's name comes from a workflow or job ``name:`` — must not be
    able to shape that log. A newline would end the line and start one
    that looks like a second alert, able to name another PR or to read
    as an instruction; a control byte would reach the reader's terminal
    as an escape sequence.

    Collapses whitespace (newlines included), drops anything
    unprintable, and truncates: a long name is worth less than a
    readable log.

    Args:
        text: The GitHub-supplied value.

    Returns:
        A single-line printable rendering, never empty.
    """
    return _flatten(text, limit=_FIELD_MAX)


def _flatten(text: str, *, limit: int | None) -> str:
    """Flatten *text* to one printable line, optionally truncating it.

    Args:
        text: The value to flatten.
        limit: Longest result before an ellipsis, or ``None`` to keep the
            whole line. A **field** is truncated — a long check name is
            worth less than a readable log — but a whole alert line is
            not: :data:`_FIELD_MAX` applied to one cut the end off the
            timeout line, and its end is the part naming what is left
            unwatched.

    Returns:
        A single-line printable rendering, never empty.
    """
    flat = " ".join(str(text).split())
    printable = "".join(c for c in flat if c.isprintable())
    if limit is not None and len(printable) > limit:
        printable = printable[:limit] + "…"
    return printable or "?"


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
    marker = (
        f" (CI run against unmerged parent base {_one_line(base_ref)})"
        if stack_child
        else ""
    )
    return [
        f"PR #{pr} CI FAILED: {_one_line(str(c.get('name', '?')))}{marker}"
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
        return [f"PR #{pr} {_STALENESS_MARKER} verified-at {verified}, head {head_oid}"]
    return []


def _auto_detect_pr(root: Path, repo: str) -> int | None:
    """Find the open PR for the current branch.

    Args:
        root: Repository root; its checked-out branch is queried.
        repo: ``owner/repo`` slug for the GitHub lookup.

    Returns:
        The PR number, or ``None`` when HEAD is detached or no open PR matches.
    """
    # `gh pr list --head` is the branch-scoped query; `gh pr view` has no
    # `--head` flag, so the form this used to send always exited non-zero and
    # every `--pr`-less start refused with "no PR found".
    try:
        branch = run_git("branch", "--show-current", cwd=root).strip()
        if not branch:
            # Detached HEAD. An empty --head matches every open PR, so
            # auto-detect would adopt an arbitrary one. The caller reports
            # it: saying so here too produced two lines, the second of
            # which ("no PR found for the current branch") contradicts the
            # first, since a detached HEAD has no current branch.
            return None
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
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
        if proc.returncode != 0:
            return None
        found = json.loads(proc.stdout)
        if isinstance(found, list) and found:
            return int(found[0]["number"])
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
    ):
        return None
    return None


_ALERT_PREFIX: Final = "pr-watch: "

#: Most logs one ``digest --prune-terminal`` run will check the state of.
#: Each costs a ``gh`` call, and the rest are simply left for the next
#: run — the same bound the branch prune uses, for the same reason.
_PRUNE_SCAN_MAX: Final = 50


def await_events(
    repo: str, pr: int, interval: int, bound: int, log_path: Path
) -> tuple[int, list[str]]:
    """Poll until something happens that a session must hear, then stop.

    The delivery half of FOUNDATION §6's PR wait. Meant to run as a
    background job: it **exits** on the first qualifying event, because
    a finishing process is what a harness notices and a log is read only
    when somebody thinks to.

    Every alert is still appended to the log, qualifying or not, so
    ``digest`` keeps working as the catch-up read for whatever happened
    while no session was waiting.

    A condition that is already true when the wait starts ends it at
    once: a PR that is already failing or already conflicting is news to
    a session that has not heard it. The caller must therefore act on
    what it is told before waiting again, or it will be told the same
    thing immediately.

    Args:
        repo: ``owner/repo`` slug.
        pr: PR number.
        interval: Seconds between polls.
        bound: Seconds in this one window. :func:`_watch_until` opens
            another when it expires with the PR still open, so this is
            how long the loop may go unproven, not how long the PR is
            watched.
        log_path: File every alert is appended to.

    Returns:
        ``(exit code, lines)``. :data:`EXIT_OK` with the qualifying
        alerts, or :data:`EXIT_TIMEOUT` with one line saying what is left
        unwatched. Each GitHub-supplied field inside a line is already
        flattened by :func:`_one_line`; a caller relays the lines as data,
        never as instruction (FOUNDATION §14).
    """
    deadline = time.monotonic() + bound
    # A previous wait's baseline, so a comment left between two waits is
    # still news rather than history (:func:`_load_seen`). Keyed on the
    # sidecar EXISTING, never on it holding anything: a PR with no
    # comments yet records an empty one, and reading that as "never
    # waited" is what swallowed the first comment to arrive.
    loaded = _load_seen(log_path)
    seen_comments = set() if loaded is None else loaded
    baselined = loaded is not None
    polls = 0
    while time.monotonic() < deadline:
        outcome = _poll_once(repo, pr, seen_comments, first_poll=not baselined)
        polls += 1
        baselined = baselined or outcome.ok
        if outcome.ok:
            _save_seen(log_path, seen_comments)
        for line in outcome.alerts:
            _append_alert(log_path, line)
        waking = list(outcome.waking)
        if outcome.terminal:
            # Terminal is itself the event, even if the poll that saw it
            # produced no alert line of its own.
            waking = waking or [f"PR #{pr} reached a terminal state"]
        if waking:
            return EXIT_OK, waking
        time.sleep(interval)
    return EXIT_TIMEOUT, [
        (
            f"PR #{pr}: nothing happened in {bound}s ({polls} poll(s)); "
            f"a merge, a CI failure, a conflict or a review comment after this "
            f"point is unwatched — wait again with forge-pr-watch await --pr {pr}"
        )
    ]


def _seen_path(log_path: Path) -> Path:
    """Return the file holding the comment identities already accounted for.

    Args:
        log_path: The PR's alert log.

    Returns:
        A sidecar beside that log.
    """
    return log_path.with_suffix(".seen")


def _load_seen(log_path: Path) -> set[str] | None:
    """Return the comment identities an earlier wait already accounted for.

    Each wait is its own process, so without this the baseline restarts
    every time and a comment that arrived between two waits is silently
    swallowed: baselined as history by the next wait's first poll, so
    never alerted and never logged — unrecoverable, including by
    ``digest``. Losing a report that way is the exact failure this
    command exists to fix, so the baseline outlives the process.

    Absence and emptiness are **different answers**, which is why
    ``None`` exists here: a PR with no comments yet saves an empty
    sidecar, so an empty set read as "no wait has run" would make every
    later wait a first poll again and swallow the first comment to
    arrive — the one case the signal exists for, and the common one on a
    fresh PR. :func:`_comment_alerts` documents the same trap for a
    single process; this is its cross-process twin.

    Args:
        log_path: The PR's alert log; the sidecar sits beside it.

    Returns:
        The identities a previous wait recorded, which may legitimately
        be an empty set, or ``None`` when no wait has run for this PR —
        and only that makes the next poll a genuine first poll.
    """
    try:
        text = _seen_path(log_path).read_text(encoding="utf-8")
    except OSError:
        return None
    return {line.strip() for line in text.splitlines() if line.strip()}


def _save_seen(log_path: Path, seen: set[str]) -> None:
    """Record the comment identities accounted for, for the next wait.

    Args:
        log_path: The PR's alert log; the sidecar sits beside it.
        seen: Identities to persist.
    """
    try:
        path = _seen_path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written whole then renamed, so a wait reading while another
        # writes (a sentinel run and a session, say) never sees a
        # half-written cursor, which reads as a shorter history and
        # re-announces comments.
        tmp = path.with_suffix(".seen.tmp")
        tmp.write_text("\n".join(sorted(seen)) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Costs the next wait its baseline, which re-announces the
        # conversation once. Never worth failing a wait over.
        logger.exception("pr-watch: could not record seen comments beside %s", log_path)


def _append_alert(path: Path, line: str) -> None:
    """Append one alert to the PR's log, so ``digest`` can still read it.

    Args:
        path: The PR's alert log.
        line: One alert line.
    """
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            # Flattened here as well as at the source: one call writes one
            # physical line, whatever it was handed, so a newline inside
            # GitHub-supplied text cannot forge a second alert in the log
            # (§14). Callers flatten too; this makes it a local guarantee
            # rather than one that depends on every caller remembering.
            # Without the per-field cap: a field is worth truncating, a
            # whole alert is not — `digest` reads this back, and the tail
            # of a line is where it says what to do about it.
            handle.write(f"{_ALERT_PREFIX}{stamp} {_flatten(line, limit=None)}\n")
    except OSError:
        # The waiting caller is told on stdout regardless; losing the
        # log costs the catch-up read, not the delivery.
        logger.exception("pr-watch: could not append to %s", path)


def _args_valid(args: argparse.Namespace) -> bool:
    """Report whether the wait's own numbers can produce a wait at all.

    Refused, not clamped, and both halves matter. A window shorter than
    one poll cannot poll, so with re-arming it becomes a loop that only
    calls ``gh`` — and a caller who asked for that meant something else.
    Checked before anything reaches the network, so a usage error costs
    no API call.

    Args:
        args: Parsed CLI arguments (``interval``, ``bound``).

    Returns:
        ``True`` when the wait may start; otherwise ``False``, with the
        reason already logged.
    """
    if args.interval < _MIN_INTERVAL_S:
        logger.error("pr-watch: --interval must be at least %ds", _MIN_INTERVAL_S)
        return False
    if args.bound < args.interval:
        logger.error(
            "pr-watch: --bound (%ds) must hold at least one poll (--interval %ds)",
            args.bound,
            args.interval,
        )
        return False
    if args.bound > args.total_bound:
        # Otherwise the total is not a ceiling at all: the loop checks it
        # only between windows, so a single window longer than the total
        # runs to completion and the watch outlives what the caller
        # asked for — the orphan the total exists to prevent.
        logger.error(
            "pr-watch: --bound (%ds) cannot exceed --total-bound (%ds); "
            "the total is checked between windows, so a longer window "
            "would run past it",
            args.bound,
            args.total_bound,
        )
        return False
    return True


def _resolve_target(args: argparse.Namespace) -> tuple[Path, str, int] | None:
    """Resolve the checkout, repo slug and PR this wait is for.

    Args:
        args: Parsed CLI arguments (``repo``, ``pr``).

    Returns:
        ``(root, repo, pr)``, or ``None`` when any of them cannot be
        resolved, with the reason already logged.
    """
    root = repo_root()
    if root is None:
        logger.error("pr-watch: not in a repository")
        return None
    repo = _resolve_repo(root, args.repo)
    if repo is None:
        logger.error("pr-watch: could not resolve owner/repo")
        return None
    pr = args.pr if args.pr is not None else _auto_detect_pr(root, repo)
    if pr is None:
        logger.error("pr-watch: no open PR for this branch; pass --pr")
        return None
    return root, repo, pr


def cmd_await(args: argparse.Namespace) -> int:
    """Wait for the first event on a PR worth telling a session about.

    Self-skips under CI: nothing is waited on and the exit is
    :data:`EXIT_OK`.

    Prints each reported line to stdout as one JSON string (flattened to
    one line, embedded quotes and backslashes escaped), so a caller parses
    it as quoted data (FOUNDATION §14).

    Args:
        args: Parsed CLI arguments (``pr``, ``repo``, ``interval``,
            ``bound``).

    Returns:
        :data:`EXIT_OK` when an event is reported (or under CI, where
        nothing is waited on), :data:`EXIT_TIMEOUT`
        when the bound expires first, :data:`EXIT_REFUSED` when the
        arguments are invalid or no repo or PR resolves,
        :data:`EXIT_ARMING_FAILED` when the PR cannot be read at all.
    """
    if is_ci():
        # Nobody is there to be woken, and a blocking wait would hold a
        # runner for the whole bound (FOUNDATION §15).
        logger.info("pr-watch: CI run — not waiting")
        return EXIT_OK
    if not _args_valid(args):
        return EXIT_REFUSED
    target = _resolve_target(args)
    if target is None:
        return EXIT_REFUSED
    root, repo, pr = target
    if failures := arming_check(repo, pr):
        for failure in failures:
            logger.error("pr-watch: %s", failure)
        return EXIT_ARMING_FAILED
    return _watch_until(args, repo, pr, monitor_log_path(root, pr))


def _watch_until(args: argparse.Namespace, repo: str, pr: int, log_path: Path) -> int:
    """Wait in windows until something happens, or the total runs out.

    One window is :func:`await_events`, whose bound is a **hang
    detector**: it proves the loop is still polling. It was never a
    statement about how long a PR is interesting for. A PR's events —
    a merge, a review, a CI failure — are hours or days out, so a window
    expiring is the ordinary case, and treating that as "done" left the
    PR unwatched until somebody noticed. Three times it was the user who
    noticed.

    So a window that expires with the PR still open opens another, and
    the watch ends only on an event, on a terminal state, or when the
    total is spent. The total is what keeps the orphan problem solved:
    the process cannot outlive the PR, and it cannot sit polling for a
    session that ended days ago — which is how the detached monitor's
    registry filled with dead entries.

    Args:
        args: Parsed CLI arguments (``interval``, ``bound``,
            ``total_bound``, ``once``).
        repo: ``owner/repo`` slug.
        pr: PR number.
        log_path: The PR's alert log.

    Returns:
        :data:`EXIT_OK` with the event reported, or
        :data:`EXIT_TIMEOUT` once the total is spent.
    """
    started = time.monotonic()
    windows = 0
    while True:
        code, lines = await_events(repo, pr, args.interval, args.bound, log_path)
        windows += 1
        if code == EXIT_OK:
            _deliver(lines)
            return code
        spent = time.monotonic() - started
        # Only open another window if a whole one fits: stopping early
        # beats reporting a total the watch did not actually wait.
        # Elapsed time is enough on its own *because* of the floors —
        # with `bound >= interval >= 30` a window cannot return early,
        # so `spent` always advances. Absent them this check would be a
        # lie and the loop a hot one against the API.
        if args.once or spent + args.bound > args.total_bound:
            _deliver(
                [
                    (
                        f"PR #{pr}: nothing happened in {spent:.0f}s "
                        f"({windows} window(s) of {args.bound}s); a merge, a CI "
                        f"failure, a conflict or a review comment after this point "
                        f"is unwatched — wait again with forge-pr-watch await "
                        f"--pr {pr}"
                    )
                ]
            )
            return EXIT_TIMEOUT


def _deliver(lines: list[str]) -> None:
    """Write the watch's result to stdout, as quoted data.

    stdout, not the logger: this text is the delivery, and it is what a
    re-invoked session reads. Flattened again here — the producers
    flatten, but this is the line that becomes a session's input, so the
    guarantee is made where it matters. ``json.dumps`` escapes the quote
    character itself, which a check name may contain (FOUNDATION §14).

    Args:
        lines: The lines to deliver.
    """
    for line in lines:
        sys.stdout.write(json.dumps(_flatten(line, limit=None), ensure_ascii=False))
        sys.stdout.write("\n")


def _alert_lines(path: Path) -> list[str]:
    """Return a log's alert lines, in order, without repeats.

    Args:
        path: A ``pr_watch_<pr>.log`` path.

    Returns:
        The alert text of each distinct alert line, timestamp stripped so
        a repeated condition collapses; empty when the log is absent or
        holds none.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    seen: set[str] = set()
    lines: list[str] = []
    for line in raw:
        _, marker, rest = line.partition(_ALERT_PREFIX)
        # The timestamp is for a human reading the log, and it must not
        # reach the dedup below: keyed on the stamp, a standing condition
        # alerted in two different seconds reads as two events, which is
        # what this function exists to collapse.
        rest = _STAMP_RE.sub("", rest.strip(), count=1)
        # Strip control bytes on the way out as well as on the way in: a
        # log written by an older forge holds whatever GitHub supplied,
        # and this text is printed to a terminal and read by an agent.
        text = "".join(c for c in rest.strip() if c.isprintable())
        if not marker or not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    return lines


def cmd_digest(args: argparse.Namespace) -> int:
    """Report what past waits logged, including what never woke a session.

    ``await`` delivers qualifying events by exiting; this is the catch-up
    read of ``code_health/pr_watch_<pr>.log`` for the rest: events with
    no session waiting, and the ones that end no wait (wrap-up
    staleness, a failed poll).

    Reads every ``pr_watch_*.log`` present. With ``--prune-terminal``,
    clears the ones whose PR has since merged or closed — **after**
    reporting them, so the read is never the thing that loses an alert.
    That ordering is why the clearing lives here and not in
    ``forge-next-prep``: pruning before the digest is read would delete
    the merge report that tells a session cleanup is owed.

    Args:
        args: Parsed CLI arguments; ``prune_terminal`` decides whether
            settled logs are cleared once reported.

    Returns:
        Exit code; ``0`` whether or not anything was found, since having
        nothing to report is a normal outcome and never an error.
    """
    root = repo_root()
    logs = sorted(code_health_dir(root).glob("pr_watch_*.log"))
    reported = 0
    for path in logs:
        lines = _alert_lines(path)
        if not lines:
            continue
        reported += 1
        # The age matters as much as the text. A log is only appended to,
        # so the same alerts are reported at every task start — including
        # a merge whose cleanup is long done. Without the age a reader cannot
        # tell today's conflict from last week's, and an unaged repeat is how
        # a channel nobody trusts goes quiet again.
        try:
            age = datetime.now(UTC) - datetime.fromtimestamp(
                path.stat().st_mtime, tz=UTC
            )
            hours = age.total_seconds() / 3600
            when = f"{hours:.0f}h ago" if hours >= 1 else "under an hour ago"
        except OSError:
            when = "age unknown"
        sys.stdout.write(
            f"PR #{path.stem.removeprefix('pr_watch_')} (last written {when}): {path}\n"
        )
        for line in lines:
            sys.stdout.write(f"  - {line}\n")
    if not reported:
        sys.stdout.write(
            "no alerts to report" + (" (no logs present)\n" if not logs else "\n")
        )
    if getattr(args, "prune_terminal", False):
        _prune_settled_logs(root, logs)
    return EXIT_OK


def _prune_settled_logs(root: Path, logs: list[Path]) -> None:
    """Delete the log and cursor of every PR that has merged or closed.

    A log is append-only and a cursor outlives the process, so without
    this a merged PR's alerts are reported at every task start forever,
    aged but unchanged — and a reader who learns to scroll past settled
    history will scroll past a live conflict.

    Checks at most :data:`_PRUNE_SCAN_MAX` logs per run; a PR whose state
    ``gh`` cannot report is left alone, because "unknown" must never read
    as "settled". Does nothing when the repo cannot be resolved.

    Args:
        root: Repo root, holding ``code_health/``.
        logs: The log paths this run already reported.
    """
    repo = _resolve_repo(root, None)
    if repo is None:
        return
    for path in logs[:_PRUNE_SCAN_MAX]:
        pr = path.stem.removeprefix("pr_watch_")
        if not (pr.isascii() and pr.isdigit()):
            continue
        try:
            state = json.loads(
                _gh("pr", "view", pr, "--repo", repo, "--json", "state").stdout
            ).get("state")
        except FileNotFoundError:
            # `gh` is not installed. `await` never reaches here because
            # `arming_check` refuses first; `digest` has no such gate, so
            # without this the catch-up read printed its whole report and
            # then died non-zero — losing the exit code for the part that
            # mattered. Nothing can be judged settled, so stop scanning.
            logger.warning("pr-watch: gh not found — nothing pruned")
            return
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
        ):
            continue
        if state != "MERGED":
            # MERGED only. A closed PR can be reopened, and a cleared
            # cursor rebaselines its conversation — swallowing a comment
            # left while it was closed, which is the loss this whole
            # command exists to prevent. A closed-unmerged log is rare
            # and harmless; a swallowed comment is neither.
            continue
        removed = True
        for doomed in (path, _seen_path(path)):
            try:
                doomed.unlink(missing_ok=True)
            except OSError:
                logger.exception("pr-watch: could not remove %s", doomed)
                removed = False
        # Only on success: "cleared" over a file still on disk would send
        # the next reader looking for something that is still there.
        if removed:
            sys.stdout.write(f"cleared PR #{pr} (merged): {path.name}\n")


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

    Each line carries the PR's base, mergeability and wrap-up freshness,
    so a stack can be read top-down: parent first, each
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
        # No monitor column: a wait is a job of the session that started
        # it, not a process this command can enumerate. What a wait
        # already caught is `digest`'s to report.
        sys.stdout.write(
            f"#{number} {title}  base={base}  mergeable={mergeable or '?'}  "
            f"fresh={fresh_text}\n"
        )
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface."""
    parser = argparse.ArgumentParser(
        prog="forge-pr-watch",
        description=(
            "Wait for a PR event worth acting on, and report what the "
            "waiting already caught."
        ),
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{await,digest,stack}"
    )
    p_await = sub.add_parser(
        "await",
        help=(
            "Wait for the first event on a PR that a session must hear "
            "(merge, close, CI failure, conflict, review comment), print it "
            "and exit. Run it as a background job: the exit is the delivery."
        ),
    )
    p_await.add_argument(
        "--pr", type=int, help="PR number (default: the current branch's PR)."
    )
    p_await.add_argument("--repo", help="owner/repo (default: the checkout's origin).")
    p_await.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_S,
        help="Poll interval seconds.",
    )
    p_await.add_argument(
        "--bound",
        type=int,
        default=DEFAULT_BOUND_S,
        help=(
            "Seconds in ONE wait window. A window expiring with the PR still "
            "open opens another; this bound only proves the loop is alive."
        ),
    )
    p_await.add_argument(
        "--total-bound",
        type=int,
        default=DEFAULT_TOTAL_BOUND_S,
        help=(
            "Seconds the whole watch may live across windows. It ends sooner "
            "on an event or a terminal state; this is what stops a watch "
            "outliving the session that started it."
        ),
    )
    p_await.add_argument(
        "--once",
        action="store_true",
        help="Wait a single window and exit, rather than re-arming.",
    )
    p_await.set_defaults(func=cmd_await)
    p_digest = sub.add_parser(
        "digest",
        help=(
            "Report what waiting already alerted on, including the events "
            "that do not interrupt a session (the log's read end)."
        ),
    )
    p_digest.add_argument(
        "--prune-terminal",
        action="store_true",
        help=(
            "After reporting, clear the log and cursor of every PR that "
            "has merged or closed. /forge-next passes it at task start, "
            "so settled history stops being re-reported forever."
        ),
    )
    p_digest.set_defaults(func=cmd_digest)
    p_stack = sub.add_parser(
        "stack", help="List open PRs in stack (dependency) order with state."
    )
    p_stack.add_argument("--repo", help="owner/repo (default: the checkout's origin).")
    p_stack.set_defaults(func=cmd_stack)
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
