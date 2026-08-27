"""Tests for ``forge.telemetry`` — the resource-profiling wrapper."""

# MOCKING STRATEGY: two seams, never mixed within one test.
#   (1) ``forge.telemetry.psutil`` — monkeypatched to the Fake* objects below
#       (FakeNoSuchProcess / FakeProcess / FakePsutil) for every
#       ``_tree_rss_bytes`` / ``_sample`` / ``telemetry_available`` unit test,
#       so no test ever touches a real process tree.
#   (2) ``forge.telemetry.run_command`` — monkeypatched in the ``main()`` CLI
#       tests to capture the argv it would have spawned, without running a
#       real child. The exceptions are the ``test_run_command_integration_*``
#       and ``test_run_command_sample_first_wait_*`` tests, which exercise the
#       real psutil + real subprocess path end-to-end against a ``tmp_path``
#       repo root (``pytest.importorskip("psutil")`` guards each; ``plot =
#       false`` keeps them independent of matplotlib).
# ``_render_plot``'s missing-matplotlib path uses
# ``monkeypatch.setitem(sys.modules, "matplotlib", None)`` — the standard
# trick that makes ``import matplotlib`` raise ``ImportError`` without
# uninstalling the real package.

from __future__ import annotations

import logging
import re
import sys
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from forge import telemetry


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fake psutil seam
# ---------------------------------------------------------------------------


class FakeNoSuchProcessError(Exception):
    """Stand-in for ``psutil.NoSuchProcess`` — a process that has exited."""


class FakeAccessDeniedError(Exception):
    """Stand-in for ``psutil.AccessDenied`` — a process the sampler can't read."""


class FakeProcess:
    """Stand-in for a ``psutil.Process`` handle.

    Attributes:
        rss: Bytes reported by ``memory_info().rss``.
        children_procs: Flattened descendant list returned by ``children()``.
        gone: When True, both ``children()`` and ``memory_info()`` raise
            ``FakeNoSuchProcess`` — a process that exited mid-sample.
        denied: When True, ``memory_info()`` raises ``FakeAccessDenied`` — a
            process the sampler lacks permission to read (e.g. sandboxed CI).
    """

    def __init__(
        self,
        rss: int,
        children: list[FakeProcess] | None = None,
        *,
        gone: bool = False,
        denied: bool = False,
    ) -> None:
        """Store the configured RSS, children, gone-ness, and denied-ness for this fake.

        Args:
            rss: Bytes reported by memory_info().rss.
            children: Flattened descendant list returned by children().
            gone: When True, both children() and memory_info() raise FakeNoSuchProcess.
            denied: When True, memory_info() raises FakeAccessDenied.
        """
        self.rss = rss
        self.children_procs = children or []
        self.gone = gone
        self.denied = denied

    def children(self, *, recursive: bool = True) -> list[FakeProcess]:
        """Return the flattened descendant list, or raise if this process exited.

        Args:
            recursive: All descendants if True; immediate children if False (not used).

        Returns:
            Flattened list of descendant FakeProcess objects.
        """
        del recursive
        if self.gone:
            msg = "process exited"
            raise FakeNoSuchProcessError(msg)
        return self.children_procs

    def memory_info(self) -> SimpleNamespace:
        """Return ``rss``-bearing namespace or raise if exited or denied."""
        if self.gone:
            msg = "process exited"
            raise FakeNoSuchProcessError(msg)
        if self.denied:
            msg = "access denied"
            raise FakeAccessDeniedError(msg)
        return SimpleNamespace(rss=self.rss)


class FakePsutil:
    """Stand-in for the ``psutil`` module surface ``telemetry.py`` depends on."""

    def __init__(
        self,
        *,
        process: FakeProcess | None = None,
        cpu_percent_value: float = 0.0,
    ) -> None:
        """Store the configured process handle and host CPU percentage.

        Args:
            process: Preconfigured FakeProcess handle, or None for no process.
            cpu_percent_value: Host CPU percentage to return from cpu_percent().
        """
        self.NoSuchProcess = FakeNoSuchProcessError
        self.AccessDenied = FakeAccessDeniedError
        self._process = process
        self._cpu_percent_value = cpu_percent_value

    def cpu_percent(self, interval: float | None = None) -> float:
        """Return the configured host CPU percentage.

        Args:
            interval: Sampling interval in seconds (unused in fake).

        Returns:
            Configured host CPU percentage.
        """
        del interval
        return self._cpu_percent_value

    def Process(self, pid: int) -> FakeProcess | None:  # noqa: N802 — mirrors psutil's API
        """Return the preconfigured fake process handle for any pid.

        Args:
            pid: Process ID (unused in fake; always returns the same handle).

        Returns:
            Preconfigured FakeProcess handle, or None if not configured.
        """
        del pid
        return self._process


