from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress
from logging.handlers import RotatingFileHandler

from src.config import get_config
from src.core import repository as repo
from src.core.db import dispose, init_db, session
from src.execution.executor import run_executor
from src.market.binance_client import get_binance
from src.scheduler import jobs
from src.scheduler.runner import build_scheduler
from src.tgbot import notifier
from src.tgbot.bot import build_app
from src.tgbot.bot import run as tg_run

log = logging.getLogger(__name__)


class _SecretFilter(logging.Filter):
    """Redacts known API keys/tokens from every log record before it is emitted."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        # Only keep secrets long enough to be meaningful (avoids scrubbing short defaults).
        self._secrets = [s for s in secrets if s and len(s) > 8]

    def _scrub(self, val: object) -> object:
        if not isinstance(val, str):
            return val
        for secret in self._secrets:
            val = val.replace(secret, "***")
        return val

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._scrub(v) for k, v in record.args.items()}
            else:
                record.args = tuple(self._scrub(a) for a in record.args)
        return True


def _configure_logging(level: str, secrets: list[str] | None = None) -> None:
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level.upper())

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    os.makedirs("logs", exist_ok=True)
    file_handler = RotatingFileHandler(
        "logs/bot.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if secrets:
        secret_filter = _SecretFilter(secrets)
        for handler in root.handlers:
            handler.addFilter(secret_filter)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def amain() -> None:
    cfg = get_config()
    _configure_logging(
        cfg.log_level,
        secrets=[
            cfg.binance_api_key,
            cfg.binance_api_secret,
            cfg.telegram_bot_token,
            cfg.openrouter_api_key,
        ],
    )
    log.info("Starting bot — mode=%s universe=%s", cfg.mode.value, cfg.universe_symbols)

    await init_db()

    # Build + initialize the Telegram app FIRST so the bot's HTTP client is
    # ready. We register it with the notifier before any background work
    # (universe validation, reconcile, scheduler) so their notifications
    # actually deliver.
    tg_app = build_app()
    await tg_app.initialize()
    notifier.set_bot(tg_app.bot, cfg.telegram_allowed_user_ids)

    # Warm exchange filter cache and validate configured universe.
    binance = get_binance()
    await binance.exchange_info()
    await jobs.validate_universe_on_startup()
    # Resolve any PENDING positions left behind by a crash mid-entry.
    await jobs.reconcile_pending_positions()

    async with session() as s:
        settings = await repo.get_settings(s)
    scheduler = build_scheduler(settings.exit_poll_minutes)
    scheduler.start()

    # Start polling for inbound user messages last.
    await tg_run(tg_app)

    executor_task = asyncio.create_task(run_executor(), name="executor")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop_handler() -> None:
        log.info("Shutdown signal received.")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop_handler)

    try:
        await stop.wait()
    finally:
        log.info("Stopping bot…")

        # 1) Stop the scheduler first, waiting up to 30s for in-flight jobs
        # (a mid-flight `_handle_entry` order placement should NOT be torn
        # down half-way through). Run in a thread so `wait=True`'s blocking
        # join doesn't deadlock the event loop.
        await asyncio.to_thread(_shutdown_scheduler, scheduler)

        # 2) Stop accepting new events, drain the executor.
        executor_task.cancel()
        with suppress(asyncio.CancelledError):
            await executor_task

        # 3) Telegram polling can be torn down now.
        if tg_app.updater is not None:
            await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

        await binance.close()
        await dispose()
        log.info("Bye.")


def _shutdown_scheduler(scheduler) -> None:  # noqa: ANN001
    try:
        scheduler.shutdown(wait=True)
    except Exception:
        log.exception("Scheduler shutdown raised")


if __name__ == "__main__":
    asyncio.run(amain())
