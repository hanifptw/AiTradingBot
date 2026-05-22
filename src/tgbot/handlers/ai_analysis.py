from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.ai.evaluator import generate_report
from src.tgbot.auth import restricted

log = logging.getLogger(__name__)

_TELEGRAM_LIMIT = 4096

# Serializes /ai requests so a button-mashing user can't fan out N concurrent
# OpenRouter calls (the LLM is slow and each call costs tokens).
_ai_inflight = asyncio.Lock()


@restricted
async def show_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _ai_inflight.locked():
        await update.effective_message.reply_text(
            "🤖 AI analysis already running — tunggu sebentar."
        )
        return
    async with _ai_inflight:
        msg = await update.effective_message.reply_text("🤖 Generating AI analysis…")
        try:
            report = await generate_report(trigger="on_demand")
        except Exception:
            log.exception("AI analysis failed")
            await msg.edit_text("❌ AI analysis failed — cek log.")
            return
        text = (
            f"🤖 *AI Analysis* (model `{report.model}`, {report.trades_count} trades)\n\n"
            f"{report.report_md}"
        )
        # Telegram caps at 4096 chars per message.
        for i in range(0, len(text), _TELEGRAM_LIMIT):
            chunk = text[i : i + _TELEGRAM_LIMIT]
            if i == 0:
                await msg.edit_text(chunk, parse_mode=ParseMode.MARKDOWN)
            else:
                await update.effective_message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
