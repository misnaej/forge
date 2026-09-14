"""Tests for forge.pr_evidence — the ``/pr`` review evidence pack.

# MOCKING STRATEGY:
#   - `pr_evidence._run_tool`: the module's single subprocess seam — every
#     `forge-audit-*` invocation it runs itself (`dup`, `layering`) AND the
#     `forge-precommit --only ...` generated-artifact gate route through it,
#     so a fake dispatches on `argv[0]` to tell the two apart; no real audit
#     or precommit CLI spawns. Audit fakes seed the on-disk log a real audit
#     would have (over)written, via the local `_seed_audit_log` helper
#     (never the real `audit.common.write_log`, which resolves the cached
#     process-wide `repo_root()`, not a test's `tmp_path`). Gate fakes write
#     `code_health/precommit_timing.log` themselves — the file a real
#     `forge-precommit --only` run leaves behind, which `_gate_lines` reads
#     back for `digest_checked`.
#   - `pr_evidence.branch_added_fragments` / `pr_evidence.resolve_pr_base_ref`:
#     replaced together in the fragments-mode cases only.
#     `branch_added_fragments` resolves the PR base *internally* through its
#     own imported `resolve_pr_base_ref` — a separate name living in
#     `changelog_fragments.py`, unrelated to the one imported into
#     `pr_evidence` — which falls through to `git_utils.gh_api` with no
#     `cwd`. Left real, that call runs in pytest's own working directory (a
#     real, `gh`-authenticated forge checkout), not the ephemeral test repo.
#     Every other test keeps fragments mode off, so `_fragment_line` returns
#     before reaching either function and neither patch is needed.
#   - Everything else (`git`) is real, run against ephemeral repos this file
#     builds — the same convention as test_pr_plan.py's `_init_feature_repo`.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import git_utils, pr_evidence, precommit
from forge.audit.all import SUB_AUDITS
from tests.conftest import (
    GIT_ENV,
    PRODUCED_AT_RE,
    FakeProc,
    commit_all,
    init_git_repo,
    timing_log,
)


if TYPE_CHECKING:
    from pathlib import Path


# --- repo-building helpers ---------------------------------------------


def _repo(tmp_path: Path, *, fragments_mode: bool = False) -> Path:
    """Build a real git repo with ``main`` (one commit) and ``feature`` checked out.

    Every ``build_pack``/``write_pack`` test keeps *fragments_mode* False —
    the wiring section's fragments line then never reaches
    ``branch_added_fragments``'s own ``gh`` seam (see file MOCKING STRATEGY).

    Args:
        tmp_path: Pytest ``tmp_path`` fixture directory.
        fragments_mode: Whether to configure
            ``[tool.forge.changelog].mode = "fragments"``.

    Returns:
        The repo root, checked out on ``feature``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    toml = '[tool.forge]\nbase_branch = "main"\n'
    if fragments_mode:
        toml += '\n[tool.forge.changelog]\nmode = "fragments"\n'
    (repo / "pyproject.toml").write_text(toml, encoding="utf-8")
    commit_all(repo, "config")
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=GIT_ENV, check=True
    )
    return repo


def _seed_audit_log(root: Path, name: str, *, count: int, findings: str = "") -> None:
    r"""Write a ``write_log``-shaped ``code_health/audit_<name>.log``, fresh for *root*.

    Real ``audit.common.write_log`` is not used here — it always resolves
    the cached, process-wide ``repo_root()``, not a passed root, so a
    hand-built log matching its exact shape (stamp, header order, the
    ``\\n## Findings\\n`` heading ``_audit_result`` partitions on) is the
    correct fake for a scoped test repo.

    Args:
        root: Repo root.
        name: Audit short name (e.g. ``"dup"``).
        count: The ``# findings: N`` header value.
        findings: Raw findings body, already newline-terminated; empty
            renders ``write_log``'s own ``"(none)\\n"`` placeholder.
    """
    log_dir = root / "code_health"
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        git_utils.produced_at_stamp(root),
        f"# forge-audit-{name}",
        f"# findings: {count}",
        "",
        "## Summary",
        "summary",
        "",
        "## Findings",
        "",
    ]
    body = findings or "(none)\n"
    (log_dir / f"audit_{name}.log").write_text(
        "\n".join(lines) + body, encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# _pr_lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("added", "expected_added_tail"),
    [
        (["src/new.py"], ["- added files:", "````", "src/new.py", "````"]),
        ([], ["- added files:", "  none"]),
    ],
)
def test_pr_lines_renders_identity_and_added_files(
    tmp_path: Path, added: list[str], expected_added_tail: list[str]
) -> None:
    """``_pr_lines`` renders head/base/mode/reasons/diffstat, then added files.

    Args:
        added: Paths the branch adds, per case.
        expected_added_tail: The exact trailing lines the bullet renders.
    """
    repo = _repo(tmp_path)
    plan = {"mode": "full", "reasons": ["reason one", "reason two"]}

    lines = pr_evidence._pr_lines(repo, "main", plan, added)

    assert lines[0].startswith("- head: ")
    assert lines[1] == "- base: main"
    assert lines[2] == "- mode: full"
    assert "  - reason one" in lines
    assert "  - reason two" in lines
    assert "- diff stat:" in lines
    assert lines[-len(expected_added_tail) :] == expected_added_tail


