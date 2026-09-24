"""Bridge desktop colour preferences to Qt5, whose default palette may be light.

On Linux read the XDG Settings portal asynchronously, falling back to GNOME's
settings on desktops without a portal. Other platforms retain Qt's native
palette. Explicit Light/Dark choices also work in remote/container sessions.
"""

import sys
from typing import Any

from PyQt5 import QtCore, QtGui, QtWidgets


def dark_palette() -> QtGui.QPalette:
    """A complete dark widget palette, including readable disabled controls."""
    palette = QtGui.QPalette()
    colors = {
        "Window": "#292929",
        "WindowText": "#eeeeee",
        "Base": "#202020",
        "AlternateBase": "#343434",
        "Text": "#eeeeee",
        "Button": "#383838",
        "ButtonText": "#eeeeee",
        "ToolTipBase": "#eeeeee",
        "ToolTipText": "#202020",
        "BrightText": "#ffffff",
        "Highlight": "#3578b8",
        "HighlightedText": "#ffffff",
        "Link": "#78baff",
        "LinkVisited": "#c6a0f6",
        "Light": "#555555",
        "Midlight": "#444444",
        "Mid": "#333333",
        "Dark": "#181818",
        "Shadow": "#101010",
    }
    for name, color in colors.items():
        palette.setColor(getattr(QtGui.QPalette.ColorRole, name), QtGui.QColor(color))
    for role in (
        QtGui.QPalette.ColorRole.Text,
        QtGui.QPalette.ColorRole.WindowText,
        QtGui.QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled, role, QtGui.QColor("#999999")
        )
    return palette


class Appearance(QtCore.QObject):
    """Keep System mode in sync without blocking window startup on D-Bus."""

    def __init__(self, parent: QtCore.QObject) -> None:
        super().__init__(parent)
        self.native_palette = QtWidgets.QApplication.palette()
        style = QtWidgets.QApplication.style()
        self.native_style = style.objectName() if style is not None else "Fusion"
        self.preference: bool | None = None
        self.applied: bool | None = None
        self.mode = "System"
        self.busy = False
        self.settings = QtCore.QSettings("DiamondLightSource", "dls-motor-scanning")
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.select(str(self.settings.value("appearance", "System")))

    def select(self, mode: str) -> None:
        self.mode = mode if mode in ("System", "Light", "Dark") else "System"
        self.settings.setValue("appearance", self.mode)
        self.apply()
        if self.mode == "System":
            self.refresh()

    def apply(self) -> None:
        dark = self.preference if self.mode == "System" else self.mode == "Dark"
        if dark == self.applied:
            return
        self.applied = dark
        if dark is None:
            QtWidgets.QApplication.setStyle(self.native_style)
            QtWidgets.QApplication.setPalette(self.native_palette)
        else:
            style = QtWidgets.QStyleFactory.create("Fusion")
            if style is not None:
                QtWidgets.QApplication.setStyle(style)
                QtWidgets.QApplication.setPalette(
                    dark_palette() if dark else style.standardPalette()
                )

    def refresh(self) -> None:
        if self.mode != "System" or self.busy or not sys.platform.startswith("linux"):
            return
        self.busy = True
        try:
            from PyQt5 import QtDBus
        except ImportError:
            self.read_gnome()
            return
        message = QtDBus.QDBusMessage.createMethodCall(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Settings",
            "Read",
        )
        message.setArguments(["org.freedesktop.appearance", "color-scheme"])
        call = QtDBus.QDBusConnection.sessionBus().asyncCall(message, 1000)
        watcher = QtDBus.QDBusPendingCallWatcher(call, self)
        watcher.finished.connect(self.portal_finished)

    def portal_finished(self, watcher: Any) -> None:
        from PyQt5 import QtDBus

        reply = QtDBus.QDBusPendingReply(watcher)
        value = reply.value() if not reply.isError() else None
        # Older portal versions wrap the returned variant twice.
        while isinstance(value, QtDBus.QDBusVariant):
            value = value.variant()
        watcher.deleteLater()
        # XDG: 0 = no preference, 1 = dark, 2 = light.
        if value in (1, 2):
            self.detected(value == 1)
        else:
            self.read_gnome()

    def read_gnome(self) -> None:
        process = QtCore.QProcess(self)
        process.setProgram("gsettings")
        process.setArguments(["list-recursively", "org.gnome.desktop.interface"])
        timeout = QtCore.QTimer(process)
        timeout.setSingleShot(True)
        timeout.timeout.connect(process.kill)

        def finished() -> None:
            timeout.stop()
            output = bytes(process.readAllStandardOutput()).decode(errors="replace")
            preference = None
            if process.exitCode() == 0:
                if "prefer-dark" in output:
                    preference = True
                elif "prefer-light" in output:
                    preference = False
                else:
                    themes = [
                        line for line in output.splitlines() if " gtk-theme " in line
                    ]
                    if themes:
                        preference = True if "dark" in themes[0].lower() else None
            process.deleteLater()
            self.detected(preference)

        def failed(error: QtCore.QProcess.ProcessError) -> None:
            if error == QtCore.QProcess.ProcessError.FailedToStart:
                timeout.stop()
                process.deleteLater()
                self.detected(None)

        process.finished.connect(finished)
        process.errorOccurred.connect(failed)
        process.start()
        timeout.start(1000)

    def detected(self, preference: bool | None) -> None:
        self.busy = False
        self.preference = preference
        self.apply()
