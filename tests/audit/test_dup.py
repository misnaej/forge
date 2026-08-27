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
    _build_exact_findings,
    _build_name_findings,
    _build_near_findings,
    _find_name_collisions,
    _find_near_dups,
    _group_by_hash,
    _jaccard,
    _normalize_body,
    _shingles,
    _summary,
    _tokenize_body,
    _touches_changed,
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

UNRELATED_BODY = """
def compute_total(items):
    \"\"\"Sum values in a list.\"\"\"
    total = 0
    for item in items:
        total += item
    return total
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


def _fake_changed(monkeypatch: pytest.MonkeyPatch, paths: list[str]) -> None:
    """Patch get_modified_files so CHANGED scope reports exactly ``paths``.

    Args:
        paths: Modified-file paths the patched function should report.
    """
    monkeypatch.setattr(common, "get_modified_files", lambda **_: paths)


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


def test_tokenize_body_returns_empty_on_token_error() -> None:
    """Unterminated source is swallowed and yields ``[]``, not an exception.

    Regression: the handler caught ``tokenize.TokenizeError`` — a name that
    does not exist in the stdlib (the real exception is
    ``tokenize.TokenError``) — so a tokenization failure raised
    ``AttributeError`` instead of being caught. The unclosed parenthesis
    below makes ``tokenize`` raise ``TokenError`` at EOF.
    """
    assert _tokenize_body("x = (1 +") == []


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


def test_run_changed_scope_finds_prior_art_in_unchanged_file(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed file's exact duplicate in an *unchanged* file is found.

    Regression: previously changed files were compared only against each
    other, so a changed file duplicating an unchanged one went unreported.
    """
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "b.py", IDENTICAL_BODY_B)
    _fake_changed(monkeypatch, ["src/b.py"])
    code = run(
        Scope.CHANGED, [fake_repo / "src"], DupConfig(min_tokens=5, shingle_size=3)
    )
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "[HIGH]" in log_text
    assert "exact body duplicate" in log_text
    assert code == 1


