[![CI](https://github.com/DiamondLightSource/dls-motor-scanning/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/dls-motor-scanning/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/dls-motor-scanning/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/dls-motor-scanning)
[![PyPI](https://img.shields.io/pypi/v/dls-motor-scanning.svg)](https://pypi.org/project/dls-motor-scanning)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# dls_motor_scanning

Tools for characterising the motion performance of EPICS motors.

Three commands are provided:

- `scan` drives a motor through a range in fixed steps, recording the position
  actually reached and the time each move took, then reports the positioning
  error and move time as statistics, a data file and a plot.
- `calibrate` fits a 5th order polynomial that converts an unscaled feedback
  device, such as a potentiometer read as raw ADC counts, into engineering
  units, and emits it as an EPICS calc record and an Excel formula.
- `verify` scans a motor like `scan`, and reports how far another PV, such as
  the calibrated calc record, is from the motor readback at every step, and
  optionally the unidirectional and bidirectional repeatability.

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
dls-motor-scanning calibrate DATA_PATH [options]
```

The usual input is the data file from a scan run with `--extra-pv` reading the
raw feedback, such as a potentiometer's ADC counts, at every step:

```
dls-motor-scanning scan BL01I-MO-STAGE-01:X 0 50 1 0.5 --extra-pv BL01I-MO-POT-01:ADC --no-plot
dls-motor-scanning calibrate Scan_BL01I-MO-STAGE-01:X_<date>_0.0_50.0_1.0.txt --egu mm
```

The extra PV column is fitted against the `Actual` column, and the calc
record's `INPA` defaults to the extra PV's name, so no other options are
needed. The fit needs at least six distinct raw readings, and many more across
the full travel give a better one.

A comma separated CSV works too. With exactly two columns, the raw feedback is
the integer column if just one of them holds integers, otherwise the first
column:

```
raw,encoder
0,2.0
200,2.5
400,3.0
```

```
dls-motor-scanning calibrate pot.csv --raw-input-pv BL01I-MO-POT-01:ADC --excel-cell C2
```

Option                     | Effect
:---                       | :---
`--raw-column COLUMN`      | Column holding the raw feedback, instead of the default above
`--position-column COLUMN` | Column holding the scaled position, instead of `Actual`
`--raw-input-pv PV`        | `INPA` of the calc record (default: the raw column's name)
`--egu UNITS`              | `EGU` of the calc record (default `mm`)
`--excel-cell CELL`        | Cell the Excel formula reads the raw value from (default `A1`)

This prints the largest residual of the fit, the six coefficients, a Builder
`records.calc` entry with them loaded into fields B to G, and an equivalent
Excel formula referencing `--excel-cell`. The record's `name` and `record` are
left blank for you to fill in.

## Verifying a calibration

```
dls-motor-scanning verify MOTOR START STOP --compare-pv PV [options]
```

Once the calc record is loaded, give the full range of the stage and the
calibrated PV. The motor readback, ideally from a trusted encoder, is the
reference:

```
dls-motor-scanning verify BL01I-MO-STAGE-01:X 0 50 --compare-pv BL01I-MO-POT-01:POS --no-plot
```

By default this is a single pass from `START` to `STOP` in 20 steps, with a
0.5 second settling delay, reporting the signed `Actual - PV` at each step. It
adds an `Actual-Extra` column to the data file, a block of statistics to the
summary, and a plot of the PV and the difference against the readback. The mean
is the calibration's offset, the standard deviation its scatter, and the largest
disagreement the worst case across the travel. The stage's own positioning,
demand against readback, says nothing about the calibration, so it isn't
reported or plotted.

### Repeatability

Add `--repeats N` to also measure repeatability. The range is then traversed
there and back `N` times, so that each target between the two ends is approached
`N` times moving each way:

```
dls-motor-scanning verify BL01I-MO-STAGE-01:X 0 50 --compare-pv BL01I-MO-POT-01:POS --repeats 5 --no-plot
```

The readings at each target are grouped by approach direction and reduced to a
mean and sample standard deviation `s`, then reported in the style of ISO
230-2, each at its worst target:

Figure                             | Meaning
:---                               | :---
Unidirectional repeatability R+/R- | `4s` of the approaches moving positive / negative
Reversal B                         | Mean moving positive minus mean moving negative: hysteresis or backlash
Mean reversal                      | B averaged over every target
Bidirectional repeatability R      | `max(2s+ + 2s- + abs(B), R+, R-)`, the spread whichever way a target is approached
Bidirectional accuracy A           | Lowest `mean - 2s` to highest `mean + 2s`, over every target and direction

These are for `Actual - PV`, the calibrated device against the readback. The
data file gains `Cycle` and `Direction` (`+1` or `-1`) columns, and the outputs
are named `Verify_..._x<N>`. The plot has three panels:

- `RBV - PV` at each target, as mean and `2s` for each direction, over the band
  of A. The shape is the calibration's systematic error, the gap between the
  directions is the reversal.
- Each reading minus the mean of its target and direction, coloured by cycle.
  The spread is the repeatability, and a steady move from cycle to cycle is
  drift.
- R+, R-, |B| and R at every target, whose worst are the printed figures.

Option              | Effect
:---                | :---
`--compare-pv PV`   | The PV to compare with the motor readback (required)
`--step EGU`        | Step size (default: a 20th of the range)
`--delay SECS`      | Settling time after each move (default 0.5)
`--repeats N`       | Scan there and back `N` times (at least 2) and report repeatability
`--timestamp`, `--no-txt`, `--no-png`, `--no-plot` | As for `scan`

If `--step` doesn't divide the range exactly, the last target falls short of
`STOP`. Avoid a step that is a multiple of a periodic error in the device, such
as a leadscrew's pitch, because every target then lands at the same phase and
the error doesn't show.

## Development

```
uv sync
uv run pytest
```

The scan code reaches EPICS through `cothread.catools`, which is imported lazily
so the tests, `--help` and the `calibrate` command all work without an IOC. The
test suite substitutes a fake Channel Access layer, so a full scan can be
exercised without hardware.
