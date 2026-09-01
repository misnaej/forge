"""forge-pr-plan — deterministic finalization-path decision for the ``/pr`` skill.

The ``/pr`` skill used to carry the finalization-path decision (docs-only
light path vs regen-verified light path vs delta mode vs full round) as a
prose decision tree the agent re-derived every run. This CLI makes that
decision executable: it composes the :mod:`forge.pr_delta` primitives —
which own every threshold, glob, and classifier — over the actual diff and
emits one JSON object the skill consumes directly.

Composition only, by design: ``pr_delta`` stays the single source of the
classification rules; this module adds exactly the two I/O seams
``pr_delta`` deliberately does not own (``git`` for the diff, ``gh`` for
prior wrap-up comments on the delta path) and the mode→plan mapping.

Output contract (single JSON object on stdout; diagnostics go to stderr)::

    {
        "mode": "full" | "light-docs" | "light-regen" | "light-code" | "delta",
        "reporters": [...],
        "precommit_scope": [...],
        "reasons": [...],
        "classified_at": "<sha>",
    }

``reporters`` names the verification agents the skill must run.
``precommit_scope`` lists step names for ``forge-precommit --only``; empty
means the full strict battery for "full" and no pre-commit run at all for
"delta". ``reasons`` is the human-readable classification trail.
``classified_at`` is HEAD at classification time; ``pr-manager`` warns when
posting at a different HEAD.

``light-regen`` is *eligibility only*: the skill still earns the escape by
running the provenance gates (``precommit_scope`` lists them); any gate
failure falls back to the full round. ``light-code`` (small, no added
files, no ``src/`` or high-blast-radius path) skips the reporters and
authorizes the short-form ``wrapup-mode: light`` wrap-up — enforced
fail-closed by ``block_unverified_pr_create``, which re-runs this
classifier at ``gh pr create`` time; the strict pre-commit battery still
runs in full. The delta path degrades, never
crashes: no ``--pr``, a missing/unauthenticated ``gh``, or no
``verified-at:`` comment each add a reason and classification continues
to ``full``.

Exit codes:
    0  plan emitted
    1  not inside a git repository (``repo_root``'s own ``SystemExit``)
    2  the base ref is invalid (dash-prefixed) or unresolvable by git
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from forge.git_utils import configure_cli_logging, emit, repo_root, run_git
from forge.pr_delta import (
    PROVENANCE_GATE_STEPS,
    configured_docs_only_globs,
    delta_decision,
    docs_only_diff,
    extract_verified_shas,
    light_wrapup_decision,
    regen_only_diff,
)


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)

# The verification agents each mode requires — the skill runs exactly these.
FULL_REPORTERS = ("design-checker", "security-checker", "docs-types-checker")
DOCS_ONLY_REPORTERS = ("docs-types-checker",)

# Pre-commit steps for the docs-only light path (`forge-precommit --only ...`).
# Mirrors the /pr skill's docs-only narrowing: path-relevant gates only.
DOCS_ONLY_PRECOMMIT_STEPS = (
    "changelog_version",
    "changelog_updated",
    "doc_consistency",
)

# Minimum number of parts in a numstat line (insertions and deletions counts).
_NUMSTAT_MIN_PARTS = 2


@dataclass(frozen=True)
class PrPlan:
    """The finalization plan for one classification run.

    Attributes:
        mode: One of ``full`` / ``light-docs`` / ``light-regen`` /
            ``light-code`` /
            ``delta``.
        reporters: Verification agents the skill must invoke for this mode.
        precommit_scope: Step names for ``forge-precommit --only``; empty
            means the full strict battery in ``full`` mode and no
            pre-commit run at all in ``delta`` mode.
        reasons: Human-readable classification trail, one entry per
            decision taken (including why higher-priority modes were
            rejected).
        classified_at: Short ``HEAD`` SHA at classification time.
    """

    mode: str
    reporters: list[str] = field(default_factory=list)
    precommit_scope: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    classified_at: str = ""


def _changed_paths(root: Path, diff_range: str) -> list[str]:
    """Return the repo-relative paths changed across *diff_range*.

    Args:
        root: Repository root directory.
        diff_range: A git range spec (e.g. ``origin/dev...HEAD`` or
            ``<sha>..HEAD``).

    Returns:
        One path per changed file, empty for an empty diff.
    """
    out = run_git("diff", "--name-only", diff_range, cwd=root)
    return [line for line in out.splitlines() if line]


def _added_paths(root: Path, diff_range: str) -> list[str]:
    """Return the paths ADDED across *diff_range* (``--diff-filter=A``).

    The light-code class excludes file-adding diffs outright — the
    prior-art gate is an independent refusal the light path must never
    bypass.

    Args:
        root: Repository root directory.
        diff_range: A git range spec (e.g. ``origin/dev...HEAD``).

    Returns:
        One path per added file, empty when nothing was added.
    """
    out = run_git("diff", "--name-only", "--diff-filter=A", diff_range, cwd=root)
    return [line for line in out.splitlines() if line]


def _line_count(root: Path, diff_range: str) -> int:
    """Return insertions + deletions across *diff_range*.

    Args:
        root: Repository root directory.
        diff_range: A git range spec (e.g. ``<sha>..HEAD``).

    Returns:
        The summed line count ``pr_delta.delta_decision`` expects; ``0``
        for an empty diff.
    """
    out = run_git("diff", "--numstat", diff_range, cwd=root)
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        # Binary files report "-" counts; they contribute no line total.
        if (
            len(parts) >= _NUMSTAT_MIN_PARTS
            and parts[0].isdigit()
            and parts[1].isdigit()
        ):
            total += int(parts[0]) + int(parts[1])
    return total


def _latest_verified_sha(pr_number: int) -> str | None:
    """Return the newest ``verified-at:`` SHA among the PR's comments.

    The delta path's baseline: prior wrap-up / reporter comments carry the
    reporter-header contract's ``verified-at:`` line. ``gh`` failures
    (missing binary, no auth, unknown PR) return ``None`` — the caller
    degrades to full mode rather than crashing.

    Args:
        pr_number: The existing PR to read comments from.

    Returns:
        The last SHA extracted across comment bodies in posting order, or
        ``None`` when unavailable.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "comments",
                "--jq",
                ".comments[].body",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("pr-plan: could not read PR #%s comments (%s)", pr_number, exc)
        return None
    shas = extract_verified_shas(proc.stdout)
    return shas[-1] if shas else None


