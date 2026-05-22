from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
)

from src.config import get_config
from src.tgbot.auth import restricted
from src.tgbot.handlers import (
    ai_analysis,
    balance,
    history,
    menu,
    monitor,
    pnl,
    positions,
    settings,
)

log = logging.getLogger(__name__)


@restricted
async def _on_menu_click(update, context):
    """Dispatch inline-button callbacks to the matching handler."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    action = (query.data or "").removeprefix("menu:")
    if action == "main":
        await menu.show_menu(update, context)
        return
    fn = {
        "balance": balance.show_balance,
        "positions": positions.show_positions,
        "pnl": pnl.show_pnl,
        "monitor": monitor.show_monitor,
        "settings": settings.show_settings,
        "ai": ai_analysis.show_ai,
        "history": history.show_history,
    }.get(action)
    if fn is not None:
        await fn(update, context)


@restricted
async def _noop(update, context):
    if update.callback_query:
        await update.callback_query.answer()


def build_app() -> Application:
    cfg = get_config()
    if not cfg.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    app = ApplicationBuilder().token(cfg.telegram_bot_token).build()

    app.add_handler(CommandHandler("start", menu.start))
    app.add_handler(CommandHandler("menu", menu.show_menu))
    app.add_handler(CommandHandler("saldo", balance.show_balance))
    app.add_handler(CommandHandler("posisi", positions.show_positions))
    app.add_handler(CommandHandler("pnl", pnl.show_pnl))
    app.add_handler(CommandHandler("monitor", monitor.show_monitor))
    app.add_handler(CommandHandler("settings", settings.show_settings))
    app.add_handler(CommandHandler("ai", ai_analysis.show_ai))
    app.add_handler(CommandHandler("history", history.show_history))
    app.add_handler(CallbackQueryHandler(settings.handle_settings_callback, pattern=r"^set:"))
    app.add_handler(CallbackQueryHandler(positions.handle_close_callback, pattern=r"^close_pos:"))
    app.add_handler(CallbackQueryHandler(history.handle_history_callback, pattern=r"^history:"))
    app.add_handler(CallbackQueryHandler(_on_menu_click, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(_noop, pattern=r"^noop$"))

    return app


async def run(app: Application) -> None:
    """Start the Telegram app's update polling.

    The caller is expected to have already called `await app.initialize()`
    (so that `notifier.set_bot` can be wired before any background job
    publishes via `notify()`).
    """
    await app.start()
    if app.updater is not None:
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("Telegram bot started.")
