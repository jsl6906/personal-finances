"""Pair transfer legs between household accounts.

A transfer-category row is matched 1:1 with a row of the opposite amount in a different account within a few
days (the partner must be a transfer or uncategorized). Both rows point at each other via transfer_match_id;
matched pairs are left out of reports, and transfers that stay unmatched are flagged as anomalies.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MATCH_WINDOW_DAYS = 7

_CANDIDATES = text(
    """--sql
    SELECT a.id AS a_id, b.id AS b_id, abs(b.txn_date - a.txn_date) AS gap, (cb.type IS NULL) AS b_uncategorized
    FROM "transaction" a
    JOIN category ca ON ca.id = a.category_id AND ca.type = 'transfer'
    JOIN "transaction" b ON b.deleted_at IS NULL AND b.amount = -a.amount
        AND abs(b.txn_date - a.txn_date) <= CAST(:window AS integer)
        AND b.account_id IS DISTINCT FROM a.account_id
    LEFT JOIN category cb ON cb.id = b.category_id
    WHERE a.deleted_at IS NULL AND a.amount <> 0 AND coalesce(cb.type, 'transfer') = 'transfer'
    """
)

_CURRENT = text('SELECT id, transfer_match_id FROM "transaction" WHERE transfer_match_id IS NOT NULL')

_SET = text('UPDATE "transaction" SET transfer_match_id = :match WHERE id = :id')


async def match_transfers(session: AsyncSession) -> dict:
    """Recompute all transfer pairs (closest date first, transfer-to-transfer before uncategorized partners)."""
    edges = (await session.execute(_CANDIDATES, {"window": MATCH_WINDOW_DAYS})).all()
    edges.sort(key=lambda e: (e.b_uncategorized, e.gap, min(e.a_id, e.b_id), max(e.a_id, e.b_id)))
    want: dict[int, int] = {}
    for e in edges:
        if e.a_id not in want and e.b_id not in want:
            want[e.a_id], want[e.b_id] = e.b_id, e.a_id
    have = dict((await session.execute(_CURRENT)).all())
    changes = [{"id": i, "match": want.get(i)} for i in have.keys() | want.keys() if have.get(i) != want.get(i)]
    if changes:
        await session.execute(_SET, changes)
    return {"pairs": len(want) // 2, "changed": len(changes)}