def _try_delta(
    root: Path,
    pr_number: int | None,
    reasons: list[str],
) -> bool:
    """Evaluate delta-mode eligibility, appending the trail to *reasons*.

    Args:
        root: Repository root directory.
        pr_number: The existing PR, or ``None`` when no PR exists yet.
        reasons: Classification trail, mutated with each decision taken.

    Returns:
        ``True`` when the diff since the last verified SHA qualifies for
        delta mode.
    """
    if pr_number is None:
        reasons.append("delta: no --pr given (no existing PR); ineligible")
        return False
    sha = _latest_verified_sha(pr_number)
    if sha is None:
        reasons.append(
            f"delta: no verified-at: SHA found on PR #{pr_number} "
            "(or gh unavailable); falling back to full"
        )
        return False
    resolved = run_git(
        "rev-parse",
        "--verify",
        f"{sha}^{{commit}}",
        cwd=root,
        check=False,
        log_errors=False,
    )
    if not resolved:
        reasons.append(
            f"delta: verified-at SHA {sha} does not resolve; falling back to full"
        )
        return False
    use_delta, reason = delta_decision(
        line_count=_line_count(root, f"{sha}..HEAD"),
        changed_paths=_changed_paths(root, f"{sha}..HEAD"),
    )
    reasons.append(f"delta vs {sha}: {reason}")
    return use_delta


