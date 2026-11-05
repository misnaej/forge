"""Tests for the ``forge-hook-dispatch`` CLI and ``forge.hook_dispatch``.

# MOCKING STRATEGY: no unittest.mock anywhere — the fakes are REAL tiny
# shell guards written into ``tmp_path`` (pass/block/block_silent/crash/
# slow/noisy/capture_stdin/capture_env .sh) plus a real roster sidecar JSON
# (the ``roster_repo`` fixture). The dispatcher runs them as subprocesses
# exactly as Vibe would. Guard-level rows assert ``dispatch()``'s
# ``(exit_code, stdout)`` verdict pair directly; preparation-level rows
# (bad stdin / roster / event / payload shape) drive the CLI ``main()``
# with monkeypatched ``sys.stdin``/``sys.argv`` and assert the same verdict
# pair on captured stdout, because fail-closed must hold at the CLI edge.
"""

from __future__ import annotations

import io
import json
import sys
from typing import TYPE_CHECKING

import pytest

from forge import hook_dispatch


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


_VIBE_BASH_MATCHER = "re:bash|process_start"


class _StdinBytes:
    """Minimal stdin stand-in exposing the ``.buffer`` the CLI reads."""

    def __init__(self, data: bytes) -> None:
        """Wrap raw payload bytes.

        Args:
            data: The exact stdin bytes the dispatcher will read.
        """
        self.buffer = io.BytesIO(data)


def _write_roster(
    root: Path,
    matchers: dict[str, list[str]],
    *,
    timeout: float = 5.0,
    project_root: str | None = None,
) -> Path:
    """Write a roster sidecar whose guard commands point at ``root/guards``.

    Args:
        root: Project root holding ``guards/`` and the sidecar.
        matchers: Claude matcher name -> guard script basenames, in order.
        timeout: Per-guard timeout recorded in the roster.
        project_root: Override for the roster's ``project_root`` field.

    Returns:
        Path to the written roster file.
    """
    roster = {
        "project_root": str(root) if project_root is None else project_root,
        "events": {
            "PreToolUse": {
                "matchers": {
                    matcher: [
                        {"command": str(root / "guards" / guard), "timeout": timeout}
                        for guard in guards
                    ]
                    for matcher, guards in matchers.items()
                }
            }
        },
    }
    path = root / "roster.json"
    path.write_text(json.dumps(roster), encoding="utf-8")
    return path


def _bash_payload(
    root: Path, command: str = "echo hi", *, with_cwd: bool = True
) -> bytes:
    """Build a Vibe ``pre_tool`` Bash payload as raw stdin bytes.

    Args:
        root: Value for the payload's ``cwd`` field.
        command: The ``tool_input.command`` string.
        with_cwd: Whether to include the ``cwd`` field at all.

    Returns:
        JSON-encoded payload bytes (``ensure_ascii`` default).
    """
    payload: dict[str, object] = {
        "tool_name": "file_system.bash",
        "tool_input": {"command": command},
    }
    if with_cwd:
        payload["cwd"] = str(root)
    return json.dumps(payload).encode("utf-8")


def _dispatch(root: Path, payload: bytes) -> tuple[int, str]:
    """Run ``dispatch`` for a vibe ``pre_tool`` Bash event against the roster.

    Args:
        root: Project root holding ``roster.json``.
        payload: Raw stdin bytes.

    Returns:
        The ``(exit_code, stdout)`` verdict pair.
    """
    return hook_dispatch.dispatch(
        "vibe", "pre_tool", payload, root / "roster.json", _VIBE_BASH_MATCHER
    )


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: Sequence[str],
    stdin_bytes: bytes,
) -> tuple[int, str]:
    """Invoke the CLI ``main()`` with fake argv/stdin and capture stdout.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        capsys: pytest capture fixture.
        argv: Full argv including the program name.
        stdin_bytes: Raw bytes to expose as ``sys.stdin``.

    Returns:
        ``(return_code, stdout)`` — the CLI verdict pair.
    """
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(sys, "stdin", _StdinBytes(stdin_bytes))
    return_code = hook_dispatch.main()
    return return_code, capsys.readouterr().out


