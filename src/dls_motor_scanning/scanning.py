"""Step scanning of an EPICS motor record.

The motor is driven from ``start`` to ``stop`` in fixed steps. At each step the
readback position and the time the move took are recorded, so that positioning
accuracy and repeatability can be characterised.

Channel Access and matplotlib are imported lazily by the functions that need
them, so this module can be imported (and ``--help`` rendered) in environments
without EPICS or a display.
"""

import datetime
import os
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import import_module
from math import sqrt
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

__all__ = [
    "MotorInfo",
    "ScanConfig",
    "ScanPoint",
    "ScanSummary",
    "Statistics",
    "perform_scan",
    "summarise_scan",
]

TIMEOUT = 100
"""Seconds to allow for any single Channel Access get or put."""

PV_VAL = ".VAL"
PV_RBV = ".RBV"
PV_EGU = ".EGU"
PV_UEIP = ".UEIP"
PV_VELO = ".VELO"
PV_ACCL = ".ACCL"


@dataclass(frozen=True)
class ScanConfig:
    """Everything needed to run a scan, as gathered from the command line."""

    motor: str
    start: float
    stop: float
    step: float
    delay: float
    extra_pv: str | None = None
    trigger_pv: str | None = None
    trigger_width: float = 1.0
    trigger_post_delay: float = 0.0
    timestamp: bool = False
    write_txt: bool = True
    save_png: bool = True
    show_plot: bool = True


@dataclass(frozen=True)
class MotorInfo:
    """Motor record fields recorded alongside the scan for context."""

    ueip: str
    velo: str
    accl: str
    egu: str


@dataclass(frozen=True)
class ScanPoint:
    """A single step of the scan."""

    demand: float
    actual: float
    move_time: float
    extra: float | None = None
    timestamp: str | None = None

    @property
    def error(self) -> float:
        """Demand position minus actual position, signed."""
        return self.demand - self.actual


@dataclass(frozen=True)
class Statistics:
    """Population statistics for a set of measurements."""

    mean: float
    sd: float
    error_on_mean: float
    minimum: float
    maximum: float


@dataclass(frozen=True)
class ScanSummary:
    """Aggregate results of a scan.

    ``error_magnitude`` summarises the *absolute* position errors, matching how
    the tool has always reported the mean and standard deviation, while
    ``min_error`` and ``max_error`` are the signed extremes.
    """

    move_time: Statistics
    error_magnitude: Statistics
    min_error: float
    max_error: float


def summarise(values: Sequence[float]) -> Statistics:
    """Summarise ``values`` using population statistics.

    The error on the mean is the standard deviation divided by the square root
    of the sample count.
    """
    if not values:
        raise ValueError("cannot summarise an empty set of values")
    mean = fmean(values)
    sd = pstdev(values, mu=mean)
    return Statistics(
        mean=mean,
        sd=sd,
        error_on_mean=sd / sqrt(len(values)),
        minimum=min(values),
        maximum=max(values),
    )


def summarise_scan(points: Sequence[ScanPoint]) -> ScanSummary:
    """Reduce the recorded scan points to the reported statistics."""
    errors = [point.error for point in points]
    return ScanSummary(
        move_time=summarise([point.move_time for point in points]),
        error_magnitude=summarise([abs(error) for error in errors]),
        min_error=min(errors),
        max_error=max(errors),
    )


def plan_scan(config: ScanConfig) -> tuple[int, float]:
    """Work out how many steps to take, and the signed step to take them in.

    Returns the number of points and the step size with its sign set by the
    scan direction. Raises :class:`ValueError` if the request is impossible,
    rather than failing later with a division by zero.
    """
    if config.step == 0:
        raise ValueError("step must not be zero")
    span = abs(config.stop - config.start)
    if span == 0:
        raise ValueError("start and stop must be different positions")
    n_points = int(span / abs(config.step))
    if n_points == 0:
        raise ValueError(f"step ({config.step}) is larger than the scan range ({span})")
    signed_step = abs(config.step) if config.stop >= config.start else -abs(config.step)
    return n_points, signed_step


