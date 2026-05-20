"""Portfolio agent — orchestrates AI calls and publishes EntrySignal/ExitSignal.

Two cycles:
- run_bar_close_cycle: triggered once per closed 1h bar. Fetches OHLCV for the
  full universe, asks Grok for portfolio-wide decisions, validates caps, and
  publishes entry/exit signals on the event bus.
- run_exit_poll_cycle: triggered every `exit_poll_minutes`. Re-evaluates open
  positions only; can publish ExitSignal but never EntrySignal.

All failures are caught and logged; the bot never crashes from a bad LLM
response (fail-safe: no action).
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pandas as pd

from src.ai import exit_monitor, portfolio_decision
from src.config import get_config
from src.core import repository as repo
from src.core.db import session
from src.core.events import EntrySignal, ExitSignal, get_bus
from src.core.models import AIDecision, Position, Side
from src.market.binance_client import get_binance
from src.tgbot.notifier import notify

log = logging.getLogger(__name__)

# Per-process idempotency: last 1h bar close_time (ms) we processed.
_last_bar_seen_ms: int = 0


def _position_view(pos: Position, mark_price: Decimal | None) -> dict:
    """Compact JSON-able snapshot for prompts."""
    direction = Decimal("1") if pos.side == Side.LONG.value else Decimal("-1")
    upnl_pct = Decimal("0")
    if mark_price is not None and pos.entry_price > 0:
        upnl_pct = (mark_price - pos.entry_price) / pos.entry_price * Decimal("100") * direction
    return {
        "id": pos.id,
        "symbol": pos.symbol,
        "side": pos.side,
        "qty": f"{float(pos.qty):.8g}",
        "entry_price": f"{float(pos.entry_price):.6g}",
        "sl_price": f"{float(pos.sl_price):.6g}" if pos.sl_price else "n/a",
        "tp_price": f"{float(pos.tp_price):.6g}" if pos.tp_price else "n/a",
        "leverage": pos.leverage,
        "upnl_pct": float(upnl_pct),
        # bars_open is approximated by caller (needs current time).
        "bars_open": 0,
    }


async def _fetch_universe_ohlcv(symbols: list[str], bars: int) -> dict[str, pd.DataFrame]:
    binance = get_binance()
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = await binance.klines(sym, "1h", limit=bars + 1)
            if df.empty:
                continue
            # Drop the in-progress bar.
            out[sym] = df.iloc[:-1].tail(bars)
        except Exception:
            log.exception("Failed to fetch klines for %s", sym)
    return out


async def _account_balance() -> Decimal:
    binance = get_binance()
    try:
        wallet, _avail = await binance.account_balance_usdt()
        return wallet
    except Exception:
        log.exception("Failed to fetch account balance")
        return Decimal("0")


async def _latest_bar_close_ms(symbols: list[str]) -> int:
    binance = get_binance()
    latest = 0
    for sym in symbols:
        try:
            df = await binance.klines(sym, "1h", limit=2)
            if df.empty:
                continue
            df_closed = df.iloc[:-1]
            if df_closed.empty:
                continue
            ts = int(df_closed["close_time"].iloc[-1].value // 1_000_000)
            latest = max(latest, ts)
        except Exception:
            log.exception("Failed to peek latest bar for %s", sym)
    return latest


async def run_bar_close_cycle() -> None:
    """Top-level entrypoint called by the scheduler at minute 0:10 after each 1h close."""
    global _last_bar_seen_ms
    cfg = get_config()
    symbols = cfg.universe_symbols

    latest_ms = await _latest_bar_close_ms(symbols)
    if latest_ms <= _last_bar_seen_ms:
        log.debug("No new closed 1h bar yet — skipping")
        return
    _last_bar_seen_ms = latest_ms

    universe_ohlcv = await _fetch_universe_ohlcv(symbols, cfg.ohlcv_history_bars)
    if not universe_ohlcv:
        log.warning("Portfolio cycle: no OHLCV fetched — skipping")
        return

    balance = await _account_balance()

    async with session() as s:
        settings = await repo.get_settings(s)
        open_positions_db = await repo.open_positions(s)

    # Map mark prices to compute unrealized PnL for the prompt.
    mark_prices: dict[str, Decimal] = {}
    binance = get_binance()
    for pos in open_positions_db:
        try:
            mark_prices[pos.symbol] = await binance.mark_price(pos.symbol)
        except Exception:
            log.exception("mark_price failed for %s", pos.symbol)

    pos_views = [_position_view(p, mark_prices.get(p.symbol)) for p in open_positions_db]

    decision, raw_response = await portfolio_decision.decide_portfolio(
        universe_ohlcv=universe_ohlcv,
        balance=balance,
        open_positions=pos_views,
        max_leverage_cap=settings.max_leverage_cap,
        max_equity_per_trade_pct=settings.max_equity_per_trade_pct,
        ohlcv_history_bars=cfg.ohlcv_history_bars,
    )

    if decision is None:
        await notify("⚠️ *AI portfolio cycle failed* (parse/LLM error). No action this cycle.")
        return

    log.info(
        "Portfolio cycle: %d decisions (market_view=%s)",
        len(decision.decisions),
        decision.market_view[:80],
    )

    # Map symbol → existing open position for fast lookup.
    open_by_symbol = {p.symbol: p for p in open_positions_db}

    bus = get_bus()
    for td in decision.decisions:
        await _apply_decision(
            td=td,
            settings=settings,
            symbols_in_universe=symbols,
            open_by_symbol=open_by_symbol,
            universe_ohlcv=universe_ohlcv,
            raw_response=raw_response,
            bus=bus,
        )


async def _apply_decision(
    *,
    td: portfolio_decision.TradeDecision,
    settings,
    symbols_in_universe: list[str],
    open_by_symbol: dict[str, Position],
    universe_ohlcv: dict[str, pd.DataFrame],
    raw_response: str,
    bus,
) -> None:
    cfg = get_config()
    if td.symbol not in symbols_in_universe:
        log.warning("AI proposed unknown symbol %s — ignoring", td.symbol)
        return

    existing_pos = open_by_symbol.get(td.symbol)

    # Reference last close as the signal price.
    df = universe_ohlcv.get(td.symbol)
    last_close = Decimal(str(df["close"].iloc[-1])) if df is not None and not df.empty else None

    if td.action == "HOLD":
        await _audit(
            decision_type="PORTFOLIO",
            td=td,
            position_id=existing_pos.id if existing_pos else None,
            raw_response=raw_response,
        )
        return

    if td.action == "CLOSE":
        if existing_pos is None:
            log.info("AI CLOSE for %s but no open position — ignoring", td.symbol)
            await _audit(
                decision_type="PORTFOLIO",
                td=td,
                position_id=None,
                raw_response=raw_response,
            )
            return
        dec_id = await _audit(
            decision_type="PORTFOLIO",
            td=td,
            position_id=existing_pos.id,
            raw_response=raw_response,
        )
        await bus.publish(
            ExitSignal(
                symbol=td.symbol,
                position_id=existing_pos.id,
                reason="AI_EXIT",
                price=last_close or existing_pos.entry_price,
                decision_id=dec_id,
            )
        )
        return

    # OPEN_LONG / OPEN_SHORT
    if existing_pos is not None:
        log.info(
            "AI %s for %s but existing position already open — ignoring",
            td.action,
            td.symbol,
        )
        await _audit(
            decision_type="PORTFOLIO",
            td=td,
            position_id=existing_pos.id,
            raw_response=raw_response,
        )
        return

    if not settings.autotrade_enabled:
        log.info("Autotrade disabled — skipping %s %s", td.action, td.symbol)
        await _audit(
            decision_type="PORTFOLIO",
            td=td,
            position_id=None,
            raw_response=raw_response,
        )
        return

    if (
        td.sl_price is None
        or td.tp_price is None
        or td.size_pct_equity is None
        or td.leverage is None
    ):
        log.warning("AI %s %s missing required fields — skipping", td.action, td.symbol)
        await _audit(
            decision_type="PORTFOLIO",
            td=td,
            position_id=None,
            raw_response=raw_response,
        )
        return

    if last_close is None:
        log.warning("No reference price for %s — skipping entry", td.symbol)
        return

    # Clamp size + leverage against caps.
    size_pct = max(
        Decimal("0"),
        min(
            Decimal(str(td.size_pct_equity)),
            settings.max_equity_per_trade_pct,
        ),
    )
    lev = max(1, min(int(td.leverage), settings.max_leverage_cap))

    if size_pct <= 0:
        log.info("Clamped size_pct=0 for %s — skipping", td.symbol)
        await _audit(
            decision_type="PORTFOLIO",
            td=td,
            position_id=None,
            raw_response=raw_response,
        )
        return

    side = Side.LONG if td.action == "OPEN_LONG" else Side.SHORT

    # Validate SL/TP on the correct side of entry.
    if side is Side.LONG and not (td.sl_price < last_close < td.tp_price):
        log.warning(
            "Invalid SL/TP for LONG %s (sl=%s entry≈%s tp=%s) — skipping",
            td.symbol,
            td.sl_price,
            last_close,
            td.tp_price,
        )
        await _audit(
            decision_type="PORTFOLIO",
            td=td,
            position_id=None,
            raw_response=raw_response,
        )
        return
    if side is Side.SHORT and not (td.tp_price < last_close < td.sl_price):
        log.warning(
            "Invalid SL/TP for SHORT %s (tp=%s entry≈%s sl=%s) — skipping",
            td.symbol,
            td.tp_price,
            last_close,
            td.sl_price,
        )
        await _audit(
            decision_type="PORTFOLIO",
            td=td,
            position_id=None,
            raw_response=raw_response,
        )
        return

    dec_id = await _audit(
        decision_type="PORTFOLIO",
        td=td,
        position_id=None,
        raw_response=raw_response,
        applied_size_pct=size_pct,
        applied_leverage=lev,
    )

    _ = cfg  # silence unused
    await bus.publish(
        EntrySignal(
            symbol=td.symbol,
            side=side,
            price=last_close,
            size_pct_equity=size_pct,
            leverage=lev,
            sl_price=td.sl_price,
            tp_price=td.tp_price,
            confidence=td.confidence,
            decision_id=dec_id,
            reason="AI_PORTFOLIO",
        )
    )


async def _audit(
    *,
    decision_type: str,
    td: portfolio_decision.TradeDecision,
    position_id: int | None,
    raw_response: str,
    applied_size_pct: Decimal | None = None,
    applied_leverage: int | None = None,
) -> int:
    cfg = get_config()
    side: str | None
    if td.action == "OPEN_LONG":
        side = Side.LONG.value
    elif td.action == "OPEN_SHORT":
        side = Side.SHORT.value
    else:
        side = None

    async with session() as s:
        row = AIDecision(
            decision_type=decision_type,
            symbol=td.symbol,
            side=side,
            action=td.action,
            confidence=td.confidence,
            reason=td.reasoning[:500] if td.reasoning else None,
            model=cfg.openrouter_decision_model,
            raw_response=raw_response[:4000] if raw_response else None,
            position_id=position_id,
            size_pct_equity=(
                applied_size_pct
                if applied_size_pct is not None
                else (Decimal(str(td.size_pct_equity)) if td.size_pct_equity is not None else None)
            ),
            leverage=(applied_leverage if applied_leverage is not None else td.leverage),
            sl_price=td.sl_price,
            tp_price=td.tp_price,
        )
        saved = await repo.add_ai_decision(s, row)
    return saved.id


# ── Exit-monitor cycle ─────────────────────────────────────────────────────


async def run_exit_poll_cycle() -> None:
    cfg = get_config()
    async with session() as s:
        open_positions_db = await repo.open_positions(s)
    if not open_positions_db:
        return

    binance = get_binance()
    latest_prices: dict[str, Decimal] = {}
    recent_ohlcv: dict[str, pd.DataFrame] = {}
    symbols = list({p.symbol for p in open_positions_db})

    for sym in symbols:
        try:
            latest_prices[sym] = await binance.mark_price(sym)
        except Exception:
            log.exception("mark_price failed for %s", sym)
        try:
            df = await binance.klines(sym, "1h", limit=51)
            if not df.empty:
                recent_ohlcv[sym] = df.iloc[:-1].tail(50)
        except Exception:
            log.exception("klines failed for %s", sym)

    pos_views = [_position_view(p, latest_prices.get(p.symbol)) for p in open_positions_db]

    decision, raw_response = await exit_monitor.evaluate_open_positions(
        open_positions=pos_views,
        latest_prices=latest_prices,
        recent_ohlcv=recent_ohlcv,
    )
    if decision is None:
        log.warning("Exit-monitor parse/LLM error — holding all positions")
        return

    open_by_id = {p.id: p for p in open_positions_db}
    bus = get_bus()
    for item in decision.items:
        pos = open_by_id.get(item.position_id)
        if pos is None:
            # AI hallucinated a position_id; try resolving by symbol.
            pos = next((p for p in open_positions_db if p.symbol == item.symbol), None)
        if pos is None:
            log.info("Exit-monitor referenced unknown position id=%s — ignoring", item.position_id)
            continue

        dec_id = await _audit_exit(
            symbol=pos.symbol,
            side=pos.side,
            action=item.action,
            confidence=item.confidence,
            reasoning=item.reasoning,
            position_id=pos.id,
            raw_response=raw_response,
        )

        if item.action == "CLOSE":
            price = latest_prices.get(pos.symbol, pos.entry_price)
            await bus.publish(
                ExitSignal(
                    symbol=pos.symbol,
                    position_id=pos.id,
                    reason="AI_EXIT",
                    price=price,
                    decision_id=dec_id,
                )
            )
            await notify(
                f"🤖 *AI exit {pos.side} {pos.symbol}* (conf `{item.confidence}%`)\n"
                f"{item.reasoning[:200]}"
            )

    _ = cfg  # silence


async def _audit_exit(
    *,
    symbol: str,
    side: str,
    action: str,
    confidence: int,
    reasoning: str,
    position_id: int,
    raw_response: str,
) -> int:
    cfg = get_config()
    async with session() as s:
        row = AIDecision(
            decision_type="EXIT_MONITOR",
            symbol=symbol,
            side=side,
            action=action,
            confidence=confidence,
            reason=reasoning[:500] if reasoning else None,
            model=cfg.openrouter_decision_model,
            raw_response=raw_response[:4000] if raw_response else None,
            position_id=position_id,
        )
        saved = await repo.add_ai_decision(s, row)
    return saved.id