def _write_pyproject(tmp_path: Path, body: str) -> None:
    """Write a ``pyproject.toml`` with *body* under *tmp_path*.

    Args:
        tmp_path: Directory to write pyproject.toml into.
        body: TOML content to write to pyproject.toml.
    """
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# validated_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["", "depth1", "a.b-c_2"])
def test_validated_label_accepts_empty_and_safe_values(label: str) -> None:
    """Empty and safe filename-fragment labels pass through unchanged.

    Args:
        label: A valid label string to validate.
    """
    assert telemetry.validated_label(label) == label


@pytest.mark.parametrize("label", ["../etc", "a b", "-x", "!bad"])
def test_validated_label_rejects_unsafe_values(label: str) -> None:
    """A non-empty, non-safe label raises loudly instead of degrading (#376).

    Args:
        label: An unsafe label string that should raise ValueError.
    """
    with pytest.raises(ValueError, match="not a safe artifact suffix"):
        telemetry.validated_label(label)


# ---------------------------------------------------------------------------
# _telemetry_config
# ---------------------------------------------------------------------------


def test_telemetry_config_defaults_when_absent(tmp_path: Path) -> None:
    """No ``[tool.forge.telemetry]`` table yields the ``(1.0, True)`` defaults."""
    _write_pyproject(tmp_path, "[tool.forge]\n")
    assert telemetry._telemetry_config(tmp_path) == (1.0, True)


def test_telemetry_config_custom_interval_and_plot_false(tmp_path: Path) -> None:
    """A custom ``sample_interval`` and ``plot = false`` are both honored."""
    _write_pyproject(
        tmp_path,
        "[tool.forge.telemetry]\nsample_interval = 2.5\nplot = false\n",
    )
    assert telemetry._telemetry_config(tmp_path) == (2.5, False)


def test_telemetry_config_interval_floored_at_0_1(tmp_path: Path) -> None:
    """A sub-floor interval (0.001) is raised to the 0.1s floor."""
    _write_pyproject(tmp_path, "[tool.forge.telemetry]\nsample_interval = 0.001\n")
    interval, plot = telemetry._telemetry_config(tmp_path)
    assert interval == 0.1
    assert plot is True


def test_telemetry_config_misshaped_interval_degrades_to_default(
    tmp_path: Path,
) -> None:
    """A non-numeric ``sample_interval`` degrades to the 1.0s default."""
    _write_pyproject(tmp_path, '[tool.forge.telemetry]\nsample_interval = "fast"\n')
    interval, _ = telemetry._telemetry_config(tmp_path)
    assert interval == 1.0


@pytest.mark.parametrize("bad_interval", [0, -1])
def test_telemetry_config_non_positive_interval_degrades_to_default(
    tmp_path: Path, bad_interval: int
) -> None:
    """A zero or negative ``sample_interval`` degrades to the 1.0s default.

    Args:
        bad_interval: Zero or negative interval value to test.
    """
    _write_pyproject(
        tmp_path, f"[tool.forge.telemetry]\nsample_interval = {bad_interval}\n"
    )
    interval, _ = telemetry._telemetry_config(tmp_path)
    assert interval == 1.0


def test_telemetry_config_plot_zero_coerces_to_false(tmp_path: Path) -> None:
    """A truthy-looking int ``plot = 0`` coerces to boolean ``False``."""
    _write_pyproject(tmp_path, "[tool.forge.telemetry]\nplot = 0\n")
    _, plot = telemetry._telemetry_config(tmp_path)
    assert plot is False


