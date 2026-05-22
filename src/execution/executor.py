"""Subscribes to EntrySignal/ExitSignal and places orders on Binance Futures.

The executor is the only module allowed to call binance_client for order
placement. EntrySignal payloads carry the AI-issued size/leverage/SL/TP
directly — the executor does no strategy logic, only clamping that has
already happened in the portfolio agent (caps + side validation).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from decimal import Decimal

from src.core import repository as repo
from src.core.db import session
from src.core.events import EntrySignal, ExitSignal, get_bus
from src.core.models import Order, Position, PositionStatus, Side
from src.market.binance_client import get_binance
from src.strategy.risk import position_size
from src.tgbot.notifier import notify, notify_position_closed

log = logging.getLogger(__name__)

# Per-symbol locks serialize entry/exit on the same symbol so two concurrent
# signals (e.g. bar-close + exit-monitor) can't both pass the open-position
# check and double-fire orders.
_symbol_locks: dict[str, asyncio.Lock] = {}


def _lock_for(symbol: str) -> asyncio.Lock:
    lock = _symbol_locks.get(symbol)
    if lock is None:
        lock = asyncio.Lock()
        _symbol_locks[symbol] = lock
    return lock


def _parse_fill_price(resp: dict, fallback: Decimal) -> Decimal:
    """Binance testnet sometimes returns avgPrice='0.00000' for MARKET orders.
    Fall back to signal price when the reported fill price is zero or missing."""
    for key in ("avgPrice", "price"):
        raw = resp.get(key)
        if raw:
            try:
                d = Decimal(str(raw))
                if d > 0:
                    return d
            except Exception:
                pass
    return fallback


def _fq(qty: Decimal) -> str:
    """Format qty without trailing zeros."""
    return f"{float(qty):.8g}"


# Whitelist of Binance order-response fields we keep in Order.raw. Anything
# else (internal account IDs, side-channel timestamps that aren't useful for
# debugging) is dropped before persist — the DB is the most-useful artifact
# for an attacker if the file ever leaks, so we minimize.
_RAW_ORDER_WHITELIST = frozenset(
    {
        "orderId",
        "clientOrderId",
        "symbol",
        "side",
        "type",
        "status",
        "price",
        "avgPrice",
        "stopPrice",
        "origQty",
        "executedQty",
        "cumQuote",
        "timeInForce",
        "reduceOnly",
        "closePosition",
        "workingType",
        "positionSide",
    }
)


def sanitize_order_resp(resp: dict | None) -> dict | None:
    if not resp:
        return resp
    return {k: v for k, v in resp.items() if k in _RAW_ORDER_WHITELIST}


async def run_executor() -> None:
    bus = get_bus()
    q = await bus.subscribe()
    try:
        while True:
            event = await q.get()
            try:
                if isinstance(event, EntrySignal):
                    await _handle_entry(event)
                elif isinstance(event, ExitSignal):
                    await _handle_exit(event)
            except Exception:
                log.exception("Executor failed handling %s", event)
    finally:
        await bus.unsubscribe(q)


async def _handle_entry(ev: EntrySignal) -> None:
    async with _lock_for(ev.symbol):
        await _handle_entry_locked(ev)


async def _handle_entry_locked(ev: EntrySignal) -> None:
    binance = get_binance()

    # ── Pre-flight checks (read-only) ──────────────────────────────────────
    async with session() as s:
        settings = await repo.get_settings(s)
        if not settings.autotrade_enabled:
            log.info("Autotrade disabled — skipping entry %s %s", ev.side.value, ev.symbol)
            return

        existing = await repo.active_position_for(s, ev.symbol)
        if existing is not None:
            log.info(
                "Position already %s for %s — skipping new entry",
                existing.status,
                ev.symbol,
            )
            return

    wallet, _avail = await binance.account_balance_usdt()
    sizing = position_size(
        equity=wallet,
        size_pct=ev.size_pct_equity,
        leverage=ev.leverage,
        entry_price=ev.price,
    )
    await binance.exchange_info()  # warms filter cache
    qty = binance.quantize_qty(ev.symbol, sizing.qty)
    if qty <= 0:
        log.warning(
            "Computed qty=0 for %s (equity=%s, size_pct=%s, lev=%s) — skipping",
            ev.symbol,
            wallet,
            ev.size_pct_equity,
            ev.leverage,
        )
        return

    ok, reason = binance.validate_order_size(ev.symbol, qty, ev.price)
    if not ok:
        log.warning("Order size invalid for %s: %s — skipping", ev.symbol, reason)
        return

    sl_quant = binance.quantize_price(ev.symbol, ev.sl_price)
    tp_quant = binance.quantize_price(ev.symbol, ev.tp_price)

    # Hard-fail set_leverage: a silent swallow would let us trade at the
    # previous leverage value, which is a money-safety bug.
    try:
        await binance.set_leverage(ev.symbol, ev.leverage)
    except Exception as e:
        log.exception("set_leverage(%s, %d) failed — aborting entry", ev.symbol, ev.leverage)
        await notify(
            f"⚠️ Entry *{ev.symbol}* dibatalkan: gagal set leverage `{ev.leverage}x` ({type(e).__name__})."
        )
        return

    # Wipe any stale reduce-only orders from a previously closed position.
    # Without this, a leftover closePosition=True SL/TP can fire on the
    # next entry when price ticks past its stale stopPrice.
    await binance.cancel_all_open_orders(ev.symbol)

    # ── Pre-record PENDING position so a crash mid-flight is recoverable ──
    coid = uuid.uuid4().hex[:16]  # idempotency token base
    async with session() as s:
        settings = await repo.get_settings(s)
        pending = Position(
            symbol=ev.symbol,
            side=ev.side.value,
            status=PositionStatus.PENDING.value,
            mode=settings.mode,
            qty=qty,
            entry_price=ev.price,  # placeholder; updated after fill
            leverage=ev.leverage,
            sl_price=sl_quant,
            tp_price=tp_quant,
            entry_decision_id=ev.decision_id,
            client_order_id=coid,
        )
        pending = await repo.create_position(s, pending)
    pos_id = pending.id

    binance_side = "BUY" if ev.side is Side.LONG else "SELL"
    sl_side = "SELL" if ev.side is Side.LONG else "BUY"
    tp_side = sl_side

    # ── Place MARKET entry ────────────────────────────────────────────────
    try:
        market_resp = await binance.market_order(
            ev.symbol, binance_side, qty, client_order_id=f"e{coid}"
        )
    except Exception:
        log.exception("MARKET entry failed for %s — marking PENDING as CANCELLED", ev.symbol)
        async with session() as s:
            await repo.mark_position_cancelled(s, pos_id, "MARKET_FAILED")
        return

    fill_price = _parse_fill_price(market_resp, ev.price)

    # ── Place SL (protective). Failure → rollback via market close. ───────
    try:
        sl_resp = await binance.stop_market_reduce_only(
            ev.symbol, sl_side, sl_quant, client_order_id=f"s{coid}"
        )
    except Exception:
        log.exception(
            "SL placement FAILED for %s after MARKET fill — rolling back via market close",
            ev.symbol,
        )
        await _rollback_market_close(ev.symbol, sl_side, qty, coid, pos_id, "SL_FAILED")
        return

    # ── Place TP. Failure → cancel SL + rollback close. ───────────────────
    try:
        tp_resp = await binance.take_profit_market_reduce_only(
            ev.symbol, tp_side, tp_quant, client_order_id=f"t{coid}"
        )
    except Exception:
        log.exception("TP placement FAILED for %s — cancelling SL and rolling back", ev.symbol)
        sl_oid = sl_resp.get("orderId") if sl_resp else None
        if sl_oid:
            with contextlib.suppress(Exception):
                await binance.cancel_order(ev.symbol, str(sl_oid))
        await _rollback_market_close(ev.symbol, tp_side, qty, coid, pos_id, "TP_FAILED")
        return

    # ── All three orders placed. Finalize PENDING → OPEN. ─────────────────
    async with session() as s:
        await repo.finalize_pending_position(
            s,
            pos_id,
            qty=qty,
            entry_price=fill_price,
            sl_price=sl_quant,
            sl_order_id=(str(oid) if (oid := sl_resp.get("orderId")) else None),
            tp_price=tp_quant,
            tp_order_id=(str(oid) if (oid := tp_resp.get("orderId")) else None),
        )
        await repo.add_order(
            s,
            Order(
                position_id=pos_id,
                symbol=ev.symbol,
                side=binance_side,
                type="MARKET",
                qty=qty,
                price=fill_price,
                binance_order_id=str(market_resp.get("orderId")),
                client_order_id=f"e{coid}",
                status=str(market_resp.get("status", "FILLED")),
                raw=sanitize_order_resp(market_resp),
            ),
        )
        await repo.add_order(
            s,
            Order(
                position_id=pos_id,
                symbol=ev.symbol,
                side=sl_side,
                type="STOP_MARKET",
                qty=qty,
                price=sl_quant,
                binance_order_id=str(sl_resp.get("orderId")) if sl_resp else None,
                client_order_id=f"s{coid}",
                status="NEW",
                raw=sanitize_order_resp(sl_resp),
            ),
        )
        await repo.add_order(
            s,
            Order(
                position_id=pos_id,
                symbol=ev.symbol,
                side=tp_side,
                type="TAKE_PROFIT_MARKET",
                qty=qty,
                price=tp_quant,
                binance_order_id=str(tp_resp.get("orderId")) if tp_resp else None,
                client_order_id=f"t{coid}",
                status="NEW",
                raw=sanitize_order_resp(tp_resp),
            ),
        )

    log.info(
        "Opened %s %s qty=%s @%s  SL=%s  TP=%s  (conf=%d coid=%s)",
        ev.side.value,
        ev.symbol,
        qty,
        fill_price,
        sl_quant,
        tp_quant,
        ev.confidence,
        coid,
    )
    side_emoji = "🟢" if ev.side is Side.LONG else "🔴"
    await notify(
        f"{side_emoji} *{ev.side.value} {ev.symbol}* terbuka (AI conf `{ev.confidence}%`)\n"
        f"Entry: `{fill_price:.4f}` | SL: `{sl_quant:.4f}` | TP: `{tp_quant:.4f}`\n"
        f"Qty: `{_fq(qty)}` | Lev: `{ev.leverage}x` | Size: `{ev.size_pct_equity:.1f}%`"
    )


async def _rollback_market_close(
    symbol: str,
    close_side: str,
    qty: Decimal,
    coid: str,
    pos_id: int,
    reason: str,
) -> None:
    """Market-close a just-opened position when SL or TP placement failed.

    The MARKET entry has already filled, so leaving the position naked (no
    SL) is a money-safety bug. We immediately close it back to flat.
    """
    binance = get_binance()
    try:
        await binance.close_market_order(
            symbol, close_side, qty, client_order_id=f"x{coid}"
        )
    except Exception:
        log.exception(
            "Rollback close also FAILED for %s — MANUAL INTERVENTION REQUIRED", symbol
        )
        async with session() as s:
            await repo.mark_position_cancelled(s, pos_id, f"{reason}_ROLLBACK_FAILED")
        await notify(
            f"🚨 *{symbol}* SL/TP gagal & rollback close juga gagal. Posisi mungkin masih terbuka di Binance — cek manual."
        )
        return
    async with session() as s:
        await repo.mark_position_cancelled(s, pos_id, f"{reason}_ROLLED_BACK")
    await notify(
        f"⚠️ *{symbol}* SL/TP placement gagal — entry sudah di-rollback (market close)."
    )


async def _handle_exit(ev: ExitSignal) -> None:
    async with _lock_for(ev.symbol):
        await _handle_exit_locked(ev)


async def _handle_exit_locked(ev: ExitSignal) -> None:
    binance = get_binance()
    side: str | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal = ev.price
    pnl: Decimal = Decimal("0")
    db_qty: Decimal = Decimal("0")
    close_qty: Decimal = Decimal("0")
    notify_ready = False
    try:
        async with session() as s:
            pos = await repo.open_position_for(s, ev.symbol)
            if pos is None:
                return
            side = pos.side
            entry_price = pos.entry_price
            db_qty = pos.qty
            pos_id = pos.id
            sl_oid = pos.sl_order_id
            tp_oid = pos.tp_order_id

        # Cross-check against live Binance position before closing. The DB
        # qty can drift from reality (partial fill, prior reconcile, or an
        # SL/TP that already fired between the signal and this handler).
        try:
            live_amt = await binance.position_amount(ev.symbol)
        except Exception:
            log.exception("position_amount(%s) failed — falling back to DB qty", ev.symbol)
            live_amt = db_qty if side == "LONG" else -db_qty

        close_qty = abs(live_amt)
        if close_qty == 0:
            log.info(
                "Exit signal for %s but Binance position is flat — reconciling DB only",
                ev.symbol,
            )
            # Cancel any leftover protective orders.
            if sl_oid:
                with contextlib.suppress(Exception):
                    await binance.cancel_order(ev.symbol, sl_oid)
            if tp_oid:
                with contextlib.suppress(Exception):
                    await binance.cancel_order(ev.symbol, tp_oid)
            # Best-effort DB close at the signal price; sync_positions will
            # later overwrite with a better exit_price from user-trades.
            direction = Decimal("1") if side == "LONG" else Decimal("-1")
            pnl = db_qty * (ev.price - (entry_price or ev.price)) * direction
            async with session() as s:
                pos = await repo.open_position_for(s, ev.symbol)
                if pos is not None:
                    await repo.close_position(
                        s,
                        pos,
                        exit_price=ev.price,
                        realized_pnl=pnl,
                        reason=ev.reason,
                    )
            notify_ready = True
            return

        opposite = "SELL" if side == "LONG" else "BUY"
        close_resp = await binance.close_market_order(
            ev.symbol, opposite, close_qty, client_order_id=f"x{uuid.uuid4().hex[:14]}"
        )
        exit_price = _parse_fill_price(close_resp, ev.price)

        if sl_oid:
            with contextlib.suppress(Exception):
                await binance.cancel_order(ev.symbol, sl_oid)
        if tp_oid:
            with contextlib.suppress(Exception):
                await binance.cancel_order(ev.symbol, tp_oid)

        direction = Decimal("1") if side == "LONG" else Decimal("-1")
        pnl = close_qty * (exit_price - (entry_price or exit_price)) * direction
        notify_ready = True

        async with session() as s:
            await repo.add_order(
                s,
                Order(
                    position_id=pos_id,
                    symbol=ev.symbol,
                    side=opposite,
                    type="MARKET",
                    qty=close_qty,
                    price=exit_price,
                    binance_order_id=str(close_resp.get("orderId")),
                    status=str(close_resp.get("status", "FILLED")),
                    raw=sanitize_order_resp(close_resp),
                ),
            )
            pos = await repo.open_position_for(s, ev.symbol)
            if pos is not None:
                await repo.close_position(
                    s,
                    pos,
                    exit_price=exit_price,
                    realized_pnl=pnl,
                    reason=ev.reason,
                )
        log.info(
            "Closed %s %s qty=%s @%s  pnl=%s (reason=%s)",
            side,
            ev.symbol,
            close_qty,
            exit_price,
            pnl,
            ev.reason,
        )
    except Exception:
        log.exception("_handle_exit failed for %s — sending notify anyway", ev.symbol)
    finally:
        if notify_ready and side is not None and entry_price is not None:
            await notify_position_closed(
                side=side,
                symbol=ev.symbol,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl=pnl,
                reason=ev.reason,
            )
