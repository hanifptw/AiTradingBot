"""Portfolio decision call (1h bar close).

Sends the full universe context (account, open positions, OHLCV per symbol) to
Grok 4.20 via OpenRouter and returns a strongly-typed list of per-symbol
decisions. The orchestrator (`strategy.portfolio_agent`) translates these into
EntrySignal / ExitSignal events.

Failure mode: any LLM/parse error returns None — orchestrator treats this as
"no actions this cycle" (fail safe).
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from src.ai import prompts as P
from src.ai.openrouter_client import chat
from src.config import get_config

log = logging.getLogger(__name__)


Action = Literal["OPEN_LONG", "OPEN_SHORT", "CLOSE", "HOLD"]


class TradeDecision(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=False)

    symbol: str
    action: Action
    size_pct_equity: float | None = None
    leverage: int | None = None
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None
    confidence: int = 0
    reasoning: str = ""

    @field_validator("symbol", mode="before")
    @classmethod
    def _upper_symbol(cls, v: object) -> str:
        return str(v).strip().upper()

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_conf(cls, v: object) -> int:
        try:
            n = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return max(0, min(100, n))


class PortfolioDecision(BaseModel):
    market_view: str = ""
    decisions: list[TradeDecision] = []


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_response(raw: str) -> PortfolioDecision | None:
    """Extract and validate a PortfolioDecision payload from an LLM response."""
    if not raw:
        return None
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    candidates = [stripped]
    m = _JSON_BLOCK.search(stripped)
    if m is not None and m.group(0) != stripped:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        try:
            return PortfolioDecision.model_validate(data)
        except ValidationError as e:
            log.warning("Portfolio response failed schema validation: %s", e)
            return None
    return None


async def decide_portfolio(
    *,
    universe_ohlcv: dict[str, pd.DataFrame],
    balance: Decimal,
    open_positions: list[dict],
    max_leverage_cap: int,
    max_equity_per_trade_pct: Decimal,
    ohlcv_history_bars: int,
) -> tuple[PortfolioDecision | None, str]:
    """Return (decision, raw_response). Caller persists raw_response for audit."""
    cfg = get_config()
    user_prompt = P.build_portfolio_user_prompt(
        balance_usdt=balance,
        open_positions=open_positions,
        universe_ohlcv=universe_ohlcv,
        ohlcv_history_bars=ohlcv_history_bars,
        max_leverage_cap=max_leverage_cap,
        max_equity_per_trade_pct=max_equity_per_trade_pct,
    )

    raw = ""
    try:
        raw = await chat(
            P.PORTFOLIO_TRADER_SYSTEM,
            user_prompt,
            temperature=0.2,
            model=cfg.openrouter_decision_model,
            json_mode=True,
            max_tokens=1500,
        )
    except Exception:
        log.exception("Portfolio decision LLM call failed")
        return None, raw

    decision = parse_response(raw)
    if decision is None:
        log.warning("Portfolio decision response unparseable (len=%d)", len(raw or ""))
    return decision, raw
