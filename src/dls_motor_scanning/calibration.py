"""Calibration of an unscaled feedback device onto engineering units.

A 5th order polynomial is fitted to paired readings, for example a
potentiometer's raw ADC counts against the position reported by an encoder. The
readings are normally the data file written by ``scan --extra-pv``, though a
plain CSV works too. The fit is emitted both as an EPICS calc record for
Builder and as an Excel formula.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .scanning import (
    COLUMN_ACTUAL,
    COLUMN_DESIRED,
    COLUMN_MOVE_TIME,
    COLUMN_TIMESTAMP,
)

__all__ = [
    "Calibration",
    "Readings",
    "calc_record",
    "excel_formula",
    "fit_calibration",
    "fit_readings",
    "load_readings",
    "max_residual",
]

DEGREE = 5
"""Order of the fitted polynomial.

The calc record maps its coefficients onto fields B to G, so this is fixed by
the EPICS record rather than being a free choice.
"""

CALC_EXPRESSION = "((A^0)*B)+((A^1)*C)+((A^2)*D)+((A^3)*E)+((A^4)*F)+((A^5)*G)"
"""The expression evaluated by the generated calc record."""

SCAN_COLUMNS = (COLUMN_DESIRED, COLUMN_ACTUAL, COLUMN_MOVE_TIME, COLUMN_TIMESTAMP)
"""Columns a scan always writes, none of which can be the raw feedback."""


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


@dataclass(frozen=True)
class Readings:
    """Paired raw feedback and scaled position readings, and where they came from."""

    raw_column: str
    position_column: str
    raw: Any
    position: Any


def _read_table(path: Path) -> Any:
    """Read either a comma separated CSV or a whitespace separated scan file."""
    import pandas as pd

    with path.open() as file:
        header = file.readline()
    # Scan files are space separated; the timestamp and PV name columns contain
    # colons, so letting pandas sniff the delimiter is not safe
    separator = "," if "," in header else r"\s+"
    return pd.read_csv(path, sep=separator)


def _default_columns(columns: list[str], frame: Any) -> tuple[str | None, str | None]:
    """Choose the raw and position columns when they have not been named.

    A scan file is recognised by its ``Actual`` column, which becomes the
    position, and the raw feedback is the one column that is not a standard
    scan column, i.e. the extra PV. Any other file must have exactly two
    columns; if just one of them holds integers it is the raw feedback,
    otherwise the first column is.
    """
    import pandas as pd

    if COLUMN_ACTUAL in columns:
        extras = [column for column in columns if column not in SCAN_COLUMNS]
        return (extras[0] if len(extras) == 1 else None), COLUMN_ACTUAL

    if len(columns) != 2:
        return None, None
    integers = [c for c in columns if pd.api.types.is_integer_dtype(frame[c])]
    raw = integers[0] if len(integers) == 1 else columns[0]
    position = columns[1] if raw == columns[0] else columns[0]
    return raw, position


def load_readings(
    path: Path, raw_column: str | None = None, position_column: str | None = None
) -> Readings:
    """Load the paired readings from ``path``.

    Columns that are not named are chosen as described in
    :func:`_default_columns`.
    """
    import numpy as np
    import pandas as pd

    frame = _read_table(path)
    columns = [str(column) for column in frame.columns]

    default_raw, default_position = _default_columns(columns, frame)
    raw_column = raw_column or default_raw
    position_column = position_column or default_position

    found = ", ".join(columns)
    if raw_column is None or position_column is None:
        raise ValueError(
            "Cannot tell which columns hold the raw feedback and the scaled "
            f"position; name them with --raw-column and --position-column. "
            f"Columns found: {found}"
        )
    for column in (raw_column, position_column):
        if column not in columns:
            raise ValueError(f"No column named {column!r}; columns found: {found}")
    if raw_column == position_column:
        raise ValueError(f"The raw and position columns are both {raw_column!r}")

    try:
        raw = np.asarray(pd.to_numeric(frame[raw_column]), dtype=float)
        position = np.asarray(pd.to_numeric(frame[position_column]), dtype=float)
    except ValueError as error:
        raise ValueError(f"Non-numeric reading in {path}: {error}") from error

    return Readings(
        raw_column=raw_column,
        position_column=position_column,
        raw=raw,
        position=position,
    )


def fit_readings(readings: Readings) -> Calibration:
    """Fit the calibration polynomial to ``readings``."""
    import numpy as np
    from numpy.polynomial import Polynomial

    distinct = len(np.unique(readings.raw))
    if distinct <= DEGREE:
        raise ValueError(
            f"A {DEGREE}th order fit needs at least {DEGREE + 1} distinct raw "
            f"readings; {readings.raw_column!r} has {distinct}"
        )

    # Fitting over a normalised domain keeps the fit well conditioned even for
    # raw readings in the tens of thousands; convert() then maps the result
    # back onto plain coefficients in the raw units, as the calc record needs
    fit = Polynomial.fit(readings.raw, readings.position, DEGREE).convert()
    coefficients = np.zeros(DEGREE + 1)
    coefficients[: len(fit.coef)] = fit.coef
    b, c, d, e, f, g = (float(coefficient) for coefficient in coefficients)
    return Calibration(b=b, c=c, d=d, e=e, f=f, g=g)


def max_residual(calibration: Calibration, readings: Readings) -> float:
    """Largest distance between the fit and any reading, in position units."""
    import numpy as np
    from numpy.polynomial import Polynomial

    fitted = Polynomial(calibration.ascending)(readings.raw)
    return float(np.max(np.abs(fitted - readings.position)))


def fit_calibration(
    path: Path, raw_column: str | None = None, position_column: str | None = None
) -> Calibration:
    """Fit the calibration polynomial to the readings in ``path``."""
    return fit_readings(load_readings(path, raw_column, position_column))


def calc_record(calibration: Calibration, raw_input_pv: str, egu: str = "mm") -> str:
    """Render the calibration as a Builder ``records.calc`` XML entry."""
    return (
        f'<records.calc B="{calibration.b:.10e}" C="{calibration.c:.10e}" '
        f'CALC="{CALC_EXPRESSION}" '
        f'D="{calibration.d:.10e}" E="{calibration.e:.10e}" EGU="{egu}" '
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
