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
- TP must sit on the profit side. Target R:R >= 1.5 when possible.
- Respect the trend: don't fight a clean impulsive structure unless there is a high-conviction reversal signal.
- If unsure, HOLD.

SL placement (this matters as much as the entry — a tight SL forces you to babysit and over-close):
- Place SL just BEYOND the most relevant structural level on the 1h, with a buffer for noise:
  * for LONG: below the prior swing low / demand zone, NOT just below entry as a round %,
  * for SHORT: above the prior swing high / supply zone, mirror.
- Minimum SL distance: at least 1.5× the average 1h bar true range over the last 12-20 bars. Estimate from the OHLCV provided — typical 1h true range = mean of (high-low) across the lookback. If your candidate SL is closer than 1.5× that, widen it.
- Cap: if a structural SL would force per-trade risk above the implied 1R loss budget (max_equity_per_trade_pct × leverage), REDUCE size_pct_equity rather than tighten SL.
- Rationale: a SL at 1.5-2.5× noise lets the position breathe through normal pullbacks. A SL at 0.5-1× noise sits inside the bar range — it will either get hit by random noise or force you to CLOSE prematurely to avoid the hit. Both destroy edge.
- If you cannot find a structural SL level (chop / no clean swing), HOLD instead of opening at all.

Closing winners (when an open position is currently in profit):
- DO NOT close a winning position just because momentum slowed, a single bar pulled back, or volume thinned. That is the normal path to TP — closing here trades a known partial profit for a guessed reversal and is the single biggest source of lost edge.
- Only CLOSE a profitable position when at least one of these is present:
  (a) clean structural break against the position (lower-low for longs, higher-high for shorts on the 1h),
  (b) live_R already >= 1.0 AND a strong reversal candle/pattern is printing,
  (c) dist_to_sl_pct is shrinking vs dist_to_sl_pct_1h_ago while dist_to_tp_pct is not improving (price drifting toward SL, not TP).
- If live_R is between 0 and 1.0 and none of (a)/(b)/(c) hold, HOLD. The TP order on Binance will close at target.
- Fallback when live_R is null (no SL set): treat upnl_pct >= 2.0% as the rough "1R" threshold; the same trigger logic applies but in upnl_pct terms, and you must require a HARD reversal pattern in addition (not just a pullback bar).

Closing invalidated losers (symmetric — DO close decisively when the thesis is broken):
- A loser whose original entry trigger is invalidated should be CLOSEd BEFORE the SL hits — sitting on a clearly broken thesis just to honour the original SL wastes the difference.
- Concretely CLOSE a losing position when at least one of these is present:
  (d) live_R <= -0.5 AND counter-trend confirmation on the 1h (e.g. for a LONG: lower-high then lower-low printed since entry, OR 1h close back below the entry trigger level),
  (e) dist_to_sl_pct <= ~0.5% AND no bounce structure (closing here avoids slippage vs SL fill),
  (f) live_R null fallback: upnl_pct <= -1.0% AND counter-trend confirmation.
- Do NOT pre-emptively close a losing position still inside its normal noise band (live_R between -0.5 and 0) without (d)/(e)/(f) — that is just stop-shrinking and gives back edge to noise.

If a `## Historical context` section is present, treat it as ground truth about your past behavior. AVOID re-opening setups that match patterns from the worst-trades list (same symbol + same side + similar structure). When a symbol shows negative cumulative R over 7d, raise the bar for new entries on it. The evaluator report flags systemic issues — incorporate its diagnosis. Do NOT cite the historical block verbatim in `reasoning`; use it to inform conviction and selection.

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

You see ONLY: each open position (entry, side, size, live_R, dist_to_tp/sl, age) and a short OHLCV tail per symbol.

For each open position output one of:
- CLOSE — close immediately (market reduce-only) because structure flipped, momentum is clearly against the position, or a strong reversal is forming after meaningful profit.
- HOLD  — keep the position; SL and TP placed on Binance will take care of normal exits.

You CANNOT open new positions here. Be conservative — exiting too early eats edge. When in doubt, HOLD.

CLOSE a profitable position (upnl_pct > 0) only with a HARD trigger:
- 1h structure has flipped (for LONG: latest close < previous swing low; for SHORT: latest close > previous swing high),
- OR live_R >= 1.0 AND a strong reversal pattern is forming on the current bar,
- OR dist_to_sl_pct is shrinking vs dist_to_sl_pct_1h_ago while dist_to_tp_pct is not improving (price drifting toward SL, not TP).
- Fallback when live_R is null (no SL set): same triggers but use upnl_pct >= 2.0% as the rough "1R" reference, and require a HARD reversal pattern.

