"""Cron-style jobs orchestrated by APScheduler.

- portfolio_bar_close_job: runs at the top of every hour (a few seconds after
  bar close) and invokes the AI portfolio agent.
- exit_monitor_job: runs every `exit_poll_minutes` and re-evaluates only the
  currently open positions.
- sync_positions: reconciles DB open positions against Binance (detects SL/TP/
  liquidation fills the bot didn't observe).
- daily_ai_report: invokes the deep evaluator at the end of each day.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from decimal import Decimal

from src.ai.evaluator import generate_report
from src.core import repository as repo
from src.core.db import session
from src.market.binance_client import get_binance
from src.strategy import portfolio_agent
from src.tgbot.notifier import notify, notify_position_closed

log = logging.getLogger(__name__)


async def portfolio_bar_close_job() -> None:
    try:
        await portfolio_agent.run_bar_close_cycle()
    except Exception:
        log.exception("Portfolio bar-close job failed")


async def exit_monitor_job() -> None:
    try:
        await portfolio_agent.run_exit_poll_cycle()
    except Exception:
        log.exception("Exit-monitor job failed")


async def sync_positions() -> None:
    """Detect positions closed by Binance's SL/TP/liquidation and reconcile DB.

    Called periodically. When a position is gone from Binance but still OPEN
    in our DB, we close it locally, infer the close reason from price levels,
    and notify Telegram.
    """
    binance = get_binance()
    async with session() as s:
        db_open = await repo.open_positions(s)

    if not db_open:
        return

    try:
        binance_open = await binance.open_position_amounts()
    except Exception:
        log.exception("sync_positions: failed to fetch Binance positions")
        return

    for pos in db_open:
        try:
            await _reconcile_one(binance, binance_open, pos)
        except Exception:
            log.exception("sync_positions: reconcile failed for %s", pos.symbol)


def _classify_close_reason(pos, exit_price: Decimal, pnl: Decimal) -> str:
    """Heuristic: TP, SL, LIQUIDATION, or MANUAL based on exit price vs levels."""
    if pos.tp_price and (
        (pos.side == "LONG" and exit_price >= pos.tp_price * Decimal("0.995"))
        or (pos.side == "SHORT" and exit_price <= pos.tp_price * Decimal("1.005"))
    ):
        return "TP"
    near_sl = False
    if pos.sl_price and pos.sl_price > 0:
        near_sl = abs(exit_price - pos.sl_price) / pos.sl_price <= Decimal("0.02")
    if near_sl:
        return "SL"
    # No SL/TP nearby. If the loss is catastrophic relative to position margin,
    # treat as liquidation; otherwise assume the user (or AI on Binance side)
    # closed it manually.
    leverage = max(pos.leverage or 1, 1)
    notional = pos.qty * pos.entry_price
    margin = notional / Decimal(leverage)
    if margin > 0 and pnl < 0 and abs(pnl) >= margin * Decimal("0.85"):
        return "LIQUIDATION"
    return "MANUAL"


async def _reconcile_one(binance, binance_open: dict, pos) -> None:
    binance_amt = binance_open.get(pos.symbol, Decimal("0"))
    still_open = (pos.side == "LONG" and binance_amt > 0) or (
        pos.side == "SHORT" and binance_amt < 0
    )
    if still_open:
        return

    log.warning(
        "sync_positions: %s %s (id=%d) gone from Binance — closing in DB",
        pos.side,
        pos.symbol,
        pos.id,
    )

    exit_price = pos.entry_price  # safe fallback
    try:
        trades = await binance.recent_user_trades(pos.symbol, limit=10)
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        candidates = [
            t
            for t in trades
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
    reason = _classify_close_reason(pos, exit_price, pnl)

    db_ok = False
    try:
        async with session() as s:
            pos2 = await repo.open_position_for(s, pos.symbol)
            if pos2 is not None:
                if reason == "TP" and pos2.sl_order_id:
                    with contextlib.suppress(Exception):
                        await binance.cancel_order(pos.symbol, pos2.sl_order_id)
                elif reason in ("SL", "MANUAL", "LIQUIDATION") and pos2.tp_order_id:
                    with contextlib.suppress(Exception):
                        await binance.cancel_order(pos.symbol, pos2.tp_order_id)
                await repo.close_position(
                    s,
                    pos2,
                    exit_price=exit_price,
                    realized_pnl=pnl,
                    reason=reason,
                )
                db_ok = True
    except Exception:
        log.exception("sync_positions: DB close failed for %s — notify anyway", pos.symbol)

    await notify_position_closed(
        side=pos.side,
        symbol=pos.symbol,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        pnl=pnl,
        reason=reason if db_ok else reason,
    )


async def daily_ai_report() -> None:
    try:
        log.info("Running daily AI report at %s", datetime.utcnow().isoformat())
        await generate_report(trigger="daily")
    except Exception:
        log.exception("Daily AI report failed")


async def validate_universe_on_startup() -> None:
    """One-shot: ensure every configured universe symbol exists on Binance Futures USDT-M."""
    from src.config import get_config

    cfg = get_config()
    binance = get_binance()
    try:
        valid = await binance.usdt_perpetual_symbols()
    except Exception:
        log.exception("Failed to fetch exchange info — skipping universe validation")
        return
    missing = [s for s in cfg.universe_symbols if s not in valid]
    if missing:
        log.error("Configured universe symbols not listed on Binance Futures: %s", missing)
        await notify(f"⚠️ Symbol tidak ditemukan di Binance Futures USDT-M: `{', '.join(missing)}`")
