"""Position sizing: notional = trade_amount * leverage, qty = notional / price."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class SizingResult:
    qty: Decimal
    notional: Decimal


def position_size(
    *,
    trade_amount: Decimal,
    leverage: int,
    entry_price: Decimal,
) -> SizingResult:
    if trade_amount <= 0 or entry_price <= 0 or leverage <= 0:
        return SizingResult(Decimal("0"), Decimal("0"))
    notional = trade_amount * Decimal(leverage)
    qty = notional / entry_price
    return SizingResult(qty=qty, notional=notional)


def sl_price(*, side: str, entry: Decimal, sl_pct: Decimal) -> Decimal:
    factor = sl_pct / Decimal("100")
    return entry * (Decimal("1") - factor) if side == "LONG" else entry * (Decimal("1") + factor)


def tp_price(*, side: str, entry: Decimal, tp_pct: Decimal) -> Decimal:
    factor = tp_pct / Decimal("100")
    return entry * (Decimal("1") + factor) if side == "LONG" else entry * (Decimal("1") - factor)


@dataclass(frozen=True)
class TieredSL:
    """Result from `tiered_trailing_sl_price`: desired SL + milestone metadata."""
    desired: Decimal
    milestone: int          # 1-indexed (M1 = breakeven, M2 = +1×step, ...)
    sl_offset_pct: Decimal  # SL offset from entry (positive = profit-side)
    profit_pct: Decimal     # current unrealized profit %


def tiered_trailing_sl_price(
    *, side: str, current_price: Decimal, entry_price: Decimal,
    trigger_pct: Decimal, step_pct: Decimal,
) -> TieredSL | None:
    """Stepped trailing SL.

    Returns None when profit < trigger (SL should stay at its initial level).
    Otherwise returns the desired SL price plus milestone metadata for
    logging/notification.

    Milestones (trigger=1%, step=0.5%):
      profit 1.0–1.49%  → M1, SL at entry (breakeven)
      profit 1.5–1.99%  → M2, SL at entry ± step
      profit 2.0–2.49%  → M3, SL at entry ± 2×step
    """
    if entry_price <= 0 or step_pct <= 0:
        return None

    if side == "LONG":
        profit_pct = (current_price - entry_price) / entry_price * Decimal("100")
    else:
        profit_pct = (entry_price - current_price) / entry_price * Decimal("100")

    if profit_pct < trigger_pct:
        return None

    milestone_idx = ((profit_pct - trigger_pct) / step_pct).to_integral_value(
        rounding=ROUND_DOWN
    )
    sl_offset_pct = milestone_idx * step_pct  # M1 → 0, M2 → step, M3 → 2·step

    if side == "LONG":
        desired = entry_price * (Decimal("1") + sl_offset_pct / Decimal("100"))
    else:
        desired = entry_price * (Decimal("1") - sl_offset_pct / Decimal("100"))

    return TieredSL(
        desired=desired,
        milestone=int(milestone_idx) + 1,
        sl_offset_pct=sl_offset_pct,
        profit_pct=profit_pct,
    )
