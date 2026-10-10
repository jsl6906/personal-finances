"""Commit flagged backfill statements whose own statement-vs-ledger check balances (approved 2026-10-10).

Each batch is re-checked right before committing and only committed while every account on it is 'ok' and the
statement's rows add up to its own balance change; undecided duplicates are skipped (linked to the ledger copy).
File #1003, whose import #608 was already committed, is just marked done.

Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/commit_balanced_backfill.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import ledger.ai.categorize  # noqa: F401  (registers job handler)
import ledger.analytics.jobs  # noqa: F401  (registers job handlers)
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import check_batch
from ledger.imports.service import commit_batch
from ledger.jobs.worker import enqueue
from ledger.models import BackfillFile, ImportBatch, StatementCheck
from ledger.sources.backfill import mark_done

BATCHES = [
    1215, 1203, 1182, 1070,  # account 5
    267, 211, 212, 344, 266,  # accounts 56, 57
    1232, 1221, 1213, 1209, 1205, 1201, 1197, 1190, 1187, 1181, 1178, 1172, 1163, 1154, 1150, 1146, 1142,  # account 4
    168, 175,  # account 54
    1207, 1199, 1173, 1080, 1065,  # account 5, balanced once card 2656's account was merged into it
]
ALREADY_COMMITTED = {1003: 608}
ROOT = Path(__file__).resolve().parents[2]


def balanced(checks: list[dict]) -> bool:
    return bool(checks) and all(c["status"] == "ok" and c["trusted"] is True for c in checks)


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    committed = skipped = 0
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            if apply:
                rows = await conn.execute(
                    text("SELECT id, batch_id, decision FROM import_row WHERE batch_id = ANY(:ids)"), {"ids": BATCHES}
                )
                backup = {"batches": BATCHES, "import_row_decisions": [dict(r._mapping) for r in rows]}
                out = ROOT / "logs" / f"commit_balanced_backfill_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out} (undo a batch with POST /imports/<id>/rollback)")

            uncategorized: list[int] = []
            for bid in BATCHES:
                batch = await session.get(ImportBatch, bid)
                bf = await session.scalar(select(BackfillFile).where(BackfillFile.import_batch_id == bid))
                checks = await check_batch(session, batch) if batch.status == "review" else []
                head = f"#{bid} f{bf.id if bf else '-'} {batch.attachment.filename if batch.attachment else ''}"
                if batch.status != "review" or not balanced(checks):
                    skipped += 1
                    log.append(f"SKIP {head}: {batch.status} {[(c['status'], c['trusted']) for c in checks]}")
                    continue
                result = await commit_batch(session, batch, "skip")
                if bf and bf.status == "review":
                    mark_done(bf, result)
                uncategorized += result["uncategorized_ids"]
                after = (
                    await session.scalars(select(StatementCheck.status).where(StatementCheck.import_batch_id == bid))
                ).all()
                committed += 1
                log.append(
                    f"OK   {head}: inserted {result['inserted']}, linked {result['linked_to_existing']}, "
                    f"auto-fixed {batch.stats.get('auto_fixed', 0)}; check after commit {list(after)}"
                )

            for fid, bid in ALREADY_COMMITTED.items():
                bf = await session.get(BackfillFile, fid)
                batch = await session.get(ImportBatch, bid)
                if bf.import_batch_id == bid and batch.status == "committed" and bf.status == "review":
                    mark_done(bf, {"inserted": batch.stats.get("inserted", 0),
                                   "skipped_duplicates": batch.stats.get("skipped_duplicates", 0)})
                    log.append(f"DONE f{fid}: import #{bid} was already committed")

            if uncategorized:
                await enqueue(session, "categorize", {"ids": uncategorized})
            await enqueue(session, "detect_anomalies", {})
            log.append(f"committed {committed}, skipped {skipped}, {len(uncategorized)} new transactions to categorize")
            await session.commit()
        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
