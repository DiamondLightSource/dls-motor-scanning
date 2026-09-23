"""Tests for the command line interface."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FakeCatools
from dls_motor_scanning import __version__
from dls_motor_scanning.cli import app

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def cli_environment() -> dict[str, str]:
    """Environment that makes typer's help output predictable.

    Rich forces terminal mode when GITHUB_ACTIONS is set, and then colours each
    fragment of an option name separately, so "--extra-pv" is not a literal
    substring of the rendered help. A wide COLUMNS additionally stops long
    option names being wrapped across lines.
    """
    environment = dict(os.environ)
    environment.pop("FORCE_COLOR", None)
    environment.pop("GITHUB_ACTIONS", None)
    environment.update(NO_COLOR="1", TERM="dumb", COLUMNS="200")
    return environment


def run(*args: str) -> str:
    """Run the CLI and return its output, stripped of any residual styling."""
    cmd = [sys.executable, "-m", "dls_motor_scanning", *args]
    output = subprocess.check_output(cmd, env=cli_environment()).decode()
    return ANSI_ESCAPE.sub("", output)


def test_cli_version():
    cmd = [sys.executable, "-m", "dls_motor_scanning", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__


def test_help_lists_every_command():
    help_text = run("--help")
    assert "scan" in help_text
    assert "verify" in help_text
    assert "calibrate" in help_text


def test_scan_help_documents_the_options():
    help_text = run("scan", "--help")
    for option in (
        "--extra-pv",
        "--trigger-pv",
        "--trigger-width",
        "--trigger-post-delay",
        "--timestamp",
        "--no-txt",
        "--no-png",
        "--no-plot",
    ):
        assert option in help_text


def test_calibrate_help_documents_the_options():
    help_text = run("calibrate", "--help")
    for option in (
        "--raw-column",
        "--position-column",
        "--raw-input-pv",
        "--egu",
        "--excel-cell",
    ):
        assert option in help_text


def test_no_arguments_shows_help_rather_than_failing():
    cmd = [sys.executable, "-m", "dls_motor_scanning"]
    result = subprocess.run(
        cmd, capture_output=True, check=False, env=cli_environment()
    )
    assert b"scan" in result.stdout


def test_scan_command_runs_and_writes_its_outputs(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Exercises the typer wiring, not just the underlying scan function."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "scan",
            "SIM-MO-TEST-01:Y",
            "40",
            "41",
            "0.5",
            "0",
            "--no-plot",
            "--timestamp",
        ],
    )

    assert result.exit_code == 0, result.output
    written = list(tmp_path.glob("Scan_*.txt"))
    assert len(written) == 1
    assert written[0].read_text().splitlines()[0].endswith("Timestamp(UTC)")
    assert list(tmp_path.glob("Scan_*.png"))


def test_scan_command_reports_an_impossible_range(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app, ["scan", "SIM-MO-TEST-01:Y", "40", "40", "0.5", "0"]
    )

    assert result.exit_code != 0
    assert "must be different" in result.output


def test_calibrate_command_prints_a_calc_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    csv = tmp_path / "pot.csv"
    rows = ["raw,encoder"]
    rows += [f"{raw},{2.0 + 0.0025 * raw!r}" for raw in range(0, 2001, 100)]
    csv.write_text("\n".join(rows) + "\n")

    result = CliRunner().invoke(
        app, ["calibrate", str(csv), "--raw-input-pv", "BL01I-MO-POT-01:ADC"]
    )

    assert result.exit_code == 0, result.output
    assert "<records.calc" in result.output
    assert 'INPA="BL01I-MO-POT-01:ADC"' in result.output
    assert "POWER(A1,5)" in result.output


def test_calibrate_command_reads_a_scan_file_directly(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """scan --extra-pv then calibrate on its data file, with no reformatting."""
    monkeypatch.chdir(tmp_path)
    pot = "SIM-MO-POT-01:RAW"
    fake_ca.signals[pot] = lambda position: 100.0 + 200.0 * position
    scanned = CliRunner().invoke(
        app,
        ["scan", "SIM-MO-TEST-01:Y", "0", "10", "1", "0", "--extra-pv", pot]
        + ["--timestamp", "--no-png", "--no-plot"],
    )
    assert scanned.exit_code == 0, scanned.output
    data_file = next(tmp_path.glob("Scan_*.txt"))

    result = CliRunner().invoke(app, ["calibrate", str(data_file), "--egu", "deg"])

    assert result.exit_code == 0, result.output
    assert f"Fitted Actual against {pot} over 10 readings" in result.output
    assert f'INPA="{pot}"' in result.output
    assert 'EGU="deg"' in result.output


def test_calibrate_command_reports_a_missing_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    csv = tmp_path / "pot.csv"
    csv.write_text("raw,encoder\n" + "".join(f"{r},{r}.5\n" for r in range(10)))

    result = CliRunner().invoke(app, ["calibrate", str(csv), "--raw-column", "adc"])

    assert result.exit_code != 0
    assert "No column named 'adc'" in result.output


def test_verify_command_reports_the_readback_minus_the_pv(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    fake_ca.signals["SIM-MO-POT-01:POS"] = lambda position: position + 0.01
    result = CliRunner().invoke(
        app,
        ["verify", "SIM-MO-TEST-01:Y", "0", "2", "--step", "0.5", "--delay", "0"]
        + ["--compare-pv", "SIM-MO-POT-01:POS", "--no-plot"],
    )

    assert result.exit_code == 0, result.output
    assert "Actual Position - SIM-MO-POT-01:POS (mm)" in result.output
    worst = re.search(r"Largest disagreement: (\S+)", result.output)
    assert worst is not None
    assert float(worst.group(1)) == pytest.approx(0.011)
    assert list(tmp_path.glob("Verify_*.txt"))
    assert list(tmp_path.glob("Verify_*.png"))


def test_verify_command_needs_a_compare_pv():
    result = CliRunner().invoke(app, ["verify", "SIM-MO-TEST-01:Y", "0", "2"])
    assert result.exit_code != 0
    assert "--compare-pv" in result.output


def test_verify_help_documents_the_options():
    help_text = run("verify", "--help")
    for option in (
        "--compare-pv",
        "--step",
        "--delay",
        "--repeats",
        "--timestamp",
        "--no-txt",
        "--no-png",
        "--no-plot",
    ):
        assert option in help_text


class Backlash:
    """A pot that lags the motor by ``gap`` whenever the motion reverses."""

    def __init__(self, gap: float) -> None:
        self.gap = gap
        self.reading = 0.0

    def __call__(self, position: float) -> float:
        self.reading = min(max(self.reading, position - self.gap), position)
        return self.reading


def _no_sleep(seconds: float) -> None:
    """Skip the default settling delay."""


def test_verify_command_defaults_to_twenty_steps_over_the_range(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("time.sleep", _no_sleep)
    fake_ca.signals["SIM-MO-POT-01:POS"] = lambda position: position
    result = CliRunner().invoke(
        app,
        ["verify", "SIM-MO-TEST-01:Y", "0", "10"]
        + ["--compare-pv", "SIM-MO-POT-01:POS", "--no-plot", "--no-png"],
    )

    assert result.exit_code == 0, result.output
    lines = next(tmp_path.glob("Verify_*_0.0_10.0_0.5.txt")).read_text().splitlines()
    assert len(lines) == 1 + 20
    assert "Repeatability" not in result.output


def test_verify_command_repeats_report_the_pot_backlash(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    fake_ca.signals["SIM-MO-POT-01:POS"] = Backlash(0.004)
    result = CliRunner().invoke(
        app,
        ["verify", "SIM-MO-TEST-01:Y", "0", "2", "--step", "0.5", "--delay", "0"]
        + ["--repeats", "3", "--compare-pv", "SIM-MO-POT-01:POS", "--no-plot"],
    )

    assert result.exit_code == 0, result.output
    assert "Repeatability of Actual Position - SIM-MO-POT-01:POS (mm)" in result.output
    # Verifying is about the pot, so the stage against its demand is left out
    assert "Demand Position" not in result.output
    assert "Position error" not in result.output
    # The pot lags by 0.004 on the way up, and reads true on the way down
    reversals = re.findall(r"Reversal B \(mean \+ minus mean -\): (\S+)", result.output)
    assert [float(value) for value in reversals] == pytest.approx([0.004])
    assert list(tmp_path.glob("Verify_*_x3.txt"))
    assert list(tmp_path.glob("Verify_*_x3.png"))


def test_verify_command_rejects_a_single_repeat():
    result = CliRunner().invoke(
        app,
        ["verify", "SIM-MO-TEST-01:Y", "0", "2", "--repeats", "1"]
        + ["--compare-pv", "SIM-MO-POT-01:POS"],
    )
    assert result.exit_code != 0
    assert "--repeats" in result.output