# ---------------------------------------------------------------------------
# _health_lines
# ---------------------------------------------------------------------------


def test_health_lines_pairs_timing_markers_with_log_freshness(tmp_path: Path) -> None:
    """Each step renders its timing marker beside its own log's freshness.

    SCENARIO: ``precommit_timing.log`` names ``ruff`` (PASS) and
    ``docstring_verification`` (WARN); only ``ruff.log`` and
    ``typecheck.log`` exist on disk — ``typecheck`` has no timing row at
    all. An ``audit_dup.log`` and a stale leftover ``pr_evidence.log`` are
    also present.
    EXPECTED BEHAVIOR: ``ruff`` pairs its PASS marker with a fresh log;
    ``docstring_verification`` pairs its WARN marker with a missing log;
    ``typecheck`` pairs "no step row" with its own fresh log; ``audit_dup``
    and ``pr_evidence`` never appear (audits belong to their own section,
    and a previous pack describes nothing).
    """
    repo = _repo(tmp_path)
    stamp = git_utils.produced_at_stamp(repo)
    log_dir = repo / "code_health"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "precommit_timing.log").write_text(
        timing_log("ruff PASS", "docstring_verification WARN", stamp=stamp),
        encoding="utf-8",
    )
    (log_dir / "ruff.log").write_text(f"{stamp}\nok\n", encoding="utf-8")
    (log_dir / "typecheck.log").write_text(f"{stamp}\nok\n", encoding="utf-8")
    (log_dir / "audit_dup.log").write_text(f"{stamp}\nok\n", encoding="utf-8")
    (log_dir / "pr_evidence.log").write_text(
        "# produced-at: tree=" + "0" * 40 + " head=x 2020-01-01T00:00:00+00:00\n"
        "stale pack\n",
        encoding="utf-8",
    )

    lines = pr_evidence._health_lines(repo)

    assert lines[0] == "- latest pre-commit run: fresh"
    assert "- ruff: PASS, log fresh" in lines
    assert "- docstring_verification: WARN, log missing" in lines
    assert "- typecheck: no step row, log fresh" in lines
    assert not any("audit_dup" in line for line in lines)
    assert not any(line.startswith("- pr_evidence") for line in lines)


# ---------------------------------------------------------------------------
# build_pack — gather order (§13 gather-before-rewrite guarantee)
# ---------------------------------------------------------------------------


