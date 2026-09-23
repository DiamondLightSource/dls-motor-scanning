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
    interactive_backend,
    perform_scan,
    plan_scan,
    plan_targets,
    repeatability,
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


def test_interactive_backend_is_none_without_a_display(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert interactive_backend() is None


def test_interactive_backend_is_none_without_a_qt_binding(
    monkeypatch: pytest.MonkeyPatch,
):
    """A display alone is not enough; PyQt has to import as well."""
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setitem(sys.modules, "PyQt5.QtWidgets", None)
    monkeypatch.setitem(sys.modules, "PyQt6.QtWidgets", None)
    assert interactive_backend() is None


def test_png_is_written_even_when_the_plot_cannot_be_shown(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The png must not depend on a working GUI toolkit."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    perform_scan(config(save_png=True, show_plot=True))

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


def test_compare_adds_a_difference_column_after_the_extra_pv():
    assert headings(config(extra_pv="X:Y", compare=True, timestamp=True)) == [
        "Desired",
        "Actual",
        "MoveTime",
        "X:Y",
        "Actual-Extra",
        "Timestamp(UTC)",
    ]
    point = ScanPoint(demand=1.0, actual=0.9, move_time=0.5, extra=0.75)
    assert row(point, compare=True)[-1] == str(point.difference)
    assert point.difference == pytest.approx(0.15)


def test_compare_needs_an_extra_pv():
    with pytest.raises(ValueError, match="needs an extra PV"):
        plan_scan(config(compare=True))


def test_summarise_scan_compare_keeps_the_sign_of_the_differences():
    points = [
        ScanPoint(demand=1.0, actual=1.0, move_time=0.1, extra=1.25),
        ScanPoint(demand=2.0, actual=2.0, move_time=0.1, extra=2.75),
    ]
    assert summarise_scan(points).difference is None

    difference = summarise_scan(points, compare=True).difference
    assert difference is not None
    # A calibration reading high shows as a negative offset, not a magnitude
    assert difference.mean == pytest.approx(-0.5)
    assert difference.minimum == pytest.approx(-0.75)
    assert difference.maximum == pytest.approx(-0.25)


def test_perform_scan_compare_writes_a_verify_file_and_plot(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    # A calibrated pot that reads 0.01 high everywhere
    fake_ca.signals["SIM-MO-POT-01:POS"] = lambda position: position + 0.01
    perform_scan(config(extra_pv="SIM-MO-POT-01:POS", compare=True, save_png=True))

    lines = next(tmp_path.glob("Verify_*.txt")).read_text().splitlines()
    assert lines[0].split()[-1] == "Actual-Extra"
    # Actual carries the fake motor's 0.001 following error as well
    differences = [float(line.split()[-1]) for line in lines[1:]]
    assert differences == pytest.approx([-0.011] * 4)
    assert list(tmp_path.glob("Verify_*.png"))
    assert not list(tmp_path.glob("Scan_*"))


def test_plan_scan_reaches_stop_despite_float_rounding():
    # 0.3 / 0.1 is 2.9999999999999996, which would truncate to 2 steps
    assert plan_scan(config(start=0.0, stop=0.3, step=0.1)) == (3, 0.1)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"repeats": 1}, "at least 2 repeats"),
        ({"repeats": 2, "step": 2.0}, "at least 2 steps"),
    ],
)
def test_plan_scan_rejects_an_unusable_repeatability_test(
    overrides: dict[str, object], message: str
):
    with pytest.raises(ValueError, match=message):
        plan_scan(config(**overrides))


def test_plan_targets_single_pass_steps_away_from_the_start():
    targets = plan_targets(config())
    assert [target.demand for target in targets] == [40.5, 41.0, 41.5, 42.0]
    assert {target.direction for target in targets} == {None}


def test_plan_targets_repeats_go_there_and_back():
    targets = plan_targets(config(start=42.0, stop=40.0, repeats=2))
    there = [41.5, 41.0, 40.5, 40.0]
    back = [40.5, 41.0, 41.5, 42.0]
    assert [target.demand for target in targets] == (there + back) * 2
    assert [target.cycle for target in targets] == [1] * 8 + [2] * 8
    # A reverse scan moves negative on the way there
    assert [target.direction for target in targets] == ([-1] * 4 + [1] * 4) * 2


def test_headings_add_the_cycle_and_direction_when_repeating():
    assert headings(config(repeats=2, timestamp=True))[-3:] == [
        "Cycle",
        "Direction",
        "Timestamp(UTC)",
    ]
    point = ScanPoint(demand=1.0, actual=1.0, move_time=0.1, cycle=2, direction=-1)
    assert row(point)[-2:] == ["2", "-1"]


def test_repeatability_follows_iso_230_2():
    """Two targets, both approached twice from each side."""

    def point(demand: float, direction: int, error: float) -> ScanPoint:
        return ScanPoint(
            demand=demand, actual=demand - error, move_time=0.1, direction=direction
        )

    points = [
        # At 1.0: +0.010 +0.012 moving up, -0.002 +0.000 moving down
        point(1.0, 1, 0.010),
        point(1.0, 1, 0.012),
        point(1.0, -1, -0.002),
        point(1.0, -1, 0.000),
        # At 2.0: identical readings, bar a 0.001 reversal
        point(2.0, 1, 0.001),
        point(2.0, 1, 0.001),
        point(2.0, -1, 0.000),
        point(2.0, -1, 0.000),
    ]
    figures = repeatability(points, lambda point: point.error)

    assert figures is not None
    s = 0.002 / 2**0.5  # sample SD of two readings 0.002 apart
    assert figures.cycles == 2
    assert figures.positive.value == pytest.approx(4 * s)
    assert figures.positive.position == 1.0
    assert figures.negative.value == pytest.approx(4 * s)
    assert figures.reversal.value == pytest.approx(0.012)
    assert figures.reversal.position == 1.0
    assert figures.mean_reversal == pytest.approx((0.012 + 0.001) / 2)
    assert figures.bidirectional.value == pytest.approx(4 * s + 0.012)
    assert figures.accuracy == pytest.approx((0.011 + 2 * s) - (-0.001 - 2 * s))


def test_repeatability_needs_repeated_bidirectional_readings():
    single = [ScanPoint(demand=1.0, actual=1.0, move_time=0.1)]
    assert repeatability(single, lambda point: point.error) is None
    one_way = [
        ScanPoint(demand=1.0, actual=1.0, move_time=0.1, direction=1),
        ScanPoint(demand=1.0, actual=1.0, move_time=0.1, direction=1),
    ]
    assert repeatability(one_way, lambda point: point.error) is None


def test_perform_scan_repeats_record_both_directions(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    points = perform_scan(config(repeats=2))

    assert len(points) == 16
    lines = next(tmp_path.glob("Scan_*_x2.txt")).read_text().splitlines()
    assert lines[0] == "Desired Actual MoveTime Cycle Direction"
    assert [line.split()[-1] for line in lines[1:9]] == ["+1"] * 4 + ["-1"] * 4
