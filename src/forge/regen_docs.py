"""Decide when the generated docs must be regenerated at commit time.

``step_regen_docs`` (forge-precommit) keeps ``docs/api-digest.md`` and
``docs/cli-reference.md`` fresh. A full rebuild takes
seconds even when nothing they are built from has changed — mostly the
CLI reference, which captures every forge CLI's ``--help`` and changes
only when forge itself does. This module answers "does this doc need a
rebuild?" from a fingerprint of the doc's inputs (index blob shas, the
installed forge version and console scripts) compared against a per-clone
record of the last successful build, and records each build after it
succeeded. Anything unknown rebuilds: a wasted second is cheap, a stale
committed doc is not.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
from pathlib import Path
from typing import NamedTuple

from forge import config
from forge.git_utils import (
    FORGE_DIST_NAME,
    console_script_modules,
    merge_in_progress,
    run_git,
)


API_DIGEST_DOC = "docs/api-digest.md"
CLI_REFERENCE_DOC = "docs/cli-reference.md"

# Per-clone record of the last successful regeneration, resolved through
# `git rev-parse --git-path` so each worktree keeps its own (it pairs with
# that worktree's index). Never tracked: it describes this checkout's
# install and index, not the project.
RECORD_GIT_PATH = "forge/regen_docs.json"

# Minimum number of fields expected in git ls-files -s output metadata.
_MIN_BLOB_FIELDS = 2


class RegenDecision(NamedTuple):
    """Whether one generated doc is rebuilt this commit, and why.

    Attributes:
        rel: The doc's repo-relative path.
        cli: The generator CLI that writes it.
        regenerate: ``True`` to run the generator.
        reason: One line for the step output — never a silent skip.
    """

    rel: str
    cli: str
    regenerate: bool
    reason: str


class RegenSignals(NamedTuple):
    """What :func:`decide_regen` compares for one generated doc.

    Attributes:
        current: ``forge_version`` and ``inputs`` fingerprint now.
        recorded: The same keys plus ``doc_blob`` from the last build, or
            ``None`` when there is no usable record.
        doc_blob: The doc's index blob sha now.
        merge: A merge is in progress.
        doc_staged: The doc itself is staged in this commit.
    """

    current: dict[str, str]
    recorded: dict[str, str] | None
    doc_blob: str | None
    merge: bool
    doc_staged: bool


def decide_regen(rel: str, cli: str, signals: RegenSignals) -> RegenDecision:
    """Decide whether *rel* must be regenerated; the first matching reason wins.

    A skip needs positive proof that nothing the doc is built from moved
    since the last successful build: same forge, same inputs, and the
    committed doc still the bytes that build produced. Anything unknown
    rebuilds — a wasted second is cheap, a stale committed doc is not.

    Args:
        rel: The doc's repo-relative path.
        cli: The generator CLI.
        signals: The current fingerprint, the record and the commit state.

    Returns:
        The decision with its reason.
    """
    current, recorded, doc_blob, merge, doc_staged = signals
    if recorded is None:
        return RegenDecision(
            rel, cli, regenerate=True, reason="no record of a previous build"
        )
    if merge:
        return RegenDecision(rel, cli, regenerate=True, reason="merge in progress")
    if recorded.get("forge_version") != current.get("forge_version"):
        reason_msg = (
            f"forge {recorded.get('forge_version')} → {current.get('forge_version')}"
        )
        return RegenDecision(rel, cli, regenerate=True, reason=reason_msg)
    if doc_staged or doc_blob != recorded.get("doc_blob"):
        return RegenDecision(
            rel, cli, regenerate=True, reason="doc edited outside generator"
        )
    if recorded.get("inputs") != current.get("inputs"):
        what = "console scripts" if rel == CLI_REFERENCE_DOC else "sources"
        return RegenDecision(rel, cli, regenerate=True, reason=f"{what} changed")
    return RegenDecision(
        rel, cli, regenerate=False, reason="inputs unchanged since last build"
    )


def record_path(repo_root: Path) -> Path | None:
    """Return this checkout's regeneration-record path, ``None`` outside git.

    Args:
        repo_root: Root of the checkout whose git dir holds the record.

    Returns:
        The record path, or ``None`` when git reports no path.
    """
    out = run_git(
        "rev-parse", "--git-path", RECORD_GIT_PATH, cwd=repo_root, check=False
    )
    if not out.strip():
        return None
    path = Path(out.strip())
    return path if path.is_absolute() else repo_root / path


def load_record(repo_root: Path) -> dict[str, dict[str, str]]:
    """Load the regeneration record for the checkout.

    Args:
        repo_root: Root of the checkout whose record is read.

    Returns:
        ``{doc: {forge_version, inputs, doc_blob}}``, or ``{}`` if unusable.
    """
    path = record_path(repo_root)
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not all(
        isinstance(entry, dict) for entry in data.values()
    ):
        return {}
    return data


def save_record(repo_root: Path, record: dict[str, dict[str, str]]) -> None:
    """Write the regeneration record atomically; a no-op outside git.

    Args:
        repo_root: Root of the checkout whose record is written.
        record: Mapping of doc path to its recorded build fingerprint.
    """
    path = record_path(repo_root)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def index_blobs(repo_root: Path) -> dict[str, str]:
    """Map every indexed path to its staged blob sha (one ``git ls-files -s``).

    Args:
        repo_root: Root of the checkout whose index is read.

    Returns:
        Mapping of repo-relative path to staged blob sha.
    """
    blobs: dict[str, str] = {}
    out = run_git("ls-files", "-s", cwd=repo_root, check=False)
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) >= _MIN_BLOB_FIELDS and path:
            blobs[path] = fields[1]
    return blobs


def is_forge_repo(repo_root: Path) -> bool:
    """Report whether the repo is forge itself (its CLIs come from this tree).

    Args:
        repo_root: Root of the repo whose ``pyproject.toml`` is inspected.

    Returns:
        True when the project name is forge's own distribution name.
    """
    project = config.read_pyproject_raw(repo_root).get("project") or {}
    return project.get("name") == FORGE_DIST_NAME


def regen_inputs(repo_root: Path, rel: str, blobs: dict[str, str] | None = None) -> str:
    """Fingerprint everything *rel* is built from, from the index.

    The api-digest reads the tracked sources under its roots; the CLI
    reference reads forge's installed console scripts — and, in forge's own
    repo, the ``src/forge`` code those scripts' help text comes from.
    Fingerprints come from index blob shas, so a change that reached the
    branch while this step did not run (``--no-verify``, a skipped partial
    commit, a branch switch) still changes the fingerprint.

    Args:
        repo_root: Repo root.
        rel: The generated doc.
        blobs: Precomputed :func:`index_blobs`, to read the index once.

    Returns:
        A sha256 hex digest.
    """
    blobs = blobs if blobs is not None else index_blobs(repo_root)
    lines: list[str] = []
    if rel == CLI_REFERENCE_DOC:
        scripts = console_script_modules(FORGE_DIST_NAME) or {}
        lines.extend(f"script\0{name}\0{mod}" for name, mod in sorted(scripts.items()))
        if is_forge_repo(repo_root):
            sources = sorted(
                p for p in blobs if p.startswith("src/forge/") and p.endswith(".py")
            )
            lines.extend(
                f"{p}\0{blobs[p]}" for p in [*sources, "pyproject.toml"] if p in blobs
            )
    else:
        roots = config.resolve_tool_roots(repo_root, "api_digest")
        files = sorted(config.tracked_files_under_roots(repo_root, roots))
        lines.extend(f"{p}\0{blobs.get(p, '')}" for p in files)
        lines.append(f"pyproject.toml\0{blobs.get('pyproject.toml', '')}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def forge_version() -> str:
    """Return the installed forge version, or ``""`` when unknowable."""
    try:
        return importlib.metadata.version(FORGE_DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return ""


def regen_decisions(
    repo_root: Path, targets: list[tuple[str, str]]
) -> tuple[list[RegenDecision], dict[str, dict[str, str]]]:
    """Decide each target doc and return the inputs needed to record a build.

    Args:
        repo_root: Repo root.
        targets: ``(cli, rel)`` pairs whose doc exists.

    Returns:
        The decisions, and per doc the ``current`` fingerprint used.
    """
    record_docs = load_record(repo_root)
    blobs = index_blobs(repo_root)
    staged = set(
        run_git(
            "diff", "--cached", "--name-only", cwd=repo_root, check=False
        ).splitlines()
    )
    merge = merge_in_progress(repo_root)
    version = forge_version()
    decisions: list[RegenDecision] = []
    currents: dict[str, dict[str, str]] = {}
    for cli, rel in targets:
        try:
            current = {
                "forge_version": version,
                "inputs": regen_inputs(repo_root, rel, blobs),
            }
            recorded = record_docs.get(rel) if blobs else None
        except (OSError, ValueError, subprocess.CalledProcessError):
            current, recorded = {"forge_version": version, "inputs": ""}, None
        currents[rel] = current
        decisions.append(
            decide_regen(
                rel,
                cli,
                RegenSignals(
                    current=current,
                    recorded=recorded if isinstance(recorded, dict) else None,
                    doc_blob=blobs.get(rel),
                    merge=merge,
                    doc_staged=rel in staged,
                ),
            )
        )
    return decisions, currents


def record_regenerated(
    repo_root: Path,
    built: list[str],
    currents: dict[str, dict[str, str]],
) -> None:
    """Record the docs that were just regenerated and re-staged successfully.

    A doc is recorded only when its staged blob equals the file just
    written: if re-staging silently failed, the index still holds the old
    doc, and recording it against the new inputs would make the next
    commit skip a doc that is stale. Unrecorded, it simply rebuilds again.

    Args:
        repo_root: Repo root.
        built: Docs whose generator succeeded.
        currents: The fingerprints their decision used.
    """
    if not built:
        return
    record = load_record(repo_root)
    blobs = index_blobs(repo_root)
    for rel in built:
        written = run_git("hash-object", "--", rel, cwd=repo_root, check=False).strip()
        if written and written == blobs.get(rel):
            record[rel] = {**currents[rel], "doc_blob": written}
    save_record(repo_root, record)
