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
    openrouter_model: str = "anthropic/claude-sonnet-4.5"
    openrouter_decision_model: str = "anthropic/claude-haiku-4.5"

    coingecko_api_key: str = ""

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
