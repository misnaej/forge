"""Tests for ``forge.verify_doc_consistency``.

# MOCKING STRATEGY: each test builds a throwaway repo tree under tmp_path
# (pyproject, docs/cli-reference.md, agents/*.md, FOUNDATION.md) and runs
# the real check functions against it. ``main`` tests pin get_repo_root to
# tmp_path and patch sys.argv so argparse does not see pytest's argv.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import verify_doc_consistency as vdc


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write(path: Path, text: str) -> None:
    """Write *text* to *path*, creating parent directories as needed.

    Args:
        path: Destination file path.
        text: Contents to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_cli_coverage_skips_without_inputs(tmp_path: Path) -> None:
    """No pyproject or no cli-reference → nothing to check."""
    assert vdc._check_cli_coverage(tmp_path) == []


def test_cli_coverage_clean_when_all_documented(tmp_path: Path) -> None:
    """Every `[project.scripts]` name present in the reference → no findings."""
    _write(tmp_path / "pyproject.toml", '[project.scripts]\nfoo-cli = "x:main"\n')
    _write(tmp_path / "docs" / "cli-reference.md", "# CLIs\n- foo-cli does things\n")
    assert vdc._check_cli_coverage(tmp_path) == []


def test_cli_coverage_flags_missing(tmp_path: Path) -> None:
    """A script absent from the reference doc is reported by name."""
    _write(
        tmp_path / "pyproject.toml",
        '[project.scripts]\nfoo-cli = "x:main"\nbar-cli = "y:main"\n',
    )
    _write(tmp_path / "docs" / "cli-reference.md", "# CLIs\n- foo-cli only\n")
    findings = vdc._check_cli_coverage(tmp_path)
    assert len(findings) == 1
    assert "bar-cli" in findings[0]


def test_cli_coverage_malformed_pyproject_skips(tmp_path: Path) -> None:
    """A malformed pyproject yields no findings rather than raising."""
    _write(tmp_path / "pyproject.toml", "this is = = not [[[ toml")
    _write(tmp_path / "docs" / "cli-reference.md", "# CLIs\n")
    assert vdc._check_cli_coverage(tmp_path) == []


def test_main_returns_zero_when_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 0 on an empty repo — the check skips, nothing drifts.

    MOCK SETUP: get_repo_root pinned to an empty tmp_path; argv patched so
    argparse does not consume pytest's arguments.
    """
    monkeypatch.setattr(vdc, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vdc.sys, "argv", ["verify-forge-doc-consistency"])
    assert vdc.main() == 0


def test_main_returns_one_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 1 when CLI coverage drifts.

    MOCK SETUP: a repo with a [project.scripts] CLI absent from the
    reference doc; get_repo_root pinned to it and argv patched.
    """
    _write(tmp_path / "pyproject.toml", '[project.scripts]\nundocumented = "x:main"\n')
    _write(tmp_path / "docs" / "cli-reference.md", "# CLIs\n")
    monkeypatch.setattr(vdc, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vdc.sys, "argv", ["verify-forge-doc-consistency"])
    assert vdc.main() == 1


def test_provenance_gate_names_clean_when_synced(tmp_path: Path) -> None:
    """Both prose surfaces naming every PROVENANCE_GATE_STEPS token → no findings."""
    _write(
        tmp_path / "src" / "forge" / "precommit.py",
        '"""Provenance gate steps enforced here: foundation_md_check, '
        'cli_reference_check, api_digest_check."""\n',
    )
    _write(
        tmp_path / "skills" / "pr" / "SKILL.md",
        "# /pr skill\n"
        "Provenance gates run foundation_md_check, cli_reference_check, "
        "and api_digest_check before finalizing.\n",
    )
    assert vdc._check_provenance_gate_names(tmp_path) == []


def test_provenance_gate_names_flags_missing_step(tmp_path: Path) -> None:
    """A prose surface omitting a constant step is reported by file and step name."""
    _write(
        tmp_path / "skills" / "pr" / "SKILL.md",
        "Provenance gates run foundation_md_check and cli_reference_check "
        "before finalizing.\n",
    )
    findings = vdc._check_provenance_gate_names(tmp_path)
    assert len(findings) == 1
    assert "skills/pr/SKILL.md" in findings[0]
    assert "api_digest_check" in findings[0]


def test_provenance_gate_names_flags_stale_token(tmp_path: Path) -> None:
    """A `*_check` token in provenance prose absent from the constant is stale."""
    _write(
        tmp_path / "skills" / "pr" / "SKILL.md",
        "Provenance gates run foundation_md_check, cli_reference_check, "
        "api_digest_check, and legacy_gate_check before finalizing.\n",
    )
    findings = vdc._check_provenance_gate_names(tmp_path)
    assert len(findings) == 1
    assert "legacy_gate_check" in findings[0]
    assert "skills/pr/SKILL.md" in findings[0]


def test_provenance_gate_names_ignores_step_prefixed_function_reference(
    tmp_path: Path,
) -> None:
    """A `step_<name>` function reference is not itself flagged as a stale token.

    The missing-step check is unstripped (a bare `step_foundation_md_check`
    reference alone does not satisfy it — see the
    ``..._flags_missing_step_for_substring_only_match`` regression test), so
    this fixture also names the step in plain form, mirroring precommit.py's
    real layout where the function definition and a plain step-name mention
    coexist.
    """
    _write(
        tmp_path / "skills" / "pr" / "SKILL.md",
        "Provenance gates call step_foundation_md_check, foundation_md_check, "
        "cli_reference_check, and api_digest_check to verify sync.\n",
    )
    assert vdc._check_provenance_gate_names(tmp_path) == []


