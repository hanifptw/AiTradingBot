from __future__ import annotations

from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from src.config import get_config


def restricted(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        allowed = set(get_config().telegram_allowed_user_ids)
        if user is None or user.id not in allowed:
            return  # drop silently — don't reveal the bot to randos
        return await handler(update, context)

    return wrapper
