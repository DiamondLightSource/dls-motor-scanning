"""Command line interface for the DLS motor scanning tools."""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from . import __version__

if TYPE_CHECKING:
    from .scanning import ScanConfig

__all__ = ["app"]

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Tools for characterising the motion performance of EPICS motors.",
)


def _version_callback(value: bool) -> None:
    if value:
        print(__version__)
        raise typer.Exit()


@app.callback()
def cli(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            callback=_version_callback,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    """Tools for characterising the motion performance of EPICS motors."""


# Arguments and options shared by the commands that drive a scan
Motor = Annotated[str, typer.Argument(help="Motor record PV to scan.")]
Start = Annotated[float, typer.Argument(help="Start position, in motor EGUs.")]
Stop = Annotated[float, typer.Argument(help="Stop position, in motor EGUs.")]
Step = Annotated[float, typer.Argument(help="Step size, in motor EGUs.")]
Delay = Annotated[
    float, typer.Argument(help="Settling time in seconds to wait after each move.")
]
Timestamp = Annotated[
    bool,
    typer.Option(
        "--timestamp/--no-timestamp",
        help="Add a UTC timestamp column from the EPICS timestamp of the readback.",
    ),
]
Txt = Annotated[
    bool, typer.Option("--txt/--no-txt", help="Write the raw scan data as txt.")
]
Png = Annotated[bool, typer.Option("--png/--no-png", help="Save the plot as a png.")]
Plot = Annotated[
    bool, typer.Option("--plot/--no-plot", help="Display the plot on screen.")
]


def _run_scan(config: "ScanConfig") -> None:
    from .scanning import perform_scan

    try:
        perform_scan(config)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@app.command()
def scan(
    motor: Motor,
    start: Start,
    stop: Stop,
    step: Step,
    delay: Delay,
    extra_pv: Annotated[
        str | None,
        typer.Option(
            "--extra-pv",
            metavar="PV",
            help="Additional PV to read at each step and plot against position.",
        ),
    ] = None,
    trigger_pv: Annotated[
        str | None,
        typer.Option(
            "--trigger-pv",
            metavar="PV",
            help="PV to pulse high then low after each move.",
        ),
    ] = None,
    trigger_width: Annotated[
        float,
        typer.Option(
            "--trigger-width",
            metavar="SECS",
            help="How long the trigger PV is held high.",
        ),
    ] = 1.0,
    trigger_post_delay: Annotated[
        float,
        typer.Option(
            "--trigger-post-delay",
            metavar="SECS",
            help="How long to wait after the trigger PV returns low.",
        ),
    ] = 0.0,
    timestamp: Timestamp = False,
    txt: Txt = True,
    png: Png = True,
    plot: Plot = True,
) -> None:
    """Step scan a motor and measure its positioning performance.

    The motor is moved from START to STOP in fixed steps. At each step the
    readback position and the time taken for the move are recorded, then
    summarised as statistics, a txt file and a plot.
    """
    from .scanning import ScanConfig

    _run_scan(
        ScanConfig(
            motor=motor,
            start=start,
            stop=stop,
            step=step,
            delay=delay,
            extra_pv=extra_pv,
            trigger_pv=trigger_pv,
            trigger_width=trigger_width,
            trigger_post_delay=trigger_post_delay,
            timestamp=timestamp,
            write_txt=txt,
            save_png=png,
            show_plot=plot,
        )
    )


VERIFY_STEPS = 20
"""How many steps verify divides the range into when no --step is given."""


@app.command()
def verify(
    motor: Motor,
    start: Start,
    stop: Stop,
    compare_pv: Annotated[
        str,
        typer.Option(
            "--compare-pv",
            metavar="PV",
            help="Scaled PV to compare with the motor readback, e.g. a calc record.",
        ),
    ],
    step: Annotated[
        float | None,
        typer.Option(
            "--step",
            metavar="EGU",
            help=f"Step size. Defaults to a {VERIFY_STEPS}th of the range.",
        ),
    ] = None,
    delay: Annotated[
        float,
        typer.Option(
            "--delay",
            metavar="SECS",
            help="Settling time to wait after each move.",
        ),
    ] = 0.5,
    repeats: Annotated[
        int | None,
        typer.Option(
            "--repeats",
            metavar="N",
            min=2,
            help=(
                "Scan there and back N times and report unidirectional and "
                "bidirectional repeatability. Without it, one pass checks accuracy."
            ),
        ),
    ] = None,
    timestamp: Timestamp = False,
    txt: Txt = True,
    png: Png = True,
    plot: Plot = True,
) -> None:
    """Step scan a motor and compare its readback with another PV.

    Use this to verify a calibration against the motor's encoder. Give the
    full range of the stage as START and STOP: one pass reports the readback
    minus --compare-pv at each step, as statistics, a column in the txt file
    and an extra plot. With --repeats, the range is traversed there and back
    that many times, and the repeatability of both that difference and the
    stage's own positioning is reported in the style of ISO 230-2.
    """
    from .scanning import ScanConfig

    _run_scan(
        ScanConfig(
            motor=motor,
            start=start,
            stop=stop,
            step=abs(stop - start) / VERIFY_STEPS if step is None else step,
            delay=delay,
            extra_pv=compare_pv,
            compare=True,
            repeats=repeats,
            timestamp=timestamp,
            write_txt=txt,
            save_png=png,
            show_plot=plot,
        )
    )


@app.command()
def calibrate(
    data_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help=(
                "Scan data file written with --extra-pv, or a CSV of paired raw "
                "feedback and scaled position readings."
            ),
        ),
    ],
    raw_column: Annotated[
        str | None,
        typer.Option(
            "--raw-column",
            metavar="COLUMN",
            help="Column holding the raw feedback. Defaults to a scan's extra PV.",
        ),
    ] = None,
    position_column: Annotated[
        str | None,
        typer.Option(
            "--position-column",
            metavar="COLUMN",
            help="Column holding the scaled position. Defaults to a scan's Actual.",
        ),
    ] = None,
    raw_input_pv: Annotated[
        str | None,
        typer.Option(
            "--raw-input-pv",
            metavar="PV",
            help="Raw input PV read by the calc record. Defaults to the raw column.",
        ),
    ] = None,
    egu: Annotated[
        str,
        typer.Option("--egu", metavar="UNITS", help="EGU of the calc record."),
    ] = "mm",
    excel_cell: Annotated[
        str,
        typer.Option(
            "--excel-cell",
            metavar="CELL",
            help="Cell holding the first raw feedback value in Excel.",
        ),
    ] = "A1",
) -> None:
    """Fit a 5th order polynomial converting raw feedback into EGUs.

    For example, converting a potentiometer from raw ADC counts to EGUs. Given
    the data file from a scan run with --extra-pv, the extra PV is fitted
    against the Actual position with no further options. An EPICS calc record
    and an equivalent Excel formula are printed.
    """
    from .calibration import (
        calc_record,
        excel_formula,
        fit_readings,
        load_readings,
        max_residual,
    )

    try:
        readings = load_readings(data_path, raw_column, position_column)
        calibration = fit_readings(readings)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    worst = max_residual(calibration, readings)
    print(
        f"Fitted {readings.position_column} against {readings.raw_column} "
        f"over {len(readings.raw)} readings, max residual {worst:.3e} {egu}\n"
    )

    for name, coefficient in zip("BCDEFG", calibration.ascending, strict=True):
        print(f"{name} = {coefficient:.10e}")

    print("\nEPICS calc record:")
    print(calc_record(calibration, raw_input_pv or readings.raw_column, egu))

    print("\nExcel formula:")
    print(excel_formula(calibration, excel_cell))
