from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.tgbot.auth import restricted


def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💰 Saldo", callback_data="menu:balance"),
                InlineKeyboardButton("📊 Posisi", callback_data="menu:positions"),
            ],
            [
                InlineKeyboardButton("📈 PNL", callback_data="menu:pnl"),
                InlineKeyboardButton("👀 Monitor Coin", callback_data="menu:monitor"),
            ],
            [
                InlineKeyboardButton("⚙️ Setting", callback_data="menu:settings"),
                InlineKeyboardButton("🤖 AI Analysis", callback_data="menu:ai"),
            ],
            [
                InlineKeyboardButton("📜 History", callback_data="menu:history"),
            ],
        ]
    )


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Binance Futures bot ready. Pilih menu:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_markup(),
    )


@restricted
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    markup = main_menu_markup()
    if update.callback_query:
        from telegram.error import BadRequest

        try:
            await update.callback_query.edit_message_text("Menu:", reply_markup=markup)
        except BadRequest:
            await update.effective_message.reply_text("Menu:", reply_markup=markup)
    else:
        await update.effective_message.reply_text("Menu:", reply_markup=markup)
