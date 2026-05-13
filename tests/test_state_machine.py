from __future__ import annotations

from src.core.models import SignalState
from src.strategy.state import Bar, Decision, step


def test_idle_to_long_armed_on_oversold_cross_up():
    prev = Bar(k=10, d=15)
    curr = Bar(k=18, d=16)  # %K crossed up %D, both <20
    t = step(SignalState.IDLE, prev, curr, None)
    assert t.new_state is SignalState.LONG_ARMED
    assert t.decision is Decision.HOLD
    assert t.armed_extreme_k == 10  # min(prev_k, curr_k)


def test_idle_no_arm_if_cross_outside_zone():
    # Cross up but already above 20.
    prev = Bar(k=25, d=27)
    curr = Bar(k=30, d=28)
    t = step(SignalState.IDLE, prev, curr, None)
    assert t.new_state is SignalState.IDLE
    assert t.decision is Decision.HOLD


def test_long_armed_to_enter_when_k_breaks_above_20():
    prev = Bar(k=18, d=16)
    curr = Bar(k=21, d=18)
    t = step(SignalState.LONG_ARMED, prev, curr, armed_extreme_k=10)
    assert t.new_state is SignalState.IN_LONG
    assert t.decision is Decision.ENTER_LONG


def test_long_armed_invalidates_on_new_low():
    prev = Bar(k=18, d=16)
    curr = Bar(k=8, d=12)  # drops below the previous extreme
    t = step(SignalState.LONG_ARMED, prev, curr, armed_extreme_k=10)
    assert t.new_state is SignalState.IDLE
    assert t.decision is Decision.RESET


def test_idle_to_short_armed_on_overbought_cross_down():
    prev = Bar(k=90, d=85)
    curr = Bar(k=82, d=84)  # %K crossed down %D, both >80
    t = step(SignalState.IDLE, prev, curr, None)
    assert t.new_state is SignalState.SHORT_ARMED
    assert t.decision is Decision.HOLD
    assert t.armed_extreme_k == 90


def test_short_armed_to_enter_when_k_breaks_below_80():
    prev = Bar(k=82, d=84)
    curr = Bar(k=79, d=82)
    t = step(SignalState.SHORT_ARMED, prev, curr, armed_extreme_k=90)
    assert t.new_state is SignalState.IN_SHORT
    assert t.decision is Decision.ENTER_SHORT


def test_in_long_exits_on_cross_down():
    prev = Bar(k=60, d=58)
    curr = Bar(k=55, d=58)  # %K crossed below %D
    t = step(SignalState.IN_LONG, prev, curr, None)
    assert t.new_state is SignalState.IDLE
    assert t.decision is Decision.EXIT_LONG


def test_in_short_exits_on_cross_up():
    prev = Bar(k=40, d=42)
    curr = Bar(k=45, d=42)
    t = step(SignalState.IN_SHORT, prev, curr, None)
    assert t.new_state is SignalState.IDLE
    assert t.decision is Decision.EXIT_SHORT


def test_cold_start_holds():
    t = step(SignalState.IDLE, None, Bar(k=50, d=50), None)
    assert t.decision is Decision.HOLD
