from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import get_config
from src.scheduler import jobs

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the live scheduler reference (None until main.py builds one)."""
    return _scheduler


def reschedule_exit_monitor(minutes: int) -> None:
    """Update the exit-monitor interval at runtime.

    Called from the Telegram settings handler after the user changes
    `exit_poll_minutes`. Without this, the new value sits in the DB but the
    scheduler keeps firing at the boot-time interval until the next restart.
    """
    if _scheduler is None:
        return
    try:
        _scheduler.reschedule_job(
            "exit_monitor", trigger=IntervalTrigger(minutes=minutes)
        )
        log.info("Rescheduled exit_monitor to every %d minutes", minutes)
    except Exception:
        log.exception("Failed to reschedule exit_monitor")


def build_scheduler(exit_poll_minutes: int) -> AsyncIOScheduler:
    global _scheduler
    cfg = get_config()
    scheduler = AsyncIOScheduler(timezone=cfg.timezone)
    # Portfolio cycle: fires 10s after each 1h close to make sure the kline is
    # finalized. Use UTC for the cron, then convert via scheduler timezone.
    scheduler.add_job(
        jobs.portfolio_bar_close_job,
        CronTrigger(minute=0, second=10, timezone="UTC"),
        id="portfolio_bar_close",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        jobs.exit_monitor_job,
        IntervalTrigger(minutes=exit_poll_minutes),
        id="exit_monitor",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        jobs.sync_positions,
        IntervalTrigger(minutes=2),
        id="sync_positions",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        jobs.daily_ai_report,
        CronTrigger(hour=0, minute=5, timezone="UTC"),
        id="daily_ai",
        max_instances=1,
        coalesce=True,
    )
    _scheduler = scheduler
    return scheduler
