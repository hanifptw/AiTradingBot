"""Cron-style jobs orchestrated by APScheduler.

`refresh_universe` runs every 6h and replaces the monitored-symbols list.
`poll_klines_and_signal` runs once per minute and only acts on each symbol when
the configured timeframe has produced a freshly-closed bar (cheap idempotency
on `close_time`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from src.ai.evaluator import generate_report
from src.core import repository as repo
from src.core.db import session
from src.execution.trailing import run_trailing_tick
from src.indicators.stochastic import StochParams
from src.market.binance_client import get_binance
from src.market.universe import fetch_top_universe
from src.strategy.signal_engine import process_symbol
from src.tgbot.notifier import notify

log = logging.getLogger(__name__)

# symbol -> last processed bar close_time (UTC ms)
_last_bar_seen: dict[str, int] = defaultdict(int)


async def refresh_universe() -> None:
    binance = get_binance()
    try:
        perpetuals = await binance.usdt_perpetual_symbols()
        items = await fetch_top_universe(perpetuals)
        async with session() as s:
            await repo.replace_universe(s, items)
        log.info("Universe refreshed: %d symbols", len(items))
    except Exception:
        log.exception("Universe refresh failed")


async def poll_klines_and_signal() -> None:
    binance = get_binance()
    async with session() as s:
        settings = await repo.get_settings(s)
        universe = await repo.list_universe(s)
    if not universe:
        log.info("Universe empty — refreshing first")
        await refresh_universe()
        async with session() as s:
            universe = await repo.list_universe(s)

    params = StochParams(k=settings.stoch_k, d=settings.stoch_d, smooth=settings.stoch_smooth)
    # Fan out symbol fetches with bounded concurrency.
    sem = asyncio.Semaphore(5)

    async def _process(sym: str) -> None:
        async with sem:
            try:
                df = await binance.klines(sym, settings.timeframe, limit=200)
                if df.empty:
                    return
                # Only act when a new closed bar appears. Binance returns the in-progress
                # bar as the last row; we drop it.
                df_closed = df.iloc[:-1]
                if df_closed.empty:
                    return
                latest_closed_ms = int(df_closed["close_time"].iloc[-1].value // 1_000_000)
                if latest_closed_ms <= _last_bar_seen[sym]:
                    return
                _last_bar_seen[sym] = latest_closed_ms
                await process_symbol(sym, df_closed, params)
            except Exception:
                log.exception("kline/signal failed for %s", sym)

    await asyncio.gather(*(_process(u.symbol) for u in universe))


async def trailing_tick() -> None:
    try:
        await run_trailing_tick()
    except Exception:
        log.exception("Trailing tick failed")


async def sync_positions() -> None:
    """Detect positions closed by Binance's SL/liquidation and reconcile the DB.

    Called every 2 minutes. Compares DB open positions against actual Binance
    positions. When a position is gone from Binance but still OPEN in our DB,
    we close it and reset the signal state so new signals can fire.
    """
    binance = get_binance()
    async with session() as s:
        db_open = await repo.open_positions(s)
        settings = await repo.get_settings(s)

    if not db_open:
        return

    try:
        binance_open = await binance.open_position_amounts()
    except Exception:
        log.exception("sync_positions: failed to fetch Binance positions")
        return

    for pos in db_open:
        binance_amt = binance_open.get(pos.symbol, Decimal("0"))
        still_open = (pos.side == "LONG" and binance_amt > 0) or (
            pos.side == "SHORT" and binance_amt < 0
        )
        if still_open:
            continue

        log.warning(
            "sync_positions: %s %s (id=%d) gone from Binance — closing in DB as SL",
            pos.side, pos.symbol, pos.id,
        )

        exit_price = pos.entry_price  # safe fallback
        try:
            trades = await binance.recent_user_trades(pos.symbol, limit=10)
            close_side = "SELL" if pos.side == "LONG" else "BUY"
            candidates = [
                t for t in trades
                if t.get("side", "").upper() == close_side
                and int(t.get("time", 0)) >= int(pos.opened_at.timestamp() * 1000)
            ]
            if candidates:
                best = max(candidates, key=lambda t: t.get("time", 0))
                price = Decimal(str(best["price"]))
                if price > 0:
                    exit_price = price
        except Exception:
            log.exception("sync_positions: could not fetch exit trade for %s", pos.symbol)

        direction = Decimal("1") if pos.side == "LONG" else Decimal("-1")
        pnl = pos.qty * (exit_price - pos.entry_price) * direction

        # Determine close reason: compare exit price to TP price if available.
        reason = "SL"
        if pos.tp_price and (
            (pos.side == "LONG" and exit_price >= pos.tp_price * Decimal("0.995"))
            or (pos.side == "SHORT" and exit_price <= pos.tp_price * Decimal("1.005"))
        ):
            reason = "TP"

        async with session() as s:
            pos2 = await repo.open_position_for(s, pos.symbol)
            if pos2 is None:
                continue  # Already closed by another path
            # Cancel the order that didn't fire (Binance may auto-cancel, but be explicit).
            if reason == "TP" and pos2.sl_order_id:
                with contextlib.suppress(Exception):
                    await binance.cancel_order(pos.symbol, pos2.sl_order_id)
            elif reason == "SL" and pos2.tp_order_id:
                with contextlib.suppress(Exception):
                    await binance.cancel_order(pos.symbol, pos2.tp_order_id)
            await repo.close_position(
                s, pos2,
                exit_price=exit_price,
                realized_pnl=pnl,
                reason=reason,
                sl_pct=settings.sl_pct,
            )

        sign = "+" if pnl >= 0 else ""
        emoji = "🎯" if reason == "TP" else "🛑"
        label = "kena TP" if reason == "TP" else "kena SL"
        await notify(
            f"{emoji} *{pos.side} {pos.symbol}* {label}\n"
            f"Entry: `{pos.entry_price:.4f}` → Exit: `{exit_price:.4f}`\n"
            f"PnL: `{sign}{pnl:.2f}` USDT"
        )

    # After closing any externally-closed positions, reset orphaned signal states.
    async with session() as s:
        reset = await repo.reconcile_states(s)
    if reset:
        log.warning("sync_positions: reconciled %d orphaned signal states → IDLE", reset)


async def weekly_ai_report() -> None:
    try:
        log.info("Running weekly AI report at %s", datetime.utcnow().isoformat())
        await generate_report(trigger="weekly")
    except Exception:
        log.exception("Weekly AI report failed")
