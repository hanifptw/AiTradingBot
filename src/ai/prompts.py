"""System + user prompts for all three AI calls.

- PORTFOLIO_TRADER_SYSTEM / build_portfolio_user_prompt
    1h bar-close: model sees account, open positions, and 1h OHLCV per universe symbol.
    Returns OPEN_LONG / OPEN_SHORT / CLOSE / HOLD per symbol with full trade params.

- EXIT_MONITOR_SYSTEM / build_exit_monitor_user_prompt
    Intra-bar polling: model only re-evaluates open positions. CLOSE or HOLD only.

- DAILY_EVALUATOR_SYSTEM / build_daily_evaluator_user_prompt
    Daily review: model analyses recent closed trades to suggest tuning.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

# ── Live trader (Grok 4.20) ────────────────────────────────────────────────

PORTFOLIO_TRADER_SYSTEM = """You are an autonomous Binance Futures USDT-M trader running on the 1-hour timeframe.

Each cycle (one per closed 1h bar) you receive:
- account balance in USDT
- a list of currently open positions (entry, side, size, unrealized PnL, age in bars)
- 1-hour OHLCV history for each symbol in the universe

For every symbol in the universe you must decide one of:
- OPEN_LONG   — open a new long position (only allowed when there is no existing open position on this symbol)
- OPEN_SHORT  — open a new short position (only when no existing open position)
- CLOSE       — close the currently open position on this symbol (only allowed when one exists)
- HOLD        — do nothing (default; valid for both "no position" and "existing position you want to keep")

For OPEN_* actions you must also output:
- size_pct_equity (float, 0-100): position notional as percentage of total equity. Will be hard-capped at max_equity_per_trade_pct.
- leverage (int, 1..max_leverage_cap): per-position leverage. Will be hard-capped.
- sl_price (decimal): absolute stop-loss price.
- tp_price (decimal): absolute take-profit price.
- confidence (int, 0-100): your conviction in this setup.
- reasoning (string, <= 1 sentence): the trigger.

For CLOSE and HOLD, size/leverage/sl/tp may be null; confidence + reasoning still required.

Trade hygiene:
- Be selective. HOLD is acceptable — and expected — for most symbols on most bars.
- Never propose an OPEN on a symbol that already has an open position; CLOSE first if you want to flip.
- SL must sit on the loss side of the entry; TP on the profit side. Target R:R >= 1.5 when possible.
- Respect the trend: don't fight a clean impulsive structure unless there is a high-conviction reversal signal.
- If unsure, HOLD.

Output STRICT JSON ONLY (no prose, no markdown fences) matching this schema:
{
  "market_view": "<1 short paragraph: regime / volatility / risk-on/off>",
  "decisions": [
    {
      "symbol": "<UPPER>",
      "action": "OPEN_LONG" | "OPEN_SHORT" | "CLOSE" | "HOLD",
      "size_pct_equity": <0-100 or null>,
      "leverage": <1-N or null>,
      "sl_price": <decimal or null>,
      "tp_price": <decimal or null>,
      "confidence": <0-100>,
      "reasoning": "<short string>"
    },
    ...
  ]
}"""


EXIT_MONITOR_SYSTEM = """You re-evaluate OPEN crypto-futures positions between 1h bar closes.

You see ONLY: each open position (entry, side, size, unrealized PnL, age) and a short OHLCV tail per symbol.

For each open position output one of:
- CLOSE — close immediately (market reduce-only) because structure flipped, momentum is clearly against the position, or a strong reversal is forming after meaningful profit.
- HOLD  — keep the position; SL and TP placed on Binance will take care of normal exits.

You CANNOT open new positions here. Be conservative — exiting too early eats edge. When in doubt, HOLD.

Output STRICT JSON ONLY:
{
  "items": [
    {
      "symbol": "<UPPER>",
      "position_id": <int>,
      "action": "CLOSE" | "HOLD",
      "confidence": <0-100>,
      "reasoning": "<short string>"
    }
  ]
}"""


# ── Daily evaluator (Sonnet 4.5) ───────────────────────────────────────────

DAILY_EVALUATOR_SYSTEM = """You are an experienced crypto futures trading coach reviewing the
performance of an AI-controlled trading bot (Grok 4.20 on Hyperliquid-style 1h portfolio strategy).

You receive: the last 24 hours of closed trades, aggregate stats, and the bot's current
safety caps. Output Markdown (no code fences) under 400 words covering:

1. What patterns are emerging in winners vs losers? Reference specific trades.
2. Is the AI sizing/leverage choice well-calibrated to outcomes, or systematically too aggressive/conservative?
3. Are the safety caps (max leverage, max equity per trade) being hit often? Suggest concrete numeric tweaks.
4. Up to three actionable suggestions for the operator (which caps to adjust, which symbols to add/remove from universe, etc.).

