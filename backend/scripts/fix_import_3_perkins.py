"""Re-link import #3 (Perkins loan payment history) to the ledger's payments and use the ledger's sign (2026-10-10).

The PDF was read from the borrower's side (payments negative) and created 15 "ACH" copies of the Mint/Tiller
"Loan Payment" rows already in account 72 (payments positive). The 2026-10-01 account cleanup soft-deleted 14 of the
copies and added the statement as a source of the Tiller payments, but the rows still point at the deleted copies,
so the check shows every row missing and every payment extra. Rows now point at the Tiller payments and take the
loan account's sign.

Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/fix_import_3_perkins.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cleanup_accounts import q
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import check_batch, save_checks
from ledger.models import ImportBatch, ImportRow

BATCH, ACCOUNT = 3, 72
ROOT = Path(__file__).resolve().parents[2]


def show(c: dict) -> str:
    kinds: dict[str, int] = {}
    for i in c["detail"]["issues"]:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
    return f"{c['status']} stmt {c['statement_total']} ledger {c['ledger_total']} diff {c['difference']} {kinds}"


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            batch = await session.get(ImportBatch, BATCH)
            rows = (
                await session.scalars(select(ImportRow).where(ImportRow.batch_id == BATCH).order_by(ImportRow.row_index))
            ).all()
            if batch.status != "committed" or len(rows) != 15 or sum(r.amount for r in rows) != Decimal("-5429.45"):
                raise SystemExit(f"#{BATCH} changed since the review, aborting")
            [before] = await check_batch(session, batch)
            log.append(f"before: {show(before)}")

            if apply:
                backup = {
                    "import_batch": [
                        dict(r._mapping)
                        for r in await q(conn, "SELECT id, doc_meta, stats FROM import_batch WHERE id = :b", b=BATCH)
                    ],
                    "import_row": [
                        dict(r._mapping) for r in await q(conn, "SELECT * FROM import_row WHERE batch_id = :b", b=BATCH)
                    ],
                }
                out = ROOT / "logs" / f"fix_import_3_perkins_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            for r in rows:
                if r.amount == 0:
                    zero = (
                        await q(
                            conn,
                            """--sql
                            SELECT t.id, t.amount, t.account_id,
                                   (SELECT count(*) FROM transaction_source s WHERE s.transaction_id = t.id) AS n_src
                            FROM "transaction" t WHERE t.id = :t AND t.deleted_at IS NULL
                            """,
                            t=r.transaction_id,
                        )
                    ).one()
                    if (zero.amount, zero.account_id, zero.n_src) != (Decimal(0), ACCOUNT, 1):
                        raise SystemExit(f"Zero row's transaction {zero} isn't as reviewed; aborting")
                    await q(conn, 'UPDATE "transaction" SET deleted_at = :now WHERE id = :t', now=now, t=zero.id)
                    await q(conn, "DELETE FROM transaction_source WHERE import_row_id = :r", r=r.id)
                    r.decision, r.transaction_id = "invalid", None
                    r.errors = [*r.errors, "Zero-amount line; its 0.00 transaction was removed"]
                    log.append(f"  row {r.row_index:>2} {r.txn_date} 0.00: transaction {zero.id} soft-deleted, row invalid")
                    continue
                live = (
                    await q(
                        conn,
                        """--sql
                        SELECT t.id, t.amount, t.txn_date FROM transaction_source s
                        JOIN "transaction" t ON t.id = s.transaction_id
                        WHERE s.import_row_id = :r AND t.deleted_at IS NULL AND t.account_id = :a
                        """,
                        r=r.id,
                        a=ACCOUNT,
                    )
                ).all()
                if len(live) != 1 or live[0].amount != -r.amount or live[0].txn_date != r.txn_date:
                    raise SystemExit(f"Row {r.row_index}: expected one live {-r.amount} payment, found {live}; aborting")
                note = "" if live[0].id == r.transaction_id else f" (was {r.transaction_id}, deleted)"
                log.append(f"  row {r.row_index:>2} {r.txn_date} {r.amount:>9} -> {-r.amount:>9} txn {live[0].id}{note}")
                r.transaction_id = live[0].id
                r.amount = -r.amount
                r.raw = {**r.raw, "Amount": f"{r.amount:.2f}"}
            batch.doc_meta = {
                **batch.doc_meta,
                "sign_note": "Payments are positive (they reduce the loan balance), as in the ledger's loan account.",
                "signs_flipped_by_script": now.isoformat(),
            }
            await session.flush()

            [after] = await save_checks(session, batch)
            log.append(f"after: {show(after)}")
            for i in after["detail"]["issues"]:
                x = i["txn"] or i["row"]
                log.append(f"  {i['kind']} fix={i['fix']} suggested={i['suggested']}: {x['date']} {x['amount']} "
                           f"{x['description']} | {i['hint']}")
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