def test_provenance_gate_names_skips_without_prose_files(tmp_path: Path) -> None:
    """Neither prose surface present → nothing to check."""
    assert vdc._check_provenance_gate_names(tmp_path) == []


def test_provenance_prose_tokens_excludes_token_outside_window() -> None:
    """A `*_check` token beyond `_PROVENANCE_WINDOW` lines from a "provenance" mention.

    Beyond the window, the token is dropped.
    """
    filler = "\n".join(f"Line filler {i}" for i in range(vdc._PROVENANCE_WINDOW + 1))
    text = f"Line0: Provenance overview.\n{filler}\nfar_away_check appears here"
    assert vdc._provenance_prose_tokens(text) == set()


def test_main_returns_one_on_provenance_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 1 when provenance gate-step prose drifts from the constant.

    MOCK SETUP: only the /pr skill prose exists and omits two constant
    steps; get_repo_root pinned to it and argv patched.
    """
    _write(
        tmp_path / "skills" / "pr" / "SKILL.md",
        "Provenance gates run foundation_md_check only.\n",
    )
    monkeypatch.setattr(vdc, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(vdc.sys, "argv", ["verify-forge-doc-consistency"])
    assert vdc.main() == 1


def test_provenance_gate_names_window_boundary_with_step_prefix_not_flagged(
    tmp_path: Path,
) -> None:
    """A `step_` function ref at the window boundary is neither missing nor stale.

    Mirrors precommit.py's real layout: `step_foundation_md_check` (the
    function name) sits exactly `_PROVENANCE_WINDOW` lines above the prose
    line naming the gates in plain form. Locks the window-inclusive
    boundary and the `step_` prefix strip working together — either one
    alone regressing would turn this into a false stale-token finding.
    """
    filler = "\n".join(f"filler line {i}" for i in range(vdc._PROVENANCE_WINDOW - 1))
    _write(
        tmp_path / "src" / "forge" / "precommit.py",
        (
            f"step_foundation_md_check line\n{filler}\n"
            "Provenance gates: foundation_md_check, cli_reference_check, "
            "api_digest_check.\n"
        ),
    )
    assert vdc._check_provenance_gate_names(tmp_path) == []


def test_provenance_prose_tokens_includes_token_at_window_boundary() -> None:
    """A `*_check` token exactly `_PROVENANCE_WINDOW` lines from provenance.

    Collected at the window boundary. Complements
    ``test_provenance_prose_tokens_excludes_token_outside_window``: the window
    is inclusive at its edge, not merely close to it.
    """
    filler = "\n".join(f"Line filler {i}" for i in range(vdc._PROVENANCE_WINDOW - 1))
    text = f"Line0: Provenance overview.\n{filler}\nboundary_check appears here"
    assert vdc._provenance_prose_tokens(text) == {"boundary_check"}


def test_provenance_gate_names_flags_missing_step_for_substring_only_match(
    tmp_path: Path,
) -> None:
    """A step name embedded inside a longer identifier is not a mention.

    ``xxfoundation_md_checkxx`` contains ``foundation_md_check`` as a
    substring but not as a word-boundary token, so it must not count as
    the step being named — locking the word-boundary fix for the
    missing-step direction (symmetric with the stale-token direction).
    """
    _write(
        tmp_path / "skills" / "pr" / "SKILL.md",
        "Provenance gates run xxfoundation_md_checkxx, cli_reference_check, "
        "and api_digest_check before finalizing.\n",
    )
    findings = vdc._check_provenance_gate_names(tmp_path)
    assert len(findings) == 1
    assert "foundation_md_check" in findings[0]
    assert "is not mentioned" in findings[0]


def test_provenance_gate_names_multi_file_attributes_to_drifting_file_only(
    tmp_path: Path,
) -> None:
    """Findings from one drifting file never leak the clean file's path.

    Both provenance prose surfaces exist; precommit.py names every
    constant step while SKILL.md omits one, so every finding must name
    only SKILL.md.
    """
    _write(
        tmp_path / "src" / "forge" / "precommit.py",
        '"""Provenance gates: foundation_md_check, cli_reference_check, '
        'api_digest_check."""\n',
    )
    _write(
        tmp_path / "skills" / "pr" / "SKILL.md",
        "Provenance gates run foundation_md_check and cli_reference_check "
        "before finalizing.\n",
    )
    findings = vdc._check_provenance_gate_names(tmp_path)
    assert len(findings) == 1
    assert "skills/pr/SKILL.md" in findings[0]
    assert "src/forge/precommit.py" not in findings[0]


def test_provenance_gate_names_covers_configuration_md(tmp_path: Path) -> None:
    """`forge-docs/configuration.md` is a covered prose surface.

    Covered the same as the other prose surfaces.
    """
    _write(
        tmp_path / "forge-docs" / "configuration.md",
        "Provenance gates run foundation_md_check and cli_reference_check "
        "before finalizing.\n",
    )
    findings = vdc._check_provenance_gate_names(tmp_path)
    assert len(findings) == 1
    assert "forge-docs/configuration.md" in findings[0]
    assert "api_digest_check" in findings[0]
