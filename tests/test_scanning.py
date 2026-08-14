"""Tests for the scan logic."""

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeCatools
from dls_motor_scanning.scanning import (
    ScanConfig,
    ScanPoint,
    headings,
    perform_scan,
    plan_scan,
    row,
    summarise,
    summarise_scan,
)


def config(**overrides: object) -> ScanConfig:
    """A short scan, with plotting off unless a test asks for it."""
    defaults: dict[str, object] = {
        "motor": "SIM-MO-TEST-01:Y",
        "start": 40.0,
        "stop": 42.0,
        "step": 0.5,
        "delay": 0.0,
        "save_png": False,
        "show_plot": False,
    }
    defaults.update(overrides)
    return ScanConfig(**defaults)  # type: ignore[arg-type]


def test_summarise_uses_population_statistics():
    stats = summarise([1.0, 2.0, 3.0, 4.0])
    assert stats.mean == 2.5
    # Population standard deviation, not the sample one (which would be ~1.291)
    assert stats.sd == pytest.approx(1.1180339887)
    assert stats.error_on_mean == pytest.approx(stats.sd / 2)
    assert (stats.minimum, stats.maximum) == (1.0, 4.0)


def test_summarise_rejects_no_values():
    with pytest.raises(ValueError, match="empty"):
        summarise([])


def test_plan_scan_forward():
    assert plan_scan(config()) == (4, 0.5)


def test_plan_scan_reverse_flips_the_step():
    assert plan_scan(config(start=42.0, stop=40.0)) == (4, -0.5)


def test_plan_scan_ignores_a_negative_step_sign():
    assert plan_scan(config(step=-0.5)) == (4, 0.5)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"step": 0.0}, "must not be zero"),
        ({"stop": 40.0}, "must be different"),
        ({"step": 10.0}, "larger than the scan range"),
    ],
)
def test_plan_scan_rejects_impossible_requests(
    overrides: dict[str, object], message: str
):
    with pytest.raises(ValueError, match=message):
        plan_scan(config(**overrides))


def test_headings_track_the_enabled_columns():
    assert headings(config()) == ["Desired", "Actual", "MoveTime"]
    assert headings(config(extra_pv="X:Y"))[-1] == "X:Y"
    assert headings(config(timestamp=True))[-1] == "Timestamp(UTC)"
    assert headings(config(extra_pv="X:Y", timestamp=True)) == [
        "Desired",
        "Actual",
        "MoveTime",
        "X:Y",
        "Timestamp(UTC)",
    ]


def test_row_matches_the_headings():
    point = ScanPoint(
        demand=1.0,
        actual=0.9,
        move_time=0.5,
        extra=7.0,
        timestamp="2026-01-01T00:00:00Z",
    )
    assert len(row(point)) == len(headings(config(extra_pv="X:Y", timestamp=True)))


def test_summarise_scan_reports_signed_extremes():
    # Every error is negative: the maximum must be an observed value, not zero
    points = [
        ScanPoint(demand=1.0, actual=1.5, move_time=0.1),
        ScanPoint(demand=2.0, actual=2.25, move_time=0.2),
    ]
    summary = summarise_scan(points)
    assert summary.max_error == pytest.approx(-0.25)
    assert summary.min_error == pytest.approx(-0.5)
    # The mean and SD are of the magnitudes
    assert summary.error_magnitude.mean == pytest.approx(0.375)


def test_perform_scan_writes_the_expected_txt(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    points = perform_scan(config())

    assert len(points) == 4
    written = list(tmp_path.glob("Scan_*.txt"))
    assert len(written) == 1
    lines = written[0].read_text().splitlines()
    assert lines[0] == "Desired Actual MoveTime"
    assert len(lines) == 5
    # Demand positions step away from the start
    assert [line.split()[0] for line in lines[1:]] == ["40.5", "41.0", "41.5", "42.0"]


def test_perform_scan_timestamp_column_is_utc(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    perform_scan(config(timestamp=True))

    lines = next(tmp_path.glob("Scan_*.txt")).read_text().splitlines()
    assert lines[0].split()[-1] == "Timestamp(UTC)"
    stamp = lines[1].split()[-1]
    assert stamp.startswith("2026-") and stamp.endswith("Z")
    assert "T" in stamp


def test_perform_scan_honours_no_txt(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    perform_scan(config(write_txt=False))
    assert list(tmp_path.glob("Scan_*.txt")) == []


def test_perform_scan_saves_a_png(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    perform_scan(config(save_png=True))

    written = list(tmp_path.glob("Scan_*.png"))
    assert len(written) == 1
    assert written[0].stat().st_size > 0


def test_perform_scan_plots_the_extra_pv(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    perform_scan(config(extra_pv="SIM-DI-TEST-01:SIGNAL", save_png=True))

    lines = next(tmp_path.glob("Scan_*.txt")).read_text().splitlines()
    assert lines[0].split()[-1] == "SIM-DI-TEST-01:SIGNAL"
    assert list(tmp_path.glob("Scan_*.png"))


def test_perform_scan_pulses_the_trigger_pv(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    perform_scan(config(trigger_pv="SIM-DI-TEST-01:TRIG", trigger_width=0.0))

    pulses = [value for pv, value in fake_ca.puts if pv == "SIM-DI-TEST-01:TRIG"]
    # One high and one low per step
    assert pulses == [1, 0] * 4


def test_scanning_does_not_import_cothread_at_module_scope():
    """Importing the module must not need EPICS; ``--help`` relies on this."""
    code = (
        "import sys; import dls_motor_scanning.scanning; "
        "sys.exit(1 if 'cothread' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
