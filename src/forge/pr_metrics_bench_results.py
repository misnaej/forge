"""Benchmark results kept in the sandbox repo, so any machine can run the bench.

``bench finish`` records a run in the machine's database, then
:func:`share_results` merges every locally recorded run into
``results/benchmarks.json`` on the sandbox's ``main`` (entries keyed by
run id: re-adding a run replaces it, other machines' runs are kept),
regenerates ``results/index.html`` from the merged file, commits and
pushes. The commit is made on ``main`` in the clone itself, once its
working tree is clean (a dirty clone is refused, never cleaned); the
bench branch's commits are not touched. A rejected push (another machine
pushed first) is retried after merging the new ``main``; after
:data:`PUSH_ATTEMPTS` the results stay local and the caller is told.

Entries hold what the local database holds for a run, minus anything
naming a machine: no host, user, path or branch.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import TYPE_CHECKING, Any, Final

from forge.git_utils import (
    PUSH_TIMEOUT_S,
    create_commit,
    merge_in_progress,
    network_git_env,
    push_branch,
    ref_exists,
    run_git,
    unmerged_paths,
)
from forge.pr_metrics_bench import (
    DEFAULT_SEAMS,
    SANDBOX_BASE,
    BenchError,
    Seams,
    check_access,
    default_clone_dir,
    read_sandbox,
    sync_clone,
)
from forge.pr_metrics_report import (
    bench_row_from_json,
    bench_row_json,
    render_bench_html,
)


if TYPE_CHECKING:
    from pathlib import Path

    from forge.pr_metrics_report import BenchRow


logger = logging.getLogger(__name__)

RESULTS_DIR: Final = "results"
RESULTS_JSON: Final = f"{RESULTS_DIR}/benchmarks.json"
RESULTS_HTML: Final = f"{RESULTS_DIR}/index.html"
#: Layout version of ``benchmarks.json``.
RESULTS_SCHEMA: Final = 1
#: Pushes tried before the results are left local.
PUSH_ATTEMPTS: Final = 3


def parse_results(text: str | None) -> dict[str, dict[str, Any]]:
    """The run entries of a ``benchmarks.json`` text, by run id.

    Args:
        text: The file's content; ``None`` when the file does not exist.

    Returns:
        Run id → entry; empty for no file.

    Raises:
        BenchError: The file is not a results file this forge can read
            (unparseable, a newer schema, or an entry that is not a run).
            Refusing keeps a later write from dropping what it cannot read.
    """
    if text is None:
        return {}
    try:
        raw = json.loads(text)
    except ValueError as exc:
        msg = f"forge-metrics bench: {RESULTS_JSON} is not valid JSON"
        raise BenchError(msg) from exc
    schema = raw.get("schema") if isinstance(raw, dict) else None
    runs = raw.get("runs") if isinstance(raw, dict) else None
    known = isinstance(schema, int) and schema <= RESULTS_SCHEMA
    if not known or not isinstance(runs, dict):
        msg = (
            f"forge-metrics bench: {RESULTS_JSON} has schema {schema!r}, "
            f"expected {RESULTS_SCHEMA}; upgrade forge"
        )
        raise BenchError(msg)
    bad = [key for key, entry in runs.items() if not isinstance(entry, dict)]
    if bad:
        msg = (
            f"forge-metrics bench: {RESULTS_JSON} has {len(bad)} malformed run "
            f"entr{'y' if len(bad) == 1 else 'ies'} "
            f"({', '.join(repr(key) for key in bad[:3])}); "
            "fix the file in the sandbox before results can be written"
        )
        raise BenchError(msg)
    return dict(runs)


def merge_results(
    existing: dict[str, dict[str, Any]], rows: list[BenchRow]
) -> dict[str, dict[str, Any]]:
    """*existing* entries with *rows* added; a run already there is replaced.

    Args:
        existing: Run id → entry, from the sandbox.
        rows: Locally recorded runs.

    Returns:
        The merged entries.
    """
    merged = dict(existing)
    for row in rows:
        if row.run_id:
            merged[row.run_id] = bench_row_json(row)
    return merged


def results_rows(entries: dict[str, dict[str, Any]]) -> list[BenchRow]:
    """Entries as report rows, oldest recorded first.

    Args:
        entries: Run id → entry.

    Returns:
        The rows.
    """
    rows = [bench_row_from_json(run_id, entry) for run_id, entry in entries.items()]
    return sorted(rows, key=lambda r: (r.recorded_on or "", r.run_id))


def results_files(entries: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The two results files for *entries*, in a stable form.

    Args:
        entries: Run id → entry.

    Returns:
        Repo path → content.
    """
    payload = {"schema": RESULTS_SCHEMA, "runs": entries}
    return {
        RESULTS_JSON: json.dumps(payload, indent=2, sort_keys=True) + "\n",
        RESULTS_HTML: render_bench_html(results_rows(entries)),
    }


