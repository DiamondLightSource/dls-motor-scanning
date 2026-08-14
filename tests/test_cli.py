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


def test_help_lists_both_commands():
    help_text = run("--help")
    assert "scan" in help_text
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
    assert "--raw-input-pv" in help_text
    assert "--excel-cell" in help_text


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
