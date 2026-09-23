"""A Qt window for verifying a calibration, the ``verify`` command with a face.

The form takes the same settings as ``verify``. As they are edited, the plan
tab previews the motor's position against time, and the number of moves and
the time the scan will take are worked out from the motor's VELO and ACCL plus
a fixed overhead per move. While it
runs, the results, summary and data tabs fill in, and any earlier verify file
can be opened into them.

The window never touches Channel Access itself. Reading the motor and running
the scan are both done by the command line, in a separate process, whose
printed output the window follows. cothread can't share a thread with Qt here:
running Qt's event loop in a coroutine segfaulted, and running cothread's
coroutines under Qt's froze. A separate process also means a crash in either
can't take the other down.
"""

import datetime
import faulthandler
import json
import sys
import time
import traceback
from dataclasses import dataclass
from math import isclose
from pathlib import Path
from types import TracebackType
from typing import Any

# PyQt5 first: matplotlib prefers PyQt6 if it is installed, unless a binding
# has already been imported
from PyQt5 import QtCore, QtGui, QtWidgets

# isort: split
from matplotlib.backends.backend_qt import NavigationToolbar2QT
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from .planning import (
    MotionProfile,
    Plan,
    format_duration,
    plan_scan_timeline,
)
from .scanning import (
    MotorInfo,
    ScanConfig,
    ScanOutput,
    ScanPoint,
    build_verify_figure,
    format_summary,
    headings,
    plan_scan,
    read_scan_file,
    row,
    summarise_scan,
)

__all__ = ["main"]

REDRAW_INTERVAL = 10.0
"""Seconds between redraws of the results while a scan runs."""

STOP_TIMEOUT_MS = 5_000
"""How long a stopped scan gets to exit before it is killed outright."""

ERROR_LINES = 20
"""How many of the scan's last error lines to keep, to show if it fails."""


@dataclass(frozen=True)
class MotorState:
    """What was read from the motor record, for the plan and the limits."""

    position: float
    egu: str
    low_limit: float
    high_limit: float

    @property
    def has_limits(self) -> bool:
        """The motor record treats equal soft limits as no limits at all."""
        return self.low_limit != self.high_limit

    def outside(self, *positions: float) -> bool:
        """Whether any of ``positions`` is outside the soft limits."""
        return self.has_limits and any(
            not self.low_limit <= position <= self.high_limit for position in positions
        )


def _text(data: QtCore.QByteArray) -> str:
    """Decode what a process wrote."""
    return bytes(data).decode(errors="replace")


def _spin(
    value: float, minimum: float, maximum: float, decimals: int, suffix: str = ""
) -> QtWidgets.QDoubleSpinBox:
    box = QtWidgets.QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(decimals)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    return box


class Canvas(FigureCanvasQTAgg):
    """A matplotlib figure embedded in the window, with a zoom/pan toolbar."""

    figure: Figure

    def __init__(self) -> None:
        super().__init__(Figure(layout="tight"))  # pyright: ignore[reportUnknownMemberType]

    def with_toolbar(self) -> QtWidgets.QWidget:
        """This canvas under a toolbar, ready to put in a tab."""
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addWidget(NavigationToolbar2QT(self, widget))
        layout.addWidget(self)
        return widget


