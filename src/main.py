from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

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


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Tame chatty libraries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def amain() -> None:
    cfg = get_config()
    _configure_logging(cfg.log_level)
    log.info("Starting bot — mode=%s", cfg.mode.value)

    await init_db()

    # Reset any IN_LONG/IN_SHORT states that lost their position on a previous crash.
    async with session() as s:
        reset = await repo.reconcile_states(s)
    if reset:
        log.warning("Reconciled %d orphaned signal states → IDLE", reset)

    # Warm exchange filters and load universe before the first signal tick.
    binance = get_binance()
    await binance.exchange_info()
    await jobs.refresh_universe()

    scheduler = build_scheduler()
    scheduler.start()

    tg_app = build_app()
    await tg_run(tg_app)
    notifier.set_bot(tg_app.bot, cfg.telegram_allowed_user_ids)

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
        executor_task.cancel()
        with suppress(asyncio.CancelledError):
            await executor_task

        if tg_app.updater is not None:
            await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

        scheduler.shutdown(wait=False)
        await binance.close()
        await dispose()
        log.info("Bye.")


if __name__ == "__main__":
    asyncio.run(amain())
