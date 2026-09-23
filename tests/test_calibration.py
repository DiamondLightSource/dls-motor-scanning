"""Tests for the feedback calibration fit."""

from pathlib import Path

import pytest

from conftest import FakeCatools
from dls_motor_scanning.calibration import (
    CALC_EXPRESSION,
    Calibration,
    calc_record,
    excel_formula,
    fit_calibration,
    load_readings,
)
from dls_motor_scanning.scanning import ScanConfig, perform_scan

# A polynomial that is easy to recover from a short, well conditioned dataset
COEFFICIENTS = (2.0, 0.5, -0.01, 0.002, -1e-4, 1e-6)

POT = "SIM-MO-POT-01:RAW"


def evaluate(coefficients: tuple[float, ...], raw: float) -> float:
    return sum(
        coefficient * raw**power for power, coefficient in enumerate(coefficients)
    )


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    """A plain two column CSV, raw feedback first."""
    path = tmp_path / "scan.csv"
    rows = ["raw,encoder"]
    rows += [f"{raw},{evaluate(COEFFICIENTS, raw)!r}" for raw in range(21)]
    path.write_text("\n".join(rows) + "\n")
    return path


def test_fit_reproduces_the_source_polynomial(csv_path: Path):
    calibration = fit_calibration(csv_path)
    for raw in range(21):
        assert evaluate(calibration.ascending, raw) == pytest.approx(
            evaluate(COEFFICIENTS, raw), abs=1e-6
        )


def test_integer_column_is_the_raw_feedback_whichever_order(tmp_path: Path):
    path = tmp_path / "swapped.csv"
    rows = ["encoder,raw"]
    rows += [f"{evaluate(COEFFICIENTS, raw)!r},{raw}" for raw in range(21)]
    path.write_text("\n".join(rows) + "\n")

    readings = load_readings(path)
    assert (readings.raw_column, readings.position_column) == ("raw", "encoder")


def test_float_raw_feedback_is_accepted(tmp_path: Path):
    """Raw readings need not be integers; an ai record reads back as a float."""
    path = tmp_path / "floats.csv"
    rows = ["raw,encoder"]
    rows += [f"{raw}.0,{evaluate(COEFFICIENTS, raw)!r}" for raw in range(21)]
    path.write_text("\n".join(rows) + "\n")

    readings = load_readings(path)
    assert readings.raw_column == "raw"
    fit_calibration(path)


def test_fit_is_well_conditioned_for_large_raw_readings(tmp_path: Path):
    """A 16 bit ADC's counts raised to the 5th power swamp a naive fit."""
    path = tmp_path / "adc.csv"
    rows = ["raw,encoder"]
    rows += [f"{raw},{1.5 + raw / 6553.6!r}" for raw in range(0, 65536, 2048)]
    path.write_text("\n".join(rows) + "\n")

    calibration = fit_calibration(path)
    for raw in range(0, 65536, 2048):
        assert evaluate(calibration.ascending, raw) == pytest.approx(
            1.5 + raw / 6553.6, abs=1e-6
        )


def test_unnamed_columns_are_ambiguous_beyond_two(tmp_path: Path):
    path = tmp_path / "three.csv"
    path.write_text("a,b,c\n1,2.0,3.0\n")
    with pytest.raises(ValueError, match="--raw-column and --position-column"):
        load_readings(path)


def test_named_columns_must_exist(csv_path: Path):
    with pytest.raises(ValueError, match="No column named 'pot'"):
        load_readings(csv_path, raw_column="pot")


def test_fit_needs_enough_distinct_readings(tmp_path: Path):
    path = tmp_path / "short.csv"
    path.write_text("raw,encoder\n" + "".join(f"{r},{r}.5\n" for r in range(5)))
    with pytest.raises(ValueError, match="at least 6 distinct raw readings"):
        fit_calibration(path)


def test_scan_file_round_trips_through_the_fit(
    fake_ca: FakeCatools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The data file from ``scan --extra-pv`` calibrates with no further options."""
    monkeypatch.chdir(tmp_path)
    fake_ca.signals[POT] = lambda position: 100.0 + 200.0 * position
    config = ScanConfig(
        motor="SIM-MO-TEST-01:Y",
        start=0.0,
        stop=10.0,
        step=0.5,
        delay=0.0,
        extra_pv=POT,
        timestamp=True,
        save_png=False,
        show_plot=False,
    )
    points = perform_scan(config)
    data_file = next(tmp_path.glob("Scan_*.txt"))

    readings = load_readings(data_file)
    assert (readings.raw_column, readings.position_column) == (POT, "Actual")

    calibration = fit_calibration(data_file)
    for point in points:
        assert point.extra is not None
        assert evaluate(calibration.ascending, point.extra) == pytest.approx(
            point.actual, abs=1e-6
        )


def test_calc_record_carries_every_coefficient():
    calibration = Calibration(b=1.0, c=2.0, d=3.0, e=4.0, f=5.0, g=6.0)
    record = calc_record(calibration, "BL01I-MO-TEST-01:ADC", egu="deg")

    for field, value in zip("BCDEFG", calibration.ascending, strict=True):
        assert f'{field}="{value:.10e}"' in record
    assert f'CALC="{CALC_EXPRESSION}"' in record
    assert 'INPA="BL01I-MO-TEST-01:ADC"' in record
    assert 'EGU="deg"' in record


def test_excel_formula_has_a_term_per_coefficient():
    calibration = Calibration(b=1.0, c=2.0, d=3.0, e=4.0, f=5.0, g=6.0)
    formula = excel_formula(calibration, "B7")

    assert formula.startswith("=(")
    # No stray leading "+" on the first term
    assert not formula.startswith("=+")
    for power in range(6):
        assert f"POWER(B7,{power})" in formula
    assert formula.count("POWER(") == 6
