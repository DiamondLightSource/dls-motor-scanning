"""Predicting what a scan will do before it is run.

A scan's moves are laid out against time using the motor record's velocity and
acceleration, so that the GUI can preview the sawtooth of position against
time, count the moves and estimate how long the scan will take.

The motion is modelled as a trapezoid: ``ACCL`` seconds to reach ``VELO``, a
cruise, then ``ACCL`` seconds to stop, or a triangle if the move is too short
to reach full speed. A real motor takes longer, as the controller settles in
position and Channel Access completes the put, so a fixed overhead per move is
added.
"""

from dataclasses import dataclass
from math import sqrt

from .scanning import ScanConfig, Target, plan_targets

__all__ = [
    "MotionProfile",
    "Plan",
    "format_duration",
    "move_duration",
    "plan_scan_timeline",
]


@dataclass(frozen=True)
class MotionProfile:
    """How the motor moves, for estimating how long a move takes."""

    velocity: float
    """The motor record's VELO, in EGU per second."""
    acceleration_time: float
    """The motor record's ACCL, the seconds taken to reach VELO."""
    overhead: float = 0.0
    """Seconds each move takes beyond the ideal trapezoid."""


def move_duration(distance: float, profile: MotionProfile) -> float:
    """Seconds to move ``distance``, including the overhead.

    Raises :class:`ValueError` for a velocity that isn't positive, which would
    never arrive.
    """
    if profile.velocity <= 0:
        raise ValueError("the velocity must be positive to estimate the time")
    distance = abs(distance)
    if distance == 0:
        return profile.overhead
    accl = max(profile.acceleration_time, 0.0)
    if distance >= profile.velocity * accl:
        # Reaches full speed: ACCL to speed up and to slow down, cruise between
        motion = distance / profile.velocity + accl
    else:
        # Too short to reach full speed: accelerate for half, decelerate for half
        motion = 2 * sqrt(distance * accl / profile.velocity)
    return motion + profile.overhead


@dataclass(frozen=True)
class Plan:
    """What a scan will do, laid out against time from when it is started."""

    targets: list[Target]
    """The targets measured at, in order, after the move to the start."""
    path: list[tuple[float, float]]
    """(seconds, position) vertices of the motor's path, for plotting."""
    readings: list[tuple[float, float]]
    """(seconds, position) at which each target is read."""

    @property
    def moves(self) -> int:
        """Every move the scan makes, including the one to the start."""
        return len(self.targets) + 1

    @property
    def duration(self) -> float:
        """Seconds from starting the scan to its last reading."""
        return self.path[-1][0]

    def path_until(self, readings: int) -> list[tuple[float, float]]:
        """The part of :attr:`path` done once ``readings`` have been taken.

        With none taken, the motor is still on its way to the start. After
        that, the path has two vertices per reading, arriving and then waiting
        out the delay, following the start and the move to it.
        """
        if readings <= 0:
            return self.path[:1]
        return self.path[: 2 + 2 * min(readings, len(self.readings))]


def plan_scan_timeline(
    config: ScanConfig, profile: MotionProfile, position: float | None = None
) -> Plan:
    """Lay out a scan against time.

    ``position`` is where the motor is now; without it the scan is assumed to
    start at ``config.start``. Each move takes :func:`move_duration`, then the
    settling delay is waited out before the reading. Raises
    :class:`ValueError` if the scan is impossible.
    """
    targets = plan_targets(config)
    here = config.start if position is None else position
    now = 0.0
    path = [(now, here)]

    now += move_duration(config.start - here, profile)
    here = config.start
    path.append((now, here))

    readings: list[tuple[float, float]] = []
    for target in targets:
        now += move_duration(target.demand - here, profile)
        here = target.demand
        path.append((now, here))
        now += config.delay
        path.append((now, here))
        readings.append((now, here))
    return Plan(targets=targets, path=path, readings=readings)


def format_duration(seconds: float) -> str:
    """Render a duration the way a person would say it, e.g. ``1 h 05 min``."""
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"
