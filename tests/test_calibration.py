"""Tests for the feedback calibration fit."""

from pathlib import Path

import pytest

from dls_motor_scanning.calibration import (
    CALC_EXPRESSION,
    Calibration,
    calc_record,
    excel_formula,
    fit_calibration,
)

# A polynomial that is easy to recover from a short, well conditioned dataset
COEFFICIENTS = (2.0, 0.5, -0.01, 0.002, -1e-4, 1e-6)


def evaluate(raw: int) -> float:
    return sum(
        coefficient * raw**power for power, coefficient in enumerate(COEFFICIENTS)
    )


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    """A CSV with one integer column and one float column, as the tool expects."""
    path = tmp_path / "scan.csv"
    rows = ["raw,encoder"]
    rows += [f"{raw},{evaluate(raw)!r}" for raw in range(21)]
    path.write_text("\n".join(rows) + "\n")
    return path


def test_fit_reproduces_the_source_polynomial(csv_path: Path):
    calibration = fit_calibration(csv_path)
    for raw in range(21):
        fitted = sum(
            coefficient * raw**power
            for power, coefficient in enumerate(calibration.ascending)
        )
        assert fitted == pytest.approx(evaluate(raw), abs=1e-6)


def test_fit_rejects_a_csv_without_both_column_types(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text("a,b\n1,2\n3,4\n")
    with pytest.raises(ValueError, match="must contain at least one integer column"):
        fit_calibration(path)


def test_calc_record_carries_every_coefficient():
    calibration = Calibration(b=1.0, c=2.0, d=3.0, e=4.0, f=5.0, g=6.0)
    record = calc_record(calibration, "BL01I-MO-TEST-01:ADC")

    for field, value in zip("BCDEFG", calibration.ascending, strict=True):
        assert f'{field}="{value:.10e}"' in record
    assert f'CALC="{CALC_EXPRESSION}"' in record
    assert 'INPA="BL01I-MO-TEST-01:ADC"' in record


def test_excel_formula_has_a_term_per_coefficient():
    calibration = Calibration(b=1.0, c=2.0, d=3.0, e=4.0, f=5.0, g=6.0)
    formula = excel_formula(calibration, "B7")

    assert formula.startswith("=(")
    # No stray leading "+" on the first term
    assert not formula.startswith("=+")
    for power in range(6):
        assert f"POWER(B7,{power})" in formula
    assert formula.count("POWER(") == 6
