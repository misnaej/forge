"""Superset regression harness for forge-smart-test's three selection channels.

Verifies that the union of (static import graph + mock-patch edges +
coverage-validated contexts) is a superset of the ground-truth test set for
every category of test↔code link that forge guarantees to catch.

Layout under test::

    src/pkg/a.py   — imported by tests/test_a.py (static edge)
    src/pkg/b.py   — ONLY patched via @patch("pkg.b.thing") in tests/test_b.py
                     (no import; follow_mock_patches)
    src/pkg/c.py   — covered only via a coverage JSON context for test_c.py
                     (no import, no patch; coverage_json / coverage validation)

PASS criterion: for each change ``{src/pkg/X.py}``, the combined selected set
(follow_mock_patches=True, coverage_json=the fixture JSON) contains
``tests/test_X.py``.  This is the regression guarantee for both opt-in
alignment features; a regression in either causes this file to fail first.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from forge.smart_test import coverage as cov_stage
from forge.smart_test.dependencies import select_tests


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def superset_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Real on-disk repo with three source files, each reached via a different channel.

    Writes a ``coverage.json`` export that records ``src/pkg/c.py`` as covered
    by ``tests/test_c.py`` so the coverage channel can be tested without
    running an actual instrumented suite.

    Returns:
        A ``(repo_root, coverage_json_path)`` tuple.
    """
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src"]\ntest_dirs = ["tests"]\n',
        encoding="utf-8",
    )

    pkg = root / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "b.py").write_text("thing = 42\n", encoding="utf-8")
    (pkg / "c.py").write_text("val = 99\n", encoding="utf-8")

    tests_dir = root / "tests"
    tests_dir.mkdir()

    # test_a: static import edge → selected by import graph
    (tests_dir / "test_a.py").write_text(
        "from pkg.a import x\n\n\ndef test_a():\n    assert x == 1\n",
        encoding="utf-8",
    )

    # test_b: patch-only edge → selected only via follow_mock_patches
    (tests_dir / "test_b.py").write_text(
        "from unittest.mock import patch\n\n\n"
        '@patch("pkg.b.thing")\n'
        "def test_b(mock_thing: object) -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )

    # test_c: no import, no patch → selected only via coverage context
    (tests_dir / "test_c.py").write_text(
        "def test_c() -> None:\n    pass\n",
        encoding="utf-8",
    )

    # Coverage JSON: pkg/c.py line 1 is covered by test_c.py
    cov_data = {
        "files": {
            "src/pkg/c.py": {
                "contexts": {
                    "1": ["tests/test_c.py::test_c"],
                }
            }
        }
    }
    cov_file = root / "coverage.json"
    cov_file.write_text(json.dumps(cov_data), encoding="utf-8")

    return root, cov_file


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _selected(root: Path, changed: set[str], cov_file: Path) -> set[str]:
    """Compute the full selection for *changed* with both opt-in features active.

    Args:
        root: Repo root (the ``superset_repo`` fixture root).
        changed: Repo-relative changed source paths.
        cov_file: Path to the coverage JSON file.

    Returns:
        Union of the static-graph selection and coverage-validated extras.
    """
    plan = select_tests(root, changed, max_depth=0, follow_mock_patches=True)
    extra = cov_stage.tests_covering(cov_file, changed)
    return set(plan.tests_up_to(0)) | extra


# ---------------------------------------------------------------------------
# Per-change superset assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changed_path", "expected_test"),
    [
        ("src/pkg/a.py", "test_a"),
        ("src/pkg/b.py", "test_b"),
        ("src/pkg/c.py", "test_c"),
    ],
)
def test_superset_for_each_change(
    superset_repo: tuple[Path, Path],
    changed_path: str,
    expected_test: str,
) -> None:
    """Selected set contains the expected test file for each change category.

    Args:
        changed_path: The single source file that changed.
        expected_test: Substring expected in at least one selected test path.
    """
    root, cov_file = superset_repo
    selected = _selected(root, {changed_path}, cov_file)
    assert any(expected_test in t for t in selected), (
        f"Expected {expected_test!r} in selection for change {changed_path!r}; "
        f"got: {sorted(selected)}"
    )


def test_superset_b_absent_without_follow_mock_patches(
    superset_repo: tuple[Path, Path],
) -> None:
    """Proves the mock-patch channel is necessary: test_b is absent without it.

    When ``follow_mock_patches=False``, the static import graph has no edge
    from ``test_b`` to ``pkg.b`` (there is no import statement), so a change
    to ``pkg/b.py`` does NOT select ``test_b``. This is the complement of the
    parametrized positive case for ``src/pkg/b.py``.
    """
    root, _cov_file = superset_repo
    plan = select_tests(root, {"src/pkg/b.py"}, max_depth=0, follow_mock_patches=False)
    selected = set(plan.tests_up_to(0))
    assert not any("test_b" in t for t in selected), (
        f"test_b should be absent without follow_mock_patches; got: {sorted(selected)}"
    )


def test_superset_c_absent_without_coverage_json(
    superset_repo: tuple[Path, Path],
) -> None:
    """Proves the coverage channel is necessary: test_c is absent without it.

    When no coverage union is applied (no ``cov_stage.tests_covering`` call),
    static analysis alone cannot reach ``test_c`` from a change to ``pkg/c.py``
    because ``test_c`` has no import of it. This is the complement of the
    positive ``src/pkg/c.py`` parametrized case.
    """
    root, _cov_file = superset_repo
    plan = select_tests(root, {"src/pkg/c.py"}, max_depth=0, follow_mock_patches=True)
    # Static selection only — no coverage_json union.
    selected = set(plan.tests_up_to(0))
    assert not any("test_c" in t for t in selected), (
        f"test_c should be absent without coverage_json; got: {sorted(selected)}"
    )


def test_superset_combined_change_covers_all(
    superset_repo: tuple[Path, Path],
) -> None:
    """Selected set covers all three tests when all three files change."""
    root, cov_file = superset_repo
    changed = {"src/pkg/a.py", "src/pkg/b.py", "src/pkg/c.py"}
    selected = _selected(root, changed, cov_file)

    missing = [
        name
        for name in ("test_a", "test_b", "test_c")
        if not any(name in t for t in selected)
    ]
    assert not missing, (
        f"Tests not in selection despite all three files changed: {missing}; "
        f"selected: {sorted(selected)}"
    )
