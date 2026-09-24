"""A Qt window for scanning, automatic calibration and feedback characterisation.

Scan and Characterise feedback each have settings, a motion preview and results.
Scans with an extra PV automatically fit raw feedback against motor readback
in the Scan tab's Calibration page. Existing scan files and CSVs can also be
fitted there. Calibration generates formulas; it never writes them to an IOC.

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
from dataclasses import dataclass, replace
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
from matplotlib.text import Text

from .appearance import Appearance
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
    build_figure,
    format_summary,
    headings,
    plan_scan,
    read_scan_file,
    row,
    scan_filename,
    summarise_scan,
)

__all__ = ["main"]

REDRAW_INTERVAL = 10.0
"""Seconds between redraws of the results while a scan runs."""

STOP_TIMEOUT_MS = 5_000
"""How long a stopped scan gets to exit before it is killed outright."""

ERROR_LINES = 20
"""How many of the scan's last error lines to keep, to show if it fails."""

AUTOMATIC = "(automatic)"
"""The column choice that lets calibrate pick the column itself."""


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


def _fixed_font_text() -> QtWidgets.QPlainTextEdit:
    text = QtWidgets.QPlainTextEdit()
    text.setReadOnly(True)
    text.setFont(
        QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
    )
    return text


class Canvas(FigureCanvasQTAgg):
    """A matplotlib figure embedded in the window, with a zoom/pan toolbar."""

    figure: Figure

    def __init__(self) -> None:
        super().__init__(Figure(layout="tight"))  # pyright: ignore[reportUnknownMemberType]
        app = QtWidgets.QApplication.instance()
        if isinstance(app, QtWidgets.QApplication):
            app.paletteChanged.connect(self.draw_idle)

    def draw(self) -> None:
        """Follow the current desktop palette, including after a theme change."""
        palette = QtWidgets.QApplication.palette()
        background = palette.color(QtGui.QPalette.ColorRole.Window).name()
        base = palette.color(QtGui.QPalette.ColorRole.Base).name()
        foreground = palette.color(QtGui.QPalette.ColorRole.WindowText).name()
        self.figure.set_facecolor(background)
        for axes in self.figure.axes:
            axes.set_facecolor(base)
            axes.tick_params(colors=foreground)  # pyright: ignore[reportUnknownMemberType]
            for spine in axes.spines.values():
                spine.set_edgecolor(foreground)
            for line in axes.lines:
                if line.get_color() == "black" or line.get_gid() == "palette-text":
                    line.set_gid("palette-text")
                    line.set_color(foreground)
            legend = axes.get_legend()
            if legend is not None:
                legend.get_frame().set_facecolor(background)
                legend.get_frame().set_edgecolor(foreground)
        for text in self.figure.findobj(Text):
            text.set_color(foreground)
        super().draw()

    def with_toolbar(self) -> QtWidgets.QWidget:
        """This canvas under a toolbar, ready to put in a tab."""
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addWidget(NavigationToolbar2QT(self, widget))
        layout.addWidget(self)
        return widget