# ---------------------------------------------------------------------------
# _format_log
# ---------------------------------------------------------------------------


def test_format_log_header_includes_duration_exit_and_sample_count() -> None:
    """The header line reports duration, exit code, and sample count."""
    body = telemetry._format_log(["pytest", "-q"], [], exit_code=0, elapsed=12.345)
    assert "command: pytest -q" in body
    assert "duration: 12.3s" in body
    assert "exit code: 0" in body
    assert "samples: 0" in body


def test_format_log_empty_samples_omits_peak_and_mean() -> None:
    """No samples means no peak-rss/mean-cpu summary line at all."""
    body = telemetry._format_log(["true"], [], exit_code=0, elapsed=1.0)
    assert "peak rss" not in body
    assert "mean cpu" not in body


def test_format_log_peak_and_mean_exact_over_three_samples() -> None:
    """Peak RSS and mean CPU are computed exactly over the sample set."""
    samples = [
        telemetry.Sample(elapsed=0.0, rss_mb=10.0, cpu_percent=20.0),
        telemetry.Sample(elapsed=1.0, rss_mb=30.0, cpu_percent=40.0),
        telemetry.Sample(elapsed=2.0, rss_mb=20.0, cpu_percent=60.0),
    ]
    body = telemetry._format_log(["true"], samples, exit_code=0, elapsed=2.0)
    assert "peak rss: 30.0MB" in body
    assert f"mean cpu: {(20.0 + 40.0 + 60.0) / 3:.1f}%" in body


# ---------------------------------------------------------------------------
# _summarize
# ---------------------------------------------------------------------------


def test_summarize_empty_samples_returns_none() -> None:
    """No samples means no aggregate — ``None``, not a zeroed summary."""
    assert telemetry._summarize([]) is None


def test_summarize_computes_peak_rss_and_mean_cpu() -> None:
    """Peak RSS and mean CPU are computed exactly over the sample set."""
    samples = [
        telemetry.Sample(elapsed=0.0, rss_mb=10.0, cpu_percent=20.0),
        telemetry.Sample(elapsed=1.0, rss_mb=30.0, cpu_percent=40.0),
        telemetry.Sample(elapsed=2.0, rss_mb=20.0, cpu_percent=60.0),
    ]
    summary = telemetry._summarize(samples)
    assert summary is not None
    assert summary.peak_rss_mb == pytest.approx(30.0)
    assert summary.mean_cpu == pytest.approx((20.0 + 40.0 + 60.0) / 3)


# ---------------------------------------------------------------------------
# _append_history
# ---------------------------------------------------------------------------


def test_append_history_empty_summary_writes_na_peak_rss(tmp_path: Path) -> None:
    """A ``None`` summary (no samples) writes ``peak_rss=n/a``, not a crash."""
    history = telemetry._RunHistory(
        cmd=["true"], summary=None, exit_code=0, elapsed=1.5
    )
    telemetry._append_history(tmp_path, history, label="")
    history_log = tmp_path / "code_health" / "telemetry_history.log"
    line = history_log.read_text(encoding="utf-8")
    assert "peak_rss=n/a" in line
    assert "label=-" in line
    assert "exit=0" in line
    assert "wall=1.5s" in line
    assert "cmd=true" in line
    assert "ts=" in line


