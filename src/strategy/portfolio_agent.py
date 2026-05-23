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

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai import exit_monitor, portfolio_decision
from src.ai import prompts as P
from src.config import get_config
from src.core import repository as repo
from src.core.db import session
from src.core.events import EntrySignal, ExitSignal, get_bus
from src.core.models import AIDecision, Position, Side, Trade
from src.market.binance_client import get_binance
from src.tgbot.notifier import notify

log = logging.getLogger(__name__)

# Serializes overlapping invocations of the bar-close / exit-poll cycles so
# a slow LLM call can't be lapped by the next scheduler tick.
_bar_cycle_lock = asyncio.Lock()
_exit_cycle_lock = asyncio.Lock()


def _prev_close_from_ohlcv(df: pd.DataFrame | None) -> Decimal | None:
    """Most recently closed 1h bar's close, used as the "1h ago" snapshot.

    The OHLCV passed to prompts has the in-progress bar already dropped, so
    `iloc[-1]` is the most recently completed bar — close-price roughly an
    hour behind the current mark_price.
    """
    if df is None or df.empty:
        return None
    try:
        return Decimal(str(df.iloc[-1]["close"]))
    except (KeyError, IndexError, TypeError):
        return None


def _position_view(
    pos: Position,
    mark_price: Decimal | None,
    prev_close: Decimal | None = None,
) -> dict:
    """Compact JSON-able snapshot for prompts.

    `prev_close` is the close price of the bar immediately preceding the current
    one — used to compute `dist_to_sl_pct_1h_ago` / `dist_to_tp_pct_1h_ago` so
    the LLM can evaluate "SL distance shrinking" without re-deriving from OHLCV.
    """
    direction = Decimal("1") if pos.side == Side.LONG.value else Decimal("-1")
    upnl_pct: Decimal | None = None
    if mark_price is not None and pos.entry_price > 0:
        upnl_pct = (mark_price - pos.entry_price) / pos.entry_price * Decimal("100") * direction

    # live_R is anchored to the original SL distance — gives the LLM a unit
    # consistent with the R-multiple in the historical context block.
    sl_distance_pct: Decimal | None = None
    if pos.sl_price and pos.entry_price > 0:
        sl_distance_pct = abs(pos.entry_price - pos.sl_price) / pos.entry_price * Decimal("100")
        if sl_distance_pct == 0:
            sl_distance_pct = None
    live_r: Decimal | None = None
    if upnl_pct is not None and sl_distance_pct:
        live_r = upnl_pct / sl_distance_pct

    def _dist_to(target: Decimal, ref_price: Decimal) -> Decimal:
        # Percent move from ref_price to `target` in the favorable direction
        # (positive = still need that much further; negative = already past).
        return (target - ref_price) / ref_price * Decimal("100") * direction

    dist_to_tp_pct: Decimal | None = None
    dist_to_sl_pct: Decimal | None = None
    if mark_price is not None:
        if pos.tp_price:
            dist_to_tp_pct = _dist_to(pos.tp_price, mark_price)
        if pos.sl_price:
            # For SL the "favorable" direction is away from SL, so flip sign:
            # positive = buffer remaining; negative = already breached.
            dist_to_sl_pct = -_dist_to(pos.sl_price, mark_price)

    dist_to_tp_pct_1h_ago: Decimal | None = None
    dist_to_sl_pct_1h_ago: Decimal | None = None
    if prev_close is not None:
        if pos.tp_price:
            dist_to_tp_pct_1h_ago = _dist_to(pos.tp_price, prev_close)
        if pos.sl_price:
            dist_to_sl_pct_1h_ago = -_dist_to(pos.sl_price, prev_close)

    # Bars open on the 1h timeframe. opened_at is stored as tz-aware UTC.
    now = datetime.now(UTC)
    opened = pos.opened_at
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=UTC)
    bars_open = max(0, int((now - opened).total_seconds() // 3600))

    return {
        "id": pos.id,
        "symbol": pos.symbol,
        "side": pos.side,
        "qty": f"{float(pos.qty):.8g}",
        "entry_price": f"{float(pos.entry_price):.6g}",
        "sl_price": f"{float(pos.sl_price):.6g}" if pos.sl_price else "n/a",
        "tp_price": f"{float(pos.tp_price):.6g}" if pos.tp_price else "n/a",
        "leverage": pos.leverage,
        "upnl_pct": float(upnl_pct) if upnl_pct is not None else None,
        "live_r": float(live_r) if live_r is not None else None,
        "dist_to_tp_pct": float(dist_to_tp_pct) if dist_to_tp_pct is not None else None,
        "dist_to_sl_pct": float(dist_to_sl_pct) if dist_to_sl_pct is not None else None,
        "dist_to_tp_pct_1h_ago": (
            float(dist_to_tp_pct_1h_ago) if dist_to_tp_pct_1h_ago is not None else None
        ),
        "dist_to_sl_pct_1h_ago": (
            float(dist_to_sl_pct_1h_ago) if dist_to_sl_pct_1h_ago is not None else None
        ),
        "bars_open": bars_open,
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


def _trade_to_history_dict(t: Trade) -> dict:
    """Compact serialization for the worst-trades list in the historical block."""
    return {
        "symbol": t.symbol,
        "side": t.side,
        "entry_price": f"{float(t.entry_price):.6g}",
        "exit_price": f"{float(t.exit_price):.6g}",
        "r_multiple": t.r_multiple,
        "close_reason": t.close_reason,
    }


async def _build_historical_context(s: AsyncSession) -> str | None:
    """Render the 7-day historical context block for trading prompts.

    Returns None when there is nothing useful to surface (no trades AND no
    evaluator report) — caller passes None through to the prompt builder,
    which skips the block entirely.
    """
    since = datetime.now(UTC) - timedelta(days=7)
    trades = await repo.trades_since(s, since)
    report = await repo.last_ai_report(s)

    stats = repo.aggregate_trade_stats(trades) if trades else None
    per_sym = repo.per_symbol_stats(trades) if trades else []
    worst = [_trade_to_history_dict(t) for t in repo.worst_trades(trades, limit=5)]

    age_h: float | None = None
    md: str | None = None
    if report is not None:
        created = report.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_h = (datetime.now(UTC) - created).total_seconds() / 3600
        md = report.report_md

    return P.format_historical_context(
        trades_count=len(trades),
        stats=stats,
        per_symbol=per_sym,
        worst=worst,
        last_report_md=md,
        last_report_age_hours=age_h,
    )


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
    if _bar_cycle_lock.locked():
        log.info("Bar-close cycle already running — skipping this tick")
        return
    async with _bar_cycle_lock:
        await _run_bar_close_cycle_locked()


async def _run_bar_close_cycle_locked() -> None:
    cfg = get_config()
    symbols = cfg.universe_symbols

    async with session() as s:
        settings = await repo.get_settings(s)
        last_seen = int(getattr(settings, "last_bar_seen_ms", 0) or 0)

    latest_ms = await _latest_bar_close_ms(symbols)
    if latest_ms <= last_seen:
        log.debug("No new closed 1h bar yet — skipping")
        return

    # Persist *before* the heavy LLM call so a crash mid-cycle doesn't replay
    # the same bar on restart.
    async with session() as s:
        await repo.update_setting(s, last_bar_seen_ms=latest_ms)

    universe_ohlcv = await _fetch_universe_ohlcv(symbols, cfg.ohlcv_history_bars)
    if not universe_ohlcv:
        log.warning("Portfolio cycle: no OHLCV fetched — skipping")
        return

    balance = await _account_balance()

    async with session() as s:
        settings = await repo.get_settings(s)
        open_positions_db = await repo.open_positions(s)
        historical_ctx = await _build_historical_context(s)

    # Map mark prices to compute unrealized PnL for the prompt.
    mark_prices: dict[str, Decimal] = {}
    binance = get_binance()
    for pos in open_positions_db:
        try:
            mark_prices[pos.symbol] = await binance.mark_price(pos.symbol)
        except Exception:
            log.exception("mark_price failed for %s", pos.symbol)

    pos_views = [
        _position_view(
            p,
            mark_prices.get(p.symbol),
            prev_close=_prev_close_from_ohlcv(universe_ohlcv.get(p.symbol)),
        )
        for p in open_positions_db
    ]

    decision, raw_response = await portfolio_decision.decide_portfolio(
        universe_ohlcv=universe_ohlcv,
        balance=balance,
        open_positions=pos_views,
        max_leverage_cap=settings.max_leverage_cap,
        max_equity_per_trade_pct=settings.max_equity_per_trade_pct,
        ohlcv_history_bars=cfg.ohlcv_history_bars,
        historical_context=historical_ctx,
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
        # Autotrade gate covers CLOSE too — dry-run mode means *no* automated
        # order activity, including exits. Operator must close manually.
        if not settings.autotrade_enabled:
            log.info("Autotrade disabled — skipping AI CLOSE %s", td.symbol)
            await _audit(
                decision_type="PORTFOLIO",
                td=td,
                position_id=existing_pos.id,
                raw_response=raw_response,
            )
            return
        # Confidence gate also applies to closes — a low-conviction "maybe
        # close" shouldn't kick out a position the user is still holding.
        if td.confidence < settings.ai_min_confidence:
            log.info(
                "AI CLOSE %s confidence %d < min %d — holding",
                td.symbol,
                td.confidence,
                settings.ai_min_confidence,
            )
            await _audit(
                decision_type="PORTFOLIO",
                td=td,
                position_id=existing_pos.id,
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

    if td.confidence < settings.ai_min_confidence:
        log.info(
            "AI %s %s confidence %d < min %d — skipping",
            td.action,
            td.symbol,
            td.confidence,
            settings.ai_min_confidence,
        )
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

    # Liquidation-distance sanity check. Liquidation sits roughly at
    # 100/leverage % from entry (ignoring maintenance margin). If the SL is
    # placed beyond that, the position liquidates before the stop ever
    # triggers — converting a "controlled loss" into a wipeout.
    sl_dist_pct = abs(last_close - td.sl_price) / last_close * Decimal("100")
    liq_dist_pct = Decimal("100") / Decimal(lev)
    min_safe_sl_pct = liq_dist_pct * Decimal("0.9")  # 10% buffer for maint. margin
    if sl_dist_pct >= min_safe_sl_pct:
        log.warning(
            "SL beyond liquidation for %s (sl_dist=%.2f%% lev=%dx liq≈%.2f%%) — skipping",
            td.symbol,
            float(sl_dist_pct),
            lev,
            float(liq_dist_pct),
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
    if _exit_cycle_lock.locked():
        log.info("Exit-poll cycle already running — skipping this tick")
        return
    async with _exit_cycle_lock:
        await _run_exit_poll_cycle_locked()


async def _run_exit_poll_cycle_locked() -> None:
    cfg = get_config()
    async with session() as s:
        settings = await repo.get_settings(s)
        open_positions_db = await repo.open_positions(s)
        historical_ctx = await _build_historical_context(s) if open_positions_db else None
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

    pos_views = [
        _position_view(
            p,
            latest_prices.get(p.symbol),
            prev_close=_prev_close_from_ohlcv(recent_ohlcv.get(p.symbol)),
        )
        for p in open_positions_db
    ]

    decision, raw_response = await exit_monitor.evaluate_open_positions(
        open_positions=pos_views,
        latest_prices=latest_prices,
        recent_ohlcv=recent_ohlcv,
        historical_context=historical_ctx,
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

        if item.action != "CLOSE":
            continue

        if not settings.autotrade_enabled:
            log.info("Autotrade disabled — skipping exit-monitor CLOSE %s", pos.symbol)
            continue

        if item.confidence < settings.ai_min_confidence:
            log.info(
                "Exit-monitor CLOSE %s confidence %d < min %d — holding",
                pos.symbol,
                item.confidence,
                settings.ai_min_confidence,
            )
            continue

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
        # The final close confirmation (with PnL) is sent by
        # executor._handle_exit via notify_position_closed.

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
