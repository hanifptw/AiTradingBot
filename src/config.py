from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    TESTNET = "testnet"
    LIVE = "live"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    mode: Mode = Mode.TESTNET

    binance_api_key: str = ""
    binance_api_secret: str = ""

    telegram_bot_token: str = ""
    telegram_allowed_user_ids: list[int] = Field(default_factory=list)

    openrouter_api_key: str = ""
    # Daily evaluator (deep): reads trades, suggests tweaks.
    openrouter_model: str = "anthropic/claude-sonnet-4.5"
    # Live trader: portfolio decisions + exit monitor.
    openrouter_decision_model: str = "x-ai/grok-4.20"

    # Trading universe — fixed list of perpetual symbols on Binance Futures USDT-M.
    universe_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "HYPEUSDT", "ZECUSDT"])
    # Bars of 1h OHLCV sent to the LLM per coin in the portfolio call.
    ohlcv_history_bars: int = 100
    # Interval (minutes) between exit-monitor polls for open positions.
    exit_poll_minutes: int = 30

    db_path: Path = Path("./data/bot.db")
    log_level: str = "INFO"
    timezone: str = "Asia/Jakarta"

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_ids(cls, v: object) -> list[int]:
        if v in (None, ""):
            return []
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [int(x) for x in v]
        raise ValueError("telegram_allowed_user_ids must be a comma-separated string or list")

    @field_validator("universe_symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object) -> list[str]:
        if v in (None, ""):
            return ["BTCUSDT", "HYPEUSDT", "ZECUSDT"]
        if isinstance(v, str):
            return [x.strip().upper() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [str(x).strip().upper() for x in v if str(x).strip()]
        raise ValueError("universe_symbols must be a comma-separated string or list")

    @property
    def binance_base_url(self) -> str:
        return (
            "https://testnet.binancefuture.com"
            if self.mode is Mode.TESTNET
            else "https://fapi.binance.com"
        )

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


_cached: AppConfig | None = None


def get_config() -> AppConfig:
    global _cached
    if _cached is None:
        _cached = AppConfig()
        _cached.db_path.parent.mkdir(parents=True, exist_ok=True)
    return _cached
