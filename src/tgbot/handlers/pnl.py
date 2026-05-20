from __future__ import annotations

import contextlib
from datetime import datetime
from decimal import Decimal

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.core import repository as repo
from src.core.db import session
from src.market.binance_client import get_binance
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

    upnl: Decimal | None = None
    open_count = 0
    with contextlib.suppress(Exception):
        binance = get_binance()
        positions = await binance.all_open_positions()
        open_count = len(positions)
        upnl = sum(
            (
                Decimal(str(p.get("unRealizedProfit") or p.get("unrealizedProfit") or "0"))
                for p in positions
            ),
            Decimal("0"),
        )

    await update.effective_message.reply_text(
        fmt_pnl_windows(stats, upnl=upnl, open_count=open_count),
        parse_mode=ParseMode.MARKDOWN,
    )
