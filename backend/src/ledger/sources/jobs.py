import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.crypto import decrypt
from ledger.db.engine import get_sessionmaker
from ledger.jobs.worker import JobContext, enqueue, job_handler, notify_worker
from ledger.models import Job, Source
from ledger.sources import backfill, simplefin, tiller
from ledger.sources.service import apply_feed, record_run

log = logging.getLogger(__name__)


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


@job_handler("backfill_scan")
async def backfill_scan_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        await ctx.progress(0.1, "Listing archive files")
        return await backfill.scan(session)


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