def test_build_pack_snapshots_timing_before_the_gates_rewrite_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code health reads ``precommit_timing.log`` before the gates overwrite it.

    MOCK SETUP: the single ``_run_tool`` fake dispatches on ``argv[0]`` — its
    ``forge-precommit`` branch overwrites ``code_health/precommit_timing.log``
    with a different row set before returning, modelling what a real
    ``forge-precommit --only`` gate run does; every other argv (the
    ``dup``/``layering`` audits) fails fast, since the Audits section's own
    content is irrelevant here.
    EXPECTED BEHAVIOR: the rendered Code health section still shows the
    pre-gate marker (``ruff: PASS``), even though the file on disk now
    holds the post-gate one (``ruff: FAIL``) — pinning the module
    docstring's stated gather order.
    """
    repo = _repo(tmp_path)
    timing_path = repo / "code_health" / "precommit_timing.log"
    timing_path.parent.mkdir(parents=True, exist_ok=True)
    timing_path.write_text(
        timing_log("ruff PASS", stamp=git_utils.produced_at_stamp(repo)),
        encoding="utf-8",
    )

    def _fake_run_tool(argv: list[str], *, cwd: Path, timeout: object) -> FakeProc:
        del timeout
        if argv[0] == "forge-precommit":
            (cwd / "code_health" / "precommit_timing.log").write_text(
                timing_log("ruff FAIL", stamp=git_utils.produced_at_stamp(cwd)),
                encoding="utf-8",
            )
            return FakeProc(returncode=1, stderr="fail")
        return FakeProc(returncode=3, stderr="boom")

    monkeypatch.setattr(pr_evidence, "_run_tool", _fake_run_tool)

    pack = pr_evidence.build_pack(
        repo, base="main", plan={"mode": "full", "reasons": []}, added=[], pr_body=None
    )

    health_section = pack.split("## Code health")[1].split("## Audits")[0]
    assert "ruff: PASS" in health_section
    assert "FAIL" not in health_section
    on_disk = precommit.timing_markers(timing_path.read_text(encoding="utf-8"))
    assert on_disk == {"ruff": "FAIL"}  # the gate really did overwrite it


# ---------------------------------------------------------------------------
# _audit_lines / _audit_result / _reported_audit / _reason
# ---------------------------------------------------------------------------


def test_audit_lines_runs_only_dup_and_layering_at_changed_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run audits selectively: dup and layering via ``_run_tool``, others from logs.

    Only ``dup`` and ``layering`` invoke ``_run_tool`` at changed scope.
    All other audits read their own pre-written logs instead.
    """
    repo = _repo(tmp_path)
    invoked: list[tuple[str, ...]] = []

    def _fake_run_tool(argv: list[str], *, cwd: object, timeout: object) -> FakeProc:
        del cwd, timeout
        invoked.append(tuple(argv))
        _seed_audit_log(repo, argv[0].removeprefix("forge-audit-"), count=0)
        return FakeProc(returncode=0)

    monkeypatch.setattr(pr_evidence, "_run_tool", _fake_run_tool)

    pr_evidence._audit_lines(repo, "main", git_utils.working_tree_sha(repo))

    assert invoked == [
        ("forge-audit-dup", "--scope", "changed"),
        ("forge-audit-layering", "--scope", "changed"),
    ]


def test_audit_result_embeds_findings_when_the_audit_finds_something(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rc=1 exit is a result to embed, not a crash to hide behind ``unavailable``."""
    repo = _repo(tmp_path)
    current = git_utils.working_tree_sha(repo)
    _seed_audit_log(
        repo, "dup", count=1, findings="[HIGH] src/a.py:1 duplicate body\n\n"
    )
    monkeypatch.setattr(
        pr_evidence, "_run_tool", lambda *_a, **_kw: FakeProc(returncode=1)
    )

    lines = pr_evidence._audit_result(repo, "dup", current)

    assert lines[0] == "1 finding(s)"
    assert lines[1] == "````"
    assert any("duplicate body" in line for line in lines)


def test_audit_result_zero_findings_omits_the_findings_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean run (0 findings) renders only the count, never a fence."""
    repo = _repo(tmp_path)
    current = git_utils.working_tree_sha(repo)
    _seed_audit_log(repo, "dup", count=0)
    monkeypatch.setattr(
        pr_evidence, "_run_tool", lambda *_a, **_kw: FakeProc(returncode=0)
    )

    assert pr_evidence._audit_result(repo, "dup", current) == ["0 finding(s)"]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (subprocess.TimeoutExpired(cmd=["x"], timeout=300), "timed out after 300s"),
        (subprocess.CalledProcessError(returncode=2, cmd=["x"]), "exited 2"),
        (
            FileNotFoundError(2, "No such file or directory", "forge-audit-dup"),
            "forge-audit-dup not found",
        ),
        (ValueError("first line\nsecond line"), "first line"),
    ],
)
def test_reason_describes_common_exception_shapes(
    exc: Exception, expected: str
) -> None:
    """``_reason`` renders a short, sanitized line for every item-error type.

    Args:
        exc: The exception ``_run_tool`` can raise.
        expected: The one-line description that item's bullet should show.

    Shared by every pack item's failure path (audits, gates) — pinning
    its shape once here means the composition tests don't need to
    re-derive every exception-message format.
    """
    assert pr_evidence._reason(exc) == expected


