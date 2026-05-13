"""Glues the indicator + state machine + event bus + DB persistence together."""

from __future__ import annotations

import logging
from decimal import Decimal

import pandas as pd

from src.core import repository as repo
from src.core.db import session
from src.core.events import EntrySignal, StateChanged, get_bus
from src.core.models import Side, SignalState
from src.indicators.stochastic import StochParams, stochastic
from src.strategy.state import Bar, Decision, step

log = logging.getLogger(__name__)


async def process_symbol(symbol: str, df: pd.DataFrame, params: StochParams) -> None:
    """Compute Stoch, take last *two closed* bars, advance state, publish events."""
    enriched = stochastic(df, params)
    valid = enriched.dropna(subset=["stoch_k", "stoch_d"])
    if len(valid) < 2:
        return
    prev_row = valid.iloc[-2]
    curr_row = valid.iloc[-1]

    bus = get_bus()
    async with session() as s:
        row = await repo.get_state_row(s, symbol)
        prev_state = SignalState(row.state)
        prev_extreme = float(row.armed_extreme_k) if row.armed_extreme_k is not None else None

        prev_bar = Bar(k=float(prev_row["stoch_k"]), d=float(prev_row["stoch_d"]))
        curr_bar = Bar(k=float(curr_row["stoch_k"]), d=float(curr_row["stoch_d"]))

        transition = step(prev_state, prev_bar, curr_bar, prev_extreme)
        last_close = Decimal(str(curr_row["close"]))

        if transition.new_state != prev_state:
            log.info(
                "[%s] state %s -> %s  (k=%.2f d=%.2f close=%s)",
                symbol, prev_state.value, transition.new_state.value,
                curr_bar.k, curr_bar.d, last_close,
            )
            await bus.publish(
                StateChanged(symbol=symbol, old=prev_state, new=transition.new_state)
            )

        await repo.save_state_row(
            s,
            symbol,
            state=transition.new_state,
            last_k=Decimal(str(curr_bar.k)),
            last_d=Decimal(str(curr_bar.d)),
            armed_at_bar=(
                curr_row["close_time"].to_pydatetime()
                if transition.new_state in (SignalState.LONG_ARMED, SignalState.SHORT_ARMED)
                else None
            ),
            armed_extreme_k=(
                Decimal(str(transition.armed_extreme_k))
                if transition.armed_extreme_k is not None
                else None
            ),
        )

    # Publish entry/exit *outside* the DB session so subscribers can open their own.
    if transition.decision is Decision.ENTER_LONG:
        await bus.publish(EntrySignal(symbol=symbol, side=Side.LONG, price=last_close))
    elif transition.decision is Decision.ENTER_SHORT:
        await bus.publish(EntrySignal(symbol=symbol, side=Side.SHORT, price=last_close))
