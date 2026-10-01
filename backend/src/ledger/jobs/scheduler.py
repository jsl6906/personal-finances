"""Cron-style schedule that only enqueues jobs, so work still runs through the job queue and its UI."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ledger.config import get_settings
from ledger.db.engine import get_sessionmaker
from ledger.jobs.worker import enqueue, notify_worker

log = logging.getLogger(__name__)


async def _enqueue(job_type: str, payload: dict | None = None) -> None:
    try:
        async with get_sessionmaker()() as session:
            await enqueue(session, job_type, payload or {})
            await session.commit()
        notify_worker()
    except Exception:
        log.exception("Scheduled enqueue of %s failed", job_type)


async def _backfill_watchdog() -> None:
    from ledger.sources.jobs import backfill_watchdog

    try:
        async with get_sessionmaker()() as session:
            await backfill_watchdog(session)
    except Exception:
        log.exception("Backfill watchdog failed")


def build_scheduler() -> AsyncIOScheduler:
    s = get_settings()
    scheduler = AsyncIOScheduler(timezone=s.timezone)
    scheduler.add_job(
        _enqueue,
        "cron",
        hour=s.nightly_hour,
        minute=0,
        args=["sync_sources"],
        id="nightly-sources",
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _enqueue,
        "cron",
        hour=s.nightly_hour,
        minute=15,
        args=["detect_anomalies"],
        id="nightly-anomalies",
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _enqueue,
        "cron",
        day_of_week=s.digest_weekday,
        hour=7,
        minute=0,
        args=["weekly_digest"],
        id="weekly-digest",
        coalesce=True,
        misfire_grace_time=6 * 3600,
    )
    scheduler.add_job(
        _enqueue,
        "cron",
        hour=s.nightly_hour,
        minute=45,
        args=["cleanup"],
        id="nightly-cleanup",
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _backfill_watchdog, "interval", minutes=10, id="backfill-watchdog", coalesce=True, max_instances=1
    )
    return scheduler
