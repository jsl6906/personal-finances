import asyncio
import json

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_sessionmaker


async def main() -> None:
    async with get_sessionmaker()() as s:
        b = (await s.execute(text("select doc_meta, defaults, stats from import_batch where id=998"))).one()
        meta = b.doc_meta or {}
        print("doc_meta keys:", list(meta))
        for a in meta.get("accounts", []):
            print("ACCOUNT", json.dumps(a, default=str))
        for k, v in meta.items():
            if k != "accounts":
                print("META", k, json.dumps(v, default=str)[:600])
        print("defaults", json.dumps(b.defaults, default=str))
        rows = await s.execute(
            text("""--sql
            select r.id, r.row_index, r.txn_date, r.amount, r.description, r.account_id, r.decision, r.transaction_id, r.raw
            from import_row r where r.batch_id = 998 and (r.account_id = 3 or r.raw::text ilike '%5902%')
            order by r.row_index
            """)
        )
        for r in rows:
            print("ROW", r.row_index, r.txn_date, r.amount, r.description, "acct", r.account_id, r.decision, "txn", r.transaction_id)
            print("    raw", json.dumps(r.raw, default=str))
        txns = await s.execute(
            text("""--sql
            select t.id, t.txn_date, t.amount, t.description, t.deleted_at, t.transfer_match_id
            from transaction t where t.account_id = 3 and t.txn_date between '2016-09-20' and '2016-10-31'
            order by t.txn_date, t.id
            """)
        )
        for t in txns:
            print("TXN", t.id, t.txn_date, t.amount, t.description, "deleted" if t.deleted_at else "", "xfer", t.transfer_match_id)
            srcs = await s.execute(
                text("""--sql
                select ts.role, ts.import_batch_id, ts.import_row_id, ir.row_index, ts.created_at
                from transaction_source ts left join import_row ir on ir.id = ts.import_row_id
                where ts.transaction_id = :id order by ts.id
                """),
                {"id": t.id},
            )
            for x in srcs:
                print("    src", x.role, "batch", x.import_batch_id, "row", x.row_index)
        await s.rollback()
    await dispose_engine()


asyncio.run(main())
