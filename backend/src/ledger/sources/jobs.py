import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.crypto import decrypt
from ledger.db.engine import get_sessionmaker
from ledger.jobs.worker import JobContext, enqueue, job_handler, notify_worker
from ledger.models import Job, Source
from ledger.sources import backfill, simplefin, tiller
from ledger.sources.service import apply_feed, record_run

log = logging.getLogger(__name__)

# A backfill_run handles FILES_PER_JOB files; healthy runs finish in well under this.
STALE_BACKFILL_RUN = timedelta(hours=1)


async def run_sync(session: AsyncSession, source_id: int, full: bool = False, progress=None) -> dict:
    src = await session.get(Source, source_id)
    if src is None:
        raise ValueError(f"Source {source_id} not found")
    try:
        if src.kind == "tiller":
            feed = await tiller.fetch(src.config, full)
        elif src.kind == "simplefin":
            if not src.secret:
                raise simplefin.SimpleFinError("SimpleFIN isn't connected")
            feed = await simplefin.fetch(decrypt(src.secret), src.config, full)
        else:
            raise ValueError(f"Unknown source kind {src.kind}")
        if progress:
            await progress(0.3, f"Fetched {len(feed.transactions)} transactions from {src.name}")
        result = await apply_feed(session, src, feed)
    except Exception as exc:
        await session.rollback()
        src = await session.get(Source, source_id, populate_existing=True)
        await record_run(session, src, None, str(exc)[:1000] or type(exc).__name__)
        raise
    await record_run(session, src, result, None)
    return result


@job_handler("sync_source")
async def sync_source_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        return await run_sync(session, int(ctx.payload["source_id"]), bool(ctx.payload.get("full")), ctx.progress)


@job_handler("sync_sources")
async def sync_all_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        ids = (await session.scalars(select(Source.id).where(Source.enabled).order_by(Source.id))).all()
    results = {}
    for i, sid in enumerate(ids):
        await ctx.progress(i / max(len(ids), 1), f"Syncing source {sid}")
        async with get_sessionmaker()() as session:
            try:
                results[str(sid)] = await run_sync(session, sid)
            except Exception as exc:
                log.exception("Source %s sync failed", sid)
                results[str(sid)] = {"error": str(exc)[:300]}
    return results


async def backfill_job_active(session: AsyncSession, exclude: int | None = None) -> bool:
    q = select(Job.id).where(Job.type == "backfill_run", Job.status.in_(("queued", "running")))
    if exclude:
        q = q.where(Job.id != exclude)
    return (await session.scalar(q.limit(1))) is not None


async def backfill_watchdog(session: AsyncSession) -> int | None:
    """Re-queue the backfill when it should be running but its job chain broke (restarts, hung calls)."""
    # Serialise across app instances so two watchdogs can't start parallel chains.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('backfill_run'))"))
    if (await backfill.load_settings(session))["paused"] or await backfill.next_file(session) is None:
        return None
    cutoff = datetime.now(UTC) - STALE_BACKFILL_RUN
    # Cancelled (not failed) so a hung run that wakes up stops at its next progress check.
    await session.execute(
        update(Job)
        .where(Job.type == "backfill_run", Job.status == "running", Job.started_at < cutoff)
        .values(status="cancelled", error="Stalled; restarted by watchdog", finished_at=datetime.now(UTC))
    )
    if await backfill_job_active(session):
        await session.commit()
        return None
    job = await enqueue(session, "backfill_run", {})
    await session.commit()
    log.warning("Backfill job chain was broken; queued job %s", job.id)
    notify_worker()
    return job.id


async def backfill_auto_scan(session: AsyncSession) -> int | None:
    """Queue a scan of the archive folders when the configured interval has passed since the last one."""
    cfg = await backfill.load_settings(session)
    hours = cfg["scan_every_hours"]
    if not hours:
        return None
    last = (cfg["last_scan"] or {}).get("at")
    # A few minutes' slack so an hourly tick doesn't skip a scan that finished just after the previous tick.
    if last and datetime.fromisoformat(last) > datetime.now(UTC) - timedelta(hours=hours, minutes=-10):
        return None
    if await session.scalar(
        select(Job.id).where(Job.type == "backfill_scan", Job.status.in_(("queued", "running"))).limit(1)
    ):
        return None
    job = await enqueue(session, "backfill_scan", {"auto": True})
    await session.commit()
    notify_worker()
    return job.id


@job_handler("backfill_scan")
async def backfill_scan_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        await ctx.progress(0.1, "Listing archive files")
        was_idle = await backfill.next_file(session) is None
        result = await backfill.scan(session)
        if not ctx.payload.get("auto") or not was_idle:
            return result
        # A finished backfill auto-pauses; resume it for new files unless it stopped on an error.
        cfg = await backfill.load_settings(session)
        if cfg["paused"] and not cfg["last_error"] and await backfill.next_file(session) is not None:
            await backfill.save_settings(session, paused=False)
            result["started_job"] = await backfill_watchdog(session)
        return result


@job_handler("backfill_run")
async def backfill_run_job(ctx: JobContext) -> dict:
    """Works through FILES_PER_JOB files, then re-queues itself so restarts and other jobs aren't starved."""
    done: list[dict] = []
    remaining = True
    for i in range(backfill.FILES_PER_JOB):
        async with get_sessionmaker()() as session:
            if (await backfill.load_settings(session))["paused"]:
                remaining = False
                break
            step = await backfill.process_next(session)
        if step is None:
            remaining = False
            break
        done.append(step)
        await ctx.progress((i + 1) / backfill.FILES_PER_JOB, f"{step['stage']} → {step['status']}")
    async with get_sessionmaker()() as session:
        if remaining and not await backfill_job_active(session, exclude=ctx.job_id):
            await enqueue(session, "backfill_run", {})
            await session.commit()
            notify_worker()
        elif not remaining:
            cfg = await backfill.load_settings(session)
            if not cfg["paused"] and await backfill.next_file(session) is None:
                await backfill.save_settings(session, paused=True, last_error=None)
                await session.commit()
    return {"processed": len(done), "files": done[-10:]}
