from __future__ import annotations

from decimal import Decimal

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
