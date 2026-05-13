"""Position sizing: notional = trade_amount * leverage, qty = notional / price."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


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


def trailing_sl_price(
    *, side: str, current_price: Decimal, offset_pct: Decimal
) -> Decimal:
    factor = offset_pct / Decimal("100")
    return (
        current_price * (Decimal("1") - factor)
        if side == "LONG"
        else current_price * (Decimal("1") + factor)
    )
