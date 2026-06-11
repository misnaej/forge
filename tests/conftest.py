"""Shared test fixtures for forge.

Lives at ``tests/conftest.py`` so pytest auto-discovers it. Exposes a
canonical ``FakeProc`` stand-in and a ``fake_subprocess_run`` factory
that captures call argvs — used by tests that monkeypatch
``subprocess.run`` in any of the forge CLIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class FakeProc:
    """Minimal ``subprocess.CompletedProcess`` stand-in.

    Attributes:
        returncode: Mock exit code.
        stdout: Mock standard output.
        stderr: Mock standard error.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class CapturedCalls:
    """Holder for ``subprocess.run`` argvs intercepted by a fake.

    Attributes:
        calls: List of argv lists captured in invocation order.
    """

    calls: list[list[str]] = field(default_factory=list)


def make_fake_run(
    *,
    stdout: str = "",
    returncode: int = 0,
    captured: CapturedCalls | None = None,
) -> Callable[..., FakeProc]:
    """Return a ``subprocess.run`` replacement that records argvs.

    Args:
        stdout: ``stdout`` to return on every invocation.
        returncode: ``returncode`` to return on every invocation.
        captured: Optional ``CapturedCalls`` to push the argv into.
            If ``None``, calls are not retained.

    Returns:
        A callable compatible with ``subprocess.run`` signatures used by
        forge code. Ignores ``**kwargs`` (cwd, capture_output, etc.).
    """

    def _fake_run(cmd: list[str], **kwargs: object) -> FakeProc:
        del kwargs
        if captured is not None:
            captured.calls.append(cmd)
        return FakeProc(returncode=returncode, stdout=stdout)

    return _fake_run
