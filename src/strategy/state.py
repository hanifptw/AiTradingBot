"""Pure state-machine transition logic for the Stochastic two-stage signal.

Kept free of I/O so it can be unit-tested with synthetic %K/%D samples.

Stages:
  IDLE
    ── %K crosses up %D inside <20 zone        → LONG_ARMED
    ── %K crosses down %D inside >80 zone      → SHORT_ARMED
  LONG_ARMED
    ── %K closes above 20                      → ENTER_LONG (caller transitions to IN_LONG)
    ── %K drops below armed-extreme low        → IDLE (invalidation)
  SHORT_ARMED
    ── %K closes below 80                      → ENTER_SHORT
    ── %K rises above armed-extreme high       → IDLE
  IN_LONG
    ── %K crosses below %D                     → EXIT_LONG (TP)
  IN_SHORT
    ── %K crosses above %D                     → EXIT_SHORT (TP)

A "cross" requires two consecutive bars (prev and curr). The caller is
responsible for feeding the latest *closed* bar — not the in-progress one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.core.models import SignalState

OVERSOLD = 20.0
OVERBOUGHT = 80.0


class Decision(str, Enum):
    HOLD = "HOLD"
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT_LONG = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"
    RESET = "RESET"


@dataclass(frozen=True)
class Bar:
    k: float
    d: float


@dataclass(frozen=True)
class Transition:
    new_state: SignalState
    decision: Decision
    # When entering ARMED, remember the most extreme %K seen — used for invalidation.
    armed_extreme_k: float | None = None


def _crossed_up(prev_k: float, prev_d: float, k: float, d: float) -> bool:
    return prev_k <= prev_d and k > d


def _crossed_down(prev_k: float, prev_d: float, k: float, d: float) -> bool:
    return prev_k >= prev_d and k < d


def step(
    state: SignalState,
    prev: Bar | None,
    curr: Bar,
    armed_extreme_k: float | None,
) -> Transition:
    """One transition. `prev` is the previous closed bar's K/D (None on cold start)."""
    if prev is None:
        return Transition(state, Decision.HOLD, armed_extreme_k)

    if state is SignalState.IDLE:
        if _crossed_up(prev.k, prev.d, curr.k, curr.d) and prev.k < OVERSOLD and curr.k < OVERSOLD:
            return Transition(
                SignalState.LONG_ARMED,
                Decision.HOLD,
                armed_extreme_k=min(prev.k, curr.k),
            )
        if (
            _crossed_down(prev.k, prev.d, curr.k, curr.d)
            and prev.k > OVERBOUGHT
            and curr.k > OVERBOUGHT
        ):
            return Transition(
                SignalState.SHORT_ARMED,
                Decision.HOLD,
                armed_extreme_k=max(prev.k, curr.k),
            )
        return Transition(SignalState.IDLE, Decision.HOLD, None)

    if state is SignalState.LONG_ARMED:
        # Entry: %K closes above 20 (it was <20 when armed).
        if curr.k >= OVERSOLD:
            return Transition(SignalState.IN_LONG, Decision.ENTER_LONG, None)
        # Invalidation: %K drops below the armed extreme.
        if armed_extreme_k is not None and curr.k < armed_extreme_k:
            return Transition(SignalState.IDLE, Decision.RESET, None)
        new_extreme = (
            min(armed_extreme_k, curr.k) if armed_extreme_k is not None else curr.k
        )
        return Transition(SignalState.LONG_ARMED, Decision.HOLD, new_extreme)

    if state is SignalState.SHORT_ARMED:
        if curr.k <= OVERBOUGHT:
            return Transition(SignalState.IN_SHORT, Decision.ENTER_SHORT, None)
        if armed_extreme_k is not None and curr.k > armed_extreme_k:
            return Transition(SignalState.IDLE, Decision.RESET, None)
        new_extreme = (
            max(armed_extreme_k, curr.k) if armed_extreme_k is not None else curr.k
        )
        return Transition(SignalState.SHORT_ARMED, Decision.HOLD, new_extreme)

    if state is SignalState.IN_LONG:
        if _crossed_down(prev.k, prev.d, curr.k, curr.d):
            return Transition(SignalState.IDLE, Decision.EXIT_LONG, None)
        return Transition(SignalState.IN_LONG, Decision.HOLD, None)

    if state is SignalState.IN_SHORT:
        if _crossed_up(prev.k, prev.d, curr.k, curr.d):
            return Transition(SignalState.IDLE, Decision.EXIT_SHORT, None)
        return Transition(SignalState.IN_SHORT, Decision.HOLD, None)

    return Transition(state, Decision.HOLD, armed_extreme_k)
