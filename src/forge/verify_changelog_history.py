"""verify-forge-changelog-history — guard main's curated CHANGELOG history.

During a ``dev → main`` promotion the release branch merges
``origin/<base>`` in, bringing main's curated ``## vX.Y.0`` CHANGELOG
entries onto the branch (``docs/release-process.md`` §3 step 2, §5). If
that merge's ``CHANGELOG.md`` conflict is resolved **blindly toward dev**
(``git checkout --ours``), main's curated entries are silently dropped —
a release-notes history regression. #119 documented the rule that a
CHANGELOG conflict must never be resolved blindly; this guard (#120)
turns it into a checked invariant.

It fires **only when the branch has incorporated the base branch's
history** — i.e. ``origin/<base>`` is an ancestor of ``HEAD`` — which is
exactly the promotion (or any main-merge) context. On plain ``dev``,
where promotions are squash commits and ``origin/<base>`` is *not* an
ancestor, it self-skips, so ``dev``'s CHANGELOG is still allowed to lag
(§5). It also self-skips single-branch repos and repos with no
``CHANGELOG.md``. This structural trigger (ancestor, not branch name)
protects any repo that merges its base in, regardless of its release
branch naming convention.

The check: every ``## v<semver>`` heading present in ``origin/<base>``'s
``CHANGELOG.md`` must also be present in the working tree's
``CHANGELOG.md``. Missing headings → non-zero exit, naming each dropped
entry.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from forge.config import load_config
from forge.git_utils import configure_cli_logging, parse_semver, run_git


configure_cli_logging()
logger = logging.getLogger(__name__)

_CHANGELOG = "CHANGELOG.md"
_HEADING_RE = re.compile(r"^##\s+(v\d+\.\d+\.\d+)\b", re.MULTILINE)


def _headings(text: str) -> set[str]:
    """Return the set of ``## v<semver>`` release headings in *text*.

    Args:
        text: CHANGELOG markdown body.

    Returns:
        Each ``vX.Y.Z`` named in a level-2 release heading; empty when none.
    """
    return set(_HEADING_RE.findall(text))


def _base_is_ancestor(repo_root: Path, base_ref: str) -> bool:
    """Return ``True`` when *base_ref* is an ancestor of ``HEAD``.

    Determined without an exit-code-only git call (``run_git`` returns
    stdout): the merge-base of *base_ref* and ``HEAD`` equals *base_ref*'s
    own commit exactly when *base_ref* is reachable from ``HEAD`` — i.e.
    the branch has merged the base branch in. False on a plain dev-based
    branch whose promotions are squash commits (base not an ancestor).

    Args:
        repo_root: Repo root for the git invocation.
        base_ref: Base branch ref (e.g. ``origin/main``).

    Returns:
        ``True`` when *base_ref* resolves and is an ancestor of ``HEAD``.
    """
    base_sha = run_git("rev-parse", base_ref, cwd=repo_root, check=False)
    merge_base = run_git("merge-base", base_ref, "HEAD", cwd=repo_root, check=False)
    return bool(base_sha) and base_sha == merge_base


def main() -> int:
    """Fail when the working tree's CHANGELOG drops a curated ``@base`` entry.

    Returns:
        ``1`` when a ``## vX.Y.Z`` heading present on ``origin/<base>`` is
        missing locally (a promotion conflict resolved blindly toward dev);
        ``0`` on success or when skipped (single-branch, no CHANGELOG, base
        not an ancestor of HEAD, or no CHANGELOG on base).
    """
    argparse.ArgumentParser(
        prog="verify-forge-changelog-history",
        description=(
            "Fail when the working tree's CHANGELOG.md drops a `## vX.Y.0` "
            "heading present on origin/<base> — the dropped-curated-entry "
            "guard for dev→main promotions. Self-skips unless origin/<base> "
            "is an ancestor of HEAD (a promotion / main-merge context)."
        ),
    ).parse_args()

    repo_root = Path.cwd()
    cfg = load_config(repo_root)
    if not cfg.dual_track:
        logger.info("(single-branch repo: base == dev — skipped)")
        return 0

    changelog = repo_root / _CHANGELOG
    if not changelog.is_file():
        logger.info("(no %s — skipped)", _CHANGELOG)
        return 0

    base_ref = f"origin/{cfg.base_branch}"
    run_git(
        "fetch", "--quiet", "origin", "--", cfg.base_branch, cwd=repo_root, check=False
    )
    if not _base_is_ancestor(repo_root, base_ref):
        logger.info(
            "(%s is not an ancestor of HEAD — not a promotion context, skipped)",
            base_ref,
        )
        return 0

    base_changelog = run_git(
        "show", f"{base_ref}:{_CHANGELOG}", cwd=repo_root, check=False
    )
    if not base_changelog:
        logger.info("(no %s on %s — skipped)", _CHANGELOG, base_ref)
        return 0

    base_headings = _headings(base_changelog)
    local_headings = _headings(changelog.read_text(encoding="utf-8"))
    missing = sorted(
        base_headings - local_headings, key=lambda tag: parse_semver(tag) or (0, 0, 0)
    )
    if missing:
        logger.error(
            "%s drops %d curated entr%s present on %s: %s. A promotion "
            "CHANGELOG conflict was likely resolved blindly toward dev "
            "(`git checkout --ours`), erasing main's release notes. "
            "Reconcile by hand — keep every `## vX.Y.0` heading on %s "
            "(docs/release-process.md §5).",
            _CHANGELOG,
            len(missing),
            "y" if len(missing) == 1 else "ies",
            base_ref,
            ", ".join(missing),
            base_ref,
        )
        return 1
    logger.info(
        "All %d curated CHANGELOG entr%s on %s are preserved.",
        len(base_headings),
        "y" if len(base_headings) == 1 else "ies",
        base_ref,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