class VerifyWindow(QtWidgets.QMainWindow):
    """The main window: settings and plan on the left, tabs of output on the right."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("dls-motor-scanning verify")
        self.motor_state: MotorState | None = None
        self.plan: Plan | None = None
        self.running = False
        self.stop_requested = False
        self.reader: QtCore.QProcess | None = None
        self.reading = ""
        self.scanner: QtCore.QProcess | None = None
        # Following the scan's printed output while it runs
        self.output = ScanOutput()
        self.pending = ""
        self.errors: list[str] = []
        self.scan_plan: Plan | None = None
        # What _mark_progress moves along the plan while a scan runs
        self.plan_axes: Any = None
        self.plan_done: Any = None
        self.plan_now: Any = None
        self.began = self.last_draw = 0.0
        # What the result tabs are showing: a run in progress or an opened file
        self.shown_config: ScanConfig | None = None
        self.shown_points: list[ScanPoint] = []
        self.shown_started = datetime.datetime.now()

        self._build_form()
        self._build_tabs()
        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.form_panel)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self._status("Enter a motor and press Read motor")
        self.resize(1400, 900)
        self._update_plan()

    def show_error(self, message: str) -> None:
        """Report an unexpected error without a modal dialog."""
        self._status(message)
        self.summary_text.appendPlainText(f"\nERROR: {message}")

    def _status(self, message: str) -> None:
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(message)

    # ------------------------------------------------------------------ layout

    def _build_form(self) -> None:
        self.motor_edit = QtWidgets.QLineEdit()
        self.motor_edit.setPlaceholderText("e.g. TS04I-AL-APTR-03:Y")
        self.read_button = QtWidgets.QPushButton("Read motor")
        self.read_button.clicked.connect(self._read_motor)
        self.motor_edit.returnPressed.connect(self._read_motor)
        self.motor_label = QtWidgets.QLabel("Not read")
        self.motor_label.setWordWrap(True)
        self.compare_edit = QtWidgets.QLineEdit()
        self.compare_edit.setPlaceholderText("calibrated PV, e.g. ...:POT")

        motor_row = QtWidgets.QHBoxLayout()
        motor_row.addWidget(self.motor_edit)
        motor_row.addWidget(self.read_button)
        motor_box = QtWidgets.QGroupBox("Motor")
        motor_form = QtWidgets.QFormLayout(motor_box)
        motor_form.addRow("Motor PV", motor_row)
        motor_form.addRow("", self.motor_label)
        motor_form.addRow("Compare PV", self.compare_edit)

        self.start_spin = _spin(-1.0, -1e6, 1e6, 4)
        self.stop_spin = _spin(1.0, -1e6, 1e6, 4)
        self.step_spin = _spin(0.5, 0.0001, 1e6, 4)
        self.repeats_spin = QtWidgets.QSpinBox()
        self.repeats_spin.setRange(1, 1000)
        self.repeats_spin.setValue(5)
        self.repeats_spin.setSpecialValueText("1 (single pass)")
        self.repeats_spin.setKeyboardTracking(False)
        self.delay_spin = _spin(0.5, 0.0, 600.0, 2, " s")
        scan_box = QtWidgets.QGroupBox("Scan")
        scan_form = QtWidgets.QFormLayout(scan_box)
        scan_form.addRow("Start", self.start_spin)
        scan_form.addRow("Stop", self.stop_spin)
        scan_form.addRow("Step", self.step_spin)
        scan_form.addRow("Repeats (there and back)", self.repeats_spin)
        scan_form.addRow("Settling delay", self.delay_spin)

        self.velocity_spin = _spin(1.0, 0.0, 1e6, 4, " EGU/s")
        self.accl_spin = _spin(0.5, 0.0, 600.0, 3, " s")
        self.overhead_spin = _spin(0.0, 0.0, 600.0, 2, " s")
        self.overhead_spin.setToolTip(
            "Time each move takes beyond accelerating, cruising and stopping: "
            "the controller settling in position and the put completing."
        )
        timing_box = QtWidgets.QGroupBox("Timing estimate")
        timing_form = QtWidgets.QFormLayout(timing_box)
        timing_form.addRow("Velocity (VELO)", self.velocity_spin)
        timing_form.addRow("Acceleration (ACCL)", self.accl_spin)
        timing_form.addRow("Overhead per move", self.overhead_spin)

        self.moves_label = QtWidgets.QLabel()
        self.duration_label = QtWidgets.QLabel()
        self.warning_label = QtWidgets.QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #c0392b")
        bold = QtGui.QFont()
        bold.setBold(True)
        self.moves_label.setFont(bold)
        self.duration_label.setFont(bold)
        plan_box = QtWidgets.QGroupBox("Plan")
        plan_form = QtWidgets.QFormLayout(plan_box)
        plan_form.addRow("Moves", self.moves_label)
        plan_form.addRow("Estimated time", self.duration_label)
        plan_form.addRow(self.warning_label)

        self.folder_edit = QtWidgets.QLineEdit(str(Path.cwd()))
        browse = QtWidgets.QPushButton("...")
        browse.setMaximumWidth(30)
        browse.clicked.connect(self._choose_folder)
        folder_row = QtWidgets.QHBoxLayout()
        folder_row.addWidget(self.folder_edit)
        folder_row.addWidget(browse)
        output_box = QtWidgets.QGroupBox("Output folder")
        output_box.setLayout(folder_row)

        self.run_button = QtWidgets.QPushButton("Run")
        self.run_button.clicked.connect(self._run)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._request_stop)
        self.open_button = QtWidgets.QPushButton("Open data file...")
        self.open_button.clicked.connect(self._open_file)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setFormat("%v / %m readings")
        self.progress_label = QtWidgets.QLabel()
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.stop_button)

        self.form_panel = QtWidgets.QWidget()
        panel = QtWidgets.QVBoxLayout(self.form_panel)
        for box in (motor_box, scan_box, timing_box, plan_box, output_box):
            panel.addWidget(box)
        panel.addLayout(buttons)
        panel.addWidget(self.progress)
        panel.addWidget(self.progress_label)
        panel.addWidget(self.open_button)
        panel.addStretch()
        self.form_panel.setMaximumWidth(460)

        self.editable: list[QtWidgets.QWidget] = [
            self.motor_edit,
            self.read_button,
            self.compare_edit,
            self.start_spin,
            self.stop_spin,
            self.step_spin,
            self.repeats_spin,
            self.delay_spin,
            self.velocity_spin,
            self.accl_spin,
            self.overhead_spin,
            self.folder_edit,
            browse,
            self.open_button,
        ]
        for spin in (
            self.start_spin,
            self.stop_spin,
            self.step_spin,
            self.delay_spin,
            self.velocity_spin,
            self.accl_spin,
            self.overhead_spin,
        ):
            spin.valueChanged.connect(self._update_plan)
        self.repeats_spin.valueChanged.connect(self._update_plan)
        self.compare_edit.textChanged.connect(self._update_plan)

    def _build_tabs(self) -> None:
        self.plan_canvas = Canvas()
        self.results_canvas = Canvas()
        self.summary_text = QtWidgets.QPlainTextEdit()
        self.summary_text.setReadOnly(True)
        self.summary_text.setFont(
            QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        )
        self.table = QtWidgets.QTableWidget()
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.plan_canvas.with_toolbar(), "Plan")
        self.tabs.addTab(self.results_canvas.with_toolbar(), "Results")
        self.tabs.addTab(self.summary_text, "Summary")
        self.tabs.addTab(self.table, "Data")

    # ------------------------------------------------------------ the settings

    def _config(self) -> ScanConfig:
        """The scan the form describes, as the verify command would run it."""
        repeats = self.repeats_spin.value()
        return ScanConfig(
            motor=self.motor_edit.text().strip(),
            start=self.start_spin.value(),
            stop=self.stop_spin.value(),
            step=self.step_spin.value(),
            delay=self.delay_spin.value(),
            extra_pv=self.compare_edit.text().strip() or "COMPARE-PV",
            compare=True,
            repeats=None if repeats == 1 else repeats,
            show_plot=False,
        )

    def _profile(self) -> MotionProfile:
        return MotionProfile(
            velocity=self.velocity_spin.value(),
            acceleration_time=self.accl_spin.value(),
            overhead=self.overhead_spin.value(),
        )

    def _egu(self) -> str:
        return self.motor_state.egu if self.motor_state else "EGU"

    def _update_plan(self) -> None:
        """Re-plan from the form, and redraw the preview and the estimates."""
        config = self._config()
        position = self.motor_state.position if self.motor_state else None
        try:
            self.plan = plan_scan_timeline(config, self._profile(), position)
        except ValueError as error:
            self.plan = None
            self.moves_label.setText("-")
            self.duration_label.setText("-")
            self.warning_label.setText(str(error))
            self.run_button.setEnabled(False)
            self._draw_plan()
            return

        n_steps, signed_step = plan_scan(config)
        per_pass = f"{n_steps} steps each way" if config.repeats else ""
        self.moves_label.setText(
            f"{self.plan.moves} ({len(self.plan.targets)} readings"
            f"{', ' + per_pass if per_pass else ''}, plus 1 to the start)"
        )
        finish = datetime.datetime.now() + datetime.timedelta(
            seconds=self.plan.duration
        )
        self.duration_label.setText(
            f"{format_duration(self.plan.duration)}, "
            f"finishing about {finish.strftime('%H:%M')}"
        )

        warnings: list[str] = []
        last = config.start + n_steps * signed_step
        if not isclose(last, config.stop, abs_tol=1e-9):
            warnings.append(
                f"The step doesn't divide the range: the last target is {last:g}."
            )
        state = self.motor_state
        if state and state.outside(config.start, config.stop):
            warnings.append(
                f"The range is outside the soft limits "
                f"{state.low_limit:g} to {state.high_limit:g}."
            )
        if not self.compare_edit.text().strip():
            warnings.append("Enter the PV to compare with the readback.")
        if not config.motor:
            warnings.append("Enter the motor PV.")
        self.warning_label.setText("\n".join(warnings))
        self.run_button.setEnabled(
            not self.running
            and bool(config.motor)
            and bool(self.compare_edit.text().strip())
            and not (state and state.outside(config.start, config.stop))
        )
        self._draw_plan()

    def _draw_plan(self) -> None:
        figure = self.plan_canvas.figure
        figure.clear()
        axes: Any = figure.subplots()
        self.plan_axes = axes
        self.plan_done = self.plan_now = None
        # While a scan runs, show the plan it is following
        if self.running and self.scan_plan is not None:
            self.plan = self.scan_plan
        if self.plan is not None:
            times = [seconds / 60 for seconds, _ in self.plan.path]
            positions = [position for _, position in self.plan.path]
            axes.plot(times, positions, color="tab:blue", linewidth=1)
            axes.plot(
                [seconds / 60 for seconds, _ in self.plan.readings],
                [position for _, position in self.plan.readings],
                linestyle="none",
                marker=".",
                markersize=3,
                color="tab:orange",
                label="reading",
            )
            state = self.motor_state
            if state and state.has_limits:
                for limit in (state.low_limit, state.high_limit):
                    axes.axhline(limit, color="#c0392b", linestyle="--", linewidth=1)
            axes.set_title(
                f"{self.plan.moves} moves, about {format_duration(self.plan.duration)}"
            )
            if self.running:
                # Where the scan has got to, drawn over the plan
                (self.plan_done,) = axes.plot(
                    [], [], color="tab:green", linewidth=3, label="done"
                )
                (self.plan_now,) = axes.plot(
                    [], [], color="tab:red", marker="o", markersize=10, zorder=5
                )
                self._mark_progress(len(self.output.points))
            axes.legend(loc="upper right")
        axes.set_xlabel("Time from start (min)")
        axes.set_ylabel(f"Position ({self._egu()})")
        axes.grid(True, alpha=0.3)
        self.plan_canvas.draw_idle()

    # ---------------------------------------------------------- motor & timing

    def _command(self, *args: str) -> QtCore.QProcess:
        """A process that runs this tool's command line with ``args``."""
        process = QtCore.QProcess(self)
        environment = QtCore.QProcessEnvironment.systemEnvironment()
        # Rows are printed as they are read, so don't let them sit in a buffer
        environment.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(environment)
        process.setProgram(sys.executable)
        process.setArguments(["-m", "dls_motor_scanning", *args])
        return process

    def _read_motor(self) -> None:
        motor = self.motor_edit.text().strip()
        if not motor or self.reader is not None:
            return
        self.read_button.setEnabled(False)
        self._status(f"Reading {motor}...")
        process = self._command("motor-info", motor)
        process.finished.connect(self._reader_finished)
        process.errorOccurred.connect(self._reader_failed)
        self.reader = process
        self.reading = motor
        process.start()

    def _reader_finished(self, code: int, status: QtCore.QProcess.ExitStatus) -> None:
        del status  # a crash shows as a non-zero code too
        self._motor_read(self.reading, code)

    def _reader_failed(self, error: QtCore.QProcess.ProcessError) -> None:
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self._motor_read(self.reading, -1)

    def _motor_read(self, motor: str, code: int) -> None:
        process = self.reader
        self.reader = None
        self.read_button.setEnabled(not self.running)
        if process is None:
            return
        output = _text(process.readAllStandardOutput()).strip()
        errors = _text(process.readAllStandardError()).strip()
        try:
            if code != 0:
                raise ValueError(errors.splitlines()[-1] if errors else f"code {code}")
            state = json.loads(output.splitlines()[-1])
        except (ValueError, IndexError) as error:
            self.motor_state = None
            self.motor_label.setText(f"Could not read {motor}: {error}")
            self._status(f"Could not read {motor}")
            self._update_plan()
            return

        self.motor_state = MotorState(
            position=float(state["position"]),
            egu=str(state["egu"]),
            low_limit=float(state["low_limit"]),
            high_limit=float(state["high_limit"]),
        )
        limits = (
            f"soft limits {self.motor_state.low_limit:g} to "
            f"{self.motor_state.high_limit:g}"
            if self.motor_state.has_limits
            else "no soft limits"
        )
        self.motor_label.setText(
            f"At {self.motor_state.position:g} {self.motor_state.egu}, {limits}"
        )
        self.velocity_spin.setValue(float(state["velocity"]))
        self.accl_spin.setValue(float(state["acceleration_time"]))
        if not self.compare_edit.text().strip():
            self.compare_edit.setText(f"{motor}:POT")
        self._status(f"Read {motor}")
        self._update_plan()

    # ----------------------------------------------------------------- running

    def _folder(self) -> Path:
        return Path(self.folder_edit.text().strip() or ".")

    def _choose_folder(self) -> None:
        name = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Output folder", str(self._folder())
        )
        if name:
            self.folder_edit.setText(name)

    def _set_running(self, running: bool) -> None:
        self.running = running
        for widget in self.editable:
            widget.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.stop_button.setText("Stop")
        self._update_plan()

    def _request_stop(self) -> None:
        """End the scan; the motor still finishes the move it was given.

        Ctrl+C would be politer, but cothread doesn't notice SIGINT while it
        waits on a put. Terminating loses nothing: every row is flushed to the
        txt file as it is read, and the window has them all anyway.
        """
        process = self.scanner
        if process is None:
            return
        self.stop_requested = True
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stopping...")
        process.terminate()
        QtCore.QTimer.singleShot(STOP_TIMEOUT_MS, self._kill_scan)

    def _kill_scan(self) -> None:
        if self.scanner is not None:
            self.scanner.kill()

    def _run(self) -> None:
        if self.plan is None or self.running:
            return
        config = self._config()
        folder = self._folder()
        if not folder.is_dir():
            self.warning_label.setText(f"The output folder {folder} doesn't exist.")
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Run the scan?",
            f"Move {config.motor} through {self.plan.moves} moves between "
            f"{config.start:g} and {config.stop:g}, taking about "
            f"{format_duration(self.plan.duration)}?",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        options = ["--compare-pv", config.extra_pv or "", "--step", str(config.step)]
        options += ["--delay", str(config.delay), "--no-plot"]
        if config.repeats is not None:
            options += ["--repeats", str(config.repeats)]
        # After --, so that a negative start isn't taken for an option
        positions = ["--", config.motor, str(config.start), str(config.stop)]
        process = self._command("verify", *options, *positions)
        process.setWorkingDirectory(str(folder))
        process.readyReadStandardOutput.connect(self._scan_output)
        process.readyReadStandardError.connect(self._scan_errors)
        process.finished.connect(self._scan_finished)
        process.errorOccurred.connect(self._scanner_failed)

        self.output = ScanOutput()
        self.pending = ""
        self.errors = []
        self.scan_plan = self.plan
        self.began = time.monotonic()
        self.last_draw = self.began
        self._show(config, self.output.points, datetime.datetime.now())
        self.progress.setRange(0, len(self.plan.targets))
        self.progress.setValue(0)
        self.progress_label.setText(f"Moving to the start, {config.start:g}")
        self.stop_requested = False
        self.scanner = process
        self._set_running(True)
        self.tabs.setCurrentIndex(0)
        print(f"Running: {' '.join(process.arguments())}", flush=True)
        process.start()

    def _scan_output(self) -> None:
        """Take in what the scan has printed, a reading at a time."""
        if self.scanner is None:
            return
        self.pending += _text(self.scanner.readAllStandardOutput())
        *lines, self.pending = self.pending.split("\n")
        for line in lines:
            self._scan_line(line)
        if time.monotonic() - self.last_draw > REDRAW_INTERVAL:
            self._draw_results()
            self.last_draw = time.monotonic()

    def _scan_line(self, line: str) -> None:
        print(line, flush=True)
        point = self.output.feed(line)
        if point is not None and self.shown_config is not None:
            self._add_table_row(self.shown_config, point)
            self._show_progress(len(self.output.points) - 1)
            self._mark_progress(len(self.output.points))

    def _scan_errors(self) -> None:
        if self.scanner is None:
            return
        text = _text(self.scanner.readAllStandardError())
        sys.stderr.write(text)
        self.errors = (self.errors + text.splitlines())[-ERROR_LINES:]

    def _scanner_failed(self, error: QtCore.QProcess.ProcessError) -> None:
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self.errors.append(f"Could not start {sys.executable}")
            self._scan_finished(-1, QtCore.QProcess.ExitStatus.CrashExit)

    def _mark_progress(self, readings: int) -> None:
        """Move the progress drawn on the plan on to the latest reading."""
        plan = self.scan_plan
        if plan is None or self.plan_done is None or self.plan_now is None:
            return
        done = plan.path_until(readings)
        self.plan_done.set_data(
            [seconds / 60 for seconds, _ in done], [position for _, position in done]
        )
        seconds, position = done[-1]
        self.plan_now.set_data([seconds / 60], [position])
        if self.plan_axes is not None:
            self.plan_axes.set_title(
                f"Reading {readings} of {len(plan.readings)}"
                if readings
                else f"Moving to the start, {position:g}"
            )
        self.plan_canvas.draw_idle()

    def _show_progress(self, index: int) -> None:
        """Count the readings, and re-estimate what's left from the pace so far."""
        plan = self.scan_plan
        if plan is None or index >= len(plan.readings):
            return
        self.progress.setValue(index + 1)
        elapsed = time.monotonic() - self.began
        predicted = plan.readings[index][0]
        pace = elapsed / predicted if predicted > 0 else 1.0
        remaining = (plan.duration - predicted) * pace
        finish = datetime.datetime.now() + datetime.timedelta(seconds=remaining)
        self.progress_label.setText(
            f"{format_duration(elapsed)} elapsed, about "
            f"{format_duration(remaining)} left (finishing {finish:%H:%M})"
        )

    def _scan_finished(self, code: int, status: QtCore.QProcess.ExitStatus) -> None:
        del status  # a crash shows as a non-zero code too
        if self.scanner is None:
            return
        self._scan_output()
        if self.pending:
            self._scan_line(self.pending)
            self.pending = ""
        self.scanner = None
        stopped = self.stop_requested
        points = self.output.points
        config = self.shown_config

        if self.output.summary:
            text = "\n".join(self.output.summary)
        elif points and config is not None:
            # Stopped or failed before the command printed its own summary
            summary = summarise_scan(points, compare=True)
            _, signed_step = plan_scan(config)
            info = MotorInfo(ueip="?", velo="?", accl="?", egu=self._egu())
            text = format_summary(
                config, info, signed_step, len(points), self.shown_started, summary
            )
        else:
            text = "No readings were taken."
        if stopped:
            text = f"STOPPED after {len(points)} readings\n{text}"
        elif code != 0:
            detail = "\n".join(self.errors)
            text = f"SCAN FAILED (exit code {code})\n{detail}\n\n{text}"
        self.summary_text.setPlainText(text)
        self._draw_results()

        saved = f", saved {self.output.data_file}" if self.output.data_file else ""
        outcome = "Stopped" if stopped else "Failed" if code != 0 else "Finished"
        self._status(f"{outcome} after {len(points)} readings{saved}")
        self.progress_label.setText(f"{outcome} after {len(points)} readings")
        self._set_running(False)
        self.tabs.setCurrentIndex(1 if points and code == 0 else 2)

    # -------------------------------------------------------- viewing the data

    def _open_file(self) -> None:
        name, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open verify data",
            str(self._folder()),
            "Verify data (Verify_*.txt);;All files (*)",
        )
        if not name:
            return
        try:
            recorded = read_scan_file(Path(name))
            if not recorded.config.compare:
                raise ValueError("it has no comparison column, so isn't from verify")
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, "Can't open", f"{name}: {error}")
            return

        config = recorded.config
        started = recorded.started or datetime.datetime.now()
        self._show(config, recorded.points, started)
        for point in recorded.points:
            self._add_table_row(config, point)
        self._draw_results()
        summary = summarise_scan(recorded.points, compare=True)
        _, signed_step = plan_scan(config)
        info = MotorInfo(ueip="?", velo="?", accl="?", egu=self._egu())
        self.summary_text.setPlainText(
            format_summary(
                config, info, signed_step, len(recorded.points), started, summary
            )
        )
        # Fill in the form, so the same scan can be run again
        self.motor_edit.setText(config.motor)
        self.compare_edit.setText(config.extra_pv or "")
        self.start_spin.setValue(config.start)
        self.stop_spin.setValue(config.stop)
        self.step_spin.setValue(config.step)
        self.repeats_spin.setValue(config.repeats or 1)
        self.tabs.setCurrentIndex(1)
        self._status(f"Opened {Path(name).name}")

    def _show(
        self, config: ScanConfig, points: list[ScanPoint], started: datetime.datetime
    ) -> None:
        """Point the result tabs at a scan, and clear them."""
        self.shown_config = config
        self.shown_points = points
        self.shown_started = started
        self.summary_text.clear()
        columns = headings(config)
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.results_canvas.figure.clear()
        self.results_canvas.draw_idle()

    def _add_table_row(self, config: ScanConfig, point: ScanPoint) -> None:
        index = self.table.rowCount()
        self.table.insertRow(index)
        for column, text in enumerate(row(point, config.compare)):
            self.table.setItem(index, column, QtWidgets.QTableWidgetItem(text))
        self.table.scrollToBottom()

    def _draw_results(self) -> None:
        config = self.shown_config
        if config is None or not self.shown_points:
            return
        info = MotorInfo(ueip="?", velo="?", accl="?", egu=self._egu())
        summary = summarise_scan(self.shown_points, compare=True)
        build_verify_figure(
            config,
            info,
            self.shown_points,
            self.shown_started,
            summary,
            self.results_canvas.figure,
        )
        self.results_canvas.draw_idle()

    def closeEvent(self, a0: QtGui.QCloseEvent | None) -> None:  # noqa: N802
        """Ask before closing mid-scan, and stop the scan if the answer is yes."""
        if self.running and a0 is not None:
            answer = QtWidgets.QMessageBox.question(
                self,
                "Scan running",
                "A scan is running. Closing stops it: the motor finishes its "
                "current move, and the readings so far are kept in the txt "
                "file. Close anyway?",
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                a0.ignore()
                return
            self._request_stop()
            if self.scanner is not None and not self.scanner.waitForFinished(
                STOP_TIMEOUT_MS
            ):
                self.scanner.kill()
        super().closeEvent(a0)


def _report_uncaught(
    kind: type[BaseException], error: BaseException, trace: TracebackType | None
) -> None:
    """Print an error from a Qt callback, rather than let PyQt5 abort on it."""
    traceback.print_exception(kind, error, trace)
    for window in _windows:
        window.show_error(f"{kind.__name__}: {error}")


_windows: list[VerifyWindow] = []
"""The open windows, to report uncaught errors in."""


def main() -> None:
    """Open the window, and run until it is closed."""
    faulthandler.enable()
    sys.excepthook = _report_uncaught
    app = QtWidgets.QApplication(sys.argv)
    window = VerifyWindow()
    _windows.append(window)
    window.show()
    app.exec_()
