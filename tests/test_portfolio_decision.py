from __future__ import annotations

from decimal import Decimal

import pandas as pd

from src.ai import prompts as P
from src.ai.portfolio_decision import parse_response


def test_parse_clean_json():
    raw = """{
        "market_view": "mild bullish",
        "decisions": [
            {"symbol": "btcusdt", "action": "OPEN_LONG", "size_pct_equity": 15,
             "leverage": 5, "sl_price": "60000", "tp_price": "65000",
             "confidence": 75, "reasoning": "trend continuation"},
            {"symbol": "ZECUSDT", "action": "HOLD", "confidence": 50, "reasoning": "ranging"}
        ]
    }"""
    decision = parse_response(raw)
    assert decision is not None
    assert decision.market_view == "mild bullish"
    assert len(decision.decisions) == 2
    btc = decision.decisions[0]
    assert btc.symbol == "BTCUSDT"  # upper-cased by validator
    assert btc.action == "OPEN_LONG"
    assert btc.sl_price == Decimal("60000")
    assert btc.tp_price == Decimal("65000")
    assert btc.confidence == 75


def test_parse_with_markdown_fence():
    raw = "```json\n" + '{"market_view": "x", "decisions": []}' + "\n```"
    decision = parse_response(raw)
    assert decision is not None
    assert decision.market_view == "x"
    assert decision.decisions == []


def test_parse_with_prose_before_json():
    raw = "Sure, here is the JSON:\n" + '{"market_view": "x", "decisions": []}'
    decision = parse_response(raw)
    assert decision is not None
    assert decision.market_view == "x"


def test_parse_invalid_action_rejected():
    raw = '{"market_view": "x", "decisions": [{"symbol": "BTCUSDT", "action": "MAYBE_LONG", "confidence": 50, "reasoning": "x"}]}'
    decision = parse_response(raw)
    # Schema validation rejects unknown enum values.
    assert decision is None


def test_parse_empty_string():
    assert parse_response("") is None


def test_parse_garbage():
    assert parse_response("totally not json") is None


def test_confidence_clamped():
    raw = '{"market_view": "", "decisions": [{"symbol": "BTCUSDT", "action": "HOLD", "confidence": 250, "reasoning": "x"}]}'
    decision = parse_response(raw)
    assert decision is not None
    assert decision.decisions[0].confidence == 100


def test_confidence_negative_clamped():
    raw = '{"market_view": "", "decisions": [{"symbol": "BTCUSDT", "action": "HOLD", "confidence": -50, "reasoning": "x"}]}'
    decision = parse_response(raw)
    assert decision is not None
    assert decision.decisions[0].confidence == 0


def _dummy_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "close_time": pd.to_datetime(["2026-05-22T00:00Z", "2026-05-22T01:00Z"], utc=True),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [10.0, 12.0],
        }
    )


def test_portfolio_prompt_embeds_historical_context():
    sentinel = "## Historical context (last 7d) — UNIQUE-SENTINEL-12345"
    prompt = P.build_portfolio_user_prompt(
        balance_usdt=Decimal("1000"),
        open_positions=[],
        universe_ohlcv={"BTCUSDT": _dummy_ohlcv()},
        ohlcv_history_bars=2,
        max_leverage_cap=10,
        max_equity_per_trade_pct=Decimal("20"),
        historical_context=sentinel,
    )
    assert sentinel in prompt
    # Caps appear before the historical block; OHLCV after.
    assert prompt.index("max leverage") < prompt.index(sentinel) < prompt.index("BTCUSDT")


def test_portfolio_prompt_omits_block_when_no_history():
    prompt = P.build_portfolio_user_prompt(
        balance_usdt=Decimal("1000"),
        open_positions=[],
        universe_ohlcv={"BTCUSDT": _dummy_ohlcv()},
        ohlcv_history_bars=2,
        max_leverage_cap=10,
        max_equity_per_trade_pct=Decimal("20"),
        historical_context=None,
    )
    assert "Historical context" not in prompt


def test_exit_monitor_prompt_embeds_historical_context():
    sentinel = "## Historical context (last 7d) — UNIQUE-SENTINEL-67890"
    prompt = P.build_exit_monitor_user_prompt(
        open_positions=[
            {
                "id": 1,
                "symbol": "BTCUSDT",
                "side": "LONG",
                "qty": "0.1",
                "entry_price": "100",
                "leverage": 5,
                "upnl_pct": 1.0,
                "bars_open": 3,
            }
        ],
        recent_ohlcv={"BTCUSDT": _dummy_ohlcv()},
        latest_prices={"BTCUSDT": Decimal("102")},
        historical_context=sentinel,
    )
    assert sentinel in prompt
    assert prompt.index(sentinel) < prompt.index("## Open positions")
