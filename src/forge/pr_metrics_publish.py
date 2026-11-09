"""``forge-metrics publish``: the public metrics page, pushed to a page repo.

Builds the public variant of the report (:func:`render_public_html`,
:func:`public_payload`: no dates, no names, rows shuffled within each forge
version) and pushes it to a dedicated public repo served by GitHub Pages.
The target is checked before anything is written: it must exist, be
public, be administered by the ``gh`` user and hold nothing but files this
command writes. ``--dry-run`` writes the two files locally instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from forge.git_utils import (
    GH_TIMEOUT_S,
    PUSH_TIMEOUT_S,
    create_commit,
    push_branch,
    ref_exists,
    run_git,
)
from forge.pr_metrics_bench import Runner, run_command, valid_slug
from forge.pr_metrics_report import public_order, public_payload, render_public_html


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from forge.pr_metrics_report import PrRow


#: The files a publish generates from the database.
PAGE_FILES: Final = ("index.html", "metrics.json")
#: Every file a page repo may track; anything else makes publish refuse.
ALLOWED_FILES: Final = frozenset({*PAGE_FILES, "README.md", ".nojekyll"})
PAGE_BRANCH: Final = "main"
README: Final = """# forge metrics

Cost of pull requests built with forge, grouped by forge version: tokens,
active time, review rounds and size per PR.

