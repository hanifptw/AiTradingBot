"""Periodic trailing-stop adjuster.

For each open position with trailing enabled, compute the desired SL based on
the current mark price (LONG: max(current_sl, price*(1 - offset_pct))). If the
new SL is materially better than the existing one, cancel the old stop order
and place a fresh one.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from src.core import repository as repo
from src.core.db import session
from src.market.binance_client import get_binance
from src.strategy.risk import trailing_sl_price
from src.tgbot.notifier import notify

log = logging.getLogger(__name__)

# Minimum % improvement before we bother re-placing the stop. Keeps API noise down.
_MIN_BUMP_PCT = Decimal("0.1")


def _valid_order_id(oid: str | None) -> bool:
    """Return True only for a real, non-placeholder order ID."""
    return bool(oid) and oid not in ("None", "0")


async def run_trailing_tick() -> None:
    binance = get_binance()
    async with session() as s:
        settings = await repo.get_settings(s)
        if not settings.trailing_enabled:
            return
        positions = await repo.open_positions(s)

    for pos in positions:
        try:
            price = await binance.mark_price(pos.symbol)
            desired_raw = trailing_sl_price(
                side=pos.side, current_price=price, offset_pct=settings.trailing_offset_pct
            )
            desired = binance.quantize_price(pos.symbol, desired_raw)
            current = pos.sl_price or Decimal("0")
            better = (
                pos.side == "LONG" and desired > current
            ) or (pos.side == "SHORT" and (current == 0 or desired < current))
            if not better:
                continue
            bump = abs(desired - current) / current * Decimal("100") if current > 0 else Decimal("999")
            if bump < _MIN_BUMP_PCT:
                continue
            if _valid_order_id(pos.sl_order_id):
                await binance.cancel_order(pos.symbol, pos.sl_order_id)
            sl_side = "SELL" if pos.side == "LONG" else "BUY"
            resp = await binance.stop_market_reduce_only(pos.symbol, sl_side, desired)
            new_order_id = str(oid) if (oid := resp.get("orderId")) else None
            async with session() as s:
                pos2 = await repo.open_position_for(s, pos.symbol)
                if pos2 is not None:
                    pos2.sl_price = desired
                    pos2.sl_order_id = new_order_id
                    await s.commit()
            log.info("Trailed SL %s: %s -> %s (price=%s)", pos.symbol, current, desired, price)
            await notify(
                f"📍 Trailing SL *{pos.symbol}* {pos.side}\n"
                f"SL: `{current}` → `{desired}` (mark=`{price:.4f}`)"
            )
        except Exception as exc:
            log.exception("Trailing update failed for %s: %s", pos.symbol, exc)