class ScanPanel(QtWidgets.QWidget):
    """Scan or characterise feedback, with settings on the left and output tabs
    on the right.

    With ``characterise-feedback`` it compares a PV with the readback, and
    can repeat the scan there and back; otherwise it runs ``scan``, with an
    optional extra PV and trigger PV.
    """

    def __init__(self, window: "MainWindow", characterise_feedback: bool) -> None:
        super().__init__()
        self.window_ = window
        self.characterise_feedback = characterise_feedback
        self.command = "characterise-feedback" if characterise_feedback else "scan"
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
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.update_plan()

    def show_error(self, message: str) -> None:
        """Report an unexpected error without a modal dialog."""
        self.summary_text.appendPlainText(f"\nERROR: {message}")

    def _status(self, message: str) -> None:
        self.window_.status(message)

    # ------------------------------------------------------------------ layout

    def _build_form(self) -> None:
        self.motor_edit = QtWidgets.QLineEdit()
        self.motor_edit.setPlaceholderText("e.g. TS04I-AL-APTR-03:Y")
        self.read_button = QtWidgets.QPushButton("Read motor")
        self.read_button.clicked.connect(self._read_motor)
        self.motor_edit.returnPressed.connect(self._read_motor)
        self.motor_label = QtWidgets.QLabel("Not read")
        self.motor_label.setWordWrap(True)
        self.extra_edit = QtWidgets.QLineEdit()
        self.motor_edit.setMinimumWidth(
            self.motor_edit.fontMetrics().horizontalAdvance("TS04I-AL-APTR-03:Y") + 24
        )
        if self.characterise_feedback:
            self.extra_edit.setPlaceholderText("calibrated PV, e.g. ...:POT")
        else:
            self.extra_edit.setPlaceholderText("optional raw PV to calibrate")
        self.trigger_edit = QtWidgets.QLineEdit()
        self.trigger_edit.setPlaceholderText("optional, pulsed after each reading")

        motor_row = QtWidgets.QHBoxLayout()
        motor_row.addWidget(self.motor_edit)
        motor_row.addWidget(self.read_button)
        motor_box = QtWidgets.QGroupBox("Motor")
        motor_form = QtWidgets.QFormLayout(motor_box)
        motor_form.addRow("Motor PV", motor_row)
        motor_form.addRow("", self.motor_label)
        motor_form.addRow(
            "Feedback PV" if self.characterise_feedback else "Extra PV", self.extra_edit
        )
        if not self.characterise_feedback:
            motor_form.addRow("Trigger PV", self.trigger_edit)

        self.start_spin = _spin(-1.0, -1e6, 1e6, 4)
        self.stop_spin = _spin(1.0, -1e6, 1e6, 4)
        self.step_spin = _spin(0.5, 0.0001, 1e6, 4)
        self.repeats_spin = QtWidgets.QSpinBox()
        self.repeats_spin.setRange(1, 1000)
        self.repeats_spin.setValue(5)
        self.repeats_spin.setSpecialValueText("1 (single pass)")
        self.repeats_spin.setKeyboardTracking(False)
        self.delay_spin = _spin(0.5, 0.0, 600.0, 2, " s")
        self.trigger_width_spin = _spin(1.0, 0.0, 600.0, 2, " s")
        self.trigger_post_spin = _spin(0.0, 0.0, 600.0, 2, " s")
        self.timestamp_check = QtWidgets.QCheckBox("Add a UTC timestamp column")
        scan_box = QtWidgets.QGroupBox("Scan")
        scan_form = QtWidgets.QFormLayout(scan_box)
        scan_form.addRow("Start", self.start_spin)
        scan_form.addRow("Stop", self.stop_spin)
        scan_form.addRow("Step", self.step_spin)
        if self.characterise_feedback:
            scan_form.addRow("Repeats (there and back)", self.repeats_spin)
        scan_form.addRow("Settling delay", self.delay_spin)
        if not self.characterise_feedback:
            scan_form.addRow("Trigger width", self.trigger_width_spin)
            scan_form.addRow("After trigger", self.trigger_post_spin)
        scan_form.addRow(self.timestamp_check)

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
        self.moves_label.setWordWrap(True)
        self.duration_label.setWordWrap(True)
        self.warning_label = QtWidgets.QLabel()
        self.warning_label.setWordWrap(True)

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
        output_box = QtWidgets.QGroupBox("Default save folder")
        output_box.setLayout(folder_row)

        self.run_button = QtWidgets.QPushButton("Run")
        self.run_button.clicked.connect(self._run)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.request_stop)
        self.open_button = QtWidgets.QPushButton("Open data file...")
        self.open_button.clicked.connect(self._open_file)
        self.save_button = QtWidgets.QPushButton("Save data...")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_data)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setFormat("%v / %m readings")
        self.progress_label = QtWidgets.QLabel()
        self.progress_label.setWordWrap(True)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.stop_button)

        form_content = QtWidgets.QWidget()
        panel = QtWidgets.QVBoxLayout(form_content)
        for box in (motor_box, scan_box, timing_box, plan_box, output_box):
            panel.addWidget(box)
        panel.addLayout(buttons)
        panel.addWidget(self.progress)
        panel.addWidget(self.progress_label)
        panel.addWidget(self.open_button)
        panel.addWidget(self.save_button)
        panel.addStretch()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(form_content)
        scroll.setWidgetResizable(True)
        # QScrollArea otherwise advertises a small default size to the splitter.
        width = form_content.sizeHint().width()
        style = self.style()
        if style is not None:
            width += style.pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ScrollBarExtent)
        width += 2 * scroll.frameWidth()
        scroll.setMinimumWidth(width)
        scroll.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Expanding
        )
        self.form_panel = scroll

        self.editable: list[QtWidgets.QWidget] = [
            self.motor_edit,
            self.read_button,
            self.extra_edit,
            self.trigger_edit,
            self.start_spin,
            self.stop_spin,
            self.step_spin,
            self.repeats_spin,
            self.delay_spin,
            self.trigger_width_spin,
            self.trigger_post_spin,
            self.timestamp_check,
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
            self.trigger_width_spin,
            self.trigger_post_spin,
            self.velocity_spin,
            self.accl_spin,
            self.overhead_spin,
        ):
            spin.valueChanged.connect(self.update_plan)
        self.repeats_spin.valueChanged.connect(self.update_plan)
        self.extra_edit.textChanged.connect(self.update_plan)
        self.trigger_edit.textChanged.connect(self.update_plan)
        self.motor_edit.textChanged.connect(self.update_plan)

    def _build_tabs(self) -> None:
        self.plan_canvas = Canvas()
        self.results_canvas = Canvas()
        self.summary_text = _fixed_font_text()
        self.table = QtWidgets.QTableWidget()
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.plan_canvas.with_toolbar(), "Plan")
        self.tabs.addTab(self.results_canvas.with_toolbar(), "Results")
        self.tabs.addTab(self.summary_text, "Summary")
        self.tabs.addTab(self.table, "Data")
        self.calibration_panel: CalibratePanel | None = None
        if not self.characterise_feedback:
            self.calibration_panel = CalibratePanel(self.window_)
            self.tabs.addTab(self.calibration_panel, "Calibration")

    # ------------------------------------------------------------ the settings

    def _config(self) -> ScanConfig:
        """The scan the form describes, as the command would run it."""
        extra_pv = self.extra_edit.text().strip()
        timestamp = self.timestamp_check.isChecked()
        if self.characterise_feedback:
            repeats = self.repeats_spin.value()
            return ScanConfig(
                motor=self.motor_edit.text().strip(),
                start=self.start_spin.value(),
                stop=self.stop_spin.value(),
                step=self.step_spin.value(),
                delay=self.delay_spin.value(),
                extra_pv=extra_pv or "COMPARE-PV",
                compare=True,
                repeats=None if repeats == 1 else repeats,
                timestamp=timestamp,
                show_plot=False,
            )
        return ScanConfig(
            motor=self.motor_edit.text().strip(),
            start=self.start_spin.value(),
            stop=self.stop_spin.value(),
            step=self.step_spin.value(),
            delay=self.delay_spin.value(),
            extra_pv=extra_pv or None,
            trigger_pv=self.trigger_edit.text().strip() or None,
            trigger_width=self.trigger_width_spin.value(),
            trigger_post_delay=self.trigger_post_spin.value(),
            timestamp=timestamp,
            show_plot=False,
        )

    def _timeline_config(self, config: ScanConfig) -> ScanConfig:
        """``config`` with the trigger's time folded into the settling delay,
        which is how long each reading holds the motor still."""
        if not config.trigger_pv:
            return config
        return replace(
            config,
            delay=config.delay + config.trigger_width + config.trigger_post_delay,
        )

    def _profile(self) -> MotionProfile:
        return MotionProfile(
            velocity=self.velocity_spin.value(),
            acceleration_time=self.accl_spin.value(),
            overhead=self.overhead_spin.value(),
        )

    def _egu(self) -> str:
        return self.motor_state.egu if self.motor_state else "EGU"

    def update_plan(self) -> None:
        """Re-plan from the form, and redraw the preview and the estimates."""
        config = self._config()
        position = self.motor_state.position if self.motor_state else None
        try:
            self.plan = plan_scan_timeline(
                self._timeline_config(config), self._profile(), position
            )
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
        needs_extra = self.characterise_feedback and not self.extra_edit.text().strip()
        if needs_extra:
            warnings.append("Enter the PV to compare with the readback.")
        if not config.motor:
            warnings.append("Enter the motor PV.")
        other = self.window_.running_panel()
        if other is not None and other is not self:
            warnings.append(f"A {other.command} scan is running in another tab.")
        self.warning_label.setText("\n".join(warnings))
        self.run_button.setEnabled(
            not self.running
            and other is None
            and bool(config.motor)
            and not needs_extra
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

    def _read_motor(self) -> None:
        motor = self.motor_edit.text().strip()
        if not motor or self.reader is not None:
            return
        self.read_button.setEnabled(False)
        self._status(f"Reading {motor}...")
        process = self.window_.command("motor-info", motor)
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
            self.update_plan()
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
        if self.characterise_feedback and not self.extra_edit.text().strip():
            self.extra_edit.setText(f"{motor}:POT")
        self._status(f"Read {motor}")
        self.update_plan()

    # ----------------------------------------------------------------- running

    def _folder(self) -> Path:
        return Path(self.folder_edit.text().strip() or ".")

    def _choose_folder(self) -> None:
        name = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Default save folder", str(self._folder())
        )
        if name:
            self.folder_edit.setText(name)

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.save_button.setEnabled(not running and bool(self.shown_points))
        for widget in self.editable:
            widget.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.stop_button.setText("Stop")
        self.window_.running_changed()

    def request_stop(self) -> None:
        """End the scan; the motor still finishes the move it was given.

        Ctrl+C would be politer, but cothread doesn't notice SIGINT while it
        waits on a put. The window retains readings already received for saving
        after stopping.
        """
        process = self.scanner
        if process is None:
            return
        self.stop_requested = True
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stopping...")
        process.terminate()
        QtCore.QTimer.singleShot(STOP_TIMEOUT_MS, lambda: self._kill_scan(process))

    def _kill_scan(self, process: QtCore.QProcess) -> None:
        if self.scanner is process:
            process.kill()

    def _arguments(self, config: ScanConfig) -> list[str]:
        """The command line that runs ``config``."""
        if self.characterise_feedback:
            options = [
                "--compare-pv",
                config.extra_pv or "",
                "--step",
                str(config.step),
            ]
            options += ["--delay", str(config.delay)]
            if config.repeats is not None:
                options += ["--repeats", str(config.repeats)]
            # After --, so that a negative start isn't taken for an option
            positions = ["--", config.motor, str(config.start), str(config.stop)]
        else:
            options: list[str] = []
            if config.extra_pv:
                options += ["--extra-pv", config.extra_pv]
            if config.trigger_pv:
                options += ["--trigger-pv", config.trigger_pv]
                options += ["--trigger-width", str(config.trigger_width)]
                options += ["--trigger-post-delay", str(config.trigger_post_delay)]
            positions = [
                "--",
                config.motor,
                str(config.start),
                str(config.stop),
                str(config.step),
                str(config.delay),
            ]
        options.extend(["--no-plot", "--no-txt", "--no-png"])
        if config.timestamp:
            options.append("--timestamp")
        return [self.command, *options, *positions]

    def _run(self) -> None:
        if self.plan is None or self.running or self.window_.running_panel():
            return
        config = self._config()
        answer = QtWidgets.QMessageBox.question(
            self,
            "Run the scan?",
            f"Move {config.motor} through {self.plan.moves} moves between "
            f"{config.start:g} and {config.stop:g}, taking about "
            f"{format_duration(self.plan.duration)}?",
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        process = self.window_.command(*self._arguments(config))
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

    def _info(self) -> MotorInfo:
        return MotorInfo(ueip="?", velo="?", accl="?", egu=self._egu())

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
            summary = summarise_scan(points, compare=self.characterise_feedback)
            _, signed_step = plan_scan(config)
            text = format_summary(
                config,
                self._info(),
                signed_step,
                len(points),
                self.shown_started,
                summary,
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
        # Calibrate only a complete, successful scan.
        if (
            not self.characterise_feedback
            and code == 0
            and not stopped
            and config is not None
            and config.extra_pv
        ):
            self._calibrate_scan(config, points)

    def _calibrate_scan(self, config: ScanConfig, points: list[ScanPoint]) -> None:
        if self.calibration_panel is not None and config.extra_pv:
            self.calibration_panel.offer_scan(points, config.extra_pv, self._egu())
            self.tabs.setCurrentWidget(self.calibration_panel)

    # -------------------------------------------------------- viewing the data

    def _save_data(self) -> None:
        """Save the displayed readings only when the user chooses a destination."""
        config = self.shown_config
        if self.running or config is None or not self.shown_points:
            return
        suggested = self._folder() / f"{scan_filename(config, self.shown_started)}.txt"
        name, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save scan data", str(suggested), "Scan data (*.txt)"
        )
        if not name:
            return
        path = Path(name)
        if not path.suffix:
            path = path.with_suffix(".txt")
        lines = [" ".join(headings(config))]
        lines.extend(
            " ".join(row(point, config.compare)) for point in self.shown_points
        )
        try:
            path.write_text("\n".join(lines) + "\n")
        except OSError as error:
            QtWidgets.QMessageBox.warning(self, "Can't save data", str(error))
            return
        self._status(f"Saved {len(self.shown_points)} readings to {path}")

    def _open_file(self) -> None:
        prefix = "CharacteriseFeedback" if self.characterise_feedback else "Scan"
        name, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            f"Open {self.command} data",
            str(self._folder()),
            f"{prefix} data ({prefix}_*.txt"
            + (" Verify_*.txt" if self.characterise_feedback else "")
            + ");;All files (*)",
        )
        if not name:
            return
        try:
            recorded = read_scan_file(Path(name))
            if self.characterise_feedback and not recorded.config.compare:
                raise ValueError("This file has no feedback comparison column")
            if not self.characterise_feedback and recorded.config.compare:
                raise ValueError("Open this file in the Characterise feedback tab")
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, "Can't open", f"{name}: {error}")
            return

        config = recorded.config
        started = recorded.started or datetime.datetime.now()
        self._show(config, recorded.points, started)
        for point in recorded.points:
            self._add_table_row(config, point)
        self._draw_results()
        summary = summarise_scan(recorded.points, compare=self.characterise_feedback)
        _, signed_step = plan_scan(config)
        self.summary_text.setPlainText(
            format_summary(
                config,
                self._info(),
                signed_step,
                len(recorded.points),
                started,
                summary,
            )
        )
        # Fill in the form, so the same scan can be run again
        self.motor_edit.setText(config.motor)
        self.extra_edit.setText(config.extra_pv or "")
        self.start_spin.setValue(config.start)
        self.stop_spin.setValue(config.stop)
        self.step_spin.setValue(config.step)
        self.repeats_spin.setValue(config.repeats or 1)
        self.timestamp_check.setChecked(config.timestamp)
        self.tabs.setCurrentIndex(1)
        self._status(f"Opened {Path(name).name}")
        self._calibrate_scan(config, recorded.points)

    def _show(
        self, config: ScanConfig, points: list[ScanPoint], started: datetime.datetime
    ) -> None:
        """Point the result tabs at a scan, and clear them."""
        self.shown_config = config
        self.shown_points = points
        self.save_button.setEnabled(not self.running and bool(points))
        self.shown_started = started
        self.summary_text.clear()
        if self.calibration_panel is not None:
            self.calibration_panel.clear_scan()
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
        summary = summarise_scan(self.shown_points, compare=self.characterise_feedback)
        build_figure(
            config,
            self._info(),
            self.shown_points,
            self.shown_started,
            summary,
            self.results_canvas.figure,
        )
        self.results_canvas.draw_idle()


