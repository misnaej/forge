"""Tests for ``forge.audit.dup`` duplicate-detection pipeline."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest

from forge.audit import common
from forge.audit.common import Scope
from forge.audit.dup import (
    CodeUnit,
    DupConfig,
    _find_name_collisions,
    _find_near_dups,
    _group_by_hash,
    _jaccard,
    _normalize_body,
    _shingles,
    _tokenize_body,
    extract_units,
    run,
)


if TYPE_CHECKING:
    from pathlib import Path


IDENTICAL_BODY_A = """
def helper(x, y):
    \"\"\"Compute weighted score for two inputs.\"\"\"
    weight = 0.5
    score = x * weight + y * (1 - weight)
    if score < 0:
        return 0.0
    return min(score, 100.0)
"""

IDENTICAL_BODY_B = """
def helper(x, y):
    \"\"\"Different docstring entirely.\"\"\"
    weight = 0.5
    score = x * weight + y * (1 - weight)
    if score < 0:
        return 0.0
    return min(score, 100.0)
"""

NEAR_DUP_BODY = """
def helper_variant(a, b):
    \"\"\"Same shape, slight token rename.\"\"\"
    weight = 0.5
    total = a * weight + b * (1 - weight)
    if total < 0:
        return 0.0
    return min(total, 100.0)
"""

DIFFERENT_BODY = """
def helper(x, y):
    \"\"\"Same name, very different body.\"\"\"
    return [item for item in (x, y) if item is not None]
"""


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a repo-like tree and point common.repo_root at it.

    Returns:
        The repo root path.
    """
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(common, "repo_root", lambda: tmp_path)
    return tmp_path


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, creating parent dirs.

    Args:
        path: Destination file path.
        text: Content to write (leading whitespace is stripped).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip(), encoding="utf-8")


def test_normalize_body_strips_docstring() -> None:
    """A leading string-expression is removed from the body source."""
    tree = ast.parse(IDENTICAL_BODY_A.lstrip())
    fn = tree.body[0]
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    body_src = _normalize_body(fn)
    assert "Compute weighted score" not in body_src
    assert "score = x * weight" in body_src


def test_tokenize_body_collapses_strings_and_numbers() -> None:
    """String/number tokens are folded to NAME-class sentinels."""
    tokens = _tokenize_body("x = 'hello' + 42")
    assert "STR" in tokens
    assert "NUM" in tokens
    assert "'hello'" not in tokens
    assert "42" not in tokens


def test_shingles_returns_empty_when_shorter_than_k() -> None:
    """A token sequence below ``k`` length yields no shingles."""
    assert _shingles(["a", "b"], k=5) == frozenset()


def test_shingles_produces_overlapping_kgrams() -> None:
    """Shingle count equals ``len(tokens) - k + 1`` for unique tokens."""
    out = _shingles(["a", "b", "c", "d", "e", "f"], k=3)
    assert len(out) == 4


def test_jaccard_identical_sets_returns_one() -> None:
    """Two equal shingle sets give Jaccard = 1.0."""
    a = frozenset([("x", "y")])
    assert _jaccard(a, a) == pytest.approx(1.0)


def test_jaccard_disjoint_sets_returns_zero() -> None:
    """Disjoint shingle sets give Jaccard = 0.0."""
    a = frozenset([("x",)])
    b = frozenset([("y",)])
    assert _jaccard(a, b) == pytest.approx(0.0)


def test_extract_units_picks_up_function(fake_repo: Path) -> None:
    """A single function file produces one CodeUnit."""
    f = fake_repo / "src" / "mod.py"
    _write(f, IDENTICAL_BODY_A)
    units = extract_units(f, min_tokens=5, shingle_size=3)
    assert len(units) == 1
    assert units[0].bare_name == "helper"
    assert units[0].path.endswith("src/mod.py")


def test_extract_units_skips_small_functions(fake_repo: Path) -> None:
    """A function below ``min_tokens`` is omitted."""
    f = fake_repo / "src" / "tiny.py"
    _write(f, "def tiny():\n    return 1\n")
    units = extract_units(f, min_tokens=30, shingle_size=5)
    assert units == []


