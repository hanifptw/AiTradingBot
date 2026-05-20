from __future__ import annotations

from src.config import AppConfig


def test_universe_default():
    cfg = AppConfig(_env_file=None)
    assert cfg.universe_symbols == ["BTCUSDT", "HYPEUSDT", "ZECUSDT"]
    assert cfg.openrouter_decision_model == "x-ai/grok-4.20"
    assert cfg.ohlcv_history_bars == 100
    assert cfg.exit_poll_minutes == 30


def test_universe_parse_csv_string():
    cfg = AppConfig(_env_file=None, universe_symbols="btcusdt, ethusdt, solusdt")
    assert cfg.universe_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_universe_parse_list():
    cfg = AppConfig(_env_file=None, universe_symbols=["btcusdt", "ethusdt"])
    assert cfg.universe_symbols == ["BTCUSDT", "ETHUSDT"]


def test_allowed_ids_csv():
    cfg = AppConfig(_env_file=None, telegram_allowed_user_ids="123,456,789")
    assert cfg.telegram_allowed_user_ids == [123, 456, 789]