If none of those, HOLD. The TP and SL orders on Binance handle normal exits — your job is to react to genuine regime change between bars, not to nibble profit on every dip.

CLOSE an invalidated losing position when at least one of these is present:
- live_R <= -0.5 AND counter-trend confirmation on the 1h (lower-high then lower-low for LONG; mirror for SHORT),
- OR dist_to_sl_pct <= ~0.5% AND no bounce structure (avoids slippage vs SL fill),
- OR live_R null fallback: upnl_pct <= -1.0% AND counter-trend confirmation.

Do NOT pre-emptively close a losing position still inside normal noise (live_R between -0.5 and 0) without the above — that is just stop-shrinking.

If a `## Historical context` section is present, use it to recognize losing patterns earlier (e.g. a symbol with 7d negative R drifting against you may warrant a CLOSE sooner than usual).

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

DAILY_EVALUATOR_SYSTEM = """Kamu adalah trading coach berpengalaman yang mengulas performa bot trading crypto futures berbasis AI (Grok 4.20, strategi portfolio 1h).

Kamu menerima: trade yang sudah closed dalam 24 jam terakhir, statistik agregat, dan batas keamanan (caps) bot saat ini.
Tulis dalam Bahasa Indonesia. Output Markdown (tanpa code fence) maksimal 400 kata, mencakup:

1. Pola apa yang muncul dari trade menang vs kalah? Sebutkan trade spesifik.
2. Apakah pilihan sizing/leverage AI sudah terkalibrasi dengan baik, atau terlalu agresif/konservatif?
3. Apakah safety caps (max leverage, max equity per trade) sering tercapai? Sarankan angka konkret.
4. Hingga tiga saran actionable untuk operator (caps mana yang perlu diubah, simbol mana yang perlu ditambah/dihapus dari universe, dll).

Spesifik dan kuantitatif. Hindari klise manajemen risiko yang generik."""


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


def _fmt_pct(v: float | None) -> str:
    return f"{v:+.2f}%" if v is not None else "n/a"


def _fmt_r(v: float | None) -> str:
    return f"{v:+.2f}" if v is not None else "n/a"


def _format_position(p: dict) -> str:
    upnl = _fmt_pct(p.get("upnl_pct"))
    live_r = _fmt_r(p.get("live_r"))
    dtp = _fmt_pct(p.get("dist_to_tp_pct"))
    dsl = _fmt_pct(p.get("dist_to_sl_pct"))
    dtp_prev = _fmt_pct(p.get("dist_to_tp_pct_1h_ago"))
    dsl_prev = _fmt_pct(p.get("dist_to_sl_pct_1h_ago"))
    return (
        f"  - id={p['id']} {p['symbol']} {p['side']} qty={p['qty']} "
        f"entry={p['entry_price']} lev={p['leverage']}x "
        f"sl={p.get('sl_price', 'n/a')} tp={p.get('tp_price', 'n/a')} "
        f"upnl_pct={upnl} live_R={live_r} "
        f"dist_to_tp={dtp}(prev={dtp_prev}) dist_to_sl={dsl}(prev={dsl_prev}) "
        f"bars_open={p['bars_open']}"
    )


# Maximum age (hours) at which we still surface the evaluator report. Beyond
# this, the report is more likely to mislead than help (regime drift).
_EVAL_REPORT_MAX_AGE_HOURS = 168  # 7 days
_EVAL_REPORT_STALE_AGE_HOURS = 48
_EVAL_REPORT_MAX_WORDS = 250