def read_remote_results(clone: Path) -> dict[str, dict[str, Any]]:
    """The entries on the sandbox's ``main`` as last fetched into *clone*.

    Args:
        clone: A fetched clone of the sandbox.

    Returns:
        Run id → entry; empty when ``main`` has no results yet.
    """
    ref = f"origin/{SANDBOX_BASE}"
    if not ref_exists(clone, f"refs/remotes/{ref}"):
        return {}
    text = run_git("show", f"{ref}:{RESULTS_JSON}", cwd=clone, check=False)
    return parse_results(text or None)


def _fetch(repo: Path) -> None:
    """Fetch the sandbox's ``main``.

    Args:
        repo: A clone of the sandbox.
    """
    run_git(
        "fetch",
        "--quiet",
        "origin",
        SANDBOX_BASE,
        cwd=repo,
        env=network_git_env(repo),
        timeout=PUSH_TIMEOUT_S,
    )


def _merge_main(tree: Path) -> bool:
    """Bring ``origin/main`` into the clone's ``main``, results conflicts allowed.

    Conflicts in the results files are resolved by rewriting them from the
    merged entries; a conflict anywhere else means ``main`` changed in a
    way this command does not own, and the merge is abandoned.

    Args:
        tree: The clone, on ``main``.

    Returns:
        ``False`` when another file conflicted (merge aborted).
    """
    origin_main = f"origin/{SANDBOX_BASE}"
    run_git("merge", "--no-commit", "--quiet", origin_main, cwd=tree, check=False)
    foreign = [p for p in unmerged_paths(tree) if not p.startswith(f"{RESULTS_DIR}/")]
    if foreign:
        run_git("merge", "--abort", cwd=tree, check=False)
        logger.warning(
            "forge-metrics bench: sandbox main conflicts outside %s/ (%s)",
            RESULTS_DIR,
            ", ".join(foreign[:3]),
        )
        return False
    return True


def _write_and_commit(tree: Path, rows: list[BenchRow]) -> None:
    """Write the merged results into the clone and commit them.

    Args:
        tree: The clone on ``main``, ``origin/main`` merged in.
        rows: Locally recorded runs.

    Raises:
        subprocess.CalledProcessError: The commit was refused.
    """
    entries = merge_results(read_remote_results(tree), rows)
    for rel, text in results_files(entries).items():
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    run_git("add", "--", RESULTS_DIR, cwd=tree)
    staged = run_git("diff", "--cached", "--name-only", cwd=tree)
    if staged or merge_in_progress(tree):
        create_commit(tree, f"chore(bench): record {len(rows)} benchmark run(s)")


def _check_origin(tree: Path, slug: str, seams: Seams) -> None:
    """Refuse to push unless the clone's ``origin`` is still the sandbox.

    Run right before each push, so nothing between the check and the push
    (a merge, a hook, a retry) can retarget it.

    Args:
        tree: The clone.
        slug: The sandbox's ``owner/name``.
        seams: Supplies the origin reader.

    Raises:
        BenchError: ``origin`` names another repo.
    """
    if seams.origin_slug(tree) != slug:
        msg = f"forge-metrics bench: {tree} origin is not the sandbox; not pushed"
        raise BenchError(msg)


