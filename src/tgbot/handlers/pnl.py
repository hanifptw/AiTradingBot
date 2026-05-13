from __future__ import annotations

from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.core import repository as repo
from src.core.db import session
from src.tgbot.auth import restricted
from src.tgbot.formatters import fmt_pnl_windows


@restricted
async def show_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.utcnow()
    windows = repo.windows(now)
    stats = {}
    async with session() as s:
        for label, since in windows.items():
            trades = await repo.trades_since(s, since)
            stats[label] = repo.pnl_window(trades)
    await update.effective_message.reply_text(
        fmt_pnl_windows(stats), parse_mode=ParseMode.MARKDOWN
    )