def test_audit_result_raises_unavailable_on_a_non_result_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exit code outside {0, 1} is a crash, not a result — never embedded."""
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        pr_evidence,
        "_run_tool",
        lambda *_a, **_kw: FakeProc(returncode=2, stderr="boom\n"),
    )

    with pytest.raises(pr_evidence._UnavailableError, match="exited 2"):
        pr_evidence._audit_result(repo, "dup", git_utils.working_tree_sha(repo))


def test_audit_result_raises_unavailable_when_log_is_stale_after_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rc=0/1 exit with a log that doesn't describe this tree is unusable.

    SCENARIO: the audit ran (or claims to) but its log still names a
    different tree — a broken audit binary, or one that silently no-ops.
    """
    repo = _repo(tmp_path)
    _seed_audit_log(repo, "dup", count=0)
    stale_current = "0" * 40
    monkeypatch.setattr(
        pr_evidence, "_run_tool", lambda *_a, **_kw: FakeProc(returncode=0)
    )

    with pytest.raises(pr_evidence._UnavailableError, match="is stale after the run"):
        pr_evidence._audit_result(repo, "dup", stale_current)


@pytest.mark.parametrize(
    ("seed", "expected"),
    [
        (None, "no log — run `forge-audit-agents --scope full`"),
        ("stale", "stale — re-run at full scope"),
        ("fresh", "fresh, 2 finding(s)"),
    ],
)
def test_reported_audit_reports_freshness_without_running_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: str | None,
    expected: str,
) -> None:
    """No log / a stale log / a fresh log each render their own one-liner.

    Args:
        seed: Which log state to seed on disk before the call — ``None``
            for no log, ``"stale"`` for one naming a different tree,
            ``"fresh"`` for one matching *current*.
        expected: The single line ``_reported_audit`` must return for that
            state.

    A reported audit (everything but dup/layering) never calls
    ``_run_tool`` — only its own log's freshness and header count are read.
    """
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        pr_evidence, "_run_tool", lambda *_a, **_kw: pytest.fail("must not run")
    )
    current = git_utils.working_tree_sha(repo)
    log_dir = repo / "code_health"
    if seed == "stale":
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "audit_agents.log").write_text(
            "# produced-at: tree=" + "0" * 40 + " head=abc1234 "
            "2020-01-01T00:00:00+00:00\n# forge-audit-agents\n# findings: 2\n",
            encoding="utf-8",
        )
    elif seed == "fresh":
        _seed_audit_log(repo, "agents", count=2)

    assert pr_evidence._reported_audit(repo, "agents", current) == [expected]


def test_audit_lines_names_base_divergence_from_the_configured_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scope line names the *configured* base and flags a mismatch with `--base`."""
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        pr_evidence, "_run_tool", lambda *_a, **_kw: FakeProc(returncode=3)
    )

    lines = pr_evidence._audit_lines(repo, "parent", git_utils.working_tree_sha(repo))

    assert lines[0] == (
        "- changed scope compares against main — not parent, "
        "so commits between the two are in scope too"
    )


def test_audit_lines_isolates_one_run_audit_failure_from_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``_run_tool`` failure only takes down its own bullet, never its sibling's."""
    repo = _repo(tmp_path)

    def _fake_run_tool(argv: list[str], *, cwd: object, timeout: object) -> FakeProc:
        del cwd, timeout
        if argv[0] == "forge-audit-dup":
            raise FileNotFoundError(2, "No such file or directory", "forge-audit-dup")
        _seed_audit_log(repo, "layering", count=0)
        return FakeProc(returncode=0)

    monkeypatch.setattr(pr_evidence, "_run_tool", _fake_run_tool)

    lines = pr_evidence._audit_lines(repo, "main", git_utils.working_tree_sha(repo))

    assert any(
        "forge-audit-dup --scope changed: unavailable: forge-audit-dup not found"
        in line
        for line in lines
    )
    assert any(
        "forge-audit-layering --scope changed: 0 finding(s)" in line for line in lines
    )


