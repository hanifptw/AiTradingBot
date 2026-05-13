from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import get_config
from src.scheduler import jobs

log = logging.getLogger(__name__)


def build_scheduler() -> AsyncIOScheduler:
    cfg = get_config()
    scheduler = AsyncIOScheduler(timezone=cfg.timezone)
    scheduler.add_job(
        jobs.refresh_universe,
        IntervalTrigger(hours=6),
        next_run_time=None,  # we manually call once at startup
        id="universe",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        jobs.poll_klines_and_signal,
        IntervalTrigger(seconds=60),
        id="klines",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        jobs.trailing_tick,
        IntervalTrigger(seconds=30),
        id="trailing",
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
        jobs.weekly_ai_report,
        CronTrigger(day_of_week="sun", hour=23, minute=0, timezone=cfg.timezone),
        id="weekly_ai",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
