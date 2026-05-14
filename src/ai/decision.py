"""AI entry filter and bar-close early-exit decisions.

Two pure async entry points:
  - confirm_entry(...)     → called before placing an order
  - should_exit_early(...) → called once per bar close per open position

Both build a compact market-context block from the kline DataFrame already
fetched upstream (no extra Binance HTTP), call OpenRouter with the
configured decision model, parse JSON, and persist an `AIDecision` audit
row. On any failure they FAIL SAFE: entry → reject, exit → hold.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from src.ai import decision_prompts as P
from src.ai.openrouter_client import chat
from src.config import get_config
from src.core import repository as repo
from src.core.db import session
from src.core.models import AIDecision, Position, Side
from src.indicators.stochastic import StochParams, stochastic

log = logging.getLogger(__name__)

_LEDGER_BARS = 30           # OHLCV rows sent to the model
_FEATURE_LOOKBACK = 20      # bars used for swings/volume/atr


# ── result types ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EntryDecision:
    approve: bool
    confidence: int
    reason: str
    raw: str


@dataclass(frozen=True)
class ExitDecision:
    exit: bool
    confidence: int
    reason: str
    raw: str


# ── feature extraction ──────────────────────────────────────────────────────

def _features(df: pd.DataFrame, params: StochParams) -> dict[str, str]:
    """Compute the small numeric feature set sent in the prompt.

    Caller passes a DataFrame of closed bars only (no in-progress bar).
    """
    tail = df.tail(max(_FEATURE_LOOKBACK + params.k + params.smooth + params.d, 60))
    closes = tail["close"].astype(float)
    highs = tail["high"].astype(float)
    lows = tail["low"].astype(float)
    vols = tail["volume"].astype(float)

    last_close = closes.iloc[-1]

    def _mom(n: int) -> str:
        if len(closes) <= n:
            return "n/a"
        prev = closes.iloc[-(n + 1)]
        if prev == 0:
            return "n/a"
        return f"{((last_close - prev) / prev * 100):+.2f}%"

    swing_high = highs.tail(_FEATURE_LOOKBACK).max()
    swing_low = lows.tail(_FEATURE_LOOKBACK).min()

    vol_ratio = "n/a"
    if len(vols) >= 21:
        avg = vols.tail(21).iloc[:-1].mean()
        if avg > 0:
            vol_ratio = f"{(vols.iloc[-1] / avg):.2f}"

    # ATR-like: mean of |high-low| over 20 bars, as % of last close.
    atr_pct = "n/a"
    if len(tail) >= 20 and last_close > 0:
        tr = (highs - lows).tail(20).mean()
        atr_pct = f"{(tr / last_close * 100):.2f}"

    stoch_df = stochastic(tail, params)
    k_val = stoch_df["stoch_k"].iloc[-1]
    d_val = stoch_df["stoch_d"].iloc[-1]
    stoch_k = f"{k_val:.2f}" if pd.notna(k_val) else "n/a"
    stoch_d = f"{d_val:.2f}" if pd.notna(d_val) else "n/a"

    return {
        "n_bars": str(len(tail)),
        "swing_high": f"{swing_high:.6g}",
        "swing_low": f"{swing_low:.6g}",
        "mom_5": _mom(5),
        "mom_10": _mom(10),
        "mom_20": _mom(20),
        "vol_ratio": vol_ratio,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "atr_pct": atr_pct,
    }


def _ledger(df: pd.DataFrame, n: int = _LEDGER_BARS) -> str:
    tail = df.tail(n)
    lines = ["time | open | high | low | close | volume"]
    for _, r in tail.iterrows():
        ts = r["close_time"]
        ts_str = ts.strftime("%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
        lines.append(
            f"{ts_str} | {float(r['open']):.6g} | {float(r['high']):.6g} | "
            f"{float(r['low']):.6g} | {float(r['close']):.6g} | {float(r['volume']):.4g}"
        )
    return "\n".join(lines)


# ── response parsing ────────────────────────────────────────────────────────

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(raw: str) -> dict | None:
    """Extract the first JSON object from a possibly-noisy LLM response."""
    if not raw:
        return None
    # Strip common markdown fences.
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(stripped)
    if m is None:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _clamp_conf(v: object) -> int:
    try:
        n = int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


# ── entry filter ────────────────────────────────────────────────────────────

async def confirm_entry(
    *,
    symbol: str,
    side: Side,
    entry_price: Decimal,
    sl_price: Decimal,
    tp_price: Decimal,
    sl_pct: Decimal,
    tp_pct: Decimal,
    timeframe: str,
    df: pd.DataFrame,
    params: StochParams,
    min_confidence: int,
) -> EntryDecision:
    cfg = get_config()
    rr = (tp_pct / sl_pct) if sl_pct > 0 else Decimal("0")
    feats = _features(df, params)
    user = P.ENTRY_FILTER_USER_TEMPLATE.format(
        symbol=symbol,
        side=side.value,
        entry_price=f"{entry_price:.6g}",
        sl_price=f"{sl_price:.6g}",
        tp_price=f"{tp_price:.6g}",
        sl_pct=f"{sl_pct:.2f}",
        tp_pct=f"{tp_pct:.2f}",
        rr_ratio=f"{rr:.2f}",
        timeframe=timeframe,
        ledger_n=_LEDGER_BARS,
        ledger=_ledger(df),
        **feats,
    )

    raw = ""
    parsed: dict | None = None
    try:
        raw = await chat(
            P.ENTRY_FILTER_SYSTEM, user,
            temperature=0.2, model=cfg.openrouter_decision_model,
        )
        parsed = _parse_json(raw)
    except Exception:
        log.exception("AI entry filter call failed for %s %s", side.value, symbol)

    if parsed is None:
        decision = EntryDecision(
            approve=False, confidence=0,
            reason="AI response unparseable — failing safe (reject).", raw=raw,
        )
    else:
        approve_raw = bool(parsed.get("approve", False))
        confidence = _clamp_conf(parsed.get("confidence", 0))
        reason = str(parsed.get("reason") or "")[:500]
        # Confidence floor enforces the user-configurable threshold.
        approve = approve_raw and confidence >= min_confidence
        if approve_raw and not approve:
            reason = f"AI approved at {confidence}% (below floor {min_confidence}%). {reason}"
        decision = EntryDecision(approve=approve, confidence=confidence, reason=reason, raw=raw)

    async with session() as s:
        await repo.add_ai_decision(s, AIDecision(
            decision_type="ENTRY",
            symbol=symbol,
            side=side.value,
            action="APPROVE" if decision.approve else "REJECT",
            confidence=decision.confidence,
            reason=decision.reason,
            model=cfg.openrouter_decision_model,
            raw_response=raw[:4000] if raw else None,
            position_id=None,
        ))
    return decision


# ── early-exit monitor ──────────────────────────────────────────────────────

async def should_exit_early(
    *,
    pos: Position,
    current_price: Decimal,
    timeframe: str,
    df: pd.DataFrame,
    params: StochParams,
) -> ExitDecision:
    cfg = get_config()

    direction = Decimal("1") if pos.side == Side.LONG.value else Decimal("-1")
    unrealized_pct = (current_price - pos.entry_price) / pos.entry_price * Decimal("100") * direction

    # R-multiple needs SL distance from entry.
    unrealized_r: Decimal | None = None
    if pos.sl_price and pos.sl_price > 0:
        sl_dist_pct = abs(pos.entry_price - pos.sl_price) / pos.entry_price * Decimal("100")
        if sl_dist_pct > 0:
            unrealized_r = unrealized_pct / sl_dist_pct

    # Bars-in-trade is approximate: count rows after opened_at.
    bars_in_trade = 0
    if "close_time" in df.columns:
        bars_in_trade = int((df["close_time"] >= pd.Timestamp(pos.opened_at)).sum())

    feats = _features(df, params)
    user = P.EARLY_EXIT_USER_TEMPLATE.format(
        symbol=pos.symbol,
        side=pos.side,
        entry_price=f"{pos.entry_price:.6g}",
        current_price=f"{current_price:.6g}",
        unrealized_pct=f"{unrealized_pct:+.2f}",
        unrealized_r=f"{unrealized_r:+.2f}" if unrealized_r is not None else "n/a",
        bars_in_trade=bars_in_trade,
        sl_price=f"{pos.sl_price:.6g}" if pos.sl_price else "n/a",
        tp_price=f"{pos.tp_price:.6g}" if pos.tp_price else "n/a",
        timeframe=timeframe,
        ledger_n=_LEDGER_BARS,
        ledger=_ledger(df),
        **feats,
    )

    raw = ""
    parsed: dict | None = None
    try:
        raw = await chat(
            P.EARLY_EXIT_SYSTEM, user,
            temperature=0.2, model=cfg.openrouter_decision_model,
        )
        parsed = _parse_json(raw)
    except Exception:
        log.exception("AI early-exit call failed for %s %s", pos.side, pos.symbol)

    if parsed is None:
        decision = ExitDecision(
            exit=False, confidence=0,
            reason="AI response unparseable — failing safe (hold).", raw=raw,
        )
    else:
        decision = ExitDecision(
            exit=bool(parsed.get("exit", False)),
            confidence=_clamp_conf(parsed.get("confidence", 0)),
            reason=str(parsed.get("reason") or "")[:500],
            raw=raw,
        )

    async with session() as s:
        await repo.add_ai_decision(s, AIDecision(
            decision_type="EARLY_EXIT",
            symbol=pos.symbol,
            side=pos.side,
            action="EXIT" if decision.exit else "HOLD",
            confidence=decision.confidence,
            reason=decision.reason,
            model=cfg.openrouter_decision_model,
            raw_response=raw[:4000] if raw else None,
            position_id=pos.id,
        ))
    return decision
