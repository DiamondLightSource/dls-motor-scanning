"""Calibration of an unscaled feedback device onto engineering units.

A 5th order polynomial is fitted to a CSV of paired readings, for example a
potentiometer's raw ADC counts against the position reported by an encoder. The
fit is emitted both as an EPICS calc record for Builder and as an Excel formula.
"""

from dataclasses import dataclass
from pathlib import Path

__all__ = ["Calibration", "calc_record", "excel_formula", "fit_calibration"]

DEGREE = 5
"""Order of the fitted polynomial.

The calc record maps its coefficients onto fields B to G, so this is fixed by
the EPICS record rather than being a free choice.
"""

CALC_EXPRESSION = "((A^0)*B)+((A^1)*C)+((A^2)*D)+((A^3)*E)+((A^4)*F)+((A^5)*G)"
"""The expression evaluated by the generated calc record."""


@dataclass(frozen=True)
class Calibration:
    """Polynomial mapping raw feedback onto engineering units.

    ``egu = b + c*raw + d*raw^2 + e*raw^3 + f*raw^4 + g*raw^5``, named to match
    the calc record fields the coefficients are written to.
    """

    b: float
    c: float
    d: float
    e: float
    f: float
    g: float

    @property
    def ascending(self) -> tuple[float, float, float, float, float, float]:
        """Coefficients from the constant term upwards."""
        return (self.b, self.c, self.d, self.e, self.f, self.g)


def fit_calibration(csv_path: Path) -> Calibration:
    """Fit a polynomial to the paired readings in ``csv_path``.

    The CSV must hold one integer column, taken as the raw feedback, and one
    float column, taken as the scaled position to calibrate against.
    """
    import numpy as np
    import pandas as pd

    frame = pd.read_csv(csv_path)

    int_column = None
    float_column = None
    for column in frame.columns:
        if pd.api.types.is_integer_dtype(frame[column]):
            int_column = column
        elif pd.api.types.is_float_dtype(frame[column]):
            float_column = column

    if int_column is None or float_column is None:
        found = ", ".join(f"{name} ({frame[name].dtype})" for name in frame.columns)
        raise ValueError(
            "CSV file must contain at least one integer column (the raw "
            f"feedback) and one float column (the scaled position); found: {found}"
        )

    raw_feedback = frame[int_column].to_numpy()
    scaled = frame[float_column].to_numpy()

    # polyfit returns the highest order coefficient first
    g, f, e, d, c, b = np.polyfit(raw_feedback, scaled, DEGREE)
    return Calibration(b=b, c=c, d=d, e=e, f=f, g=g)


def calc_record(calibration: Calibration, raw_input_pv: str) -> str:
    """Render the calibration as a Builder ``records.calc`` XML entry."""
    return (
        f'<records.calc B="{calibration.b:.10e}" C="{calibration.c:.10e}" '
        f'CALC="{CALC_EXPRESSION}" '
        f'D="{calibration.d:.10e}" E="{calibration.e:.10e}" EGU="mm" '
        f'F="{calibration.f:.10e}" G="{calibration.g:.10e}" '
        f'INPA="{raw_input_pv}" PREC="4" SCAN=".1 second" name="" record=""/>'
    )


def excel_formula(calibration: Calibration, cell: str) -> str:
    """Render the calibration as an Excel formula against ``cell``."""
    terms = "".join(
        f"+({coefficient:.10e}*POWER({cell},{power}))"
        for power, coefficient in enumerate(calibration.ascending)
    )
    # The leading term needs no "+" in front of it
    return f"={terms.removeprefix('+')}"
