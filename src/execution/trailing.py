"""Periodic trailing-stop adjuster (stepped/tiered).

For each open position with trailing enabled:
- Compute current unrealized profit %.
- If profit < `trailing_trigger_pct`, leave SL untouched (initial sl_pct holds).
- Else, snap SL to the highest milestone reached: M1 = breakeven, M2 = entry ±
  step, M3 = entry ± 2·step, etc., where step = `trailing_offset_pct`.
- Only update if the new SL is materially better than the current one.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from src.core import repository as repo
from src.core.db import session
from src.market.binance_client import get_binance
from src.strategy.risk import tiered_trailing_sl_price
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
            tier = tiered_trailing_sl_price(
                side=pos.side,
                current_price=price,
                entry_price=pos.entry_price,
                trigger_pct=settings.trailing_trigger_pct,
                step_pct=settings.trailing_offset_pct,
            )
            if tier is None:
                continue  # profit below trigger — keep static SL

            desired = binance.quantize_price(pos.symbol, tier.desired)
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
            log.info(
                "Trailed SL %s M%d: %s -> %s (profit=%.2f%% offset=%s%%)",
                pos.symbol, tier.milestone, current, desired,
                float(tier.profit_pct), tier.sl_offset_pct,
            )
            sign = "+" if tier.profit_pct >= 0 else ""
            level_label = "breakeven" if tier.sl_offset_pct == 0 else (
                f"entry+{tier.sl_offset_pct}%" if pos.side == "LONG"
                else f"entry−{tier.sl_offset_pct}%"
            )
            await notify(
                f"📍 Trailing SL *{pos.symbol}* {pos.side}\n"
                f"SL: `{current}` → `{desired}` (M{tier.milestone}, {level_label})\n"
                f"Mark: `{price:.4f}` | Profit: `{sign}{tier.profit_pct:.2f}%`"
            )
        except Exception as exc:
            log.exception("Trailing update failed for %s: %s", pos.symbol, exc)
