from __future__ import annotations

from decimal import Decimal

from src.strategy.risk import position_size, sl_price, tiered_trailing_sl_price


def test_position_size_basic():
    res = position_size(
        trade_amount=Decimal("20"),
        leverage=5,
        entry_price=Decimal("100"),
    )
    # notional = 20 * 5 = 100; qty = 100 / 100 = 1
    assert res.notional == Decimal("100")
    assert res.qty == Decimal("1")


def test_position_size_zero_amount():
    res = position_size(
        trade_amount=Decimal("0"),
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


def test_tiered_below_trigger_returns_none():
    """Profit < trigger → SL should not move (return None)."""
    res = tiered_trailing_sl_price(
        side="LONG", current_price=Decimal("100.5"), entry_price=Decimal("100"),
        trigger_pct=Decimal("1.0"), step_pct=Decimal("0.5"),
    )
    assert res is None


def test_tiered_long_m1_breakeven():
    """Profit exactly at trigger → M1, SL at entry (breakeven)."""
    res = tiered_trailing_sl_price(
        side="LONG", current_price=Decimal("101"), entry_price=Decimal("100"),
        trigger_pct=Decimal("1.0"), step_pct=Decimal("0.5"),
    )
    assert res is not None
    assert res.milestone == 1
    assert res.desired == Decimal("100")
    assert res.sl_offset_pct == Decimal("0.0")


def test_tiered_long_m3():
    """Profit 2% with trigger=1%, step=0.5% → M3, SL at entry+1%."""
    res = tiered_trailing_sl_price(
        side="LONG", current_price=Decimal("102"), entry_price=Decimal("100"),
        trigger_pct=Decimal("1.0"), step_pct=Decimal("0.5"),
    )
    assert res is not None
    assert res.milestone == 3
    assert res.desired == Decimal("101.0")


def test_tiered_short_m2():
    """SHORT mirror: profit 1.5% → M2, SL at entry−0.5%."""
    res = tiered_trailing_sl_price(
        side="SHORT", current_price=Decimal("98.5"), entry_price=Decimal("100"),
        trigger_pct=Decimal("1.0"), step_pct=Decimal("0.5"),
    )
    assert res is not None
    assert res.milestone == 2
    assert res.desired == Decimal("99.5")
