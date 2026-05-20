from __future__ import annotations

import contextlib
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.core import repository as repo
from src.core.db import session
from src.core.models import Order
from src.market.binance_client import get_binance
from src.tgbot.auth import restricted
from src.tgbot.formatters import fmt_positions
from src.tgbot.notifier import notify


def _parse_fill(resp: dict, fallback: Decimal) -> Decimal:
    for key in ("avgPrice", "price"):
        raw = resp.get(key)
        if raw:
            with contextlib.suppress(Exception):
                d = Decimal(str(raw))
                if d > 0:
                    return d
    return fallback


async def _resolve_fill(
    binance, resp: dict, close_side: str, symbol: str, fallback: Decimal
) -> Decimal:
    """Get actual fill price from order response, falling back to recent trades.

    Binance testnet often returns avgPrice='0.00000' for MARKET orders.
    """
    price = _parse_fill(resp, Decimal("0"))
    if price > 0:
        return price
    with contextlib.suppress(Exception):
        trades = await binance.recent_user_trades(symbol, limit=5)
        candidates = [t for t in trades if t.get("side", "").upper() == close_side.upper()]
        if candidates:
            latest = max(candidates, key=lambda t: int(t.get("time", 0)))
            p = Decimal(str(latest["price"]))
            if p > 0:
                return p
    return fallback


@restricted
async def show_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    binance = get_binance()

    # Binance is the source of truth for what's actually open.
    binance_positions: list[dict] = []
    with contextlib.suppress(Exception):
        binance_positions = await binance.all_open_positions()

    # DB provides metadata (opened_at, sl_price, tp_price) for positions opened by this bot.
    async with session() as s:
        db_positions = await repo.open_positions(s)
    db_map = {p.symbol: p for p in db_positions}

    # Funding info per symbol.
    funding_data: dict[str, dict] = {}
    for bd in binance_positions:
        with contextlib.suppress(Exception):
            funding_data[bd["symbol"]] = await binance.mark_price_info(bd["symbol"])

    # Build Close buttons — one per Binance position.
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for bd in binance_positions:
        sym = bd["symbol"]
        row.append(InlineKeyboardButton(f"🔴 Close {sym}", callback_data=f"close_pos:{sym}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    if keyboard:
        keyboard.append([InlineKeyboardButton("← Menu", callback_data="menu:main")])
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    await update.effective_message.reply_text(
        fmt_positions(binance_positions, db_map, funding_data),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=markup,
    )


@restricted
async def handle_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""

    if data.startswith("close_pos:ok:"):
        symbol = data.split(":", 2)[2]
        binance = get_binance()

        async with session() as s:
            pos = await repo.open_position_for(s, symbol)

            # Position not tracked in DB (opened manually on Binance). Close directly.
            if pos is None:
                try:
                    all_pos = await binance.all_open_positions()
                    bd = next((p for p in all_pos if p["symbol"] == symbol), None)
                except Exception:
                    bd = None
                if bd is None:
                    await query.edit_message_text(f"Posisi {symbol} sudah tidak ada.")
                    return
                amt = Decimal(str(bd.get("positionAmt", "0")))
                if amt == 0:
                    await query.edit_message_text(f"Posisi {symbol} sudah tidak ada.")
                    return
                ext_side = "LONG" if amt > 0 else "SHORT"
                ext_qty = abs(amt)
                ext_opposite = "SELL" if ext_side == "LONG" else "BUY"
                try:
                    close_resp = await binance.close_market_order(symbol, ext_opposite, ext_qty)
                except Exception as e:
                    await query.edit_message_text(f"❌ Gagal close {symbol}: {e}")
                    return
                ext_entry = Decimal(str(bd.get("entryPrice", "0")))
                ext_exit = await _resolve_fill(binance, close_resp, ext_opposite, symbol, ext_entry)
                direction = Decimal("1") if ext_side == "LONG" else Decimal("-1")
                pnl = ext_qty * (ext_exit - ext_entry) * direction
                sign = "+" if pnl >= 0 else ""
                await notify(
                    f"✋ *{ext_side} {symbol}* ditutup manual\n"
                    f"Entry: `{ext_entry:.4f}` → Exit: `{ext_exit:.4f}`\n"
                    f"PnL: `{sign}{pnl:.2f}` USDT"
                )
                await query.edit_message_text(
                    f"✅ *{symbol}* berhasil ditutup\nPnL: `{sign}{pnl:.2f}` USDT",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("📊 Posisi", callback_data="menu:positions"),
                                InlineKeyboardButton("← Menu", callback_data="menu:main"),
                            ]
                        ]
                    ),
                )
                return

            opposite = "SELL" if pos.side == "LONG" else "BUY"
            try:
                close_resp = await binance.close_market_order(symbol, opposite, pos.qty)
            except Exception as e:
                await query.edit_message_text(f"❌ Gagal close {symbol}: {e}")
                return

            exit_price = await _resolve_fill(binance, close_resp, opposite, symbol, pos.entry_price)

            with contextlib.suppress(Exception):
                if pos.sl_order_id and pos.sl_order_id not in ("None", "0"):
                    await binance.cancel_order(symbol, pos.sl_order_id)
            with contextlib.suppress(Exception):
                if pos.tp_order_id and pos.tp_order_id not in ("None", "0"):
                    await binance.cancel_order(symbol, pos.tp_order_id)

            direction = Decimal("1") if pos.side == "LONG" else Decimal("-1")
            pnl = pos.qty * (exit_price - pos.entry_price) * direction
            entry_price = pos.entry_price
            side = pos.side

            await repo.add_order(
                s,
                Order(
                    position_id=pos.id,
                    symbol=symbol,
                    side=opposite,
                    type="MARKET",
                    qty=pos.qty,
                    price=exit_price,
                    binance_order_id=str(close_resp.get("orderId")),
                    status=str(close_resp.get("status", "FILLED")),
                    raw=close_resp,
                ),
            )
            await repo.close_position(
                s,
                pos,
                exit_price=exit_price,
                realized_pnl=pnl,
                reason="MANUAL",
            )

        sign = "+" if pnl >= 0 else ""
        await notify(
            f"✋ *{side} {symbol}* ditutup manual\n"
            f"Entry: `{entry_price:.4f}` → Exit: `{exit_price:.4f}`\n"
            f"PnL: `{sign}{pnl:.2f}` USDT"
        )
        await query.edit_message_text(
            f"✅ *{symbol}* berhasil ditutup\nPnL: `{sign}{pnl:.2f}` USDT",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("📊 Posisi", callback_data="menu:positions"),
                        InlineKeyboardButton("← Menu", callback_data="menu:main"),
                    ]
                ]
            ),
        )

    elif data.startswith("close_pos:"):
        symbol = data.split(":", 1)[1]
        await query.edit_message_text(
            f"Yakin mau close posisi *{symbol}* secara manual?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Ya, Close", callback_data=f"close_pos:ok:{symbol}"
                        ),
                        InlineKeyboardButton("❌ Batal", callback_data="menu:positions"),
                    ]
                ]
            ),
        )