def _push_results(tree: Path, rows: list[BenchRow], slug: str, seams: Seams) -> bool:
    """Merge, write, commit and push, retrying after a rejected push.

    Args:
        tree: The clone, on ``main``.
        rows: Locally recorded runs.
        slug: The sandbox's ``owner/name``.
        seams: Supplies the origin reader.

    Returns:
        Whether the results reached the sandbox.

    Raises:
        BenchError: ``origin`` stopped naming the sandbox before a push.
    """
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        _fetch(tree)
        if not _merge_main(tree):
            return False
        try:
            _write_and_commit(tree, rows)
        except subprocess.CalledProcessError:
            logger.warning("forge-metrics bench: results commit refused by its hook")
            return False
        # A CLI pushing the sandbox's main is sanctioned here only: the
        # sandbox is a private repo nobody else uses, checked just before,
        # and the seam cannot force, so it adds commits but never rewrites.
        _check_origin(tree, slug, seams)
        pushed = push_branch(tree, SANDBOX_BASE)
        if pushed.ok:
            return True
        logger.info(
            "forge-metrics bench: results push %d/%d rejected; merging and retrying",
            attempt,
            PUSH_ATTEMPTS,
        )
    return False


def _onto_main(clone: Path) -> None:
    """Switch the clean clone to ``main``, fast-forwarded to the sandbox's.

    Done in the clone itself rather than a separate worktree: a new
    checkout path would make the sandbox's ``post-checkout`` hook register
    forge's plugin for it, while the clone's own path is registered
    already (``bench start``).

    Args:
        clone: A clone of the sandbox.

    Raises:
        BenchError: The working tree has changes (never discarded), or
            local ``main`` has commits the sandbox's lacks.
    """
    if run_git("status", "--porcelain", cwd=clone):
        msg = (
            f"{clone} has uncommitted changes; commit, move or remove them "
            "(nothing is discarded), then run `bench finish` again"
        )
        raise BenchError(msg)
    _fetch(clone)
    run_git("switch", "--quiet", SANDBOX_BASE, cwd=clone)
    origin_main = f"origin/{SANDBOX_BASE}"
    run_git("merge", "--ff-only", "--quiet", origin_main, cwd=clone, check=False)
    # Ahead is fine (results an earlier finish could not push); behind
    # after the fast-forward means the two histories diverged.
    if run_git("rev-list", "--count", f"HEAD..{origin_main}", cwd=clone) != "0":
        msg = (
            f"local {SANDBOX_BASE} in {clone} has diverged from the sandbox's "
            "and cannot fast-forward; reconcile it by hand"
        )
        raise BenchError(msg)


def share_results(
    clone: Path, rows: list[BenchRow], *, slug: str, seams: Seams = DEFAULT_SEAMS
) -> bool:
    """Put *rows* into the sandbox's results files, committed on ``main``.

    The clone is switched to ``main`` and left there (the next ``bench
    start`` branches from the base tag anyway); the bench branch and its
    commits are not touched. The commit runs the sandbox's own hooks.

    Args:
        clone: A clone of the sandbox (access already checked).
        rows: Locally recorded runs; all are merged, so a run whose results
            stayed local earlier is shared now.
        slug: The sandbox's ``owner/name``; each push first re-checks that
            the clone's ``origin`` still names it.
        seams: Supplies the origin reader.

    Returns:
        Whether the results reached the sandbox; ``False`` leaves them in
        the local database only (logged with the reason).
    """
    try:
        _onto_main(clone)
        shared = _push_results(clone, rows, slug, seams)
    except (subprocess.SubprocessError, OSError, BenchError) as exc:
        logger.warning("forge-metrics bench: results not shared: %s", exc)
        shared = False
    if not shared:
        logger.warning(
            "forge-metrics bench: results stayed local (the run is in this "
            "machine's database); the next `bench finish` shares them"
        )
    return shared


def sandbox_rows(
    clone: Path | None = None, *, task: str | None = None, seams: Seams = DEFAULT_SEAMS
) -> list[BenchRow]:
    """Every run in the sandbox's results file, from any machine.

    Args:
        clone: The local clone; :func:`default_clone_dir` when ``None``
            (cloned on first use).
        task: Only this task's runs; all when ``None``.
        seams: Command runner and origin reader.

    Returns:
        The rows, oldest recorded first.

    Raises:
        BenchError: No sandbox, the sandbox refused, or the clone is another
            repo or its results file unreadable.
    """
    sandbox = read_sandbox()
    if sandbox is None:
        msg = "forge-metrics bench: no sandbox; run `bench init --repo` first"
        raise BenchError(msg)
    clone = (clone or default_clone_dir()).resolve()
    check_access(sandbox.slug, seams.runner, clone if clone.is_dir() else clone.parent)
    sync_clone(sandbox, clone, seams)
    rows = results_rows(read_remote_results(clone))
    return [r for r in rows if task is None or r.task_id == task]
