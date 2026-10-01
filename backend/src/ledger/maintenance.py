"""Housekeeping: prune old job/AI logs and attachments nothing refers to any more."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import get_sessionmaker
from ledger.jobs.worker import JobContext, job_handler

log = logging.getLogger(__name__)

JOB_DAYS = 90
AI_LOG_DAYS = 365
ORPHAN_ATTACHMENT_DAYS = 7

_STATEMENTS = {
    "jobs": f"""--sql
        DELETE FROM job WHERE status IN ('succeeded', 'failed', 'cancelled')
          AND coalesce(finished_at, created_at) < now() - interval '{JOB_DAYS} days'""",
    "ai_calls": f"DELETE FROM ai_call_log WHERE created_at < now() - interval '{AI_LOG_DAYS} days'",
    "attachments": f"""--sql
        DELETE FROM attachment a
        WHERE a.created_at < now() - interval '{ORPHAN_ATTACHMENT_DAYS} days'
          AND NOT EXISTS (SELECT 1 FROM import_batch x WHERE x.attachment_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM statement x WHERE x.attachment_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM chat_message x WHERE x.attachment_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM backfill_file x WHERE x.attachment_id = a.id)""",
}


async def cleanup(session: AsyncSession) -> dict:
    removed = {}
    for name, sql in _STATEMENTS.items():
        removed[name] = (await session.execute(text(sql))).rowcount or 0
    await session.commit()
    log.info("Cleanup removed %s", removed)
    return removed


@job_handler("cleanup")
async def cleanup_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        return await cleanup(session)


async def renormalize(session: AsyncSession, chunk: int = 5000) -> int:
    """Recompute merchant keys (honouring merges and manual overrides) and fingerprints."""
    from sqlalchemy import bindparam, select, update

    from ledger.models import Transaction
    from ledger.services.merchants import alias_map, canonical
    from ledger.services.normalize import fingerprint, normalize_merchant

    aliases = await alias_map(session)
    changed, last_id = 0, 0
    while True:
        rows = (
            await session.execute(
                select(
                    Transaction.id,
                    Transaction.account_id,
                    Transaction.txn_date,
                    Transaction.amount,
                    Transaction.description,
                    Transaction.merchant,
                    Transaction.merchant_source,
                    Transaction.fingerprint,
                )
                .where(Transaction.id > last_id)
                .order_by(Transaction.id)
                .limit(chunk)
            )
        ).all()
        if not rows:
            return changed
        updates = []
        for r in rows:
            m = r.merchant if r.merchant_source == "user" else canonical(normalize_merchant(r.description), aliases)
            fp = fingerprint(r.account_id, r.txn_date, r.amount, r.description)
            if m != r.merchant or fp != r.fingerprint:
                updates.append({"tid": r.id, "m": m, "fp": fp})
        if updates:
            await session.execute(
                update(Transaction.__table__)
                .where(Transaction.__table__.c.id == bindparam("tid"))
                .values(merchant=bindparam("m"), fingerprint=bindparam("fp")),
                updates,
            )
            await session.commit()
            changed += len(updates)
        last_id = rows[-1].id