def classify(root: Path, base: str, pr_number: int | None) -> PrPlan:
    """Classify the current branch's finalization path.

    Reproduces the ``/pr`` skill's decision order exactly: docs-only
    first, then regen-only eligibility, then delta (only with an existing
    PR), else the full round.

    Args:
        root: Repository root directory.
        base: Base ref the PR targets (e.g. ``origin/dev``).
        pr_number: Existing PR number for the delta path, or ``None``.

    Returns:
        The complete plan, ``classified_at`` stamped with the current
        short ``HEAD`` SHA.
    """
    head = run_git("rev-parse", "--short", "HEAD", cwd=root)
    reasons: list[str] = []
    paths = _changed_paths(root, f"{base}...HEAD")

    if docs_only_diff(paths, configured_docs_only_globs(root)):
        reasons.append(
            f"every changed path vs {base} is doc-shaped and none is high-blast-radius"
        )
        return PrPlan(
            mode="light-docs",
            reporters=list(DOCS_ONLY_REPORTERS),
            precommit_scope=list(DOCS_ONLY_PRECOMMIT_STEPS),
            reasons=reasons,
            classified_at=head,
        )
    reasons.append(f"not docs-only: diff vs {base} has non-doc or high-blast paths")

    if regen_only_diff(paths):
        reasons.append(
            "every changed path is a managed regen artifact; ELIGIBILITY "
            "ONLY: earn the escape by passing the provenance gates in "
            "precommit_scope; any gate failure falls back to the full round"
        )
        return PrPlan(
            mode="light-regen",
            reporters=[],
            precommit_scope=list(PROVENANCE_GATE_STEPS),
            reasons=reasons,
            classified_at=head,
        )
    reasons.append("not regen-only: diff touches non-managed paths")

    added = _added_paths(root, f"{base}...HEAD")
    use_light, why = light_wrapup_decision(
        line_count=_line_count(root, f"{base}...HEAD"),
        changed_paths=paths,
        added_paths=added,
    )
    if use_light:
        reasons.append(
            f"light-code: {why}; reporters skipped, wrap-up is short-form "
            "(wrapup-mode: light) — the publish hook re-runs this "
            "classifier fail-closed; strict pre-commit still runs in full"
        )
        return PrPlan(
            mode="light-code",
            reporters=[],
            precommit_scope=[],
            reasons=reasons,
            classified_at=head,
        )
    reasons.append(f"not light-code: {why}")

    if _try_delta(root, pr_number, reasons):
        return PrPlan(
            mode="delta",
            reporters=[],
            precommit_scope=[],
            reasons=reasons,
            classified_at=head,
        )

    reasons.append("full round: strict whole-tree pre-commit + all reporters")
    return PrPlan(
        mode="full",
        reporters=list(FULL_REPORTERS),
        precommit_scope=[],
        reasons=reasons,
        classified_at=head,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the finalization-path classifier and emit its JSON plan.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code: ``0`` with the plan on stdout; ``2`` on an
        invalid or unresolvable base ref (``1`` if run outside a git
        repository — raised by ``repo_root`` before this returns).
    """
    parser = argparse.ArgumentParser(prog="forge-pr-plan")
    parser.add_argument(
        "--base",
        required=True,
        metavar="REF",
        help="Base ref the PR targets (e.g. origin/dev); the classified "
        "diff is BASE...HEAD.",
    )
    parser.add_argument(
        "--pr",
        type=int,
        metavar="N",
        help="Existing PR number — enables the delta path (reads the PR's "
        "verified-at: comments via gh). Omit when no PR exists yet.",
    )
    args = parser.parse_args(argv)
    # A ref never starts with a dash; a dash-prefixed value would reach git
    # as an option. Reject it before any subprocess sees it.
    if args.base.startswith("-"):
        logger.error("pr-plan: %r is not a valid base ref.", args.base)
        return 2
    root = repo_root()
    try:
        plan = classify(root, args.base, args.pr)
    except subprocess.CalledProcessError:
        # An unresolvable base ref (or no shared history with HEAD) fails the
        # underlying git diff; surface the documented exit code, not a
        # traceback. run_git already logged git's own stderr.
        logger.exception("pr-plan: cannot diff against base ref %r.", args.base)
        return 2
    emit(json.dumps(asdict(plan), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