def format_historical_context(
    *,
    trades_count: int,
    stats: dict | None,
    per_symbol: list[dict],
    worst: list[dict],
    last_report_md: str | None,
    last_report_age_hours: float | None,
) -> str | None:
    """Render the historical-context block injected into trading prompts.

    Returns None when there is nothing to surface (no trades AND no report) —
    callers should skip the block entirely in that case.
    """
    has_trades = trades_count > 0
    has_fresh_report = (
        last_report_md is not None
        and last_report_age_hours is not None
        and last_report_age_hours <= _EVAL_REPORT_MAX_AGE_HOURS
    )
    if not has_trades and not has_fresh_report:
        return None

    parts: list[str] = [
        "## Historical context (last 7d) — use to AVOID repeating prior loss patterns"
    ]

    if has_trades and stats is not None:
        avg_r = stats["avg_r"]
        best_r = stats["best_r"]
        worst_r = stats["worst_r"]
        parts.append("### Aggregate")
        parts.append(
            f"- trades: {stats['count']} | wins: {stats['wins']} "
            f"({stats['win_rate']:.1f}%) | losses: {stats['losses']}"
        )
        agg_line = f"- total_pnl: {float(stats['total_pnl_usdt']):+.2f} USDT"
        agg_line += f" | avg_R: {avg_r:+.2f}" if avg_r is not None else " | avg_R: n/a"
        if best_r is not None and worst_r is not None:
            agg_line += f" | best_R: {best_r:+.2f} | worst_R: {worst_r:+.2f}"
        parts.append(agg_line)

    if per_symbol:
        parts.append("\n### Per-symbol (7d, ≥1 trade)")
        for row in per_symbol[:10]:
            avg_r_str = f"{row['avg_r']:+.2f}" if row["avg_r"] is not None else "n/a"
            parts.append(
                f"- {row['symbol']}: {row['count']} trades, {row['win_rate']:.1f}% wins, "
                f"pnl={float(row['total_pnl']):+.2f}, avg_R={avg_r_str}"
            )

    if worst:
        parts.append("\n### Worst trades (ascending R)")
        for i, t in enumerate(worst, 1):
            r = t.get("r_multiple")
            r_str = f"{float(r):+.2f}" if r is not None else "n/a"
            parts.append(
                f"{i}. {t['symbol']} {t['side']} "
                f"entry={t['entry_price']} exit={t['exit_price']} "
                f"R={r_str} reason={t.get('close_reason', 'n/a')}"
            )

    if (
        last_report_md is not None
        and last_report_age_hours is not None
        and last_report_age_hours <= _EVAL_REPORT_MAX_AGE_HOURS
    ):
        stale_tag = (
            " (STALE — older than 48h)"
            if last_report_age_hours > _EVAL_REPORT_STALE_AGE_HOURS
            else ""
        )
        words = last_report_md.split()
        trimmed = " ".join(words[:_EVAL_REPORT_MAX_WORDS])
        if len(words) > _EVAL_REPORT_MAX_WORDS:
            trimmed += " ... [truncated]"
        parts.append(f"\n### Latest evaluator report (age≈{last_report_age_hours:.1f}h){stale_tag}")
        parts.append(trimmed)

    return "\n".join(parts)


def build_portfolio_user_prompt(
    *,
    balance_usdt: Decimal,
    open_positions: list[dict],
    universe_ohlcv: dict[str, pd.DataFrame],
    ohlcv_history_bars: int,
    max_leverage_cap: int,
    max_equity_per_trade_pct: Decimal,
    historical_context: str | None = None,
) -> str:
    pos_block = (
        "\n".join(_format_position(p) for p in open_positions) if open_positions else "  (none)"
    )
    universe_block_parts: list[str] = []
    for sym, df in universe_ohlcv.items():
        universe_block_parts.append(f"### {sym} (1h)\n{_ohlcv_rows(df, ohlcv_history_bars)}")
    universe_block = "\n\n".join(universe_block_parts)

    hist_block = f"{historical_context}\n\n" if historical_context else ""

    return (
        f"## Caps (hard limits, you cannot exceed)\n"
        f"- max leverage: {max_leverage_cap}x\n"
        f"- max equity per trade: {max_equity_per_trade_pct}%\n\n"
        f"{hist_block}"
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
    historical_context: str | None = None,
) -> str:
    if not open_positions:
        return "## Open positions\n  (none)\n\nReturn an empty items array."

    pos_block = "\n".join(_format_position(p) for p in open_positions)
    price_block = "\n".join(f"  - {sym}: {price:.6g}" for sym, price in latest_prices.items())
    universe_parts = [
        f"### {sym} (last 50 1h bars)\n{_ohlcv_rows(df, 50)}" for sym, df in recent_ohlcv.items()
    ]
    universe_block = "\n\n".join(universe_parts) if universe_parts else "(no OHLCV available)"

    hist_block = f"{historical_context}\n\n" if historical_context else ""

    return (
        f"{hist_block}"
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