def test_append_history_second_call_appends_without_disturbing_first(
    tmp_path: Path,
) -> None:
    """A second run's line is appended; the first run's line is unchanged."""
    first_history = telemetry._RunHistory(
        cmd=["true"], summary=None, exit_code=0, elapsed=1.0
    )
    telemetry._append_history(tmp_path, first_history, label="r1")
    first_line = (
        (tmp_path / "code_health" / "telemetry_history.log")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )

    summary = telemetry._Summary(peak_rss_mb=5.0, mean_cpu=10.0)
    second_history = telemetry._RunHistory(
        cmd=["true"], summary=summary, exit_code=1, elapsed=2.0
    )
    telemetry._append_history(tmp_path, second_history, label="r2")

    lines = (
        (tmp_path / "code_health" / "telemetry_history.log")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == 2
    assert lines[0] == first_line
    assert "label=r2" in lines[1]
    assert "exit=1" in lines[1]
    assert "peak_rss=5.0MB" in lines[1]


# ---------------------------------------------------------------------------
# _tree_rss_bytes
# ---------------------------------------------------------------------------


def test_tree_rss_bytes_sums_root_and_two_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSS sums across the root process and all live descendants."""
    monkeypatch.setattr(telemetry, "psutil", FakePsutil())
    root = FakeProcess(rss=100, children=[FakeProcess(rss=50), FakeProcess(rss=30)])
    assert telemetry._tree_rss_bytes(root) == 180


def test_tree_rss_bytes_tolerates_one_gone_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that exited mid-sample is skipped; the rest are still summed."""
    monkeypatch.setattr(telemetry, "psutil", FakePsutil())
    root = FakeProcess(
        rss=100,
        children=[FakeProcess(rss=50), FakeProcess(rss=30, gone=True)],
    )
    assert telemetry._tree_rss_bytes(root) == 150


def test_tree_rss_bytes_root_gone_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root process that already exited yields 0, not an error."""
    monkeypatch.setattr(telemetry, "psutil", FakePsutil())
    root = FakeProcess(rss=100, gone=True)
    assert telemetry._tree_rss_bytes(root) == 0


def test_tree_rss_bytes_tolerates_access_denied_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child the sampler lacks permission to read is skipped, not fatal.

    SECURITY REGRESSION: a sandboxed-CI permission error on one process in
    the tree must not kill the telemetry wrapper — the child's exit code
    would otherwise be lost.
    """
    monkeypatch.setattr(telemetry, "psutil", FakePsutil())
    root = FakeProcess(
        rss=100,
        children=[FakeProcess(rss=50), FakeProcess(rss=30, denied=True)],
    )
    assert telemetry._tree_rss_bytes(root) == 150


# ---------------------------------------------------------------------------
# _sample
# ---------------------------------------------------------------------------


def test_sample_computes_elapsed_rss_and_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_sample`` reads elapsed time, tree RSS in MB, and host CPU percent."""
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: 100.5)
    monkeypatch.setattr(telemetry, "psutil", FakePsutil(cpu_percent_value=12.5))
    proc = FakeProcess(rss=10 * telemetry._BYTES_PER_MB)

    sample = telemetry._sample(proc, started=100.0)

    assert sample.elapsed == pytest.approx(0.5)
    assert sample.rss_mb == pytest.approx(10.0)
    assert sample.cpu_percent == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# telemetry_available
# ---------------------------------------------------------------------------


def test_telemetry_available_false_when_psutil_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``telemetry_available()`` is False when psutil failed to import."""
    monkeypatch.setattr(telemetry, "psutil", None)
    assert telemetry.telemetry_available() is False


def test_telemetry_available_true_when_psutil_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``telemetry_available()`` is True for any non-``None`` psutil sentinel."""
    monkeypatch.setattr(telemetry, "psutil", object())
    assert telemetry.telemetry_available() is True


def test_main_dashdash_alone_is_empty_command_returns_2() -> None:
    """``["--"]`` splits to an empty child command — exit code 2."""
    assert telemetry.main(["--"]) == 2


def test_main_no_args_at_all_returns_2() -> None:
    """No args at all also yields an empty child command — exit code 2."""
    assert telemetry.main([]) == 2


def test_main_missing_psutil_logs_hint_and_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A command is given but psutil is unavailable — logs the hint, exit 1."""
    monkeypatch.setattr(telemetry, "psutil", None)
    with caplog.at_level(logging.ERROR, logger="forge.telemetry"):
        code = telemetry.main(["--", "echo", "hi"])
    assert code == 1
    assert "psutil" in caplog.text
    assert "telemetry" in caplog.text


def test_main_help_flag_exits_0() -> None:
    """``-h`` before any ``--`` is argparse's own help — clean exit code 0."""
    with pytest.raises(SystemExit) as exc_info:
        telemetry.main(["-h"])
    assert exc_info.value.code == 0


def test_main_unknown_flag_before_dashdash_raises_systemexit() -> None:
    """An unrecognized flag before ``--`` is argparse's to reject, not ours."""
    with pytest.raises(SystemExit):
        telemetry.main(["--unknown-flag", "--", "true"])


def test_main_no_dashdash_at_all_raises_systemexit() -> None:
    """Without ``--``, every token is parsed as a flag — documents the contract."""
    with pytest.raises(SystemExit):
        telemetry.main(["pytest"])


def test_main_delegates_verbatim_cmd_to_run_command_and_returns_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child argv after ``--`` reaches ``run_command`` untouched; its exit code wins."""
    captured: dict[str, object] = {}

    def _fake_run_command(
        cmd: list[str], root: object, *, label: str = ""
    ) -> tuple[int, str]:
        captured["cmd"] = cmd
        captured["root"] = root
        captured["label"] = label
        return 7, ""

    monkeypatch.setattr(telemetry, "run_command", _fake_run_command)

    code = telemetry.main(["--", "pytest", "--depth", "0"])

    assert code == 7
    assert captured["cmd"] == ["pytest", "--depth", "0"]


def test_main_argv_none_uses_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``argv=None`` (the default) falls back to ``sys.argv[1:]``."""
    monkeypatch.setattr(sys, "argv", ["forge-telemetry", "--", "true"])
    captured: dict[str, object] = {}

    def _fake_run_command(
        cmd: list[str], root: object, *, label: str = ""
    ) -> tuple[int, str]:
        captured["cmd"] = cmd
        del root, label
        return 0, ""

    monkeypatch.setattr(telemetry, "run_command", _fake_run_command)

    assert telemetry.main() == 0
    assert captured["cmd"] == ["true"]


def test_main_label_flag_passed_through_to_run_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--label`` reaches ``run_command`` as its ``label`` keyword, unchanged."""
    captured: dict[str, object] = {}

    def _fake_run_command(
        cmd: list[str], root: object, *, label: str = ""
    ) -> tuple[int, str]:
        captured["cmd"] = cmd
        captured["label"] = label
        del root
        return 0, ""

    monkeypatch.setattr(telemetry, "run_command", _fake_run_command)

    code = telemetry.main(["--label", "r1", "--", "true"])

    assert code == 0
    assert captured["label"] == "r1"


def test_main_env_label_used_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FORGE_TELEMETRY_LABEL`` supplies the label when ``--label`` is not given."""
    monkeypatch.setenv("FORGE_TELEMETRY_LABEL", "env-label")
    captured: dict[str, object] = {}

    def _fake_run_command(
        cmd: list[str], root: object, *, label: str = ""
    ) -> tuple[int, str]:
        captured["label"] = label
        del cmd, root
        return 0, ""

    monkeypatch.setattr(telemetry, "run_command", _fake_run_command)

    code = telemetry.main(["--", "true"])

    assert code == 0
    assert captured["label"] == "env-label"


def test_main_label_flag_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--label`` overrides ``FORGE_TELEMETRY_LABEL`` when both are set."""
    monkeypatch.setenv("FORGE_TELEMETRY_LABEL", "env-label")
    captured: dict[str, object] = {}

    def _fake_run_command(
        cmd: list[str], root: object, *, label: str = ""
    ) -> tuple[int, str]:
        captured["label"] = label
        del cmd, root
        return 0, ""

    monkeypatch.setattr(telemetry, "run_command", _fake_run_command)

    code = telemetry.main(["--label", "flag-label", "--", "true"])

    assert code == 0
    assert captured["label"] == "flag-label"


def test_main_bad_label_logs_error_and_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bad label's ``ValueError`` from ``run_command`` logs and exits 2.

    SCENARIO: ``--label "a b"`` — an unsafe artifact suffix.
    MOCK SETUP: ``run_command`` stubbed to raise, standing in for the real
        ``validated_label`` rejection (already unit-tested above) so this
        test verifies ``main``'s own handling of that failure, not the
        validator itself.
    EXPECTED BEHAVIOR: the error is logged (no traceback surfaces to the
        caller) and ``main`` returns 2.
    """

    def _raise_run_command(*_a: object, **_kw: object) -> tuple[int, str]:
        msg = "telemetry label 'a b' is not a safe artifact suffix"
        raise ValueError(msg)

    monkeypatch.setattr(telemetry, "run_command", _raise_run_command)

    with caplog.at_level(logging.ERROR, logger="forge.telemetry"):
        code = telemetry.main(["--label", "a b", "--", "true"])

    assert code == 2
    assert "not a safe artifact suffix" in caplog.text


# ---------------------------------------------------------------------------
# run_command — real psutil + real subprocess integration
# ---------------------------------------------------------------------------


def test_run_command_integration_captures_child_output_and_writes_log(
    tmp_path: Path,
) -> None:
    """``run_command`` spawns a real child, captures output, and writes the log.

    SCENARIO: a short-lived child that sleeps briefly, prints, and exits
        non-zero.
    MOCK SETUP: none — exercises real psutil sampling against a ``tmp_path``
        repo root configured with a fast ``sample_interval`` and
        ``plot = false`` (keeps the test independent of matplotlib).
    EXPECTED BEHAVIOR: the child's exit code and combined output pass
        through unchanged; ``code_health/telemetry.log`` names both.
    """
    pytest.importorskip("psutil")
    _write_pyproject(
        tmp_path,
        "[tool.forge.telemetry]\nsample_interval = 0.05\nplot = false\n",
    )
    cmd = [
        sys.executable,
        "-c",
        "import sys, time; time.sleep(0.15); print('hello'); sys.exit(3)",
    ]

    code, output = telemetry.run_command(cmd, tmp_path, capture=True)

    assert code == 3
    assert "hello" in output
    log_path = tmp_path / "code_health" / "telemetry.log"
    assert log_path.exists()
    log_text = log_path.read_text(encoding="utf-8")
    assert "exit code: 3" in log_text
    assert "peak rss" in log_text


def test_run_command_integration_capture_false_streams_and_still_writes_log(
    tmp_path: Path,
) -> None:
    """``run_command(capture=False)`` streams the child's output and still profiles it.

    SCENARIO: a short-lived child, sampled with ``capture=False`` — the
        streaming (CLI) mode where stdio is inherited rather than captured.
    MOCK SETUP: none — exercises real psutil sampling against a ``tmp_path``
        repo root configured with a fast ``sample_interval`` and
        ``plot = false``.
    EXPECTED BEHAVIOR: the returned output is the empty string (nothing
        captured); the child's exit code passes through unchanged; the
        telemetry log is still written.
    """
    pytest.importorskip("psutil")
    _write_pyproject(
        tmp_path,
        "[tool.forge.telemetry]\nsample_interval = 0.05\nplot = false\n",
    )
    cmd = [
        sys.executable,
        "-c",
        "import sys, time; time.sleep(0.15); print('hello'); sys.exit(3)",
    ]

    code, output = telemetry.run_command(cmd, tmp_path, capture=False)

    assert code == 3
    assert output == ""
    log_path = tmp_path / "code_health" / "telemetry.log"
    assert log_path.exists()
    assert "exit code: 3" in log_path.read_text(encoding="utf-8")


def test_run_command_integration_with_label_writes_labeled_log_and_history(
    tmp_path: Path,
) -> None:
    """A labeled run writes ``telemetry_<label>.log`` and an appending history line.

    SCENARIO: two consecutive labeled runs of a short-lived child (a retry
        pattern — #376's motivating case).
    MOCK SETUP: none — real psutil + real subprocess, same as the sibling
        integration tests above.
    EXPECTED BEHAVIOR: ``telemetry_r1.log`` exists and the unlabeled
        ``telemetry.log`` does not; ``telemetry_history.log`` gains one
        ``label=r1``/``exit=`` line per run, and the first run's line is
        left unchanged after the second run appends.
    """
    pytest.importorskip("psutil")
    _write_pyproject(
        tmp_path,
        "[tool.forge.telemetry]\nsample_interval = 0.05\nplot = false\n",
    )
    cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]

    code, _ = telemetry.run_command(cmd, tmp_path, capture=True, label="r1")

    assert code == 0
    labeled_log = tmp_path / "code_health" / "telemetry_r1.log"
    default_log = tmp_path / "code_health" / "telemetry.log"
    assert labeled_log.exists()
    assert not default_log.exists()
    history_path = tmp_path / "code_health" / "telemetry_history.log"
    first_lines = history_path.read_text(encoding="utf-8").splitlines()
    assert len(first_lines) == 1
    assert "label=r1" in first_lines[0]
    assert "exit=0" in first_lines[0]

    code, _ = telemetry.run_command(cmd, tmp_path, capture=True, label="r1")

    assert code == 0
    second_lines = history_path.read_text(encoding="utf-8").splitlines()
    assert len(second_lines) == 2
    assert second_lines[0] == first_lines[0]
    assert "label=r1" in second_lines[1]


