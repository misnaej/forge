"""install-forge-bootstrap — one-shot consumer onboarding.

Runs every forge installer + generator a consumer needs in the order
defined by the :data:`STEPS` tuple below. Each sub-step is already
idempotent (re-running this CLI is safe and cheap), so consumers can
re-run after a forge upgrade to pull in any new artifacts the latest
forge produces.

Flags:

- ``--check``    Dry-run: report what each step would do without writing
                 (only steps whose underlying CLI supports ``--check`` honor
                 it strictly; others just print their intent).
- ``--skip``     Repeatable. Skip a step by name (e.g. ``--skip labels``).
- ``--strict``   Abort on the first failed step. Default is continue-on-fail.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from forge.git_utils import configure_cli_logging, repo_root
from forge.run_context import is_non_interactive


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    """One bootstrap step.

    Attributes:
        slug: Short identifier used by ``--skip`` (e.g. ``"githooks"``).
        cli: Console-script name to invoke.
        argv: Extra CLI args (without the leading CLI name).
        supports_check: Whether the underlying CLI honors ``--check``.
            When False and ``--check`` is passed, the step prints its
            intent but does not execute.
        gate: Optional pre-flight predicate. Returns a string reason when
            the step should self-skip (e.g. labels skips when ``gh`` is
            missing). ``None`` means "always runs".
    """

    slug: str
    cli: str
    argv: tuple[str, ...] = ()
    supports_check: bool = False
    gate: Callable[[Path], str | None] | None = None


def _gate_skip_in_ci(_root: Path) -> str | None:
    """Skip a step when running non-interactively per FOUNDATION §15.

    Used for dev-loop-only steps that add no value to a CI runner:
    ``forge-doctor`` checks gh auth + Claude Code plugin install (both
    legitimately missing in CI); ``forge-audit-deps`` reports the
    repo's dependency graph (the consumer's CI typically runs its own
    dep-scan with stricter tooling).

    Args:
        _root: Repo root (unused; signature matches the gate protocol).

    Returns:
        A human-readable reason when the step should self-skip; ``None``
        when the run is interactive and the step should execute.
    """
    if is_non_interactive():
        return "non-interactive context (FOUNDATION §15)"
    return None


def _gate_labels(_root: Path) -> str | None:
    """Skip ``install-forge-labels`` when ``gh`` or the GitHub remote is missing.

    Args:
        _root: Repo root (unused; signature matches the gate protocol).

    Returns:
        A human-readable reason when the step should self-skip, or
        ``None`` when prerequisites are satisfied.
    """
    if shutil.which("gh") is None:
        return "gh CLI not on PATH"
    proc = subprocess.run(
        ["git", "remote"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return "no git remote configured"
    return None


# `--refresh` is mandatory: install-forge-githooks is idempotent and
# leaves managed hook files alone after the first install, so a forge
# package upgrade leaves the hook content (and its version marker)
# stale until a subsequent git operation triggers the post-merge auto-
# refresh. Passing --refresh unconditionally makes every bootstrap pass
# pick up the current forge version.
STEPS: tuple[Step, ...] = (
    Step(slug="githooks", cli="install-forge-githooks", argv=("--refresh",)),
    Step(slug="claude-md", cli="install-forge-claude-md", supports_check=True),
    Step(slug="labels", cli="install-forge-labels", gate=_gate_labels),
    Step(slug="api-digest", cli="forge-gen-api-digest", supports_check=True),
    Step(slug="cli-reference", cli="forge-gen-cli-reference", supports_check=True),
    Step(
        slug="audit-deps",
        cli="forge-audit-deps",
        argv=("--tree",),
        gate=_gate_skip_in_ci,
    ),
    Step(slug="doctor", cli="forge-doctor", gate=_gate_skip_in_ci),
)


def _run_step(step: Step, *, check_mode: bool, root: Path) -> int:
    """Execute one bootstrap step. Return its exit code.

    Args:
        step: Step to run.
        check_mode: When True and the step supports ``--check``, run with
            ``--check``. When True and the step doesn't, just announce
            intent and return 0.
        root: Repo root (passed to the gate predicate).

    Returns:
        Exit code from the step's underlying CLI. ``0`` for self-skipped
        or check-mode-no-op steps.
    """
    if step.gate is not None:
        reason = step.gate(root)
        if reason is not None:
            logger.info("⏭  %-14s skipped (%s)", step.slug, reason)
            return 0

    if shutil.which(step.cli) is None:
        logger.error("✗ %-14s '%s' not on PATH", step.slug, step.cli)
        return 127

    argv = [step.cli, *step.argv]
    if check_mode:
        if step.supports_check:
            argv.append("--check")
        else:
            logger.info(
                "…  %-14s (--check unsupported; would run %s)", step.slug, step.cli
            )
            return 0

    logger.info("→ %-14s %s", step.slug, " ".join(argv))
    proc = subprocess.run(argv, check=False)
    return proc.returncode


def _resolve_steps(skip: Iterable[str]) -> list[Step]:
    """Return the ordered step list with *skip* entries removed.

    Args:
        skip: Step slugs to drop. Unknown slugs are logged as warnings
            but do not abort the run.

    Returns:
        The filtered step list, in dependency order.
    """
    skip_set = set(skip)
    known = {s.slug for s in STEPS}
    for slug in skip_set - known:
        logger.warning(
            "! unknown --skip slug %r (known: %s)", slug, ", ".join(sorted(known))
        )
    return [s for s in STEPS if s.slug not in skip_set]


def main() -> int:
    """Run every install / generator step in order. Return non-zero on failure.

    Returns:
        ``0`` when every executed step succeeded; otherwise the count of
        failed steps (capped at 99 so the exit code stays in shell-friendly
        range).
    """
    parser = argparse.ArgumentParser(
        prog="install-forge-bootstrap",
        description=(
            "One-shot consumer onboarding to forge's full capability set. "
            "Runs every install-forge-* installer + every forge-gen-* / "
            "forge-audit-* generator in dependency order. Idempotent."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Dry-run. Each step that supports --check runs in check mode; "
            "others just print their intent."
        ),
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="SLUG",
        help=(
            "Skip a step by slug. Repeatable. Known slugs: "
            + ", ".join(s.slug for s in STEPS)
            + "."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Abort on the first failed step. Default is continue-on-fail.",
    )
    args = parser.parse_args()

    root = repo_root()
    steps = _resolve_steps(args.skip)
    logger.info(
        "forge-bootstrap — %d step(s)%s",
        len(steps),
        " (--check)" if args.check else "",
    )
    logger.info("=" * 70)

    failed = 0
    for step in steps:
        rc = _run_step(step, check_mode=args.check, root=root)
        if rc != 0:
            failed += 1
            logger.error("✗ %-14s exit %d", step.slug, rc)
            if args.strict:
                logger.error("aborting (--strict)")
                break

    logger.info("=" * 70)
    logger.info(
        "forge-bootstrap — %d/%d step(s) passed",
        len(steps) - failed,
        len(steps),
    )
    return min(failed, 99)


if __name__ == "__main__":
    sys.exit(main())
