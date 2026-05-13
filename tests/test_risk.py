from __future__ import annotations

from decimal import Decimal

from src.strategy.risk import position_size, sl_price, trailing_sl_price


def test_position_size_basic():
    res = position_size(
        available_equity=Decimal("1000"),
        equity_pct=Decimal("2"),
        leverage=5,
        entry_price=Decimal("100"),
    )
    # margin = 1000 * 0.02 = 20; notional = 20 * 5 = 100; qty = 100 / 100 = 1
    assert res.notional == Decimal("100")
    assert res.qty == Decimal("1")


def test_position_size_zero_equity():
    res = position_size(
        available_equity=Decimal("0"),
        equity_pct=Decimal("2"),
        leverage=5,
        entry_price=Decimal("100"),
    )
    assert res.qty == 0


def test_sl_price_long_below_entry():
    sl = sl_price(side="LONG", entry=Decimal("100"), sl_pct=Decimal("2"))
    assert sl == Decimal("98.00")


def test_sl_price_short_above_entry():
    sl = sl_price(side="SHORT", entry=Decimal("100"), sl_pct=Decimal("2"))
    assert sl == Decimal("102.00")


def test_trailing_sl_long():
    sl = trailing_sl_price(side="LONG", current_price=Decimal("110"), offset_pct=Decimal("1"))
    assert sl == Decimal("108.90")


def test_trailing_sl_short():
    sl = trailing_sl_price(side="SHORT", current_price=Decimal("90"), offset_pct=Decimal("1"))
    assert sl == Decimal("90.90")
