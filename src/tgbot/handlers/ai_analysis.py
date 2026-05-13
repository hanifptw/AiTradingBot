from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.ai.evaluator import generate_report
from src.tgbot.auth import restricted

log = logging.getLogger(__name__)

_TELEGRAM_LIMIT = 4096


@restricted
async def show_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.effective_message.reply_text("🤖 Generating AI analysis…")
    try:
        report = await generate_report(trigger="on_demand")
    except Exception as e:
        log.exception("AI analysis failed")
        await msg.edit_text(f"❌ AI analysis failed: {e}")
        return
    text = f"🤖 *AI Analysis* (model `{report.model}`, {report.trades_count} trades)\n\n{report.report_md}"
    # Telegram caps at 4096 chars per message.
    for i in range(0, len(text), _TELEGRAM_LIMIT):
        chunk = text[i : i + _TELEGRAM_LIMIT]
        if i == 0:
            await msg.edit_text(chunk, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