def test_run_command_sample_first_wait_avoids_extra_interval_latency(
    tmp_path: Path,
) -> None:
    """Sample-first ``wait(timeout=)`` reports near-true wall time (#376 fix).

    SCENARIO: a long ``sample_interval`` (2.0s) with a child that exits
        almost immediately (~0.1s) — the old poll-then-sleep loop slept a
        full interval past the child's exit, inflating both the wrapper's
        own wall-clock time and the logged ``duration:`` figure by up to
        one interval.
    MOCK SETUP: none — real psutil + real subprocess; the assertions are the
        regression test itself (both fail against the pre-fix shape, which
        would take ~2s).
    EXPECTED BEHAVIOR: ``run_command`` returns within 1s of being called,
        and the ``duration:`` value it logs is also under 1s.
    """
    pytest.importorskip("psutil")
    _write_pyproject(
        tmp_path,
        "[tool.forge.telemetry]\nsample_interval = 2.0\nplot = false\n",
    )
    cmd = [sys.executable, "-c", "import time; time.sleep(0.1)"]

    started = time.monotonic()
    code, _ = telemetry.run_command(cmd, tmp_path, capture=True)
    wall = time.monotonic() - started

    assert code == 0
    assert wall < 1.0
    log_text = (tmp_path / "code_health" / "telemetry.log").read_text(encoding="utf-8")
    match = re.search(r"duration:\s*([\d.]+)s", log_text)
    assert match is not None
    assert float(match.group(1)) < 1.0


