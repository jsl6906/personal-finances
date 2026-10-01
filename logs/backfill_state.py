import asyncio

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_engine

SQL = """--sql
SELECT (SELECT value FROM app_setting WHERE key = 'backfill') AS settings,
       (SELECT count(*) FROM backfill_file WHERE status = 'review' AND message LIKE 'Pick the account%') AS flagged,
       (SELECT count(*) FROM backfill_file WHERE status IN ('pending', 'classified')) AS queued,
       (SELECT string_agg(type || ':' || status, ', ') FROM job
         WHERE status IN ('queued', 'running') AND type LIKE 'backfill%') AS jobs
"""


async def main() -> None:
    async with get_engine().connect() as conn:
        print(dict((await conn.execute(text(SQL))).one()._mapping))
    await dispose_engine()


asyncio.run(main())
