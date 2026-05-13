"""Subscribes to EntrySignal/ExitSignal and places orders on Binance Futures.

The executor is intentionally the only module allowed to call binance_client
for order placement, so swapping in a paper executor later is a one-line change.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from src.core import repository as repo
from src.core.db import session
from src.core.events import EntrySignal, ExitSignal, get_bus
from src.core.models import Order, Position, Side
from src.market.binance_client import get_binance
from src.strategy.risk import position_size, sl_price, tp_price
from src.tgbot.notifier import notify

log = logging.getLogger(__name__)


def _parse_fill_price(resp: dict, fallback: Decimal) -> Decimal:
    """Binance testnet sometimes returns avgPrice='0.00000' for MARKET orders.
    Fall back to signal price when the reported fill price is zero or missing."""
    for key in ("avgPrice", "price"):
        raw = resp.get(key)
        if raw:
            try:
                d = Decimal(str(raw))
                if d > 0:
                    return d
            except Exception:
                pass
    return fallback


def _fq(qty: Decimal) -> str:
    """Format qty without trailing zeros."""
    return f"{float(qty):.8g}"


async def run_executor() -> None:
    bus = get_bus()
    q = await bus.subscribe()
    try:
        while True:
            event = await q.get()
            try:
                if isinstance(event, EntrySignal):
                    await _handle_entry(event)
                elif isinstance(event, ExitSignal):
                    await _handle_exit(event)
            except Exception:
                log.exception("Executor failed handling %s", event)
    finally:
        await bus.unsubscribe(q)


async def _handle_entry(ev: EntrySignal) -> None:
    binance = get_binance()
    async with session() as s:
        settings = await repo.get_settings(s)
        if not settings.autotrade_enabled:
            log.info("Autotrade disabled — skipping entry %s %s", ev.side.value, ev.symbol)
            return

        # Don't double-up on a symbol already in a position.
        existing = await repo.open_position_for(s, ev.symbol)
        if existing is not None:
            log.info("Position already open for %s — skipping new entry", ev.symbol)
            return

        # Respect max-concurrent-positions cap.
        open_now = await repo.open_positions(s)
        if len(open_now) >= settings.max_positions:
            log.info("Max positions (%d) reached — skipping %s", settings.max_positions, ev.symbol)
            return

        sizing = position_size(
            trade_amount=settings.trade_amount,
            leverage=settings.leverage,
            entry_price=ev.price,
        )
        await binance.exchange_info()  # warms filter cache
        qty = binance.quantize_qty(ev.symbol, sizing.qty)
        if qty <= 0:
            log.warning("Computed qty=0 for %s (sizing=%s) — skipping", ev.symbol, sizing)
            return

        await binance.set_leverage(ev.symbol, settings.leverage)

        binance_side = "BUY" if ev.side is Side.LONG else "SELL"
        market_resp = await binance.market_order(ev.symbol, binance_side, qty)
        fill_price = _parse_fill_price(market_resp, ev.price)

        # Stop-loss (reduce-only stop market at sl_pct from entry).
        sl_raw = sl_price(side=ev.side.value, entry=fill_price, sl_pct=settings.sl_pct)
        sl_quant = binance.quantize_price(ev.symbol, sl_raw)
        sl_side = "SELL" if ev.side is Side.LONG else "BUY"
        sl_resp = await binance.stop_market_reduce_only(ev.symbol, sl_side, sl_quant)

        # Take-profit (take-profit market at tp_pct from entry).
        tp_raw = tp_price(side=ev.side.value, entry=fill_price, tp_pct=settings.tp_pct)
        tp_quant = binance.quantize_price(ev.symbol, tp_raw)
        tp_side = sl_side  # same direction as SL
        tp_resp = await binance.take_profit_market_reduce_only(ev.symbol, tp_side, tp_quant)

        pos = Position(
            symbol=ev.symbol,
            side=ev.side.value,
            mode=settings.mode,
            qty=qty,
            entry_price=fill_price,
            leverage=settings.leverage,
            sl_price=sl_quant,
            sl_order_id=str(sl_resp.get("orderId")) if sl_resp else None,
            tp_price=tp_quant,
            tp_order_id=str(tp_resp.get("orderId")) if tp_resp else None,
        )
        pos = await repo.create_position(s, pos)

        await repo.add_order(
            s,
            Order(
                position_id=pos.id,
                symbol=ev.symbol,
                side=binance_side,
                type="MARKET",
                qty=qty,
                price=fill_price,
                binance_order_id=str(market_resp.get("orderId")),
                status=str(market_resp.get("status", "FILLED")),
                raw=market_resp,
            ),
        )
        await repo.add_order(
            s,
            Order(
                position_id=pos.id,
                symbol=ev.symbol,
                side=sl_side,
                type="STOP_MARKET",
                qty=qty,
                price=sl_quant,
                binance_order_id=str(sl_resp.get("orderId")) if sl_resp else None,
                status="NEW",
                raw=sl_resp,
            ),
        )
        await repo.add_order(
            s,
            Order(
                position_id=pos.id,
                symbol=ev.symbol,
                side=tp_side,
                type="TAKE_PROFIT_MARKET",
                qty=qty,
                price=tp_quant,
                binance_order_id=str(tp_resp.get("orderId")) if tp_resp else None,
                status="NEW",
                raw=tp_resp,
            ),
        )
        lev = settings.leverage

    log.info(
        "Opened %s %s qty=%s @%s  SL=%s  TP=%s",
        ev.side.value, ev.symbol, qty, fill_price, sl_quant, tp_quant,
    )
    side_emoji = "🟢" if ev.side is Side.LONG else "🔴"
    await notify(
        f"{side_emoji} *{ev.side.value} {ev.symbol}* terbuka\n"
        f"Entry: `{fill_price:.4f}` | SL: `{sl_quant:.4f}` | TP: `{tp_quant:.4f}`\n"
        f"Qty: `{_fq(qty)}` | Lev: `{lev}x`"
    )


async def _handle_exit(ev: ExitSignal) -> None:
    binance = get_binance()
    async with session() as s:
        pos = await repo.open_position_for(s, ev.symbol)
        if pos is None:
            return
        # Close with opposite reduceOnly MARKET order.
        # reduceOnly=True prevents accidentally opening a new position if Binance's
        # SL order already fired and closed this position before this TP signal arrived.
        opposite = "SELL" if pos.side == "LONG" else "BUY"
        close_resp = await binance.close_market_order(ev.symbol, opposite, pos.qty)
        exit_price = _parse_fill_price(close_resp, ev.price)

        # Cancel SL and TP orders now that we're flat.
        if pos.sl_order_id:
            await binance.cancel_order(ev.symbol, pos.sl_order_id)
        if pos.tp_order_id:
            await binance.cancel_order(ev.symbol, pos.tp_order_id)

        # Realized PnL (linear futures): qty * (exit - entry), inverted for shorts.
        direction = Decimal("1") if pos.side == "LONG" else Decimal("-1")
        pnl = pos.qty * (exit_price - pos.entry_price) * direction

        entry_price = pos.entry_price
        side = pos.side

        settings = await repo.get_settings(s)
        await repo.add_order(
            s,
            Order(
                position_id=pos.id,
                symbol=ev.symbol,
                side=opposite,
                type="MARKET",
                qty=pos.qty,
                price=exit_price,
                binance_order_id=str(close_resp.get("orderId")),
                status=str(close_resp.get("status", "FILLED")),
                raw=close_resp,
            ),
        )
        await repo.close_position(
            s, pos,
            exit_price=exit_price,
            realized_pnl=pnl,
            reason=ev.reason,
            sl_pct=settings.sl_pct,
        )

    log.info("Closed %s %s @%s  pnl=%s (reason=%s)", side, ev.symbol, exit_price, pnl, ev.reason)
    pnl_emoji = "✅" if pnl >= 0 else "🔴"
    sign = "+" if pnl >= 0 else ""
    await notify(
        f"{pnl_emoji} *{side} {ev.symbol}* ditutup ({ev.reason})\n"
        f"Entry: `{entry_price:.4f}` → Exit: `{exit_price:.4f}`\n"
        f"PnL: `{sign}{pnl:.2f}` USDT"
    )
