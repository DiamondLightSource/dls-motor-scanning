[![CI](https://github.com/DiamondLightSource/dls-motor-scanning/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/dls-motor-scanning/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/dls-motor-scanning/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/dls-motor-scanning)
[![PyPI](https://img.shields.io/pypi/v/dls-motor-scanning.svg)](https://pypi.org/project/dls-motor-scanning)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# dls_motor_scanning

Tools for characterising the motion performance of EPICS motors.

Two commands are provided:

- `scan` drives a motor through a range in fixed steps, recording the position
  actually reached and the time each move took, then reports the positioning
  error and move time as statistics, a data file and a plot.
- `calibrate` fits a 5th order polynomial that converts an unscaled feedback
  device, such as a potentiometer read as raw ADC counts, into engineering
  units, and emits it as an EPICS calc record and an Excel formula.

What            | Where
:---:           | :---:
Source          | <https://github.com/DiamondLightSource/dls-motor-scanning>
PyPI            | `pip install dls-motor-scanning`
Docker          | `docker run ghcr.io/diamondlightsource/dls-motor-scanning:latest`
Releases        | <https://github.com/DiamondLightSource/dls-motor-scanning/releases>

## Scanning a motor

```
dls-motor-scanning scan MOTOR START STOP STEP DELAY [options]
```

`START`, `STOP` and `STEP` are in the motor's engineering units, and `DELAY` is
the settling time in seconds to wait after each move before reading back. Scan
`PS-MO-WIRE-01:Y` from 40 to 45 in half-EGU steps, with no settling time:

```
dls-motor-scanning scan PS-MO-WIRE-01:Y 40 45 0.5 0
```

The scan direction follows `START` and `STOP`, so a reverse scan needs no sign
on the step:

```
dls-motor-scanning scan PS-MO-WIRE-01:Y 45 40 0.5 0
```

Each run writes `Scan_<motor>_<date>_<start>_<stop>_<step>.txt` and `.png` into
the current directory, and prints a summary of the move times and position
errors.

Option                      | Effect
:---                        | :---
`--extra-pv PV`             | Read `PV` at each step and add it as a column and a third plot panel
`--trigger-pv PV`           | Pulse `PV` high then low after each move, for example to fire a detector
`--trigger-width SECS`      | How long the trigger is held high (default 1.0)
`--trigger-post-delay SECS` | How long to wait after the trigger returns low (default 0.0)
`--timestamp`               | Add a UTC timestamp column taken from the EPICS timestamp of the readback
`--no-txt`                  | Do not write the data file
`--no-png`                  | Do not save the plot
`--no-plot`                 | Do not open a plot window

### The output file

The data file is space separated, with a header row naming the columns:

```
Desired Actual MoveTime Timestamp(UTC)
40.5 40.499 1.1394305229187012 2026-08-14T09:15:56.375000Z
41.0 40.9992 1.1742472648620605 2026-08-14T09:15:57.625000Z
```

`Timestamp(UTC)` is the timestamp the IOC put on the readback record, not the
time the client received it, which makes it directly comparable with archiver
data. It appears only when `--timestamp` is given, as does the `--extra-pv`
column.

### Plotting on a remote machine

`--plot` needs a working display. Over a slow link, or on a headless machine,
prefer:

```
dls-motor-scanning scan PS-MO-WIRE-01:Y 40 45 0.5 0 --no-plot
```

which skips the interactive window entirely and just writes the png.

> **A note on backends.** cothread integrates with Qt and not with Tk, and the
> two segfault if combined. `PyQt5` is therefore a dependency, and the tool
> selects `qtagg` explicitly rather than letting matplotlib work through its
> own candidate list, which would fall back to `tkagg`. If Qt cannot be
> imported, or `DISPLAY` is unset, the interactive plot is skipped with a
> message instead of taking the process down with it.
>
> The png is always rendered under Agg and written to disk *before* any GUI
> toolkit is touched, so a display problem can never cost you the saved plot.

## Calibrating a feedback device

```
dls-motor-scanning calibrate CSV_PATH [options]
```

The CSV needs one integer column, taken as the raw feedback, and one float
column, taken as the scaled position to calibrate against:

```
raw,encoder
0,2.0
200,2.5
400,3.0
```

```
dls-motor-scanning calibrate pot.csv --raw-input-pv BL01I-MO-POT-01:ADC --excel-cell C2
```

This prints the six coefficients, a Builder `records.calc` entry with them
loaded into fields B to G, and an equivalent Excel formula referencing
`--excel-cell`.

## Development

```
uv sync
uv run pytest
```

The scan code reaches EPICS through `cothread.catools`, which is imported lazily
so the tests, `--help` and the `calibrate` command all work without an IOC. The
test suite substitutes a fake Channel Access layer, so a full scan can be
exercised without hardware.