class CalibratePanel(QtWidgets.QWidget):
    """The calibrate command: fit a data file, and show the fit and its output.

    Fitting is plain numpy on a file, so unlike a scan it runs in the window.
    """

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_ = window
        self.scan_data: Any = None

        self.file_edit = QtWidgets.QLineEdit()
        self.file_edit.setPlaceholderText("a Scan_*.txt with an extra PV, or a CSV")
        self.file_edit.editingFinished.connect(self._load_columns)
        browse = QtWidgets.QPushButton("...")
        browse.setMaximumWidth(30)
        browse.clicked.connect(self._choose_file)
        file_row = QtWidgets.QHBoxLayout()
        file_row.addWidget(self.file_edit)
        file_row.addWidget(browse)

        self.raw_combo = QtWidgets.QComboBox()
        self.position_combo = QtWidgets.QComboBox()
        for combo in (self.raw_combo, self.position_combo):
            combo.addItem(AUTOMATIC)
        self.raw_pv_edit = QtWidgets.QLineEdit()
        self.raw_pv_edit.setPlaceholderText("defaults to the raw column")
        self.egu_edit = QtWidgets.QLineEdit("mm")
        self.cell_edit = QtWidgets.QLineEdit("A1")
        self.fit_button = QtWidgets.QPushButton("Fit")
        self.fit_button.clicked.connect(self.fit)

        box = QtWidgets.QGroupBox("Calibrate")
        form = QtWidgets.QFormLayout(box)
        form.addRow("Data file", file_row)
        form.addRow("Raw column", self.raw_combo)
        form.addRow("Position column", self.position_combo)
        form.addRow("Raw input PV", self.raw_pv_edit)
        form.addRow("EGU", self.egu_edit)
        form.addRow("Excel cell", self.cell_edit)
        form.addRow(self.fit_button)
        note = QtWidgets.QLabel(
            "Fits a 5th order polynomial converting raw feedback into EGUs, "
            "and prints an EPICS calc record and an Excel formula. A scan with "
            "an extra PV is calibrated here automatically when it finishes."
        )
        note.setWordWrap(True)

        form_panel = QtWidgets.QWidget()
        panel = QtWidgets.QVBoxLayout(form_panel)
        panel.addWidget(box)
        panel.addWidget(note)
        panel.addStretch()
        form_panel.setMinimumWidth(form_panel.sizeHint().width())

        self.canvas = Canvas()
        self.output_text = _fixed_font_text()
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.canvas.with_toolbar(), "Fit")
        self.tabs.addTab(self.output_text, "Output")

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(form_panel)
        splitter.addWidget(self.tabs)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    def show_error(self, message: str) -> None:
        """Report an unexpected error without a modal dialog."""
        self.output_text.appendPlainText(f"\nERROR: {message}")

    def _path(self) -> Path:
        return Path(self.file_edit.text().strip())

    def _choose_file(self) -> None:
        start = self.file_edit.text().strip() or str(Path.cwd())
        name, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open calibration data",
            start,
            "Scan data (Scan_*.txt);;CSV (*.csv);;All files (*)",
        )
        if name:
            self.file_edit.setText(name)
            self._load_columns()
            self.fit()

    def clear_scan(self) -> None:
        self.scan_data = None
        self.output_text.clear()
        self.canvas.figure.clear()
        self.canvas.draw_idle()
        self.file_edit.clear()

    def offer_scan(self, points: list[ScanPoint], raw_pv: str, egu: str) -> None:
        from .calibration import scan_readings

        self.scan_data = scan_readings(points, raw_pv)
        self.file_edit.clear()
        self.file_edit.setPlaceholderText("Current scan (or choose an existing file)")
        self.raw_pv_edit.setText(raw_pv)
        self.egu_edit.setText(egu)
        for combo in (self.raw_combo, self.position_combo):
            combo.setCurrentIndex(0)
        self.fit()

    def offer_file(self, path: Path) -> None:
        """Load a scan that has just finished, ready to fit."""
        self.file_edit.setText(str(path))
        self._load_columns()
        self.fit()

    def _load_columns(self) -> None:
        """Offer the file's columns in the column choices."""
        from .calibration import _read_table  # pyright: ignore[reportPrivateUsage]

        path = self._path()
        try:
            columns = [str(column) for column in _read_table(path).columns]
        except Exception:  # anything pandas can raise on a bad file
            columns = []
        for combo in (self.raw_combo, self.position_combo):
            current = combo.currentText()
            combo.clear()
            combo.addItem(AUTOMATIC)
            combo.addItems(columns)
            if current in columns:
                combo.setCurrentText(current)

    def _column(self, combo: QtWidgets.QComboBox) -> str | None:
        text = combo.currentText()
        return None if text == AUTOMATIC else text

    def fit(self) -> None:
        """Fit the file, and show the fit, the residuals and the output."""
        from .calibration import (
            calc_record,
            excel_formula,
            fit_readings,
            load_readings,
            max_residual,
        )

        path = self._path()
        if self.scan_data is None and not path.is_file():
            self.output_text.setPlainText(f"No data file at {path}")
            self.tabs.setCurrentIndex(1)
            return
        egu = self.egu_edit.text().strip() or "mm"
        try:
            readings = (
                load_readings(
                    path,
                    self._column(self.raw_combo),
                    self._column(self.position_combo),
                )
                if self.file_edit.text().strip() or self.scan_data is None
                else self.scan_data
            )
            calibration = fit_readings(readings)
        except (OSError, ValueError) as error:
            self.output_text.setPlainText(f"Calibration unavailable:\n{error}")
            self.canvas.figure.clear()
            self.canvas.draw_idle()
            self.tabs.setCurrentIndex(1)
            self.window_.status(f"Can't fit {path.name}")
            return

        worst = max_residual(calibration, readings)
        lines = [
            f"Fitted {readings.position_column} against {readings.raw_column} "
            f"over {len(readings.raw)} readings, max residual {worst:.3e} {egu}",
            "",
        ]
        for name, coefficient in zip("BCDEFG", calibration.ascending, strict=True):
            lines.append(f"{name} = {coefficient:.10e}")
        raw_pv = self.raw_pv_edit.text().strip() or readings.raw_column
        lines += ["", "EPICS calc record:", calc_record(calibration, raw_pv, egu)]
        cell = self.cell_edit.text().strip() or "A1"
        lines += ["", "Excel formula:", excel_formula(calibration, cell)]
        self.output_text.setPlainText("\n".join(lines))
        self._draw(readings, calibration, egu)
        self.tabs.setCurrentIndex(0)
        self.window_.status(f"Fitted {path.name}, max residual {worst:.3e} {egu}")

    def _draw(self, readings: Any, calibration: Any, egu: str) -> None:
        import numpy as np
        from numpy.polynomial import Polynomial

        polynomial = Polynomial(calibration.ascending)
        raw = np.asarray(readings.raw, dtype=float)
        position = np.asarray(readings.position, dtype=float)
        smooth: Any = np.linspace(raw.min(), raw.max(), 500)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

        figure = self.canvas.figure
        figure.clear()
        fit_axes, residual_axes = figure.subplots(2, 1, sharex=True)
        fit_axes.plot(raw, position, ".", color="tab:blue", label="readings")
        fit_axes.plot(smooth, polynomial(smooth), color="tab:orange", label="fit")  # pyright: ignore[reportUnknownArgumentType]
        fit_axes.set_ylabel(f"{readings.position_column} ({egu})")
        fit_axes.set_title(f"{readings.position_column} against {readings.raw_column}")
        fit_axes.legend(loc="best")
        fit_axes.grid(True, alpha=0.3)
        residual_axes.plot(raw, position - polynomial(raw), ".", color="tab:red")
        residual_axes.axhline(0, color="black", linestyle="--", linewidth=1)
        residual_axes.set_ylabel(f"Reading - fit ({egu})")
        residual_axes.set_xlabel(readings.raw_column)
        residual_axes.grid(True, alpha=0.3)
        self.canvas.draw_idle()


