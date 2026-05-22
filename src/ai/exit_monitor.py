"""Exit-monitor call: re-evaluate OPEN positions between 1h bar closes.

Smaller prompt than portfolio_decision: only the open positions + a short
OHLCV tail. Cannot open new positions; only CLOSE or HOLD.
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


ExitAction = Literal["CLOSE", "HOLD"]


class ExitItem(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=False)

    symbol: str
    position_id: int
    action: ExitAction
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


class ExitDecision(BaseModel):
    items: list[ExitItem] = []


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_response(raw: str) -> ExitDecision | None:
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
            return ExitDecision.model_validate(data)
        except ValidationError as e:
            log.warning("Exit-monitor response failed schema validation: %s", e)
            return None
    return None


async def evaluate_open_positions(
    *,
    open_positions: list[dict],
    latest_prices: dict[str, Decimal],
    recent_ohlcv: dict[str, pd.DataFrame],
    historical_context: str | None = None,
) -> tuple[ExitDecision | None, str]:
    cfg = get_config()
    user_prompt = P.build_exit_monitor_user_prompt(
        open_positions=open_positions,
        recent_ohlcv=recent_ohlcv,
        latest_prices=latest_prices,
        historical_context=historical_context,
    )

    if log.isEnabledFor(logging.DEBUG):
        log.debug("Exit-monitor user prompt (truncated): %s", user_prompt[:2000])

    raw = ""
    try:
        raw = await chat(
            P.EXIT_MONITOR_SYSTEM,
            user_prompt,
            temperature=0.2,
            model=cfg.openrouter_decision_model,
            json_mode=True,
            max_tokens=500,
        )
    except Exception:
        log.exception("Exit-monitor LLM call failed")
        return None, raw

    decision = parse_response(raw)
    if decision is None:
        log.warning("Exit-monitor response unparseable (len=%d)", len(raw or ""))
    return decision, raw