def test_audit_lines_reports_a_bullet_for_every_non_run_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every audit the pack does not run itself still gets a reported bullet.

    The expected label set is computed independently from ``SUB_AUDITS``
    minus ``RUN_AUDITS`` — never against ``REPORTED_AUDITS`` itself — so an
    edit that drops one audit from ``REPORTED_AUDITS`` without also
    dropping it from ``SUB_AUDITS`` fails here: the rendered section is the
    behavior surface this pins, not the constant.
    """
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        pr_evidence, "_run_tool", lambda *_a, **_kw: FakeProc(returncode=3)
    )

    lines = pr_evidence._audit_lines(repo, "main", git_utils.working_tree_sha(repo))

    reported = {
        line.split(":")[0].removeprefix("- audit_")
        for line in lines
        if line.startswith("- audit_")
    }
    assert reported == set(SUB_AUDITS) - set(pr_evidence.RUN_AUDITS)


# ---------------------------------------------------------------------------
# _gate_steps / _gate_lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gate_params",
    [
        (0, "PASS", "PASS", True),
        (1, "FAIL", "FAIL", False),
        (1, "PASS", "FAIL", True),
        (0, "FAIL", "PASS", False),
    ],
)
def test_gate_lines_verdict_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_params: tuple
) -> None:
    """Verdict follows the return code; ``digest_checked`` follows the marker.

    Args:
        gate_params: Tuple of (return_code, marker, expected_verdict,
            expected_digest_checked).

    The two mismatched rows (``1``/``PASS`` and ``0``/``FAIL``) are the
    discriminating cases: they prove ``digest_checked`` reads the marker
    left in the timing log, never the overall return code. A mutant
    reading ``digest_checked = proc.returncode == 0`` passes the two
    matched rows but reports ``False`` for ``1``/``PASS`` where the
    correct reading is ``True`` (the run failed, but the api-digest step
    inside it did not) — and the reverse mismatch for ``0``/``FAIL``.
    """
    return_code, marker, expected_verdict, expected_digest_checked = gate_params
    repo = _repo(tmp_path)

    def _fake_run_tool(argv: list[str], *, cwd: Path, timeout: object) -> FakeProc:
        del timeout
        assert argv[0] == "forge-precommit"
        log_dir = cwd / "code_health"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "precommit_timing.log").write_text(
            timing_log(
                f"api_digest_check {marker}", stamp=git_utils.produced_at_stamp(cwd)
            ),
            encoding="utf-8",
        )
        return FakeProc(returncode=return_code, stdout="out")

    monkeypatch.setattr(pr_evidence, "_run_tool", _fake_run_tool)

    lines, digest_checked = pr_evidence._gate_lines(repo)

    assert any(expected_verdict in line for line in lines)
    assert digest_checked is expected_digest_checked


def test_gate_steps_includes_c4_only_when_configured(tmp_path: Path) -> None:
    """``_gate_steps`` adds the c4 check only when ``[tool.forge.c4]`` is present."""
    repo = _repo(tmp_path)
    assert pr_evidence._gate_steps(repo) == list(pr_evidence.PROVENANCE_GATE_STEPS)

    (repo / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\n\n[tool.forge.c4]\nconfig = "c4.toml"\n',
        encoding="utf-8",
    )

    assert pr_evidence._gate_steps(repo) == [*pr_evidence.PROVENANCE_GATE_STEPS, "c4"]


def test_gate_lines_reports_unavailable_on_a_gate_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout in ``_run_tool`` on the gate call renders ``unavailable``."""
    repo = _repo(tmp_path)

    def _boom(*_a: object, **_kw: object) -> FakeProc:
        raise subprocess.TimeoutExpired(cmd=["forge-precommit"], timeout=300)

    monkeypatch.setattr(pr_evidence, "_run_tool", _boom)

    lines, digest_checked = pr_evidence._gate_lines(repo)

    assert lines == [
        "## Generated artifacts",
        "",
        "unavailable: timed out after 300s",
        "",
    ]
    assert digest_checked is False


# ---------------------------------------------------------------------------
# _surface_lines
# ---------------------------------------------------------------------------