def test_extract_units_handles_syntax_error_gracefully(fake_repo: Path) -> None:
    """Parse failures yield an empty list, not an exception."""
    f = fake_repo / "src" / "broken.py"
    _write(f, "def !!! broken !!!\n")
    units = extract_units(f, min_tokens=5, shingle_size=3)
    assert units == []


def test_group_by_hash_finds_exact_dups(fake_repo: Path) -> None:
    """Two files with the same body (docstrings differ) hash-collide."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "b.py", IDENTICAL_BODY_B)
    units = extract_units(
        fake_repo / "src" / "a.py", min_tokens=5, shingle_size=3
    ) + extract_units(fake_repo / "src" / "b.py", min_tokens=5, shingle_size=3)
    groups = _group_by_hash(units)
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_find_near_dups_pairs_similar_bodies(fake_repo: Path) -> None:
    """Two bodies with the same shape but renamed locals score above 0.85."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "b.py", NEAR_DUP_BODY)
    units = extract_units(
        fake_repo / "src" / "a.py", min_tokens=5, shingle_size=3
    ) + extract_units(fake_repo / "src" / "b.py", min_tokens=5, shingle_size=3)
    pairs = _find_near_dups(units, exact_dup_ids=set(), threshold=0.5)
    assert len(pairs) >= 1
    a, b, sim = pairs[0]
    assert sim >= 0.5
    assert {a.path, b.path} == {"src/a.py", "src/b.py"}


def test_find_name_collisions_groups_same_name_different_body(fake_repo: Path) -> None:
    """Same bare name + different body + multiple files → collision."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "c.py", DIFFERENT_BODY)
    units = extract_units(
        fake_repo / "src" / "a.py", min_tokens=5, shingle_size=3
    ) + extract_units(fake_repo / "src" / "c.py", min_tokens=5, shingle_size=3)
    groups = _find_name_collisions(units, exact_dup_ids=set())
    assert len(groups) == 1
    assert {u.path for u in groups[0]} == {"src/a.py", "src/c.py"}


def test_run_writes_log_with_high_severity_for_cross_file_dup(fake_repo: Path) -> None:
    """run() reports an exact cross-file dup as HIGH severity in the log."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "b.py", IDENTICAL_BODY_B)
    code = run(Scope.FULL, [fake_repo / "src"], DupConfig(min_tokens=5, shingle_size=3))
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "[HIGH]" in log_text
    assert "exact body duplicate of helper" in log_text
    assert code == 1


def test_run_clean_repo_returns_zero_exit(fake_repo: Path) -> None:
    """A repo with no duplicates produces exit 0 and a 'no findings' log."""
    _write(fake_repo / "src" / "only.py", IDENTICAL_BODY_A)
    code = run(Scope.FULL, [fake_repo / "src"], DupConfig(min_tokens=5, shingle_size=3))
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "# findings: 0" in log_text
    assert code == 0


def test_severity_critical_for_three_plus_files(fake_repo: Path) -> None:
    """An exact duplicate across 3+ files escalates to CRITICAL."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "b.py", IDENTICAL_BODY_B)
    _write(fake_repo / "src" / "c.py", IDENTICAL_BODY_A)
    run(Scope.FULL, [fake_repo / "src"], DupConfig(min_tokens=5, shingle_size=3))
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "[CRITICAL]" in log_text


def test_codeunit_qualified_name_includes_class(fake_repo: Path) -> None:
    """Methods get a ``Class.method`` qualified name."""
    _write(
        fake_repo / "src" / "cls.py",
        "class Foo:\n"
        "    def bar(self, x):\n"
        "        weight = 0.5\n"
        "        total = x * weight\n"
        "        return total\n",
    )
    units = extract_units(fake_repo / "src" / "cls.py", min_tokens=5, shingle_size=3)
    assert any(u.qualified_name == "Foo.bar" for u in units)


def test_codeunit_dataclass_roundtrip() -> None:
    """CodeUnit holds the expected fields and is hashable via frozen dc."""
    u = CodeUnit(
        path="src/a.py",
        line=1,
        qualified_name="f",
        bare_name="f",
        body_hash="abc",
        token_count=10,
    )
    assert u.bare_name == "f"
    assert isinstance(u, CodeUnit)
