from __future__ import annotations

from src.ai.exit_monitor import parse_response


def test_parse_basic():
    raw = """{
        "items": [
            {"symbol": "btcusdt", "position_id": 42, "action": "CLOSE",
             "confidence": 80, "reasoning": "reversal"},
            {"symbol": "ZECUSDT", "position_id": 43, "action": "HOLD",
             "confidence": 60, "reasoning": "still trending"}
        ]
    }"""
    decision = parse_response(raw)
    assert decision is not None
    assert len(decision.items) == 2
    btc = decision.items[0]
    assert btc.symbol == "BTCUSDT"
    assert btc.action == "CLOSE"
    assert btc.position_id == 42


def test_parse_empty_items():
    raw = '{"items": []}'
    decision = parse_response(raw)
    assert decision is not None
    assert decision.items == []


def test_parse_invalid_action_rejected():
    raw = '{"items": [{"symbol": "BTCUSDT", "position_id": 1, "action": "SELL", "confidence": 50, "reasoning": ""}]}'
    assert parse_response(raw) is None


def test_parse_garbage():
    assert parse_response("not json") is None


def test_parse_empty():
    assert parse_response("") is None
