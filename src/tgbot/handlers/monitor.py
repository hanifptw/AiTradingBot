from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.core import repository as repo
from src.core.db import session
from src.tgbot.auth import restricted
from src.tgbot.formatters import fmt_monitor


@restricted
async def show_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with session() as s:
        states = await repo.list_states(s)
        universe = await repo.list_universe(s)
        settings = await repo.get_settings(s)
        timeframe = settings.timeframe
    # Order by mcap rank when available.
    rank_map = {u.symbol: u.mcap_rank for u in universe}
    states.sort(key=lambda st: rank_map.get(st.symbol, 999))
    await update.effective_message.reply_text(
        fmt_monitor(states, timeframe=timeframe), parse_mode=ParseMode.MARKDOWN
    )