def scan_filename(config: ScanConfig, started: datetime.datetime) -> str:
    """Build the base name shared by the txt and png outputs."""
    date_text = started.strftime("%Y-%m-%d-%H:%M:%S")
    return (
        f"Scan_{config.motor}_{date_text}_"
        f"{config.start}_{config.stop}_{abs(config.step)}"
    )


def _catools() -> Any:
    """Import cothread's Channel Access helpers on demand.

    Kept lazy so that importing this module, and rendering ``--help``, does not
    require a working EPICS installation.
    """
    from cothread import catools

    return catools


def read_motor_info(motor: str) -> MotorInfo:
    """Read the motor fields recorded alongside the scan.

    All four fields are fetched in a single Channel Access round trip.
    """
    ca = _catools()
    fields = [motor + PV_UEIP, motor + PV_VELO, motor + PV_ACCL, motor + PV_EGU]
    ueip, velo, accl, egu = ca.caget(fields)
    return MotorInfo(ueip=str(ueip), velo=str(velo), accl=str(accl), egu=str(egu))


def _utc_timestamp(value: Any) -> str:
    """Render the EPICS timestamp carried by ``value`` as ISO 8601 UTC."""
    return datetime.datetime.fromtimestamp(value.timestamp, tz=datetime.UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def move_to_start(config: ScanConfig) -> None:
    """Drive the motor to the start of the scan."""
    ca = _catools()
    print(f"Moving to start position of {config.start}")
    ca.caput(config.motor + PV_VAL, config.start, wait=True, timeout=TIMEOUT)


def scan_steps(
    config: ScanConfig, n_points: int, signed_step: float
) -> Iterator[ScanPoint]:
    """Step the motor through the scan, yielding a :class:`ScanPoint` per step."""
    import time

    ca = _catools()
    for i in range(n_points):
        demand = config.start + ((i + 1) * signed_step)

        # Move a step, timing how long the motion takes to complete
        start_time = time.time()
        ca.caput(config.motor + PV_VAL, demand, wait=True, timeout=TIMEOUT)
        move_time = time.time() - start_time

        # Wait for the motor to settle before reading back
        if config.delay > 0:
            time.sleep(config.delay)

        stamp = None
        if config.timestamp:
            # FORMAT_TIME augments the value with the record's EPICS timestamp
            actual = ca.caget(config.motor + PV_RBV, format=ca.FORMAT_TIME)
            stamp = _utc_timestamp(actual)
        else:
            actual = ca.caget(config.motor + PV_RBV)

        extra = ca.caget(config.extra_pv) if config.extra_pv else None

        if config.trigger_pv:
            ca.caput(config.trigger_pv, 1, wait=True, timeout=TIMEOUT)
            time.sleep(config.trigger_width)
            ca.caput(config.trigger_pv, 0, wait=True, timeout=TIMEOUT)
            time.sleep(config.trigger_post_delay)

        yield ScanPoint(
            demand=demand,
            actual=float(actual),
            move_time=move_time,
            extra=None if extra is None else float(extra),
            timestamp=stamp,
        )


def headings(config: ScanConfig) -> list[str]:
    """Column headings for the recorded data."""
    columns = ["Desired", "Actual", "MoveTime"]
    if config.extra_pv:
        columns.append(config.extra_pv)
    if config.timestamp:
        columns.append("Timestamp(UTC)")
    return columns


def row(point: ScanPoint) -> list[str]:
    """One row of recorded data, in the same column order as :func:`headings`."""
    fields = [str(point.demand), str(point.actual), str(point.move_time)]
    if point.extra is not None:
        fields.append(str(point.extra))
    if point.timestamp is not None:
        fields.append(point.timestamp)
    return fields


def print_summary(
    config: ScanConfig,
    info: MotorInfo,
    signed_step: float,
    n_points: int,
    started: datetime.datetime,
    summary: ScanSummary,
) -> None:
    """Print the statistics block that follows the scan."""
    move_time = summary.move_time
    error = summary.error_magnitude
    print("\n**********************************************")
    print(
        f"  Moving {config.motor} from {config.start} to {config.stop} "
        f"in steps of {signed_step}"
    )
    print(f"  Date: {started.strftime('%Y-%m-%d-%H:%M:%S')}")
    print(f"  Number of points in the scan: {n_points}")
    print(f"  UEIP:{info.ueip} VELO:{info.velo} ACCL:{info.accl}")
    print("**********************************************")
    print("Time taken for moves:")
    print(f"  Mean: {move_time.mean} +/- {move_time.error_on_mean} secs")
    print(f"  Standard Deviation: {move_time.sd}")
    print(f"  Min: {move_time.minimum} secs")
    print(f"  Max: {move_time.maximum} secs")
    print("**********************************************")
    print(
        "Position error magnitude at the end of each move "
        "(taking into account settling time delay)"
    )
    print(f"  Mean: {error.mean} +/- {error.error_on_mean}")
    print(f"  Standard Deviation: {error.sd}")
    print(f"  Min Pos Error: {summary.min_error}")
    print(f"  Max Pos Error: {summary.max_error}")
    print(f"  Delay: {config.delay} secs\n")


def _padded(low: float, high: float) -> tuple[float, float]:
    """Axis limits that are always a non-empty interval.

    Matplotlib warns when the lower and upper limits are identical, which
    happens for a perfect motor or an unchanging extra PV.
    """
    if low == high:
        return low - 1.0, high + 1.0
    return low, high


def interactive_backend() -> str | None:
    """Return a GUI backend that is safe to use here, or None if there is none.

    Only Qt will do. cothread runs the GUI event loop, and ``cothread/qt.py``
    supports PyQt6/5/4 and nothing else; matplotlib left to choose for itself
    falls through its candidate list to ``tkagg``, which segfaults the
    interpreter once cothread has been imported. Rather than risk that, this
    checks for a working Qt binding up front and reports honestly when it is
    missing, so the caller can skip the plot instead of dying.
    """
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    for binding in ("PyQt6.QtWidgets", "PyQt5.QtWidgets"):
        try:
            import_module(binding)
        except ImportError:
            # Either the binding is not installed, or its shared libraries
            # (libGL and friends) are not present on this machine.
            continue
        return "qtagg"
    return None


def build_figure(
    config: ScanConfig,
    info: MotorInfo,
    points: Sequence[ScanPoint],
    started: datetime.datetime,
    summary: ScanSummary,
) -> Any:
    """Build the multi-panel figure of position error and move time.

    Whichever backend the caller has selected is used as-is.
    """
    import matplotlib.pyplot as plt

    errors = [point.error for point in points]
    move_times = [point.move_time for point in points]

    n_rows = 3 if config.extra_pv else 2
    fig, axes = plt.subplots(n_rows, 1, figsize=(8.27, 11.69))
    # matplotlib annotates suptitle's **kwargs as Unknown, which strict mode flags
    fig.suptitle(  # pyright: ignore[reportUnknownMemberType]
        f"Step Scanning {config.motor}\n"
        f"Start={config.start} Stop={config.stop} "
        f"Step={abs(config.step)} Delay={config.delay}\n"
        f"{started.strftime('%Y-%m-%d-%H:%M:%S')}\n"
        f" UEIP:{info.ueip} VELO:{info.velo} ACCL:{info.accl}",
        fontsize=14,
    )

    # Position error against step, on axes symmetric about zero
    limit = max(abs(summary.min_error), abs(summary.max_error)) or 1.0
    error_axes = axes[0]
    error_axes.plot(errors)
    error_axes.set_ylim(-limit * 1.1, limit * 1.1)
    error_axes.set_ylabel(f"Demand Position - Actual Position ({info.egu})")
    error_axes.set_xlabel("Step")
    error_axes.text(
        len(points) / 5,
        limit * 0.9,
        f"Mean={summary.error_magnitude.mean}"
        f"+/-{summary.error_magnitude.error_on_mean}\n"
        f"SD={summary.error_magnitude.sd}",
        horizontalalignment="left",
        verticalalignment="top",
    )
    error_axes.axhline(0, linestyle="--", color="black")

    # Time taken for each move
    time_axes = axes[1]
    time_axes.plot(move_times, color="r")
    time_axes.set_ylim(*_padded(0, summary.move_time.maximum * 1.1))
    time_axes.set_ylabel("Time Taken For Move (Seconds)")
    time_axes.set_xlabel("Step")
    time_axes.text(
        len(points) / 5,
        summary.move_time.maximum / 6,
        f"Mean={summary.move_time.mean}+/-{summary.move_time.error_on_mean}\n"
        f"SD={summary.move_time.sd}",
        horizontalalignment="left",
        verticalalignment="top",
    )

    # The extra PV against the position actually reached
    if config.extra_pv:
        actuals = [point.actual for point in points]
        extras = [point.extra for point in points if point.extra is not None]
        extra_axes = axes[2]
        extra_axes.plot(actuals, extras, color="b")
        extra_axes.set_ylim(*_padded(min(extras), max(extras)))
        extra_axes.set_xlim(*_padded(min(actuals), max(actuals)))
        extra_axes.set_ylabel(config.extra_pv)
        extra_axes.set_xlabel(f"Actual Position ({info.egu})")

    return fig


def perform_scan(config: ScanConfig) -> list[ScanPoint]:
    """Run a complete scan, writing and plotting whichever outputs are enabled.

    Returns the recorded points so that callers can do their own analysis.
    """
    n_points, signed_step = plan_scan(config)

    print("Motor step scanning...")
    print(
        f"Moving {config.motor} from {config.start} to {config.stop} "
        f"in steps of {signed_step}"
    )
    print(f"Number of points in the scan: {n_points}")

    started = datetime.datetime.now()
    basename = scan_filename(config, started)
    info = read_motor_info(config.motor)
    move_to_start(config)

    points: list[ScanPoint] = []
    heading = " ".join(headings(config))
    with ExitStack() as stack:
        txt = (
            stack.enter_context(Path(f"{basename}.txt").open("w"))
            if config.write_txt
            else None
        )
        print(heading)
        if txt is not None:
            txt.write(f"{heading}\n")
        for point in scan_steps(config, n_points, signed_step):
            line = " ".join(row(point))
            print(line)
            if txt is not None:
                txt.write(f"{line}\n")
            points.append(point)

    summary = summarise_scan(points)
    print_summary(config, info, signed_step, n_points, started, summary)

    # The png is written first, under Agg, so that it survives even if setting
    # up the interactive window afterwards fails or crashes the interpreter.
    if config.save_png:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig = build_figure(config, info, points, started, summary)
        fig.savefig(f"{basename}.png")
        plt.close(fig)
        print(f"  Plot saved in {basename}.png")
    if config.write_txt:
        print(f"  Data saved in {basename}.txt\n")

    if config.show_plot:
        import matplotlib

        backend = interactive_backend()
        if backend is None:
            print(
                "  No usable Qt backend (needs PyQt5 or PyQt6 and a display), "
                "so the interactive plot has been skipped"
            )
        else:
            matplotlib.use(backend)
            import matplotlib.pyplot as plt

            build_figure(config, info, points, started, summary)
            # matplotlib annotates show's **kwargs as Unknown, which strict
            # mode flags
            plt.show()  # pyright: ignore[reportUnknownMemberType]

    return points
