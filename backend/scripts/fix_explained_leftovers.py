"""Resolve leftovers on 'explained' statements (approved 2026-10-10).

- #124, #572, #861, #990: a ledger transaction near the period edge that the neighbouring statement lists under a
  nearby date as another transaction (already in the ledger); the extra copy is removed via the check's own fix.
- #721 (Amazon Visa 2018-21): its -63.01 Amazon row on 2018-07-09 is recorded in Amazon Visa (2009-18); the
  transaction moves to the statement's account when no statement of the old card claims it.

Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/fix_explained_leftovers.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cleanup_accounts import move_rows, q
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import apply_fixes, check_batch, save_checks
from ledger.models import ImportBatch

DUPLICATES = [124, 572, 861, 990]
MOVE_BATCH, MOVE_FROM, MOVE_TO, MOVE_AMOUNT = 721, 34, 18, Decimal("-63.01")
ROOT = Path(__file__).resolve().parents[2]


def fmt(c: dict) -> str:
    return f"{c['status']} diff {c['difference']}"


async def remove_duplicates(session: AsyncSession, log: list[str]) -> list[int]:
    removed = []
    for bid in DUPLICATES:
        batch = await session.get(ImportBatch, bid)
        checks = await check_batch(session, batch)
        picks = [
            (c, i)
            for c in checks
            for i in c["detail"]["issues"]
            if i["kind"] == "edge" and i["fix"] == "remove" and "likely a duplicate" in (i["hint"] or "")
        ]
        if len(picks) != 1:
            raise SystemExit(f"#{bid}: expected one duplicate edge item, found {len(picks)}; aborting")
        c, i = picks[0]
        t = i["txn"]
        log.append(f"#{bid} {c['account_ref'] or '-'} ({fmt(c)}): remove {t['id']} {t['date']} {t['amount']} {t['description'][:40]!r}")
        log.append(f"    {i['hint']}")
        result = await apply_fixes(session, batch, [{"fix": "remove", "row_id": None, "transaction_id": t["id"]}])
        if result["applied"] != 1:
            raise SystemExit(f"#{bid}: fix not applied; aborting")
        log.append(f"    -> {fmt(next(x for x in result['checks'] if x['account_ref'] == c['account_ref']))}")
        removed.append(t["id"])
    return removed


async def move_amazon(conn, session: AsyncSession, log: list[str]) -> list[int]:
    batch = await session.get(ImportBatch, MOVE_BATCH)
    [c] = await check_batch(session, batch)
    away = [i for i in c["detail"]["issues"] if i["kind"] == "elsewhere"]
    if c["account_id"] != MOVE_TO or len(away) != 1 or Decimal(away[0]["row"]["amount"]) != MOVE_AMOUNT:
        raise SystemExit(f"#{MOVE_BATCH} no longer looks as reviewed; aborting")
    row_id = away[0]["row"]["row_id"]
    txn_ids = (
        await q(conn, "SELECT transaction_id FROM transaction_source WHERE import_row_id = :r", r=row_id)
    ).scalars().all()
    txn = (
        await q(
            conn,
            """--sql
            SELECT id, account_id, txn_date, amount, description FROM "transaction"
            WHERE id = ANY(:ids) AND deleted_at IS NULL
            """,
            ids=list(txn_ids),
        )
    ).one()
    if txn.account_id != MOVE_FROM or txn.amount != MOVE_AMOUNT:
        raise SystemExit(f"Transaction {txn.id} isn't the reviewed one; aborting")
    claims = (
        await q(
            conn,
            """--sql
            SELECT b.id, b.status, a.filename FROM transaction_source s
            JOIN import_batch b ON b.id = s.import_batch_id LEFT JOIN attachment a ON a.id = b.attachment_id
            WHERE s.transaction_id = :t AND b.source_type = 'document' AND b.id <> :b
            UNION
            SELECT b.id, b.status, a.filename FROM duplicate_pair dp
            JOIN import_row ir ON ir.id = dp.import_row_id JOIN import_batch b ON b.id = ir.batch_id
            LEFT JOIN attachment a ON a.id = b.attachment_id
            WHERE dp.txn_a_id = :t AND b.source_type = 'document' AND b.id <> :b
              AND dp.status IN ('pending', 'confirmed_duplicate') AND b.status IN ('review', 'committed')
            """,
            t=txn.id,
            b=MOVE_BATCH,
        )
    ).all()
    log.append(
        f"#{MOVE_BATCH} ({fmt(c)}): move {txn.id} {txn.txn_date} {txn.amount} {txn.description!r} "
        f"from account {MOVE_FROM} to {MOVE_TO}"
    )
    if claims:
        raise SystemExit(f"Other statements claim {txn.id}: {[tuple(x) for x in claims]}; aborting")
    await move_rows(conn, [txn.id], MOVE_TO)
    [c] = await save_checks(session, batch)
    log.append(f"    -> {fmt(c)}")
    return [txn.id]


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            if apply:
                # Snapshot before any change; the edge items and the moved row are re-found by the same rules below.
                pre = [i for bid in DUPLICATES for c in await check_batch(session, await session.get(ImportBatch, bid))
                       for i in c["detail"]["issues"] if i["kind"] == "edge"]
                ids = [i["txn"]["id"] for i in pre]
                backup = {
                    "transaction": [
                        dict(r._mapping)
                        for r in await q(
                            conn,
                            'SELECT * FROM "transaction" WHERE id = ANY(:ids) OR id IN ('
                            "SELECT s.transaction_id FROM transaction_source s JOIN import_row ir ON ir.id = s.import_row_id "
                            "WHERE ir.batch_id = :b AND ir.amount = :amt)",
                            ids=ids,
                            b=MOVE_BATCH,
                            amt=MOVE_AMOUNT,
                        )
                    ]
                }
                out = ROOT / "logs" / f"fix_explained_leftovers_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            await remove_duplicates(session, log)
            moved = await move_amazon(conn, session, log)
            # The old card's statements around the move see one transaction fewer.
            for (bid,) in await q(
                conn,
                """--sql
                SELECT DISTINCT c.import_batch_id FROM statement_check c JOIN import_batch b ON b.id = c.import_batch_id
                WHERE c.account_id = :a AND c.period_end >= '2018-06-01' AND c.period_start <= '2018-08-31'
                """,
                a=MOVE_FROM,
            ):
                before = (await q(conn, "SELECT status FROM statement_check WHERE import_batch_id = :b", b=bid)).scalars().all()
                after = [x["status"] for x in await save_checks(session, await session.get(ImportBatch, bid))]
                log.append(f"#{bid} (account {MOVE_FROM}, after moving {moved}): {before} -> {after}")
            # Releases the savepoint only; the outer transaction decides (dry run rolls back).
            await session.commit()
        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
