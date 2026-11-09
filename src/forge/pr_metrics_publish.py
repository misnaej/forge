"""``forge-metrics publish``: the public metrics page, pushed to a page repo.

Builds the public variant of the report (:func:`render_public_html`,
:func:`public_payload`: no dates, no names, rows in a stable scrambled
order within each forge version) and pushes it to a dedicated public repo
served by GitHub Pages. The target is checked before anything is written:
the ``gh`` user must be forge's maintainer (publishing is an experiment),
and the repo must be theirs, exist, be public and hold nothing but files
this command writes. ``--dry-run`` writes the two files locally instead.
"""

from __future__ import annotations

import hashlib
import html
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from forge.git_utils import (
    _FORGE_GITHUB_REPO,
    GH_TIMEOUT_S,
    PUSH_TIMEOUT_S,
    create_commit,
    push_branch,
    ref_exists,
    release_of,
    run_git,
)
from forge.pr_metrics_bench import Runner, run_command, valid_slug
from forge.pr_metrics_report import (
    PR_HEADER,
    html_page,
    pr_cells,
    token_dict,
    version_name,
    version_order,
    version_section,
)
from forge.regen_docs import forge_version


if TYPE_CHECKING:
    from collections.abc import Mapping

    from forge.pr_metrics_report import PrRow


#: The files a publish generates from the database.
PAGE_FILES: Final = ("index.html", "metrics.json")
#: Every file a page repo may track; anything else makes publish refuse.
ALLOWED_FILES: Final = frozenset({*PAGE_FILES, "README.md", ".nojekyll"})
PAGE_BRANCH: Final = "main"
#: Public repo labels: forge's own repo, and every other repo as one series.
FORGE_LABEL: Final = "forge"
OTHER_LABEL: Final = "external"
MAINTAINER_ONLY: Final = (
    "forge-metrics publish is limited to forge's maintainer while metrics "
    "publishing is an experiment."
)
README: Final = """# forge metrics

Cost of pull requests built with forge, grouped by forge version: tokens,
active time, review rounds and size per PR.

Written by `forge-metrics publish`. It holds no repo, PR or branch names and
no dates: repos appear only as random ids, and PRs are scrambled within each
forge version. The commit history shows roughly when rows were added.
`index.html` is the page, `metrics.json` the same rows as data.
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


def _scatter_key(row: PrRow) -> str:
    """A stable, merge-order-free sort key: a hash of the row's own PR key.

    The PR key is already an HMAC under the machine secret, so its hash
    says nothing about when the PR merged, yet the same rows always sort
    the same way: an unchanged database republishes byte-identical files.

    Args:
        row: The PR.

    Returns:
        The key.
    """
    return hashlib.sha256(row.pr_key.encode()).hexdigest()


def public_order(rows: list[PrRow]) -> list[PrRow]:
    """*rows* grouped by forge version (release order), scattered in each group.

    Scattering removes the merge order, which would otherwise date each PR
    against the releases around it.

    Args:
        rows: PRs.

    Returns:
        The reordered rows.
    """
    ordered: list[PrRow] = []
    for version in version_order(rows):
        group = [row for row in rows if version_name(row) == version]
        ordered += sorted(group, key=_scatter_key)
    return ordered


def public_rows(rows: list[PrRow], forge_repo_id: str | None) -> list[PrRow]:
    """*rows* relabelled ``forge`` / ``external`` and in :func:`public_order`.

    No per-repo id reaches the public page: forge's own repo is ``forge``,
    every other repo merges into one ``external`` series.

    Args:
        rows: Every recorded PR.
        forge_repo_id: The local id of forge's own repo; ``None`` when this
            machine never recorded it.

    Returns:
        The relabelled, reordered rows.
    """
    relabelled = [
        replace(
            row,
            repo_id=FORGE_LABEL if row.repo_id == forge_repo_id else OTHER_LABEL,
        )
        for row in rows
    ]
    return public_order(relabelled)


def render_public_html(rows: list[PrRow]) -> str:
    """The public page: by-version charts and per-PR numbers, no dates.

    No merge day, no ordinal and no over-time chart reach it; repos show
    only as ``forge`` or ``external``.

    Args:
        rows: PRs in :func:`public_order`.

    Returns:
        The page.
    """
    esc = html.escape
    body_rows = "".join(
        f"<tr><td>{esc(version_name(row))}</td>"
        f"<td>{esc(row.repo_id)}</td><td>{esc(row.tool)}</td>"
        f"<td>{esc(row.model or '-')}</td>{pr_cells(row)}</tr>"
        for row in rows
    )
    body = (
        f"{version_section(rows, over_time=False)}<h2>Per PR</h2>"
        '<p class="muted">Grouped by forge version, in scrambled order.</p>'
        '<div class="wrap"><table class="public"><thead><tr><th>version</th>'
        f"<th>repo</th><th>tool</th><th>model</th>{PR_HEADER}</tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div>"
    )
    intro = (
        f"{len(rows)} merged PRs built with forge, by forge version: forge's "
        "own repo and every other repo as one external series. No names and no "
        "dates. Generated by forge-metrics publish."
    )
    return html_page("Forge PR metrics", intro, body)


def public_payload(rows: list[PrRow], *, schema: int) -> dict[str, Any]:
    """The public page's rows as JSON: no keys, names or dates.

    Args:
        rows: PRs in :func:`public_order`.
        schema: The database schema version the rows follow.

    Returns:
        The JSON-ready payload.
    """
    return {
        "schema": schema,
        "generator": release_of(forge_version()),
        "prs": [
            {
                "repo": row.repo_id,
                "forge_version": row.forge_version,
                "tool": row.tool,
                "model": row.model,
                "files": row.files,
                "additions": row.additions,
                "deletions": row.deletions,
                "commits": row.commits,
                "review_rounds": row.review_rounds,
                "wrapups_posted": row.wrapups_posted,
                "precommit_runs": row.precommit_runs,
                "test_runs": row.test_runs,
                "active_s": row.active_s,
                "wall_s": row.wall_s,
                "tokens": token_dict(row),
            }
            for row in rows
        ],
    }


def public_files(
    rows: list[PrRow], *, schema: int, forge_repo_id: str | None
) -> dict[str, str]:
    """The public page and data, in one shared scrambled order.

    Args:
        rows: Every recorded PR.
        schema: The database schema version.
        forge_repo_id: The local id of forge's own repo, or ``None``.

    Returns:
        File name → content, for :data:`PAGE_FILES`.
    """
    ordered = public_rows(rows, forge_repo_id)
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


def check_maintainer(slug: str, runner: Runner, cwd: Path) -> None:
    """Refuse anyone but forge's maintainer, and any repo they do not own.

    Publishing is an experiment, limited to the owner of forge's upstream
    repo publishing to a repo under their own account.

    Args:
        slug: ``owner/name`` of the target.
        runner: Command runner.
        cwd: Where to run ``gh``.

    Raises:
        PublishError: The ``gh`` user is not the maintainer, or the target
            is not under the maintainer's account.
    """
    maintainer = _FORGE_GITHUB_REPO.partition("/")[0].lower()
    user = runner(
        ["gh", "api", "user", "--jq", ".login"], cwd=cwd, timeout=GH_TIMEOUT_S
    )
    if not user.ok or user.stdout.strip().lower() != maintainer:
        raise PublishError(MAINTAINER_ONLY)
    if slug.partition("/")[0].lower() != maintainer:
        msg = f"{MAINTAINER_ONLY} The target repo must be under {maintainer}."
        raise PublishError(msg)


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
        check_maintainer(slug, runner, tmp)
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