def test_run_changed_scope_excludes_finding_with_no_changed_unit(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate pair with neither side changed is filtered out."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "b.py", IDENTICAL_BODY_B)
    _write(fake_repo / "src" / "c.py", UNRELATED_BODY)
    _fake_changed(monkeypatch, ["src/c.py"])
    code = run(
        Scope.CHANGED, [fake_repo / "src"], DupConfig(min_tokens=5, shingle_size=3)
    )
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "exact body duplicate" not in log_text
    assert code == 0


def test_run_changed_scope_includes_near_dup_when_one_side_changed(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A near-duplicate pair is reported when one side is changed."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "b.py", NEAR_DUP_BODY)
    _fake_changed(monkeypatch, ["src/b.py"])
    code = run(
        Scope.CHANGED,
        [fake_repo / "src"],
        DupConfig(min_tokens=5, shingle_size=3, threshold=0.5),
    )
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "near-duplicate (" in log_text
    assert code == 1


def test_run_changed_scope_excludes_near_dup_with_no_changed_unit(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A near-duplicate pair with neither side changed is filtered out."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "b.py", NEAR_DUP_BODY)
    _write(fake_repo / "src" / "c.py", UNRELATED_BODY)
    _fake_changed(monkeypatch, ["src/c.py"])
    code = run(
        Scope.CHANGED,
        [fake_repo / "src"],
        DupConfig(min_tokens=5, shingle_size=3, threshold=0.5),
    )
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "near-duplicate (" not in log_text
    assert code == 0


def test_run_changed_scope_includes_name_collision_when_changed(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name-collision group is reported when one side is changed."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "c.py", DIFFERENT_BODY)
    _fake_changed(monkeypatch, ["src/c.py"])
    code = run(
        Scope.CHANGED, [fake_repo / "src"], DupConfig(min_tokens=5, shingle_size=3)
    )
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "name collision" in log_text
    assert code == 0


def test_run_changed_scope_excludes_name_collision_with_no_changed_unit(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name-collision group with neither side changed is filtered out."""
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "c.py", DIFFERENT_BODY)
    _write(fake_repo / "src" / "d.py", UNRELATED_BODY)
    _fake_changed(monkeypatch, ["src/d.py"])
    code = run(
        Scope.CHANGED, [fake_repo / "src"], DupConfig(min_tokens=5, shingle_size=3)
    )
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "name collision" not in log_text
    assert code == 0


def test_run_changed_scope_empty_changeset_short_circuits(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty changeset returns immediately without a full-tree walk.

    ``extract_units`` is patched to raise if called, proving the early
    return happens before the full-tree indexing loop runs.
    """
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _fake_changed(monkeypatch, [])

    def _fail_if_called(*_args: object, **_kwargs: object) -> list[CodeUnit]:
        """Fail the test if the full-tree walk is reached.

        Args:
            *_args: Unused positional arguments (unreachable — this stub
                always raises before accepting them).
            **_kwargs: Unused keyword arguments (unreachable, same reason).
        """
        msg = "extract_units called — full-tree walk not short-circuited"
        raise AssertionError(msg)

    monkeypatch.setattr("forge.audit.dup.extract_units", _fail_if_called)
    code = run(
        Scope.CHANGED, [fake_repo / "src"], DupConfig(min_tokens=5, shingle_size=3)
    )
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert code == 0
    assert (
        "Matched 0 changed-file unit(s) against a 0-unit full-tree index. "
        "Found 0 exact-duplicate group(s), 0 near-duplicate pair(s), "
        "0 name-collision group(s)."
    ) in log_text


def test_run_changed_scope_indexes_file_outside_roots(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed file outside ``roots`` is still indexed and matched.

    Regression: ``iter_files`` in CHANGED scope ignores ``roots`` entirely,
    so a changed file can sit outside the directories being scanned. It
    must still be extracted and checked against the full-tree index.
    """
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "other" / "outside.py", IDENTICAL_BODY_B)
    _fake_changed(monkeypatch, ["other/outside.py"])
    code = run(
        Scope.CHANGED, [fake_repo / "src"], DupConfig(min_tokens=5, shingle_size=3)
    )
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "exact body duplicate" in log_text
    assert code == 1


def test_build_exact_findings_key_is_sorted_path_qualified_names() -> None:
    """Exact-dup key joins sorted path-qualified pairs, order-independent.

    Path-qualification (added alongside #291) is what disambiguates two
    unrelated exact-dup groups that happen to share a bare/qualified name —
    without it, both groups would collapse onto the same key.
    """
    unit_z = CodeUnit(
        path="src/z.py",
        line=1,
        qualified_name="z.helper",
        bare_name="helper",
        body_hash="same",
        token_count=10,
    )
    unit_a = CodeUnit(
        path="src/a.py",
        line=1,
        qualified_name="a.helper",
        bare_name="helper",
        body_hash="same",
        token_count=10,
    )
    findings_forward, _ = _build_exact_findings([[unit_z, unit_a]])
    findings_reversed, _ = _build_exact_findings([[unit_a, unit_z]])
    expected = "src/a.py:a.helper|src/z.py:z.helper"
    assert findings_forward[0].key == expected
    assert findings_reversed[0].key == expected


def test_build_exact_findings_key_distinct_across_different_file_pairs() -> None:
    """Exact-dup groups sharing names across different files get distinct keys.

    Regression (#291): before path-qualification, two unrelated exact-dup
    groups that both involved a function named ``helper`` collided onto the
    same ``key`` — collapsing two independent findings into one in
    suppression/dedup logic keyed on ``key``.
    """
    group_one = [
        CodeUnit(
            path="src/a.py",
            line=1,
            qualified_name="helper",
            bare_name="helper",
            body_hash="same-1",
            token_count=10,
        ),
        CodeUnit(
            path="src/b.py",
            line=1,
            qualified_name="helper",
            bare_name="helper",
            body_hash="same-1",
            token_count=10,
        ),
    ]
    group_two = [
        CodeUnit(
            path="src/c.py",
            line=1,
            qualified_name="helper",
            bare_name="helper",
            body_hash="same-2",
            token_count=10,
        ),
        CodeUnit(
            path="src/d.py",
            line=1,
            qualified_name="helper",
            bare_name="helper",
            body_hash="same-2",
            token_count=10,
        ),
    ]
    findings, _ = _build_exact_findings([group_one, group_two])
    keys = {f.key for f in findings}
    assert len(keys) == len(findings) == 2


def test_build_near_findings_key_is_sorted_path_qualified_pair() -> None:
    """Near-dup key is the sorted pair of ``path:qualified_name``, order-independent."""
    unit_z = CodeUnit(
        path="src/z.py",
        line=1,
        qualified_name="z.helper",
        bare_name="helper",
        body_hash="h1",
        token_count=10,
    )
    unit_a = CodeUnit(
        path="src/a.py",
        line=1,
        qualified_name="a.helper",
        bare_name="helper",
        body_hash="h2",
        token_count=10,
    )
    findings = _build_near_findings([(unit_z, unit_a, 0.9)])
    assert findings[0].key == "src/a.py:a.helper|src/z.py:z.helper"


def test_build_near_findings_key_distinct_across_different_file_pairs() -> None:
    """Near-dup pairs sharing names across different files get distinct keys."""
    pair_one = (
        CodeUnit(
            path="src/a.py",
            line=1,
            qualified_name="helper",
            bare_name="helper",
            body_hash="h1",
            token_count=10,
        ),
        CodeUnit(
            path="src/b.py",
            line=1,
            qualified_name="helper",
            bare_name="helper",
            body_hash="h2",
            token_count=10,
        ),
        0.9,
    )
    pair_two = (
        CodeUnit(
            path="src/c.py",
            line=1,
            qualified_name="helper",
            bare_name="helper",
            body_hash="h3",
            token_count=10,
        ),
        CodeUnit(
            path="src/d.py",
            line=1,
            qualified_name="helper",
            bare_name="helper",
            body_hash="h4",
            token_count=10,
        ),
        0.9,
    )
    findings = _build_near_findings([pair_one, pair_two])
    keys = {f.key for f in findings}
    assert len(keys) == len(findings) == 2


def test_build_name_findings_key_is_name_collision_prefixed() -> None:
    """Name-collision key is ``name-collision:<bare_name>``."""
    unit_a = CodeUnit(
        path="src/a.py",
        line=1,
        qualified_name="helper",
        bare_name="helper",
        body_hash="h1",
        token_count=10,
    )
    unit_b = CodeUnit(
        path="src/b.py",
        line=2,
        qualified_name="helper",
        bare_name="helper",
        body_hash="h2",
        token_count=10,
    )
    findings = _build_name_findings([[unit_a, unit_b]])
    assert findings[0].key == "name-collision:helper"


def test_summary_full_scope_uses_scanned_wording() -> None:
    """FULL-scope summary opens with the plain scanned-unit count."""
    text = _summary(10, 1, 2, 3)
    assert text.startswith("Scanned 10 function units.")


def test_summary_changed_scope_uses_matched_wording() -> None:
    """CHANGED-scope summary opens with the matched-against-index wording."""
    text = _summary(10, 1, 2, 3, n_changed=3)
    assert text.startswith(
        "Matched 3 changed-file unit(s) against a 10-unit full-tree index."
    )


def test_touches_changed_true_when_one_unit_in_changed_set() -> None:
    """True when at least one unit's path is in the changed set."""
    unit = CodeUnit(
        path="src/a.py",
        line=1,
        qualified_name="f",
        bare_name="f",
        body_hash="x",
        token_count=1,
    )
    assert _touches_changed({"src/a.py"}, [unit]) is True


def test_touches_changed_false_when_no_unit_in_changed_set() -> None:
    """False when no unit's path is in the changed set."""
    unit = CodeUnit(
        path="src/a.py",
        line=1,
        qualified_name="f",
        bare_name="f",
        body_hash="x",
        token_count=1,
    )
    assert _touches_changed({"src/other.py"}, [unit]) is False


def test_touches_changed_false_for_empty_units() -> None:
    """False when the units iterable is empty, regardless of changed set."""
    assert _touches_changed({"src/a.py"}, []) is False


def test_run_changed_scope_findings_are_subset_of_full_scope(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Units covered by an unchanged exact-dup group stay excluded.

    Regression: ``covered`` was computed from the scope-FILTERED exact
    groups, so an exact pair living entirely in unchanged files re-entered
    near-dup candidacy in CHANGED scope — reporting a near-duplicate (once
    per group member) that FULL scope structurally cannot produce.
    """
    _write(fake_repo / "src" / "a.py", IDENTICAL_BODY_A)
    _write(fake_repo / "src" / "b.py", IDENTICAL_BODY_B)
    _write(fake_repo / "src" / "c.py", NEAR_DUP_BODY)
    _fake_changed(monkeypatch, ["src/c.py"])
    code = run(
        Scope.CHANGED,
        [fake_repo / "src"],
        DupConfig(min_tokens=5, shingle_size=3, threshold=0.5),
    )
    log_text = (fake_repo / "code_health" / "audit_dup.log").read_text(encoding="utf-8")
    assert "near-duplicate (" not in log_text
    assert "exact body duplicate" not in log_text
    assert code == 0