Written by `forge-metrics publish`. It holds no repo, PR or branch names and
no dates: repos appear only as random ids, and PRs are shuffled within each
forge version. `index.html` is the page, `metrics.json` the same rows as data.
"""


class PublishError(RuntimeError):
    """The target repo is refused, or cloning or pushing failed."""


@dataclass(frozen=True)
class Published:
    """What a publish did."""

    url: str
    #: Whether GitHub Pages already serves the repo.
    pages_on: bool
    #: ``False`` when the page repo already held exactly these files.
    changed: bool


def public_files(
    rows: list[PrRow],
    *,
    schema: int,
    shuffle: Callable[[list[PrRow]], None] | None = None,
) -> dict[str, str]:
    """The public page and data, in one shared random order.

    Args:
        rows: Every recorded PR.
        schema: The database schema version.
        shuffle: In-place list shuffle; a system-random one when ``None``.

    Returns:
        File name → content, for :data:`PAGE_FILES`.
    """
    ordered = public_order(rows) if shuffle is None else public_order(rows, shuffle)
    payload = public_payload(ordered, schema=schema)
    return {
        "index.html": render_public_html(ordered),
        "metrics.json": json.dumps(payload, indent=2) + "\n",
    }


def write_files(files: Mapping[str, str], dest: Path) -> list[Path]:
    """Write *files* into *dest*, creating it.

    Args:
        files: File name → content.
        dest: Target directory.

    Returns:
        The written paths.
    """
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files.items():
        path = dest / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def pages_url(slug: str) -> str:
    """Where GitHub Pages serves a project repo.

    Args:
        slug: ``owner/name``.

    Returns:
        The URL.
    """
    owner, name = slug.split("/")
    return f"https://{owner.lower()}.github.io/{name}/"


def enable_pages_command(slug: str) -> str:
    """The one-time ``gh`` command serving *slug*'s ``main`` with Pages.

    Args:
        slug: ``owner/name``.

    Returns:
        The command line.
    """
    return (
        f"gh api -X POST repos/{slug}/pages "
        f"-f source[branch]={PAGE_BRANCH} -f source[path]=/"
    )


def check_target(slug: str, runner: Runner, cwd: Path) -> None:
    """Refuse a repo that is private or not administered by the ``gh`` user.

    Args:
        slug: ``owner/name``.
        runner: Command runner.
        cwd: Where to run ``gh``.

    Raises:
        PublishError: The repo is malformed, unreadable, private or foreign.
    """
    if not valid_slug(slug):
        msg = f"forge-metrics publish: {slug!r} is not an owner/name repo"
        raise PublishError(msg)
    repo = runner(["gh", "api", f"repos/{slug}"], cwd=cwd, timeout=GH_TIMEOUT_S)
    try:
        info = json.loads(repo.stdout) if repo.ok else None
    except ValueError:
        info = None
    if not isinstance(info, dict):
        msg = f"forge-metrics publish: cannot read {slug} through gh; create it first"
        raise PublishError(msg)
    if info.get("private") is not False:
        msg = (
            f"forge-metrics publish: {slug} is private; GitHub Pages needs a "
            "public repo, so make it public or name another"
        )
        raise PublishError(msg)
    permissions = info.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("admin") is not True:
        msg = f"forge-metrics publish: {slug} is not yours (no admin rights); refused"
        raise PublishError(msg)


def check_tracked(clone: Path) -> None:
    """Refuse a clone tracking any file publish does not write, or off ``main``.

    Args:
        clone: The page repo clone.

    Raises:
        PublishError: A foreign file is tracked, or the default branch is
            not :data:`PAGE_BRANCH`.
    """
    if not ref_exists(clone, "HEAD"):
        return
    branch = run_git("branch", "--show-current", cwd=clone)
    if branch != PAGE_BRANCH:
        msg = f"forge-metrics publish: default branch is {branch!r}, not {PAGE_BRANCH}"
        raise PublishError(msg)
    foreign = sorted(set(run_git("ls-files", cwd=clone).splitlines()) - ALLOWED_FILES)
    if foreign:
        msg = (
            "forge-metrics publish: the repo holds files publish did not write "
            f"({', '.join(foreign[:5])}); refused — use a dedicated repo"
        )
        raise PublishError(msg)


def _commit_and_push(clone: Path, files: Mapping[str, str]) -> bool:
    """Write the page files into *clone*, commit and push when anything changed.

    Args:
        clone: The checked page repo clone.
        files: :func:`public_files` output.

    Returns:
        Whether a commit was pushed.

    Raises:
        PublishError: Committing or pushing failed.
    """
    if not ref_exists(clone, "HEAD"):
        run_git("symbolic-ref", "HEAD", f"refs/heads/{PAGE_BRANCH}", cwd=clone)
    write_files({**files, "README.md": README, ".nojekyll": ""}, clone)
    run_git("add", "--", *sorted(ALLOWED_FILES), cwd=clone)
    if not run_git("status", "--porcelain", cwd=clone):
        return False
    try:
        create_commit(clone, "chore: publish forge metrics")
    except subprocess.CalledProcessError as exc:
        msg = f"forge-metrics publish: commit failed (exit {exc.returncode})"
        raise PublishError(msg) from exc
    # A CLI pushing main is sanctioned here only: this is a dedicated page
    # repo, checked to hold nothing but publish's own files, and the seam
    # cannot force, so it can add a commit but never rewrite one.
    pushed = push_branch(clone, PAGE_BRANCH, set_upstream=True)
    if not pushed.ok:
        msg = f"forge-metrics publish: push failed: {pushed.stderr}"
        raise PublishError(msg)
    return True


def publish(
    slug: str,
    files: Mapping[str, str],
    *,
    runner: Runner = run_command,
    workdir: Path | None = None,
) -> Published:
    """Check *slug*, then push *files* to its ``main``.

    Args:
        slug: ``owner/name`` of the page repo.
        files: :func:`public_files` output.
        runner: Command runner.
        workdir: Parent of the temporary clone; the system's when ``None``.

    Returns:
        The page URL, whether Pages is on, and whether anything changed.

    Raises:
        PublishError: The repo was refused, or cloning or pushing failed.
    """
    tmp = Path(tempfile.mkdtemp(prefix="forge-metrics-publish-", dir=workdir))
    try:
        check_target(slug, runner, tmp)
        clone = tmp / "pages"
        cloned = runner(
            ["gh", "repo", "clone", slug, str(clone)], cwd=tmp, timeout=PUSH_TIMEOUT_S
        )
        if not cloned.ok:
            msg = f"forge-metrics publish: clone failed: {cloned.stderr.strip()}"
            raise PublishError(msg)
        check_tracked(clone)
        changed = _commit_and_push(clone, files)
        pages = runner(
            ["gh", "api", f"repos/{slug}/pages"], cwd=tmp, timeout=GH_TIMEOUT_S
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return Published(pages_url(slug), pages_on=pages.ok, changed=changed)
