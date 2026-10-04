"""Background jobs for imports and duplicate scans."""

import logging

from sqlalchemy import select

from ledger.config import get_settings
from ledger.db.engine import get_sessionmaker
from ledger.db.filters import id_in
from ledger.imports.service import extract_into_batch, prepare_batch
from ledger.jobs.worker import JobContext, enqueue, job_handler, notify_worker
from ledger.models import DuplicatePair, ImportBatch, Transaction
from ledger.services.dedupe import adjudicate_pending, scan_transactions

log = logging.getLogger(__name__)


async def _fail(batch_id: int, exc: Exception) -> None:
    async with get_sessionmaker()() as session:
        batch = await session.get(ImportBatch, batch_id)
        if batch:
            batch.status, batch.error = "failed", str(exc)[:2000]
            await session.commit()


@job_handler("extract_document")
async def extract_document_job(ctx: JobContext) -> dict:
    batch_id = ctx.payload["batch_id"]
    try:
        async with get_sessionmaker()() as session:
            batch = await session.get(ImportBatch, batch_id)
            await ctx.progress(0.1, "Reading the document with Gemini")
            result = await extract_into_batch(session, batch)
            await session.commit()
            return result
    except Exception as exc:
        await _fail(batch_id, exc)
        raise


@job_handler("prepare_import")
async def prepare_import_job(ctx: JobContext) -> dict:
    batch_id = ctx.payload["batch_id"]
    try:
        async with get_sessionmaker()() as session:
            batch = await session.get(ImportBatch, batch_id)
            return await prepare_batch(session, batch, progress=ctx.progress)
    except Exception as exc:
        await _fail(batch_id, exc)
        raise


@job_handler("statement_checks")
async def statement_checks_job(ctx: JobContext) -> dict:
    from ledger.imports.coverage import check_all

    apply = bool(ctx.payload.get("apply"))
    async with get_sessionmaker()() as session:
        await ctx.progress(0.01, "Comparing statements with the ledger")
        result = await check_all(session, progress=ctx.progress, apply=apply)
        new_ids = result.pop("new_ids")
        if result.get("fixed"):
            if new_ids and get_settings().gemini_key:
                uncategorized = (
                    await session.scalars(
                        select(Transaction.id).where(id_in(Transaction.id, new_ids), Transaction.category_id.is_(None))
                    )
                ).all()
                if uncategorized:
                    await enqueue(session, "categorize", {"ids": list(uncategorized)})
            await enqueue(session, "detect_anomalies", {})
            await session.commit()
            notify_worker()
        return result


@job_handler("dup_scan")
async def dup_scan_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        await ctx.progress(0.1, "Scanning for candidate pairs")
        result = await scan_transactions(session, since=ctx.payload.get("since"))
        await session.commit()
        if get_settings().gemini_key:
            pending = (
                await session.scalars(
                    select(DuplicatePair.id).where(
                        DuplicatePair.status == "pending",
                        DuplicatePair.txn_b_id.is_not(None),
                        DuplicatePair.ai_probability.is_(None),
                    )
                )
            ).all()
            if pending:
                await ctx.progress(0.4, f"Asking Gemini about {len(pending)} pairs")

                async def prog(f: float) -> None:
                    await ctx.progress(0.4 + 0.6 * f)

                result["ai_reviewed"] = await adjudicate_pending(session, list(pending), progress=prog)
        return result
