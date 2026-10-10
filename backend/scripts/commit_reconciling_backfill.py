"""Commit flagged backfill statements whose suggested fixes close the statement-vs-ledger gap to the cent.

The AI duplicate check marked many statement rows "keep" (insert as new) although the ledger already holds them (the
feed words them differently), so the statement check saw the ledger's copies as extras. Per batch:
  1. 'keep' rows go back to 'insert' so the check can pair them with the ledger;
  2. the batch must then be self-consistent and its suggested fixes must close the difference to the cent;
  3. those fixes are applied before commit (mostly 'link': the row is the ledger transaction, don't insert it);
  4. the batch is committed (undecided duplicates skipped) and must check 'ok' afterwards.

Dry run by default; pass --apply to commit. Pass batch ids to limit the run.
Run: scripts/with_env.ps1 .env.azure python scripts/commit_reconciling_backfill.py [--apply] [batch ids...]
"""

import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

import ledger.ai.categorize  # noqa: F401  (registers job handler)
import ledger.analytics.jobs  # noqa: F401  (registers job handlers)
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import apply_fixes, check_batch
from ledger.imports.service import commit_batch
from ledger.jobs.worker import enqueue
from ledger.models import BackfillFile, ImportBatch, ImportRow
from ledger.sources.backfill import mark_done

BATCHES = [
    321, 1222, 1219, 1194, 1191, 1188, 1185, 1179, 1176, 1170, 1167, 1164, 1161, 1158, 1140, 1074, 178, 61, 201, 343,
    136, 1228, 1224, 1218, 1193, 1184, 1175, 1169, 1166, 1160, 1138, 1125, 843, 563, 568, 525, 618, 612, 690, 649,
    635, 717, 153,
]
ROOT = Path(__file__).resolve().parents[2]


def reconciles(checks: list[dict]) -> bool:
    return bool(checks) and all(
        c["trusted"] is True and (c["status"] == "ok" or (c["status"] == "mismatch" and c["detail"]["fixes_reconcile"]))
        for c in checks
    )


def picks(checks: list[dict]) -> list[dict]:
    return [
        {"fix": i["fix"], "row_id": i["row_id"], "transaction_id": i["transaction_id"]}
        for c in checks
        for i in c["detail"]["issues"]
        if i["fix"] and i["suggested"]
    ]


async def run(apply: bool, only: set[int]) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    targets = [b for b in BATCHES if not only or b in only]
    done = skipped = 0
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            if apply:
                rows = await conn.execute(
                    text("SELECT id, batch_id, decision FROM import_row WHERE batch_id = ANY(:ids)"), {"ids": targets}
                )
                txns = await conn.execute(
                    text("""--sql
                        SELECT t.* FROM "transaction" t
                        WHERE t.deleted_at IS NULL AND EXISTS (
                            SELECT 1 FROM statement_check c WHERE c.import_batch_id = ANY(:ids)
                              AND c.account_id = t.account_id
                              AND t.txn_date BETWEEN c.period_start - 7 AND c.period_end + 7)
                    """),
                    {"ids": targets},
                )
                backup = {
                    "batches": targets,
                    "import_row_decisions": [dict(r._mapping) for r in rows],
                    "transaction": [dict(r._mapping) for r in txns],
                }
                out = ROOT / "logs" / f"commit_reconciling_backfill_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            new_ids: list[int] = []
            for bid in targets:
                batch = await session.get(ImportBatch, bid)
                bf = await session.scalar(select(BackfillFile).where(BackfillFile.import_batch_id == bid))
                head = f"#{bid} f{bf.id if bf else '-'} {batch.attachment.filename if batch.attachment else ''}"
                if batch.status != "review":
                    skipped += 1
                    log.append(f"SKIP {head}: {batch.status}")
                    continue
                async with session.begin_nested() as sp:
                    reset = (
                        await session.execute(
                            update(ImportRow)
                            .where(ImportRow.batch_id == bid, ImportRow.decision == "keep")
                            .values(decision="insert")
                        )
                    ).rowcount
                    checks = await check_batch(session, batch)
                    if not reconciles(checks):
                        await sp.rollback()
                        skipped += 1
                        log.append(f"SKIP {head}: {[(c['status'], c['trusted']) for c in checks]}")
                        continue
                    chosen = picks(checks)
                    planned = Counter(f["fix"] for f in chosen)
                    diff = sum(c["difference"] for c in checks)
                    fixed = await apply_fixes(session, batch, chosen) if chosen else {"applied": 0, "skipped": 0}
                    if fixed["skipped"]:
                        raise SystemExit(f"{head}: {fixed['skipped']} fixes went stale; aborting")
                    result = await commit_batch(session, batch, "skip")
                    if bf and bf.status == "review":
                        mark_done(bf, result)
                    new_ids += result["uncategorized_ids"]
                    after = await check_batch(session, batch)
                    if not all(c["status"] in ("ok", "explained") for c in after):
                        raise SystemExit(f"{head}: {[(c['status'], str(c['difference'])) for c in after]} after commit; aborting")
                done += 1
                log.append(
                    f"OK   {head}: keep->insert {reset}, diff {diff}, fixes {dict(planned)}; inserted {result['inserted']}, "
                    f"linked {result['linked_to_existing']}; now {[(c['status'], str(c['difference'])) for c in after]}"
                )
            if new_ids:
                await enqueue(session, "categorize", {"ids": new_ids})
            await enqueue(session, "detect_anomalies", {})
            log.append(f"committed {done}, skipped {skipped}")
            await session.commit()
        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(run("--apply" in args, {int(a) for a in args if a.isdigit()}))