# ---------------------------------------------------------------------------
# _render_plot
# ---------------------------------------------------------------------------


def test_render_plot_missing_matplotlib_logs_hint_and_skips_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing matplotlib logs a hint naming the package and skips the PNG."""
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    samples = [telemetry.Sample(elapsed=0.0, rss_mb=1.0, cpu_percent=1.0)]

    with caplog.at_level(logging.INFO, logger="forge.telemetry"):
        telemetry._render_plot(tmp_path, samples)

    assert not (tmp_path / "code_health" / "telemetry.png").exists()
    assert "matplotlib" in caplog.text
    assert "telemetry" in caplog.text


def test_render_plot_empty_samples_is_a_noop(tmp_path: Path) -> None:
    """No samples means no PNG is attempted at all."""
    telemetry._render_plot(tmp_path, [])
    assert not (tmp_path / "code_health" / "telemetry.png").exists()


def test_render_plot_writes_png_when_matplotlib_available(tmp_path: Path) -> None:
    """A real matplotlib renders a non-empty PNG chart."""
    pytest.importorskip("matplotlib")
    samples = [
        telemetry.Sample(elapsed=0.0, rss_mb=1.0, cpu_percent=10.0),
        telemetry.Sample(elapsed=1.0, rss_mb=2.0, cpu_percent=20.0),
    ]
    telemetry._render_plot(tmp_path, samples)
    png = tmp_path / "code_health" / "telemetry.png"
    assert png.exists()
    assert png.stat().st_size > 0


def test_render_plot_with_label_writes_suffixed_png_only(tmp_path: Path) -> None:
    """A non-empty label suffixes the PNG filename instead of the default."""
    pytest.importorskip("matplotlib")
    samples = [
        telemetry.Sample(elapsed=0.0, rss_mb=1.0, cpu_percent=10.0),
        telemetry.Sample(elapsed=1.0, rss_mb=2.0, cpu_percent=20.0),
    ]
    telemetry._render_plot(tmp_path, samples, label="r1")
    labeled_png = tmp_path / "code_health" / "telemetry_r1.png"
    assert labeled_png.exists()
    assert labeled_png.stat().st_size > 0
    assert not (tmp_path / "code_health" / "telemetry.png").exists()