def test_surface_lines_reports_missing_api_digest(tmp_path: Path) -> None:
    """No ``docs/api-digest.md`` in the repo renders a plain one-liner."""
    repo = _repo(tmp_path)
    assert pr_evidence._surface_lines(repo, "main", digest_checked=True) == [
        "no docs/api-digest.md in this repo"
    ]


def test_surface_lines_shows_changed_digest_lines_when_checked(
    tmp_path: Path,
) -> None:
    """A checked digest with a real edit lists exactly its changed lines, unflagged.

    Pins the rendered lines exactly — a leaked ``--- a/…`` / ``+++ b/…``
    diff header, or a miscounted total, must fail here rather than pass a
    looser substring check.
    """
    repo = _repo(tmp_path)
    docs = repo / "docs"
    docs.mkdir()
    (docs / "api-digest.md").write_text("old\n", encoding="utf-8")
    commit_all(repo, "seed digest")
    # Fast-forward `main` to include the seed too, so the diff below shows
    # only the one-line edit that follows — not the file's unrelated
    # addition on `feature`.
    subprocess.run(
        ["git", "branch", "-f", "main", "HEAD"], cwd=repo, env=GIT_ENV, check=True
    )
    (docs / "api-digest.md").write_text("new\n", encoding="utf-8")
    commit_all(repo, "change digest")

    lines = pr_evidence._surface_lines(repo, "main", digest_checked=True)

    assert lines == [
        "2 changed line(s) in docs/api-digest.md:",
        "````",
        "-old",
        "+new",
        "````",
    ]


def test_surface_lines_flags_stale_when_the_digest_check_did_not_pass(
    tmp_path: Path,
) -> None:
    """An unchecked digest gets the staleness warning even with no diff."""
    repo = _repo(tmp_path)
    docs = repo / "docs"
    docs.mkdir()
    (docs / "api-digest.md").write_text("same\n", encoding="utf-8")
    commit_all(repo, "seed digest")
    # Fast-forward `main` to include the digest too, so `main...HEAD` sees
    # no *change* to it — only its unrelated absence would count as one.
    subprocess.run(
        ["git", "branch", "-f", "main", "HEAD"], cwd=repo, env=GIT_ENV, check=True
    )

    lines = pr_evidence._surface_lines(repo, "main", digest_checked=False)

    assert lines[0] == "⚠️ may be stale: the api_digest_check step did not pass"
    assert lines[-1] == "no changed lines in docs/api-digest.md"


def test_surface_section_reports_unavailable_on_an_unresolvable_base(
    tmp_path: Path,
) -> None:
    """``_section`` catches the diff failure from a base git cannot resolve."""
    repo = _repo(tmp_path)
    docs = repo / "docs"
    docs.mkdir()
    (docs / "api-digest.md").write_text("x\n", encoding="utf-8")
    commit_all(repo, "seed digest")

    section = pr_evidence._section(
        "Public surface",
        lambda: pr_evidence._surface_lines(
            repo, "no-such-ref-xyz", digest_checked=True
        ),
    )

    assert section[0] == "## Public surface"
    assert any(line.startswith("unavailable: exited") for line in section)


# ---------------------------------------------------------------------------
# _wiring_lines / _fragment_line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pr_body", "expect_commits_only"),
    [
        ("Closes #12\n", False),
        (None, True),
    ],
)
def test_wiring_lines_notes_commit_only_search_without_a_pr_body(
    tmp_path: Path, *, pr_body: str | None, expect_commits_only: bool
) -> None:
    """The closing-keyword line names its source with/without a readable PR body.

    Args:
        pr_body: The PR body text to search, or ``None`` when no PR is
            readable (commit messages only).
        expect_commits_only: Whether the rendered closing-keyword line
            should note the commits-only search.

    ``render_issue_management`` already owns the exact wording (pinned in
    test_pr_wrapup_compose.py); this pins that ``_wiring_lines`` forwards
    ``pr_body_checked`` correctly from whether a PR body was available.
    """
    repo = _repo(tmp_path)

    lines = pr_evidence._wiring_lines(repo, "main", pr_body)

    closing_line = next(line for line in lines if line.startswith("- closing"))
    assert ("commit messages only" in closing_line) is expect_commits_only