def _main_argv(roster: Path, event: str = "pre_tool") -> list[str]:
    """Build the CLI argv for a vibe pre_tool dispatch against *roster*.

    Args:
        roster: Path to the roster sidecar.
        event: The ``--event`` value.

    Returns:
        argv list including the program name.
    """
    return [
        "forge-hook-dispatch",
        "--harness",
        "vibe",
        "--event",
        event,
        "--roster",
        str(roster),
        "--matcher",
        _VIBE_BASH_MATCHER,
    ]


@pytest.fixture
def roster_repo(tmp_path: Path) -> Path:
    """A tmp project with real guard scripts under ``guards/`` + a roster.

    The guards are real shell fakes: ``pass.sh`` exits 0; ``block.sh`` exits
    2 with a stderr reason; ``block_silent.sh`` exits 2 silently;
    ``crash.sh`` exits 3 with stderr; ``slow.sh`` sleeps 5s; ``noisy.sh``
    writes to stdout AND exits 2; ``capture_stdin.sh`` copies stdin to
    ``$CLAUDE_PROJECT_DIR/captured_stdin.bin``; ``capture_env.sh`` writes
    ``$CLAUDE_PROJECT_DIR`` to ``$CLAUDE_PROJECT_DIR/captured_env.txt``.

    Returns:
        The project root path; ``roster.json`` defaults to a pass guard.
    """
    root = tmp_path / "repo"
    guards = root / "guards"
    guards.mkdir(parents=True)
    scripts = {
        "pass.sh": "#!/bin/sh\nexit 0\n",
        "block.sh": '#!/bin/sh\necho "blocked: policy" >&2\nexit 2\n',
        "block_silent.sh": "#!/bin/sh\nexit 2\n",
        "crash.sh": '#!/bin/sh\necho "crash reason" >&2\nexit 3\n',
        "slow.sh": "#!/bin/sh\nsleep 5\n",
        "noisy.sh": (
            '#!/bin/sh\necho GUARD_STDOUT_MARKER\necho "noisy block" >&2\nexit 2\n'
        ),
        "capture_stdin.sh": (
            '#!/bin/sh\ncat > "$CLAUDE_PROJECT_DIR/captured_stdin.bin"\nexit 0\n'
        ),
        "capture_env.sh": (
            '#!/bin/sh\nprintf "%s" "$CLAUDE_PROJECT_DIR" '
            '> "$CLAUDE_PROJECT_DIR/captured_env.txt"\nexit 0\n'
        ),
    }
    for name, body in scripts.items():
        script = guards / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    _write_roster(root, {"Bash": ["pass.sh"]})
    return root


def test_pass_guard_allows_with_silent_verdict(roster_repo: Path) -> None:
    """SCENARIO: roster runs pass.sh (exit 0) for a Bash pre_tool payload.

    MOCK SETUP: real pass.sh in tmp_path via roster_repo; dispatch() called
    directly with the vibe Bash matcher.
    EXPECTED BEHAVIOR: vibe pass verdict — exit 0, empty stdout.
    """
    exit_code, stdout = _dispatch(roster_repo, _bash_payload(roster_repo))
    assert (exit_code, stdout) == (0, "")


def test_block_guard_denies_with_stderr_reason(roster_repo: Path) -> None:
    """SCENARIO: guard exits 2 with a stderr reason (Claude block contract).

    MOCK SETUP: real block.sh via roster_repo; dispatch() directly.
    EXPECTED BEHAVIOR: vibe deny verdict — exit 0, stdout JSON with
    decision=deny and the guard's stderr as the reason.
    """
    _write_roster(roster_repo, {"Bash": ["block.sh"]})
    exit_code, stdout = _dispatch(roster_repo, _bash_payload(roster_repo))
    assert exit_code == 0
    assert json.loads(stdout) == {"decision": "deny", "reason": "blocked: policy"}


