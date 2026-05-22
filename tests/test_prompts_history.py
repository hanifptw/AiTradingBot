from __future__ import annotations

from decimal import Decimal

from src.ai.prompts import format_historical_context


def _full_stats(**overrides) -> dict:
    base = {
        "count": 4,
        "wins": 2,
        "losses": 2,
        "win_rate": 50.0,
        "total_pnl_usdt": Decimal("15.00"),
        "avg_r": 0.5,
        "best_r": 2.0,
        "worst_r": -1.0,
    }
    base.update(overrides)
    return base


def test_returns_none_when_nothing_to_show():
    out = format_historical_context(
        trades_count=0,
        stats=None,
        per_symbol=[],
        worst=[],
        last_report_md=None,
        last_report_age_hours=None,
    )
    assert out is None


def test_returns_none_when_report_too_old():
    out = format_historical_context(
        trades_count=0,
        stats=None,
        per_symbol=[],
        worst=[],
        last_report_md="some report",
        last_report_age_hours=200.0,  # > 168h (7d)
    )
    assert out is None


def test_full_render_contains_expected_sections():
    out = format_historical_context(
        trades_count=4,
        stats=_full_stats(),
        per_symbol=[
            {
                "symbol": "ETHUSDT",
                "count": 3,
                "wins": 0,
                "win_rate": 0.0,
                "total_pnl": Decimal("-30"),
                "avg_r": -1.2,
            },
            {
                "symbol": "BTCUSDT",
                "count": 2,
                "wins": 2,
                "win_rate": 100.0,
                "total_pnl": Decimal("12"),
                "avg_r": 0.8,
            },
        ],
        worst=[
            {
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "entry_price": Decimal("3450"),
                "exit_price": Decimal("3540"),
                "r_multiple": Decimal("-1.45"),
                "close_reason": "SL",
            }
        ],
        last_report_md="Pola kemenangan terlihat pada BTC.",
        last_report_age_hours=8.0,
    )
    assert out is not None
    assert "## Historical context (last 7d)" in out
    assert "### Aggregate" in out
    assert "trades: 4" in out
    assert "wins: 2 (50.0%)" in out
    assert "avg_R: +0.50" in out
    assert "best_R: +2.00" in out
    assert "### Per-symbol" in out
    assert "ETHUSDT: 3 trades, 0.0% wins" in out
    assert "BTCUSDT: 2 trades, 100.0% wins" in out
    assert "### Worst trades" in out
    assert "ETHUSDT SHORT" in out
    assert "R=-1.45" in out
    assert "reason=SL" in out
    assert "### Latest evaluator report" in out
    assert "Pola kemenangan" in out
    assert "STALE" not in out  # 8h < 48h


def test_stale_report_tag_appears_after_48h():
    out = format_historical_context(
        trades_count=0,
        stats=None,
        per_symbol=[],
        worst=[],
        last_report_md="report body",
        last_report_age_hours=72.0,
    )
    assert out is not None
    assert "STALE — older than 48h" in out
    assert "report body" in out


def test_report_trimmed_to_word_limit():
    long_md = " ".join(f"word{i}" for i in range(400))  # 400 words
    out = format_historical_context(
        trades_count=0,
        stats=None,
        per_symbol=[],
        worst=[],
        last_report_md=long_md,
        last_report_age_hours=1.0,
    )
    assert out is not None
    assert "... [truncated]" in out
    assert "word0" in out
    assert "word249" in out  # last kept word (250 words → indices 0..249)
    assert "word250" not in out


def test_short_report_not_truncated():
    out = format_historical_context(
        trades_count=0,
        stats=None,
        per_symbol=[],
        worst=[],
        last_report_md="short body",
        last_report_age_hours=1.0,
    )
    assert out is not None
    assert "... [truncated]" not in out


def test_per_symbol_capped_to_10():
    per_symbol = [
        {
            "symbol": f"SYM{i:02d}USDT",
            "count": 1,
            "wins": 0,
            "win_rate": 0.0,
            "total_pnl": Decimal(str(-i - 1)),
            "avg_r": -0.5,
        }
        for i in range(15)
    ]
    out = format_historical_context(
        trades_count=15,
        stats=_full_stats(count=15),
        per_symbol=per_symbol,
        worst=[],
        last_report_md=None,
        last_report_age_hours=None,
    )
    assert out is not None
    assert "SYM00USDT" in out
    assert "SYM09USDT" in out
    assert "SYM10USDT" not in out


def test_handles_none_avg_r_in_stats():
    stats = _full_stats(avg_r=None, best_r=None, worst_r=None)
    out = format_historical_context(
        trades_count=1,
        stats=stats,
        per_symbol=[],
        worst=[],
        last_report_md=None,
        last_report_age_hours=None,
    )
    assert out is not None
    assert "avg_R: n/a" in out
    assert "best_R" not in out
