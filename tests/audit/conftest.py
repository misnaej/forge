"""Shared fixtures/helpers for the audit test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    import pytest


def make_fake_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *modules: ModuleType,
) -> Path:
    """Create a bare repo tree and point each module's ``repo_root`` at it.

    Audit modules bind ``repo_root`` into their own namespace
    (``from forge.git_utils import repo_root``), and ``audit.common`` holds
    a separately-bound copy used by ``write_log`` / ``relpath`` /
    ``iter_files`` — every module whose seam a test crosses must be
    patched, so callers list them explicitly.

    Args:
        tmp_path: Base temp directory (becomes the repo root).
        monkeypatch: Pytest monkeypatch fixture.
        *modules: Every module whose ``repo_root`` binding to redirect.

    Returns:
        The repo root path (with an empty ``src/`` created).
    """
    (tmp_path / "src").mkdir()
    for module in modules:
        monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    return tmp_path


def write_pyproject(root: Path, body: str) -> None:
    """Write ``body`` verbatim as ``pyproject.toml`` under ``root``.

    Args:
        root: Fake repo root.
        body: Full TOML content.
    """
    (root / "pyproject.toml").write_text(body, encoding="utf-8")
