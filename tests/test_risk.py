from __future__ import annotations

from decimal import Decimal

from src.strategy.risk import position_size


def test_position_size_basic():
    res = position_size(
        equity=Decimal("1000"),
        size_pct=Decimal("20"),
        leverage=5,
        entry_price=Decimal("100"),
    )
    # margin = 1000 * 0.20 = 200; notional = 200 * 5 = 1000; qty = 1000/100 = 10
    assert res.margin == Decimal("200.00")
    assert res.notional == Decimal("1000.00")
    assert res.qty == Decimal("10")


def test_position_size_zero_equity():
    res = position_size(
        equity=Decimal("0"),
        size_pct=Decimal("20"),
        leverage=5,
        entry_price=Decimal("100"),
    )
    assert res.qty == 0
    assert res.notional == 0
    assert res.margin == 0


def test_position_size_zero_size_pct():
    res = position_size(
        equity=Decimal("1000"),
        size_pct=Decimal("0"),
        leverage=5,
        entry_price=Decimal("100"),
    )
    assert res.qty == 0


def test_position_size_zero_leverage():
    res = position_size(
        equity=Decimal("1000"),
        size_pct=Decimal("10"),
        leverage=0,
        entry_price=Decimal("100"),
    )
    assert res.qty == 0