def test_block_without_stderr_uses_generic_reason(roster_repo: Path) -> None:
    """SCENARIO: guard exits 2 with EMPTY stderr (fail-closed row 9).

    MOCK SETUP: real block_silent.sh via roster_repo; dispatch() directly.
    EXPECTED BEHAVIOR: still a deny, with a generic reason naming the guard
    — never a pass.
    """
    _write_roster(roster_repo, {"Bash": ["block_silent.sh"]})
    exit_code, stdout = _dispatch(roster_repo, _bash_payload(roster_repo))
    assert exit_code == 0
    verdict = json.loads(stdout)
    assert verdict["decision"] == "deny"
    assert verdict["reason"].startswith("Guard blocked without reason")


def test_crash_exit_denies_stricter_than_claude(roster_repo: Path) -> None:
    """SCENARIO: guard exits 3 (not 2) with stderr — e.g. ``jq`` missing.

    MOCK SETUP: real crash.sh (exit 3) via roster_repo; dispatch() directly.
    EXPECTED BEHAVIOR: deny (fail-closed row 6 — stricter than Claude, which
    treats non-2 non-zero as fail-open); reason carries the guard's stderr.
    """
    _write_roster(roster_repo, {"Bash": ["crash.sh"]})
    exit_code, stdout = _dispatch(roster_repo, _bash_payload(roster_repo))
    assert exit_code == 0
    assert json.loads(stdout) == {"decision": "deny", "reason": "crash reason"}


def test_guard_timeout_denies(roster_repo: Path) -> None:
    """SCENARIO: guard exceeds its roster timeout (fail-closed row 5).

    MOCK SETUP: real slow.sh (sleeps 5s) with roster timeout 0.2s;
    dispatch() directly.
    EXPECTED BEHAVIOR: deny with a timeout reason — never an allow while the
    guard is still running.
    """
    _write_roster(roster_repo, {"Bash": ["slow.sh"]}, timeout=0.2)
    exit_code, stdout = _dispatch(roster_repo, _bash_payload(roster_repo))
    assert exit_code == 0
    verdict = json.loads(stdout)
    assert verdict["decision"] == "deny"
    assert "Guard timeout after 0.2s" in verdict["reason"]


def test_missing_guard_script_denies(roster_repo: Path) -> None:
    """SCENARIO: roster names a guard script that does not exist (row 4).

    MOCK SETUP: roster points at guards/missing.sh; dispatch() directly.
    EXPECTED BEHAVIOR: deny naming the missing script — never skip-with-
    warning.
    """
    _write_roster(roster_repo, {"Bash": ["missing.sh"]})
    exit_code, stdout = _dispatch(roster_repo, _bash_payload(roster_repo))
    assert exit_code == 0
    verdict = json.loads(stdout)
    assert verdict["decision"] == "deny"
    assert "Guard script missing" in verdict["reason"]


def test_non_executable_guard_denies(roster_repo: Path) -> None:
    """SCENARIO: guard script exists but is not executable (row 4).

    MOCK SETUP: noexec.sh written WITHOUT the executable bit; roster points
    at it; dispatch() directly.
    EXPECTED BEHAVIOR: deny "not executable" — never an OSError crash that
    could read as allow.
    """
    noexec = roster_repo / "guards" / "noexec.sh"
    noexec.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _write_roster(roster_repo, {"Bash": ["noexec.sh"]})
    exit_code, stdout = _dispatch(roster_repo, _bash_payload(roster_repo))
    assert exit_code == 0
    verdict = json.loads(stdout)
    assert verdict["decision"] == "deny"
    assert "not executable" in verdict["reason"]


