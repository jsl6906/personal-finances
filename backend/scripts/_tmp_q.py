import asyncio

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_sessionmaker


async def main() -> None:
    async with get_sessionmaker()() as s:
        for r in (await s.execute(text("""--sql
            SELECT t.id, t.account_id, t.txn_date, t.amount, t.description, t.import_batch_id, t.deleted_at,
                   (SELECT string_agg(ts.import_batch_id || ':' || ts.role, ',') FROM transaction_source ts
                    WHERE ts.transaction_id = t.id) AS srcs
            FROM transaction t WHERE t.id IN (3231, 3232, 66816, 66817) ORDER BY t.id
        """))).all():
            print(" ", tuple(r))
        await s.rollback()
    await dispose_engine()


asyncio.run(main())
