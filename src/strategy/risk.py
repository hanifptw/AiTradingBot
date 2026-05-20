"""Position sizing: notional = equity * size_pct/100 * leverage; qty = notional / price."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SizingResult:
    qty: Decimal
    notional: Decimal
    margin: Decimal


def position_size(
    *,
    equity: Decimal,
    size_pct: Decimal,
    leverage: int,
    entry_price: Decimal,
) -> SizingResult:
    """Compute qty + notional + margin for an AI-issued trade.

    size_pct is "margin per trade as % of equity", consistent with the
    `max_equity_per_trade_pct` safety cap. notional = margin * leverage.
    """
    if equity <= 0 or size_pct <= 0 or entry_price <= 0 or leverage <= 0:
        return SizingResult(Decimal("0"), Decimal("0"), Decimal("0"))
    margin = equity * size_pct / Decimal("100")
    notional = margin * Decimal(leverage)
    qty = notional / entry_price
    return SizingResult(qty=qty, notional=notional, margin=margin)
