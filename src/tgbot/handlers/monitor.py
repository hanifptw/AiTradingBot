"""Monitor view — shows the latest AI portfolio decision per universe symbol."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.config import get_config
from src.core import repository as repo
from src.core.db import session
from src.tgbot.auth import restricted
from src.tgbot.formatters import fmt_monitor


@restricted
async def show_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = get_config()
    async with session() as s:
        latest = await repo.latest_decision_per_symbol(s, decision_type="PORTFOLIO")
    await update.effective_message.reply_text(
        fmt_monitor(cfg.universe_symbols, latest), parse_mode=ParseMode.MARKDOWN
    )
