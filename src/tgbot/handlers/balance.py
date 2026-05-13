from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.core import repository as repo
from src.core.db import session
from src.market.binance_client import get_binance
from src.tgbot.auth import restricted
from src.tgbot.formatters import fmt_balance


@restricted
async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    binance = get_binance()
    wallet, available = await binance.account_balance_usdt()
    async with session() as s:
        settings = await repo.get_settings(s)
    text = fmt_balance(wallet, available, settings.mode)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
