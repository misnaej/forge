"""Detect the runtime context (interactive workstation vs. CI / automation).

Every forge tool, hook, and CLI that has divergent interactive vs.
non-interactive behavior consults this module instead of inlining its
own ``$CI``-style check. Centralising the detection means a new CI
marker added in one place reaches every tool, and the contract is
greppable for code review.

Public surface:

- :func:`is_non_interactive` — True when running without a human at the
  terminal. Used to suppress dev-loop aids (interactive prompts,
  staleness warnings recommending manual action, hard-fail exit codes
  that assume the user can fix what's missing).
- :func:`git_auth_mode` — best-effort detection of the git/pip auth
  context: ``ssh``, ``https-token``, ``https-anonymous``, or ``none``.
  Lets tooling pick a URL form that the environment can actually
  authenticate against.
- :func:`progress_logger` — context manager yielding a flushed printer
  for per-substep progress. Makes long-running CI steps observable
  (the original motivation: ``forge-upgrade --apply`` hung silently in
  CI for hours; no per-substep timing meant the root cause was
  invisible).

The detection logic is conservative: when in doubt, prefer reporting
**non-interactive** (the safer default — over-suppressing dev-loop
aids is a smaller mistake than hard-failing on an absent gh CLI in
GitHub Actions).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Literal


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


# Environment markers commonly set by CI / automation runners.
#
# Source: the canonical vendor list maintained by `watson/ci-info`
# (https://github.com/watson/ci-info/blob/master/vendors.json), the
# upstream of the widely-used `is-ci` npm package. That file enumerates
# ~50 vendors; this tuple is the curated subset that matches forge's
# likely user base (Python devs in mainstream CI providers).
#
# Selection criterion: (1) forge's own override at the top so a
# workstation user can simulate CI behaviour without faking ``CI=true``
# (which other tooling may react to); (2) the generic ``CI`` marker
# every major provider sets — the catch-all; (3) the named providers
# forge users most commonly adopt. ``CI`` alone covers ~99% of cases;
# the named providers are belt-and-suspenders for the rare runner that
# omits the generic var.
#
# Add a vendor here when a real consumer hits the gap. Niche providers
# (Drone, TeamCity, AppVeyor, Cirrus, etc.) are omitted by design:
# the generic ``CI`` catches them; adding every named provider buys
# nothing but maintenance.
_CI_MARKERS: Final[tuple[str, ...]] = (
    "FORGE_NON_INTERACTIVE",  # forge's explicit opt-in
    "CI",  # de facto standard, set by every major CI
    "GITHUB_ACTIONS",  # GitHub Actions
    "GITLAB_CI",  # GitLab CI
    "CIRCLECI",  # CircleCI
    "BUILDKITE",  # Buildkite
    "JENKINS_URL",  # Jenkins (set when running under a Jenkins pipeline)
    "TF_BUILD",  # Azure Pipelines (Team Foundation Build)
    "TRAVIS",  # Travis CI (still seen in Python projects)
)


# Environment variables that signal a usable HTTPS token for git/pip.
# Listed in priority order so :func:`git_auth_mode` reports the
# token-bearing env name the caller can grep for. ``GITHUB_TOKEN`` is
# the default in GitHub Actions; ``GH_TOKEN`` is the gh-CLI override.
_HTTPS_TOKEN_ENV: Final[tuple[str, ...]] = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
)


AuthMode = Literal["ssh", "https-token", "https-anonymous", "none"]


def is_non_interactive() -> bool:
    """Return True when running without a human at the terminal.

    Detection (any of the following → ``True``):

    - Any env var in :data:`_CI_MARKERS` is set to a non-empty value.
    - ``sys.stdin.isatty()`` is False (e.g. piped invocation, no TTY
      attached).

    Returns:
        ``True`` when the process is plausibly non-interactive.
        Conservative: prefer ``True`` over hard-failing on an absent
        interactive dependency.
    """
    if any(os.environ.get(name) for name in _CI_MARKERS):
        return True
    return not _stdin_is_tty()


def _stdin_is_tty() -> bool:
    """Return ``sys.stdin.isatty()`` defensively (handles closed stdin).

    Returns:
        ``False`` when stdin is closed, missing ``isatty``, or
        otherwise unusable; ``True`` when ``isatty`` returns truthy.
    """
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def git_auth_mode() -> AuthMode:
    """Detect the git / pip auth context the environment can actually use.

    Priority:

    1. **ssh** — an SSH agent has at least one loaded identity
       (``ssh-add -l`` exits 0 with non-empty output). The git
       subprocess inside pip will use the agent transparently.
    2. **https-token** — one of :data:`_HTTPS_TOKEN_ENV` is set.
       Callers can rewrite a ``git+ssh://`` URL to
       ``git+https://x-access-token:<token>@github.com/...`` or rely
       on gh-CLI's git credential helper.
    3. **https-anonymous** — no token, no SSH key, but stdout is a
       TTY (so pip's credential prompt could theoretically be answered
       by a human). Only safe for public repos.
    4. **none** — non-interactive AND no auth signal. Callers should
       fail loud rather than block on a credential prompt against
       ``/dev/null``.

    Returns:
        One of ``"ssh"`` / ``"https-token"`` / ``"https-anonymous"`` /
        ``"none"``.
    """
    if _ssh_agent_has_identity():
        return "ssh"
    if any(os.environ.get(name) for name in _HTTPS_TOKEN_ENV):
        return "https-token"
    if _stdin_is_tty():
        return "https-anonymous"
    return "none"


def _ssh_agent_has_identity() -> bool:
    """Return True when ``ssh-add -l`` reports at least one loaded key.

    Returns:
        ``True`` only when ``ssh-add`` is on PATH, the agent is
        reachable, and at least one identity is loaded. Any subprocess
        error (missing binary, agent not running, signal, timeout)
        yields ``False`` — caller falls through to the next auth-mode
        check.
    """
    if shutil.which("ssh-add") is None:
        return False
    try:
        proc = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # `ssh-add -l` exits 0 when identities are loaded; 1 when none
    # are present; 2 when the agent can't be reached. The non-empty
    # stdout guard skips the literal "The agent has no identities."
    # line that some implementations emit on exit 1.
    return proc.returncode == 0 and bool(proc.stdout.strip())


@contextmanager
def progress_logger(
    step_name: str,
    *,
    out: object = None,
) -> Iterator[Callable[[str], None]]:
    """Yield a flushed printer; emit start / end markers with elapsed time.

    Long-running steps (pip install, bootstrap, audit runs) that emit
    a single line and then go silent are invisible in CI logs — the
    runner has no way to tell "still working" from "deadlocked." Wrap
    them in this context manager so the substep boundary is visible
    and a follow-up timestamp shows how long it took:

    .. code-block:: python

        with progress_logger("pip install") as note:
            note("force-reinstall, no-deps")
            subprocess.run([...], check=True)

    The yielded callable writes a single line prefixed with the step
    name. Both the start banner and the yielded printer flush stdout
    after each write so the runner sees output promptly.

    Args:
        step_name: Short label printed on every line for this step
            (e.g. ``"pip install"``). Kept terse — long names eat
            screen width in CI logs.
        out: Optional file-like override (defaults to
            :data:`sys.stdout`). Tests use this to capture output
            without monkeypatching ``sys.stdout``.

    Yields:
        A printer ``note(msg: str) -> None`` that writes one flushed
        line prefixed with ``[<step_name>]``.
    """
    stream = sys.stdout if out is None else out
    start = time.monotonic()

    def _emit(line: str) -> None:
        """Write *line* prefixed with the step tag and flush.

        Args:
            line: Message body (newline added automatically).
        """
        stream.write(f"[{step_name}] {line}\n")
        flush = getattr(stream, "flush", None)
        if callable(flush):
            flush()

    _emit("start")
    try:
        yield _emit
    finally:
        elapsed = time.monotonic() - start
        _emit(f"done ({elapsed:.1f}s)")
