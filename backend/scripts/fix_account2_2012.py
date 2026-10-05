"""Account 2 (x1897) statement diffs for 2012_10, 2012_12, 2013_04 and 2014_05 (2026-10-04).

- 2012_12.pdf (#951) row 19: the statement's 12/21 salary deposit was read as "2012-21-21" and marked invalid, so the
  Tiller copy looked like an extra. The running balance puts it between 12/19 and 12/24: date it 2012-12-21 and link it.
- The rest are Tiller rows the statements don't list: early copies of card purchases, a pre-tip restaurant hold, and a
  check recorded twice ("Paid Withdrawal"). The statements reconcile, so their "extra" rows are removed.

Dry run by default; pass --apply to commit. Run: scripts/with_env.ps1 .env.azure python scripts/fix_account2_2012.py
"""

import asyncio
import sys
from datetime import date

from cleanup_accounts import q
from sqlalchemy.ext.asyncio import AsyncSession
from untangle_legacy_statements import statuses

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import _add_source, apply_fixes, auto_fix, save_checks
from ledger.models import ImportBatch, ImportRow, Transaction

ACCOUNT = 2
BATCHES = [949, 951, 955, 968]
# (batch, row index, misread date, real date, Tiller transaction it is)
REDATE = [(951, 19, "2012-21-21", date(2012, 12, 21), 54425)]


async def run(apply: bool) -> None:
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        before = await statuses(conn, BATCHES)
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            for bid, idx, read, real, txn_id in REDATE:
                b = await session.get(ImportBatch, bid)
                row = (
                    await q(conn, "SELECT id FROM import_row WHERE batch_id = :b AND row_index = :i", b=bid, i=idx)
                ).scalar_one()
                row = await session.get(ImportRow, row)
                t = await session.get(Transaction, txn_id)
                if row.raw.get("Date") != read or row.decision != "invalid" or t.amount != row.amount or t.txn_date != real:
                    raise SystemExit(f"#{bid} row {idx} changed since the review, aborting")
                row.txn_date, row.decision, row.errors = real, "skip_duplicate", []
                row.raw = {**row.raw, "Date": real.isoformat()}
                await session.flush()
                await _add_source(session, b, row, txn_id, "matched")
                log.append(f"#{bid} row {idx}: date {read} -> {real}, linked to {txn_id} {t.description!r}")
            for bid in BATCHES:
                b = await session.get(ImportBatch, bid)
                res = await auto_fix(session, b)
                if res["applied"]:
                    log.append(f"#{bid}: {res['applied']} confident fixes")
                extras = [
                    (i, {"fix": i["fix"], "row_id": i["row_id"], "transaction_id": i["transaction_id"]})
                    for c in res["checks"]
                    if c["account_id"] == ACCOUNT
                    for i in c["detail"]["issues"]
                    if i["kind"] == "extra" and i["fix"] == "remove"
                ]
                if extras:
                    applied = (await apply_fixes(session, b, [f for _, f in extras], auto=True))["applied"]
                    log.append(f"#{bid}: removed {applied} more:")
                    log.extend(
                        f"     {i['txn']['date']} {i['txn']['amount']:>9} {i['txn']['description'][:40]!r}"
                        f" c={i['confidence']} | {i['hint']}"
                        for i, _ in extras
                    )
            log.append(f"transfer matching: {await match_transfers(session)}")
            for bid in BATCHES:
                await save_checks(session, await session.get(ImportBatch, bid))
            # Releases the savepoint only; the outer transaction decides (dry run rolls back).
            await session.commit()
        after = await statuses(conn, BATCHES)
        log.append("x1897 checks (before -> after):")
        for bid in BATCHES:
            b = [(c[2], str(c[3]), str(c[4])) for c in before.get(bid, []) if c[1] == ACCOUNT]
            a = [(c[2], str(c[3]), str(c[4])) for c in after.get(bid, []) if c[1] == ACCOUNT]
            log.append(f"  #{bid}: {b} -> {a}")
        others = (
            await q(
                conn,
                """--sql
                SELECT count(*) FROM import_row r JOIN import_batch b ON b.id = r.batch_id
                WHERE b.status = 'committed' AND b.source_type = 'document' AND r.decision = 'invalid'
                  AND r.errors::text LIKE '%Unreadable date%'
                """,
            )
        ).scalar()
        log.append(f"other committed statement rows with an unreadable date: {others}")

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