Be specific, quantitative. Avoid generic risk-management platitudes."""


# ── User-prompt builders ───────────────────────────────────────────────────


def _ohlcv_rows(df: pd.DataFrame, n: int) -> str:
    """Compact CSV-like OHLCV (oldest → newest)."""
    tail = df.tail(n)
    lines = ["t,o,h,l,c,v"]
    for _, r in tail.iterrows():
        ts = r["close_time"]
        ts_str = ts.strftime("%Y-%m-%dT%H:%MZ") if hasattr(ts, "strftime") else str(ts)
        lines.append(
            f"{ts_str},{float(r['open']):.6g},{float(r['high']):.6g},"
            f"{float(r['low']):.6g},{float(r['close']):.6g},{float(r['volume']):.4g}"
        )
    return "\n".join(lines)


def _format_position(p: dict) -> str:
    return (
        f"  - id={p['id']} {p['symbol']} {p['side']} qty={p['qty']} "
        f"entry={p['entry_price']} lev={p['leverage']}x "
        f"sl={p.get('sl_price', 'n/a')} tp={p.get('tp_price', 'n/a')} "
        f"upnl_pct={p['upnl_pct']:+.2f}% bars_open={p['bars_open']}"
    )


def build_portfolio_user_prompt(
    *,
    balance_usdt: Decimal,
    open_positions: list[dict],
    universe_ohlcv: dict[str, pd.DataFrame],
    ohlcv_history_bars: int,
    max_leverage_cap: int,
    max_equity_per_trade_pct: Decimal,
) -> str:
    pos_block = (
        "\n".join(_format_position(p) for p in open_positions) if open_positions else "  (none)"
    )
    universe_block_parts: list[str] = []
    for sym, df in universe_ohlcv.items():
        universe_block_parts.append(f"### {sym} (1h)\n{_ohlcv_rows(df, ohlcv_history_bars)}")
    universe_block = "\n\n".join(universe_block_parts)

    return (
        f"## Caps (hard limits, you cannot exceed)\n"
        f"- max leverage: {max_leverage_cap}x\n"
        f"- max equity per trade: {max_equity_per_trade_pct}%\n\n"
        f"## Account\n"
        f"- balance: {balance_usdt:.4f} USDT\n\n"
        f"## Open positions\n{pos_block}\n\n"
        f"## Universe — last {ohlcv_history_bars} 1h bars per symbol\n"
        f"(time format: ISO8601 UTC, columns: t,open,high,low,close,volume)\n\n"
        f"{universe_block}\n\n"
        "Decide per symbol now. Strict JSON only."
    )


def build_exit_monitor_user_prompt(
    *,
    open_positions: list[dict],
    recent_ohlcv: dict[str, pd.DataFrame],
    latest_prices: dict[str, Decimal],
) -> str:
    if not open_positions:
        return "## Open positions\n  (none)\n\nReturn an empty items array."

    pos_block = "\n".join(_format_position(p) for p in open_positions)
    price_block = "\n".join(f"  - {sym}: {price:.6g}" for sym, price in latest_prices.items())
    universe_parts = [
        f"### {sym} (last 50 1h bars)\n{_ohlcv_rows(df, 50)}" for sym, df in recent_ohlcv.items()
    ]
    universe_block = "\n\n".join(universe_parts) if universe_parts else "(no OHLCV available)"

    return (
        f"## Open positions\n{pos_block}\n\n"
        f"## Latest prices\n{price_block}\n\n"
        f"## Recent 1h OHLCV\n{universe_block}\n\n"
        "Decide per open position. Strict JSON only."
    )


def build_daily_evaluator_user_prompt(
    *,
    mode: str,
    max_leverage_cap: int,
    max_equity_per_trade_pct: Decimal,
    exit_poll_minutes: int,
    universe: list[str],
    trades: list[dict],
    total_pnl: Decimal,
    win_rate: float,
    avg_r: float,
    avg_duration_min: float,
) -> str:
    if not trades:
        ledger = "(no closed trades in the last day)"
    else:
        rows = [
            "| # | symbol | side | entry | exit | pnl USDT | pnl % | R | reason | dur(min) |",
            "|---|--------|------|-------|------|----------|-------|---|--------|----------|",
        ]
        for i, t in enumerate(trades, 1):
            r = f"{t['r_multiple']:.2f}" if t.get("r_multiple") is not None else "—"
            rows.append(
                f"| {i} | {t['symbol']} | {t['side']} | {t['entry_price']} | {t['exit_price']} | "
                f"{t['pnl_usdt']:.2f} | {t['pnl_pct']:.2f}% | {r} | {t['close_reason']} | "
                f"{t['duration_min']} |"
            )
        ledger = "\n".join(rows)

    return (
        f"## Bot configuration\n"
        f"- Mode: {mode}\n"
        f"- Max leverage cap: {max_leverage_cap}x\n"
        f"- Max equity per trade: {max_equity_per_trade_pct}%\n"
        f"- Exit-monitor poll: every {exit_poll_minutes} minutes\n"
        f"- Universe: {', '.join(universe)}\n\n"
        f"## Aggregate stats (last {len(trades)} closed trades, ≤ 24h)\n"
        f"- Total PnL: {total_pnl:.2f} USDT\n"
        f"- Win rate: {win_rate:.1f}%\n"
        f"- Average R-multiple: {avg_r:.2f}\n"
        f"- Average duration: {avg_duration_min:.1f} min\n\n"
        f"## Trade ledger (most recent first)\n{ledger}\n\n"
        "Diagnose and recommend up to three concrete adjustments."
    )
