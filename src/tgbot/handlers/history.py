from __future__ import annotations

import contextlib

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.core import repository as repo
from src.core.db import session
from src.tgbot.auth import restricted
from src.tgbot.formatters import fmt_trade_row


def _history_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Reset PnL", callback_data="history:reset_confirm")],
        [InlineKeyboardButton("← Menu", callback_data="menu:main")],
    ])


async def _render_history(update: Update) -> None:
    async with session() as s:
        trades = await repo.recent_trades(s, limit=30)

    if not trades:
        text = "*📜 History Trade*\n_Belum ada trade tersimpan._"
    else:
        lines = [f"*📜 History Trade* (last {len(trades)})"]
        for t in trades:
            lines.append(fmt_trade_row(t))
        text = "\n".join(lines)

    markup = _history_markup()
    if update.callback_query:
        with contextlib.suppress(BadRequest):
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
        )


@restricted
async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _render_history(update)


@restricted
async def handle_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""

    if data == "history:reset_confirm":
        with contextlib.suppress(BadRequest):
            await query.edit_message_text(
                "⚠️ *Reset semua data PnL?*\n"
                "Semua history trade akan dihapus permanen dan tidak bisa dikembalikan.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Ya, Reset", callback_data="history:reset_do"),
                        InlineKeyboardButton("❌ Batal", callback_data="history:show"),
                    ]
                ]),
            )

    elif data == "history:reset_do":
        async with session() as s:
            deleted = await repo.delete_all_trades(s)
        with contextlib.suppress(BadRequest):
            await query.edit_message_text(
                f"✅ Berhasil reset — {deleted} trade dihapus.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("← Menu", callback_data="menu:main")]
                ]),
            )

    elif data == "history:show":
        await _render_history(update)
