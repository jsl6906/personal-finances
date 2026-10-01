"""Compare backfilled rows with Tiller rows (same amount within 5 days): uv run python scripts/backfill_overlap.py"""

import asyncio

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_sessionmaker

SQL = """--sql
SELECT b.txn_date, b.amount, b.description, ab.name AS backfill_account,
       t.txn_date AS tiller_date, t.description AS tiller_description, at.name AS tiller_account
FROM "transaction" b
LEFT JOIN account ab ON ab.id = b.account_id
LEFT JOIN LATERAL (
    SELECT * FROM "transaction" x
    WHERE x.source_type = 'tiller' AND x.deleted_at IS NULL AND x.amount = b.amount
      AND abs(x.txn_date - b.txn_date) <= 5
    ORDER BY abs(x.txn_date - b.txn_date) LIMIT 1
) t ON true
LEFT JOIN account at ON at.id = t.account_id
WHERE b.source_type = 'backfill' AND b.deleted_at IS NULL
ORDER BY b.txn_date
"""


async def main():
    async with get_sessionmaker()() as s:
        for r in (await s.execute(text(SQL))).all():
            print(" | ".join(str(v) for v in r))
    await dispose_engine()


asyncio.run(main())
