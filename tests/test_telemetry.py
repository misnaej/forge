"""Tests for ``forge.telemetry`` — the resource-profiling wrapper."""

# MOCKING STRATEGY: two seams, never mixed within one test.
#   (1) ``forge.telemetry.psutil`` — monkeypatched to the Fake* objects below
#       (FakeNoSuchProcess / FakeProcess / FakePsutil) for every
#       ``_tree_rss_bytes`` / ``_sample`` / ``telemetry_available`` unit test,
#       so no test ever touches a real process tree.
#   (2) ``forge.telemetry.run_command`` — monkeypatched in the ``main()`` CLI
#       tests to capture the argv it would have spawned, without running a
#       real child. The lone exception is
#       ``test_run_command_integration_captures_child_output_and_writes_log``,
#       which exercises the real psutil + real subprocess path end-to-end
#       against a ``tmp_path`` repo root (``pytest.importorskip("psutil")``
#       guards it; ``plot=false`` keeps it independent of matplotlib).
# ``_render_plot``'s missing-matplotlib path uses
# ``monkeypatch.setitem(sys.modules, "matplotlib", None)`` — the standard
# trick that makes ``import matplotlib`` raise ``ImportError`` without
# uninstalling the real package.

from __future__ import annotations

import logging
import sys
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

    def _fake_run_command(cmd: list[str], root: object) -> tuple[int, str]:
        captured["cmd"] = cmd
        captured["root"] = root
        return 7, ""

    monkeypatch.setattr(telemetry, "run_command", _fake_run_command)

    code = telemetry.main(["--", "pytest", "--depth", "0"])

    assert code == 7
    assert captured["cmd"] == ["pytest", "--depth", "0"]


def test_main_argv_none_uses_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``argv=None`` (the default) falls back to ``sys.argv[1:]``."""
    monkeypatch.setattr(sys, "argv", ["forge-telemetry", "--", "true"])
    captured: dict[str, object] = {}

    def _fake_run_command(cmd: list[str], root: object) -> tuple[int, str]:
        captured["cmd"] = cmd
        del root
        return 0, ""

    monkeypatch.setattr(telemetry, "run_command", _fake_run_command)

    assert telemetry.main() == 0
    assert captured["cmd"] == ["true"]


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
