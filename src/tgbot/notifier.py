"""Push notifications to all allowed Telegram users.

Set the bot reference once at startup (main.py), then call notify() from any
module without circular imports.
"""

from __future__ import annotations

import contextlib
import logging
from decimal import Decimal

from telegram import Bot
from telegram.constants import ParseMode

log = logging.getLogger(__name__)

_bot: Bot | None = None
_chat_ids: list[int] = []


def set_bot(bot: Bot, chat_ids: list[int]) -> None:
    global _bot, _chat_ids
    _bot = bot
    _chat_ids = list(chat_ids)


async def notify(text: str) -> None:
    """Send markdown text to all allowed users. Errors are swallowed."""
    if not _bot or not _chat_ids:
        return
    for chat_id in _chat_ids:
        with contextlib.suppress(Exception):
            await _bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)


_CLOSE_LABELS = {
    "TP": ("kena Take Profit", "🎯"),
    "SL": ("kena Stop Loss", "🛑"),
    "MANUAL": ("ditutup manual", "✋"),
    "AI_EXIT": ("ditutup AI", "🤖"),
    "LIQUIDATION": ("LIKUIDASI", "💀"),
}


async def notify_position_closed(
    *,
    side: str,
    symbol: str,
    entry_price: Decimal,
    exit_price: Decimal,
    pnl: Decimal,
    reason: str,
    confidence: int | None = None,
    ai_reasoning: str | None = None,
) -> None:
    """Single source of truth for close-position notifications. Never raises."""
    try:
        label, header_emoji = _CLOSE_LABELS.get(reason, (f"ditutup ({reason})", "ℹ️"))
        pnl_emoji = "✅" if pnl >= 0 else "🔴"
        sign = "+" if pnl >= 0 else ""
        lines = [f"{header_emoji} *{side} {symbol}* {label} {pnl_emoji}"]
        if confidence is not None:
            lines.append(f"AI conf: `{confidence}%`")
        lines.append(f"Entry: `{entry_price:.4f}` → Exit: `{exit_price:.4f}`")
        lines.append(f"PnL: `{sign}{pnl:.2f}` USDT")
        if ai_reasoning:
            lines.append(f"_{ai_reasoning[:200]}_")
        await notify("\n".join(lines))
    except Exception:
        log.exception("notify_position_closed failed for %s %s", side, symbol)
