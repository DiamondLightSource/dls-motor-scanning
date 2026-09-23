"""Command line interface for the DLS motor scanning tools."""

from pathlib import Path
from typing import Annotated

import typer

from . import __version__

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


@app.command()
def scan(
    motor: Annotated[str, typer.Argument(help="Motor record PV to scan.")],
    start: Annotated[float, typer.Argument(help="Start position, in motor EGUs.")],
    stop: Annotated[float, typer.Argument(help="Stop position, in motor EGUs.")],
    step: Annotated[float, typer.Argument(help="Step size, in motor EGUs.")],
    delay: Annotated[
        float, typer.Argument(help="Settling time in seconds to wait after each move.")
    ],
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
    timestamp: Annotated[
        bool,
        typer.Option(
            "--timestamp/--no-timestamp",
            help="Add a UTC timestamp column from the EPICS timestamp of the readback.",
        ),
    ] = False,
    txt: Annotated[
        bool, typer.Option("--txt/--no-txt", help="Write the raw scan data as txt.")
    ] = True,
    png: Annotated[
        bool, typer.Option("--png/--no-png", help="Save the plot as a png.")
    ] = True,
    plot: Annotated[
        bool, typer.Option("--plot/--no-plot", help="Display the plot on screen.")
    ] = True,
) -> None:
    """Step scan a motor and measure its positioning performance.

    The motor is moved from START to STOP in fixed steps. At each step the
    readback position and the time taken for the move are recorded, then
    summarised as statistics, a txt file and a plot.
    """
    from .scanning import ScanConfig, perform_scan

    config = ScanConfig(
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
    try:
        perform_scan(config)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


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
