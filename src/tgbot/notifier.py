"""Push notifications to all allowed Telegram users.

Set the bot reference once at startup (main.py), then call notify() from any
module without circular imports.
"""

from __future__ import annotations

import contextlib
import logging

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