class MainWindow(QtWidgets.QMainWindow):
    """Scan (including calibration) and Characterise feedback, sharing a status bar."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("dls-motor-scanning")
        # The panels ask about each other while they are built
        self.scan_panels: list[ScanPanel] = []
        self.scan_panel = ScanPanel(self, characterise_feedback=False)
        self.characterise_feedback_panel = ScanPanel(self, characterise_feedback=True)
        self.scan_panels = [self.scan_panel, self.characterise_feedback_panel]

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.scan_panel, "Scan")
        self.tabs.addTab(self.characterise_feedback_panel, "Characterise feedback")
        self.setCentralWidget(self.tabs)
        self.status("Enter a motor and press Read motor")
        self.appearance = Appearance(self)
        menu_bar = self.menuBar()
        assert menu_bar is not None
        appearance_menu = menu_bar.addMenu("Appearance")
        assert appearance_menu is not None
        self.appearance_actions = QtWidgets.QActionGroup(self)
        for mode in ("System", "Light", "Dark"):
            action = QtWidgets.QAction(mode, self.appearance_actions)
            action.setCheckable(True)
            action.setChecked(mode == self.appearance.mode)
            appearance_menu.addAction(action)  # pyright: ignore[reportUnknownMemberType]
        self.appearance_actions.triggered.connect(self._appearance_selected)

        # A scroll area's own size hint hides the full height of its form.
        # Recalculate after layout changes (wrapping, fonts, theme or tab changes).
        self.fit_timer = QtCore.QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.timeout.connect(self.fit_contents)
        for panel in self.scan_panels:
            content = panel.form_panel.widget()
            if content is not None:
                content.installEventFilter(self)
        self.tabs.currentChanged.connect(self.schedule_fit)
        screen = self.screen()
        available = (
            screen.availableGeometry().size() if screen else QtCore.QSize(1400, 900)
        )
        self.resize(
            self.sizeHint().expandedTo(QtCore.QSize(1400, 1)).boundedTo(available)
        )

    def _appearance_selected(self, action: QtWidgets.QAction) -> None:
        self.appearance.select(action.text())
        self.schedule_fit()

    def schedule_fit(self) -> None:
        self.fit_timer.start(0)

    def showEvent(self, a0: QtGui.QShowEvent | None) -> None:  # noqa: N802
        super().showEvent(a0)
        self.schedule_fit()

    def eventFilter(  # noqa: N802
        self, a0: QtCore.QObject | None, a1: QtCore.QEvent | None
    ) -> bool:
        if a1 is not None and a1.type() == QtCore.QEvent.Type.LayoutRequest:
            self.schedule_fit()
        return super().eventFilter(a0, a1)

    def fit_contents(self) -> None:
        """Grow to fit the form, but retain scrolling on smaller screens."""
        if not self.isVisible() or self.isMaximized():
            return
        panel = self.scan_panels[self.tabs.currentIndex()]
        scroll = panel.form_panel
        content = scroll.widget()
        viewport = scroll.viewport()
        if content is None or viewport is None:
            return
        layout = content.layout()
        if layout is None:
            return
        layout.activate()
        height = layout.totalHeightForWidth(viewport.width())
        if height < 0:
            height = content.sizeHint().height()
        chrome = self.height() - scroll.height() + 2 * scroll.frameWidth()
        desired = QtCore.QSize(self.width(), max(self.height(), height + chrome))
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            frame = self.frameGeometry().size() - self.size()
            desired = desired.boundedTo(available.size() - frame)
        self.resize(desired)
        if screen is not None:
            available = screen.availableGeometry()
            frame_rect = self.frameGeometry()
            self.move(
                max(
                    available.left(),
                    min(frame_rect.left(), available.right() - frame_rect.width() + 1),
                ),
                max(
                    available.top(),
                    min(frame_rect.top(), available.bottom() - frame_rect.height() + 1),
                ),
            )

    def status(self, message: str) -> None:
        """Show ``message`` in the status bar."""
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(message)

    def show_error(self, message: str) -> None:
        """Report an unexpected error in the tab that is showing."""
        self.status(message)
        page = self.tabs.currentIndex()
        if page < len(self.scan_panels):
            self.scan_panels[page].show_error(message)
        else:
            self.scan_panel.show_error(message)

    def command(self, *args: str) -> QtCore.QProcess:
        """A process that runs this tool's command line with ``args``."""
        process = QtCore.QProcess(self)
        environment = QtCore.QProcessEnvironment.systemEnvironment()
        # Rows are printed as they are read, so don't let them sit in a buffer
        environment.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(environment)
        process.setProgram(sys.executable)
        process.setArguments(["-m", "dls_motor_scanning", *args])
        return process

    def running_panel(self) -> ScanPanel | None:
        """The panel whose scan is running, if any: only one runs at a time."""
        return next((panel for panel in self.scan_panels if panel.running), None)

    def running_changed(self) -> None:
        """Let every panel re-check whether it may run."""
        for panel in self.scan_panels:
            panel.update_plan()

    def closeEvent(self, a0: QtGui.QCloseEvent | None) -> None:  # noqa: N802
        """Ask before closing mid-scan, and stop the scan if the answer is yes."""
        panel = self.running_panel()
        if panel is not None and a0 is not None:
            answer = QtWidgets.QMessageBox.question(
                self,
                "Scan running",
                "A scan is running. Closing stops it: the motor finishes its "
                "current move. Unsaved readings will be lost. Close anyway?",
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                a0.ignore()
                return
            panel.request_stop()
            if panel.scanner is not None and not panel.scanner.waitForFinished(
                STOP_TIMEOUT_MS
            ):
                panel.scanner.kill()
        super().closeEvent(a0)


def _report_uncaught(
    kind: type[BaseException], error: BaseException, trace: TracebackType | None
) -> None:
    """Print an error from a Qt callback, rather than let PyQt5 abort on it."""
    traceback.print_exception(kind, error, trace)
    for window in _windows:
        window.show_error(f"{kind.__name__}: {error}")


_windows: list[MainWindow] = []
"""The open windows, to report uncaught errors in."""


def main() -> None:
    """Open the window, and run until it is closed."""
    faulthandler.enable()
    sys.excepthook = _report_uncaught
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    _windows.append(window)
    window.show()
    app.exec_()
