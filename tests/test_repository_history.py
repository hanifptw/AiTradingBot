from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.core import repository as repo
from src.core.models import Trade


def _trade(
    *,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    pnl_usdt: str = "0",
    r_multiple: str | None = None,
    close_reason: str = "TP",
    position_id: int = 1,
    entry_price: str = "100",
    exit_price: str = "110",
) -> Trade:
    return Trade(
        position_id=position_id,
        symbol=symbol,
        side=side,
        mode="testnet",
        qty=Decimal("1"),
        entry_price=Decimal(entry_price),
        exit_price=Decimal(exit_price),
        leverage=5,
        pnl_usdt=Decimal(pnl_usdt),
        pnl_pct=Decimal("1"),
        r_multiple=Decimal(r_multiple) if r_multiple is not None else None,
        close_reason=close_reason,
        opened_at=datetime.now(UTC),
        closed_at=datetime.now(UTC),
        duration_sec=3600,
    )


# --- aggregate_trade_stats --------------------------------------------------


def test_aggregate_empty_returns_zeros():
    stats = repo.aggregate_trade_stats([])
    assert stats["count"] == 0
    assert stats["wins"] == 0
    assert stats["losses"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["total_pnl_usdt"] == Decimal("0")
    assert stats["avg_r"] is None
    assert stats["best_r"] is None
    assert stats["worst_r"] is None


def test_aggregate_mixed_wins_and_losses():
    trades = [
        _trade(position_id=1, pnl_usdt="10", r_multiple="1.5"),
        _trade(position_id=2, pnl_usdt="-5", r_multiple="-0.5"),
        _trade(position_id=3, pnl_usdt="20", r_multiple="2.0"),
        _trade(position_id=4, pnl_usdt="-10", r_multiple="-1.0"),
    ]
    stats = repo.aggregate_trade_stats(trades)
    assert stats["count"] == 4
    assert stats["wins"] == 2
    assert stats["losses"] == 2
    assert stats["win_rate"] == 50.0
    assert stats["total_pnl_usdt"] == Decimal("15")
    assert stats["avg_r"] == 0.5  # (1.5 - 0.5 + 2.0 - 1.0) / 4
    assert stats["best_r"] == 2.0
    assert stats["worst_r"] == -1.0


def test_aggregate_ignores_none_r_multiple():
    trades = [
        _trade(position_id=1, pnl_usdt="10", r_multiple=None),
        _trade(position_id=2, pnl_usdt="20", r_multiple="2.0"),
    ]
    stats = repo.aggregate_trade_stats(trades)
    assert stats["count"] == 2
    assert stats["avg_r"] == 2.0
    assert stats["best_r"] == 2.0
    assert stats["worst_r"] == 2.0


def test_aggregate_all_none_r_multiple_returns_none():
    trades = [_trade(position_id=1, pnl_usdt="10", r_multiple=None)]
    stats = repo.aggregate_trade_stats(trades)
    assert stats["avg_r"] is None
    assert stats["best_r"] is None
    assert stats["worst_r"] is None


# --- per_symbol_stats -------------------------------------------------------


def test_per_symbol_groups_and_sorts_worst_first():
    trades = [
        _trade(position_id=1, symbol="BTCUSDT", pnl_usdt="10", r_multiple="1.0"),
        _trade(position_id=2, symbol="BTCUSDT", pnl_usdt="5", r_multiple="0.5"),
        _trade(position_id=3, symbol="ETHUSDT", pnl_usdt="-30", r_multiple="-1.0"),
        _trade(position_id=4, symbol="SOLUSDT", pnl_usdt="2", r_multiple="0.2"),
    ]
    rows = repo.per_symbol_stats(trades)
    assert [r["symbol"] for r in rows] == ["ETHUSDT", "SOLUSDT", "BTCUSDT"]
    eth = rows[0]
    assert eth["count"] == 1
    assert eth["wins"] == 0
    assert eth["win_rate"] == 0.0
    assert eth["total_pnl"] == Decimal("-30")
    assert eth["avg_r"] == -1.0
    btc = rows[2]
    assert btc["count"] == 2
    assert btc["wins"] == 2
    assert btc["win_rate"] == 100.0
    assert btc["total_pnl"] == Decimal("15")


def test_per_symbol_empty_input():
    assert repo.per_symbol_stats([]) == []


# --- worst_trades -----------------------------------------------------------


def test_worst_trades_ascending_r_limit():
    trades = [
        _trade(position_id=1, pnl_usdt="10", r_multiple="1.0"),
        _trade(position_id=2, pnl_usdt="-20", r_multiple="-1.5"),
        _trade(position_id=3, pnl_usdt="-5", r_multiple="-0.3"),
        _trade(position_id=4, pnl_usdt="-15", r_multiple="-1.2"),
        _trade(position_id=5, pnl_usdt="2", r_multiple="0.1"),
    ]
    worst = repo.worst_trades(trades, limit=3)
    assert [float(t.r_multiple) for t in worst] == [-1.5, -1.2, -0.3]


def test_worst_trades_skips_none_r_multiple():
    trades = [
        _trade(position_id=1, pnl_usdt="-50", r_multiple=None),
        _trade(position_id=2, pnl_usdt="-5", r_multiple="-0.5"),
    ]
    worst = repo.worst_trades(trades, limit=5)
    assert len(worst) == 1
    assert worst[0].position_id == 2


def test_worst_trades_empty_returns_empty():
    assert repo.worst_trades([], limit=5) == []