def test_guard_stdout_is_never_forwarded(roster_repo: Path) -> None:
    """SCENARIO: a guard writes to stdout before blocking (fail-closed row 8).

    MOCK SETUP: real noisy.sh prints GUARD_STDOUT_MARKER on stdout, then
    exits 2; dispatch() directly.
    EXPECTED BEHAVIOR: dispatcher stdout is exactly ONE JSON object — the
    guard's stdout must never leak into it (Vibe parses dispatcher stdout
    as the verdict).
    """
    _write_roster(roster_repo, {"Bash": ["noisy.sh"]})
    exit_code, stdout = _dispatch(roster_repo, _bash_payload(roster_repo))
    assert exit_code == 0
    assert "GUARD_STDOUT_MARKER" not in stdout
    assert len(stdout.strip().splitlines()) == 1
    verdict = json.loads(stdout)
    assert verdict == {"decision": "deny", "reason": "noisy block"}


def test_original_stdin_bytes_refed_byte_exact_with_unicode(
    roster_repo: Path,
) -> None:
    """SCENARIO: payload carries unicode; guards must get the ORIGINAL bytes.

    MOCK SETUP: real capture_stdin.sh copies its stdin to a file; payload is
    JSON with ``ensure_ascii=False`` and an emoji command (row 14 — a
    re-serialized parse could shift the command text the scanner reads).
    EXPECTED BEHAVIOR: captured file bytes == original stdin bytes exactly;
    verdict is a pass.
    """
    _write_roster(roster_repo, {"Bash": ["capture_stdin.sh"]})
    payload = json.dumps(
        {
            "tool_name": "file_system.bash",
            "cwd": str(roster_repo),
            "tool_input": {"command": "echo 'héllo 🌍 — ünïcode'"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    exit_code, stdout = _dispatch(roster_repo, payload)
    assert (exit_code, stdout) == (0, "")
    captured = (roster_repo / "captured_stdin.bin").read_bytes()
    assert captured == payload


def test_claude_project_dir_exported_from_payload_cwd(roster_repo: Path) -> None:
    """SCENARIO: payload carries ``cwd``; Vibe does not set CLAUDE_PROJECT_DIR.

    MOCK SETUP: real capture_env.sh records ``$CLAUDE_PROJECT_DIR``; payload
    cwd is a separate workdir; dispatch() directly (row 10).
    EXPECTED BEHAVIOR: the guard sees CLAUDE_PROJECT_DIR == payload cwd.
    """
    _write_roster(roster_repo, {"Bash": ["capture_env.sh"]})
    workdir = roster_repo / "workdir"
    workdir.mkdir()
    payload = json.dumps(
        {
            "tool_name": "file_system.bash",
            "cwd": str(workdir),
            "tool_input": {"command": "echo hi"},
        }
    ).encode("utf-8")
    exit_code, _stdout = _dispatch(roster_repo, payload)
    assert exit_code == 0
    assert (workdir / "captured_env.txt").read_text(encoding="utf-8") == str(workdir)


def test_claude_project_dir_falls_back_to_roster_project_root(
    roster_repo: Path,
) -> None:
    """SCENARIO: payload has no ``cwd``; roster ``project_root`` is the fallback.

    MOCK SETUP: real capture_env.sh; payload built with ``with_cwd=False``;
    dispatch() directly (row 10 precedence 2).
    EXPECTED BEHAVIOR: guard sees CLAUDE_PROJECT_DIR == roster project_root.
    """
    _write_roster(roster_repo, {"Bash": ["capture_env.sh"]})
    exit_code, _stdout = _dispatch(
        roster_repo, _bash_payload(roster_repo, with_cwd=False)
    )
    assert exit_code == 0
    captured = (roster_repo / "captured_env.txt").read_text(encoding="utf-8")
    assert captured == str(roster_repo)


def test_first_block_short_circuits_later_guards(roster_repo: Path) -> None:
    """SCENARIO: ordered guards [block.sh, capture_stdin.sh] (sequential run).

    MOCK SETUP: real scripts via roster_repo; roster lists both in order;
    dispatch() directly.
    EXPECTED BEHAVIOR: deny from block.sh; the capture guard NEVER ran (its
    output file is absent) — no wasted work past the first block.
    """
    _write_roster(roster_repo, {"Bash": ["block.sh", "capture_stdin.sh"]})
    exit_code, stdout = _dispatch(roster_repo, _bash_payload(roster_repo))
    assert exit_code == 0
    assert json.loads(stdout)["decision"] == "deny"
    assert not (roster_repo / "captured_stdin.bin").exists()


def test_empty_stdin_denies(
    roster_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: zero bytes on stdin (fail-closed row 7).

    MOCK SETUP: CLI main() with empty stdin, valid roster via --roster.
    EXPECTED BEHAVIOR: deny verdict pair — exit 0 + deny JSON on stdout.
    """
    rc, out = _run_main(
        monkeypatch, capsys, _main_argv(roster_repo / "roster.json"), b""
    )
    assert rc == 0
    assert json.loads(out)["decision"] == "deny"


def test_malformed_stdin_json_denies(
    roster_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: stdin is not parseable JSON (row 7 — today this allows).

    MOCK SETUP: CLI main() with ``b"{not json"`` on stdin, valid roster.
    EXPECTED BEHAVIOR: deny verdict pair — exit 0 + deny JSON.
    """
    rc, out = _run_main(
        monkeypatch, capsys, _main_argv(roster_repo / "roster.json"), b"{not json"
    )
    assert rc == 0
    assert json.loads(out)["decision"] == "deny"


def test_missing_roster_file_denies(
    roster_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: roster sidecar does not exist (row 3).

    MOCK SETUP: CLI main() with --roster pointing at a nonexistent path.
    EXPECTED BEHAVIOR: deny verdict pair — exit 0 + deny JSON; never
    "no roster, no guards, allow".
    """
    rc, out = _run_main(
        monkeypatch,
        capsys,
        _main_argv(roster_repo / "no-such-roster.json"),
        _bash_payload(roster_repo),
    )
    assert rc == 0
    assert json.loads(out)["decision"] == "deny"


def test_malformed_roster_denies(
    roster_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: roster sidecar is unparseable JSON (row 3).

    MOCK SETUP: roster.json overwritten with garbage; CLI main() run.
    EXPECTED BEHAVIOR: deny verdict pair — exit 0 + deny JSON.
    """
    (roster_repo / "roster.json").write_text("{garbage", encoding="utf-8")
    rc, out = _run_main(
        monkeypatch,
        capsys,
        _main_argv(roster_repo / "roster.json"),
        _bash_payload(roster_repo),
    )
    assert rc == 0
    assert json.loads(out)["decision"] == "deny"


def test_roster_without_project_root_denies(
    roster_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: roster parses but has no ``project_root`` trust anchor.

    MOCK SETUP: roster.json rewritten without project_root; CLI main() run.
    EXPECTED BEHAVIOR: deny verdict pair — a rootless roster cannot prove
    its guard commands are in-tree.
    """
    roster = json.loads((roster_repo / "roster.json").read_text(encoding="utf-8"))
    del roster["project_root"]
    (roster_repo / "roster.json").write_text(json.dumps(roster), encoding="utf-8")
    rc, out = _run_main(
        monkeypatch,
        capsys,
        _main_argv(roster_repo / "roster.json"),
        _bash_payload(roster_repo),
    )
    assert rc == 0
    assert json.loads(out)["decision"] == "deny"


def test_guard_command_escaping_project_root_denies(
    roster_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: a tampered roster points a guard outside the project root.

    MOCK SETUP: evil.sh created in tmp_path (outside roster_repo);
    roster.json lists it as the guard command; CLI main() run.
    EXPECTED BEHAVIOR: deny verdict pair — the sidecar is a trust anchor,
    out-of-tree commands are refused, never executed.
    """
    outside = tmp_path / "evil.sh"
    outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside.chmod(0o755)
    roster = {
        "project_root": str(roster_repo),
        "events": {
            "PreToolUse": {
                "matchers": {"Bash": [{"command": str(outside), "timeout": 5.0}]}
            }
        },
    }
    (roster_repo / "roster.json").write_text(json.dumps(roster), encoding="utf-8")
    rc, out = _run_main(
        monkeypatch,
        capsys,
        _main_argv(roster_repo / "roster.json"),
        _bash_payload(roster_repo),
    )
    assert rc == 0
    assert json.loads(out)["decision"] == "deny"


def test_unknown_event_denies(
    roster_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: --event names an event with no mapping for the harness (row 13).

    MOCK SETUP: CLI main() with ``--event session_start`` (Vibe has no such
    blocking event mapping), valid roster.
    EXPECTED BEHAVIOR: deny verdict pair — exit 0 + deny JSON.
    """
    rc, out = _run_main(
        monkeypatch,
        capsys,
        _main_argv(roster_repo / "roster.json", event="session_start"),
        _bash_payload(roster_repo),
    )
    assert rc == 0
    assert json.loads(out)["decision"] == "deny"


def test_empty_guard_list_and_bad_payload_shape_deny(
    roster_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: row 3b + row 7b — no guards for the matcher; payload shape wrong.

    MOCK SETUP: roster has only an "Edit" matcher, so a Bash matcher resolves
    to zero guards; second run feeds a payload with no ``tool_input`` object.
    Both via CLI main().
    EXPECTED BEHAVIOR: both deny — an empty guard list for a blocking event
    is an error, and a shapeless payload must not pass through to guards
    that fail open on empty input.
    """
    _write_roster(roster_repo, {"Edit": ["pass.sh"]})
    rc, out = _run_main(
        monkeypatch,
        capsys,
        _main_argv(roster_repo / "roster.json"),
        _bash_payload(roster_repo),
    )
    assert rc == 0
    assert json.loads(out)["decision"] == "deny"
    capsys.readouterr()
    shapeless = json.dumps({"tool_name": "file_system.bash", "cwd": str(roster_repo)})
    rc, out = _run_main(
        monkeypatch,
        capsys,
        _main_argv(roster_repo / "roster.json"),
        shapeless.encode("utf-8"),
    )
    assert rc == 0
    assert json.loads(out)["decision"] == "deny"


def test_main_cli_pass_writes_silent_stdout_exit_0(
    roster_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: full happy path through the CLI — pass.sh guard, valid payload.

    MOCK SETUP: roster_repo default roster (pass.sh); main() with --roster.
    EXPECTED BEHAVIOR: return code 0 and EMPTY stdout (vibe pass = silent).
    """
    rc, out = _run_main(
        monkeypatch,
        capsys,
        _main_argv(roster_repo / "roster.json"),
        _bash_payload(roster_repo),
    )
    assert (rc, out) == (0, "")


def test_main_cli_catch_all_emits_deny_json_on_internal_error(
    roster_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SCENARIO: an unexpected internal error escapes dispatch (fail-closed row 2).

    MOCK SETUP: --roster points at a DIRECTORY, so roster loading raises
    IsADirectoryError (not a handled deny) and main()'s catch-all must fire.
    EXPECTED BEHAVIOR: exit 0 + deny JSON with an "Internal error" reason on
    stdout — the catch-all keeps every internal failure fail-closed.
    """
    rc, out = _run_main(
        monkeypatch,
        capsys,
        _main_argv(roster_repo),  # the repo dir itself: not a readable file
        _bash_payload(roster_repo),
    )
    assert rc == 0
    verdict = json.loads(out)
    assert verdict["decision"] == "deny"
    assert verdict["reason"].startswith("Internal error")
