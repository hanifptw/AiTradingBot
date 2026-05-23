from __future__ import annotations

import pytest

from src.config import AppConfig

# Required secrets so the model validator doesn't fail in tests.
_SECRETS = dict(
    _env_file=None,
    binance_api_key="x",
    binance_api_secret="x",
    telegram_bot_token="x",
    telegram_allowed_user_ids="123",
    openrouter_api_key="x",
)


def test_universe_default():
    cfg = AppConfig(**_SECRETS)
    assert cfg.universe_symbols == ["NEARUSDT", "HYPEUSDT", "ZECUSDT"]
    assert cfg.openrouter_decision_model == "x-ai/grok-4.20"
    assert cfg.ohlcv_history_bars == 100
    assert cfg.exit_poll_minutes == 15


def test_universe_parse_csv_string():
    cfg = AppConfig(**_SECRETS, universe_symbols="btcusdt, ethusdt, solusdt")
    assert cfg.universe_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_universe_parse_list():
    cfg = AppConfig(**{**_SECRETS, "universe_symbols": ["btcusdt", "ethusdt"]})
    assert cfg.universe_symbols == ["BTCUSDT", "ETHUSDT"]


def test_allowed_ids_csv():
    cfg = AppConfig(
        **{**_SECRETS, "telegram_allowed_user_ids": "123,456,789"},
    )
    assert cfg.telegram_allowed_user_ids == [123, 456, 789]


def test_missing_secrets_raises():
    with pytest.raises(ValueError, match="Missing required environment variables"):
        AppConfig(_env_file=None)
