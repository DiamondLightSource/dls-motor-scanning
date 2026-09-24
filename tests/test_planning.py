"""Tests for predicting a scan's moves and duration."""

import pytest

from dls_motor_scanning.planning import (
    MotionProfile,
    format_duration,
    move_duration,
    plan_scan_timeline,
)
from dls_motor_scanning.scanning import ScanConfig


def characterise_feedback(**overrides: object) -> ScanConfig:
    defaults: dict[str, object] = {
        "motor": "SIM-MO-TEST-01:Y",
        "start": 0.0,
        "stop": 2.0,
        "step": 0.5,
        "delay": 0.5,
        "extra_pv": "SIM-MO-POT-01:POS",
        "compare": True,
    }
    defaults.update(overrides)
    return ScanConfig(**defaults)  # type: ignore[arg-type]


def test_a_long_move_reaches_full_speed():
    # 0.5 s to speed up and 0.5 s to slow down cover 0.5 mm; 1.5 mm cruises
    assert move_duration(2.0, MotionProfile(1.0, 0.5)) == pytest.approx(2.5)


def test_a_short_move_never_reaches_full_speed():
    # 0.25 mm at 2 mm/s^2: 0.125 mm accelerating takes sqrt(0.125) s, then back
    profile = MotionProfile(velocity=1.0, acceleration_time=0.5)
    assert move_duration(0.25, profile) == pytest.approx(2 * 0.125**0.5)


def test_the_two_shapes_meet_where_the_move_just_reaches_full_speed():
    profile = MotionProfile(velocity=2.0, acceleration_time=0.5)
    boundary = profile.velocity * profile.acceleration_time
    below = move_duration(boundary * (1 - 1e-9), profile)
    assert below == pytest.approx(move_duration(boundary, profile))


def test_move_duration_adds_the_overhead_and_ignores_direction():
    profile = MotionProfile(velocity=1.0, acceleration_time=0.0, overhead=2.0)
    assert move_duration(-3.0, profile) == pytest.approx(5.0)
    assert move_duration(0.0, profile) == pytest.approx(2.0)


def test_move_duration_rejects_a_motor_that_cannot_move():
    with pytest.raises(ValueError, match="velocity"):
        move_duration(1.0, MotionProfile(velocity=0.0, acceleration_time=0.5))


def test_plan_counts_the_move_to_the_start():
    plan = plan_scan_timeline(characterise_feedback(repeats=3), MotionProfile(1.0, 0.0))
    # 4 steps up and 4 back, 3 times, plus the move to the start
    assert len(plan.targets) == 24
    assert plan.moves == 25
    assert len(plan.readings) == 24


def test_plan_duration_adds_up_the_moves_and_delays():
    profile = MotionProfile(velocity=1.0, acceleration_time=0.0, overhead=1.0)
    plan = plan_scan_timeline(characterise_feedback(), profile, position=-1.0)
    # 1 mm to the start takes 2 s, then 4 steps of 0.5 s + 1 s + 0.5 s delay
    assert plan.duration == pytest.approx(2.0 + 4 * 2.0)
    assert plan.path[0] == (0.0, -1.0)
    assert plan.path[1] == pytest.approx((2.0, 0.0))
    assert plan.readings[-1] == pytest.approx((10.0, 2.0))


def test_plan_path_is_a_sawtooth():
    plan = plan_scan_timeline(characterise_feedback(repeats=2), MotionProfile(1.0, 0.0))
    positions = [position for _, position in plan.readings]
    assert positions == [0.5, 1.0, 1.5, 2.0, 1.5, 1.0, 0.5, 0.0] * 2
    times = [seconds for seconds, _ in plan.path]
    assert times == sorted(times)


def test_plan_rejects_an_impossible_scan():
    with pytest.raises(ValueError):
        plan_scan_timeline(characterise_feedback(stop=0.0), MotionProfile(1.0, 0.5))


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(42, "42 s"), (61, "1 min 01 s"), (1780, "29 min 40 s"), (3900, "1 h 05 min")],
)
def test_format_duration(seconds: float, text: str):
    assert format_duration(seconds) == text


def test_path_until_follows_the_readings_taken():
    profile = MotionProfile(velocity=1.0, acceleration_time=0.0, overhead=1.0)
    plan = plan_scan_timeline(characterise_feedback(), profile, position=-1.0)

    # None yet: still where it started, on the way to the start
    assert plan.path_until(0) == [(0.0, -1.0)]
    # Each reading ends the path at the time and place it was taken
    for taken, reading in enumerate(plan.readings, start=1):
        assert plan.path_until(taken)[-1] == reading
    assert plan.path_until(len(plan.readings)) == plan.path
    assert plan.path_until(len(plan.readings) + 5) == plan.path