@pytest.mark.parametrize(
    ("fragments_on", "pr_base_differs", "expected_contains"),
    [
        (False, False, "fragments mode is off"),
        (True, False, "fragments added: changelog.d/x.added.md"),
        (True, True, "(compared with origin/parent)"),
    ],
)
def test_fragment_line_names_base_only_when_it_diverges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fragments_on: bool,
    pr_base_differs: bool,
    expected_contains: str,
) -> None:
    """Fragments off, fragments on with a matching base, and a diverging base.

    Args:
        fragments_on: Whether ``[tool.forge.changelog].mode = "fragments"``
            is configured for the repo.
        pr_base_differs: Whether the faked PR base ref differs from the
            configured base branch.
        expected_contains: Substring the rendered line must contain.

    MOCK SETUP: ``pr_evidence.branch_added_fragments`` and
    ``pr_evidence.resolve_pr_base_ref`` are both replaced directly — see
    the file MOCKING STRATEGY for why patching only one leaks a real
    ``gh api`` call.
    """
    repo = _repo(tmp_path, fragments_mode=fragments_on)
    monkeypatch.setattr(
        pr_evidence,
        "branch_added_fragments",
        lambda _root: ["changelog.d/x.added.md"],
    )
    monkeypatch.setattr(
        pr_evidence,
        "resolve_pr_base_ref",
        lambda _root, _base: "origin/parent" if pr_base_differs else "main",
    )

    line = pr_evidence._fragment_line(repo, "main")

    assert expected_contains in line


# ---------------------------------------------------------------------------
# build_pack / write_pack — integration
# ---------------------------------------------------------------------------


def test_write_pack_writes_a_stamp_that_reads_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The written pack's line 1 names the tree it was built against."""
    repo = _repo(tmp_path)

    def _fake_run_tool(argv: list[str], *, cwd: object, timeout: object) -> FakeProc:
        del cwd, timeout
        if argv[0] == "forge-precommit":
            return FakeProc(returncode=0, stdout="ok")
        return FakeProc(returncode=3)

    monkeypatch.setattr(pr_evidence, "_run_tool", _fake_run_tool)

    path = pr_evidence.write_pack(
        repo, base="main", plan={"mode": "full", "reasons": []}, added=[], pr_body=None
    )

    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    assert PRODUCED_AT_RE.match(first_line) is not None
    assert git_utils.log_freshness(path, git_utils.working_tree_sha(repo)) == "fresh"


def test_build_pack_renders_sections_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify sections render in the documented order.

    The pack renders sections as: PR, Code health, Audits, Generated
    artifacts, Public surface, Wiring — the *rendering* order, distinct
    from the *gather* order pinned separately above.
    """
    repo = _repo(tmp_path)

    def _fake_run_tool(argv: list[str], *, cwd: object, timeout: object) -> FakeProc:
        del cwd, timeout
        if argv[0] == "forge-precommit":
            return FakeProc(returncode=0, stdout="ok")
        return FakeProc(returncode=3)

    monkeypatch.setattr(pr_evidence, "_run_tool", _fake_run_tool)

    pack = pr_evidence.build_pack(
        repo, base="main", plan={"mode": "full", "reasons": []}, added=[], pr_body=None
    )

    headers = [line for line in pack.splitlines() if line.startswith("## ")]
    assert headers == [
        "## PR",
        "## Code health",
        "## Audits",
        "## Generated artifacts",
        "## Public surface",
        "## Wiring",
    ]


# ---------------------------------------------------------------------------
# _data
# ---------------------------------------------------------------------------


def test_data_sanitizes_and_fences_hostile_content() -> None:
    """``_data`` escapes control characters per line and fences past content backticks.

    Untrusted tool output — audit findings, diffs, filenames — reaches
    ``_data`` verbatim; this pins the composition FOUNDATION §13 requires:
    every line sanitized before fencing, and the fence long enough that a
    bare backtick line inside the content can never close it early.
    """
    hostile = "before\n````\nred\x1b[31mtext"

    lines = pr_evidence._data(hostile)

    assert lines[0] == "`````"
    assert lines[-1] == "`````"
    body = "\n".join(lines[1:-1])
    assert "\x1b" not in body
    assert "\\x1b[31mtext" in body
    assert "````" in body  # the hostile line survives as inert content
