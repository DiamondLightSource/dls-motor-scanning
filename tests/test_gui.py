"""Offscreen checks of the combined workflow and desktop palette handling."""

import os
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5.QtWidgets", exc_type=ImportError)

from dls_motor_scanning.gui import MainWindow, QtGui, QtWidgets  # noqa: E402
from dls_motor_scanning.scanning import ScanPoint  # noqa: E402


@pytest.fixture
def window(tmp_path: Any, monkeypatch: Any):
    from dls_motor_scanning.appearance import Appearance

    # Tests control preference changes without querying a live desktop or
    # writing the user's saved appearance choice.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def no_desktop_query(self: Any) -> None:
        pass

    monkeypatch.setattr(Appearance, "refresh", no_desktop_query)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    app.processEvents()
    yield window
    window.appearance.select("System")
    window.appearance.detected(None)
    window.close()
    window.deleteLater()
    app.processEvents()


def test_scan_calibration_and_reset(window: Any):
    assert window.tabs.count() == 2
    scan = window.scan_panel
    panel = scan.calibration_panel
    points = [
        ScanPoint(demand=i, actual=i - 0.01, move_time=0.1, extra=100 + 200 * i)
        for i in range(10)
    ]
    panel.offer_scan(points, "SIM:RAW", "deg")
    assert 'INPA="SIM:RAW"' in panel.output_text.toPlainText()
    assert 'EGU="deg"' in panel.output_text.toPlainText()
    assert len(panel.canvas.figure.axes) == 2
    panel.offer_scan(points[:2], "SIM:RAW", "deg")
    assert "Calibration unavailable" in panel.output_text.toPlainText()
    assert not panel.canvas.figure.axes
    panel.clear_scan()
    assert not panel.output_text.toPlainText()


def test_canvas_follows_light_and_dark_palettes(window: Any):
    from matplotlib.colors import to_hex

    original = QtWidgets.QApplication.palette()
    try:
        for background, text in [("#202020", "#eeeeee"), ("#eeeeee", "#202020")]:
            palette = QtGui.QPalette(original)
            palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(background))
            palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(text))
            QtWidgets.QApplication.setPalette(palette)
            canvas = window.scan_panel.plan_canvas
            canvas.draw()
            assert to_hex(canvas.figure.get_facecolor()) == background
            assert canvas.figure.axes[0].xaxis.label.get_color() == text
    finally:
        QtWidgets.QApplication.setPalette(original)


def test_parameter_column_fits_form(window: Any):
    scroll = window.scan_panel.form_panel
    assert scroll.width() >= scroll.widget().sizeHint().width()
    assert scroll.maximumWidth() > 480


@pytest.mark.parametrize("characterise_feedback", [False, True])
def test_gui_disables_automatic_files(window: Any, characterise_feedback: bool):
    panel = (
        window.characterise_feedback_panel
        if characterise_feedback
        else window.scan_panel
    )
    arguments = panel._arguments(panel._config())
    assert all(flag in arguments for flag in ("--no-txt", "--no-png", "--no-plot"))


def test_save_displayed_data_round_trips(window: Any, tmp_path: Any, monkeypatch: Any):
    import datetime

    from dls_motor_scanning.scanning import ScanConfig, read_scan_file, scan_filename

    panel = window.scan_panel
    config = ScanConfig(
        motor="SIM:Y", start=0, stop=2, step=1, delay=0, extra_pv="SIM:RAW"
    )
    points = [
        ScanPoint(demand=i, actual=i - 0.01, move_time=0.1, extra=i * 200)
        for i in (1, 2)
    ]
    started = datetime.datetime(2026, 9, 24, 12)
    panel._show(config, points, started)
    assert panel.save_button.isEnabled()
    destination = tmp_path / f"{scan_filename(config, started)}.txt"

    def choose_file(*args: Any) -> tuple[str, str]:
        return str(destination), ""

    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", choose_file)
    panel._save_data()
    assert read_scan_file(destination).points == points
    assert list(tmp_path.iterdir()) == [destination]
    panel._show(config, [], started)
    assert not panel.save_button.isEnabled()


def test_cancel_save_creates_no_files(window: Any, tmp_path: Any, monkeypatch: Any):
    import datetime

    panel = window.scan_panel
    panel._show(
        panel._config(),
        [ScanPoint(demand=1, actual=1, move_time=0.1)],
        datetime.datetime.now(),
    )
    monkeypatch.chdir(tmp_path)

    def cancel(*args: Any) -> tuple[str, str]:
        return "", ""

    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", cancel)
    panel._save_data()
    assert not list(tmp_path.iterdir())


def test_system_dark_mode_and_explicit_override(window: Any):
    appearance = window.appearance
    appearance.select("System")
    appearance.detected(True)
    palette = QtWidgets.QApplication.palette()
    assert palette.color(QtGui.QPalette.ColorRole.Window).lightness() < 128
    appearance.select("Light")
    appearance.detected(True)
    assert (
        QtWidgets.QApplication.palette()
        .color(QtGui.QPalette.ColorRole.Window)
        .lightness()
        > 128
    )
    appearance.select("System")
    assert (
        QtWidgets.QApplication.palette()
        .color(QtGui.QPalette.ColorRole.Window)
        .lightness()
        < 128
    )
    appearance.detected(False)
    assert (
        QtWidgets.QApplication.palette()
        .color(QtGui.QPalette.ColorRole.Window)
        .lightness()
        > 128
    )


def test_height_grows_to_fit_form_and_respects_screen(window: Any, monkeypatch: Any):
    from dls_motor_scanning.gui import QtCore

    class Screen:
        height = 2000

        def availableGeometry(self) -> QtCore.QRect:  # noqa: N802
            return QtCore.QRect(0, 0, 2400, self.height)

    screen = Screen()

    def current_screen() -> Screen:
        return screen

    monkeypatch.setattr(window, "screen", current_screen)
    content = window.scan_panel.form_panel.widget()
    extra = QtWidgets.QLabel("Extra form content")
    extra.setFixedHeight(300)
    content.layout().addWidget(extra)
    window.fit_contents()
    QtWidgets.QApplication.processEvents()
    window.fit_contents()
    scroll = window.scan_panel.form_panel
    required = content.layout().totalHeightForWidth(scroll.viewport().width())
    assert scroll.viewport().height() >= required
    screen.height = 600
    window.fit_contents()
    assert window.frameGeometry().height() <= 600
