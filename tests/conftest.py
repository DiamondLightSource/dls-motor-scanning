"""Shared fixtures.

The scanning code reaches EPICS through ``cothread.catools``, which needs a
live IOC. :func:`fake_ca` installs a stand-in so the scan can be exercised end
to end without one.
"""

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest


class TimestampedFloat(float):
    """Stands in for the augmented float cothread returns under FORMAT_TIME."""

    timestamp: float = 0.0


class FakeCatools:
    """A perfectly repeatable motor, with a small constant following error."""

    FORMAT_TIME = 1

    def __init__(self, following_error: float = 0.001) -> None:
        self.following_error = following_error
        self.position = 0.0
        self.clock = 1786000000.0
        self.puts: list[tuple[str, Any]] = []

    def _read(self, pv: str, fmt: int) -> Any:
        if pv.endswith(".RBV"):
            self.clock += 1.25
            value = TimestampedFloat(self.position - self.following_error)
            if fmt == self.FORMAT_TIME:
                value.timestamp = round(self.clock, 6)
            return value
        if pv.endswith(".EGU"):
            return "mm"
        if pv.endswith(".UEIP"):
            return 0
        if pv.endswith(".VELO"):
            return 15.0
        if pv.endswith(".ACCL"):
            return 0.5
        return 42.0

    def caget(self, pvs: str | list[str], format: int = 0, **kwargs: Any) -> Any:  # noqa: A002
        if isinstance(pvs, list):
            return [self._read(pv, format) for pv in pvs]
        return self._read(pvs, format)

    def caput(self, pv: str, value: Any, **kwargs: Any) -> None:
        self.puts.append((pv, value))
        if pv.endswith(".VAL"):
            self.position = float(value)


@pytest.fixture
def fake_ca(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeCatools]:
    """Install a fake ``cothread.catools`` for the duration of a test."""
    catools = FakeCatools()
    package = types.ModuleType("cothread")
    package.catools = catools  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cothread", package)
    monkeypatch.setitem(sys.modules, "cothread.catools", catools)
    yield catools
