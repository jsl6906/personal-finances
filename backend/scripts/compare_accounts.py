"""Read-only: compare two accounts to judge whether they should be merged.
Run: scripts/with_env.ps1 .env.azure python scripts/compare_accounts.py 5 103
"""

import asyncio
import json
import sys

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_engine

ACCOUNT_SQL = """--sql
SELECT a.id, a.name, a.account_type, a.mask, a.is_closed, a.is_hidden, a.notes, a.external_refs, i.name AS institution,
       (SELECT count(*) FROM "transaction" t WHERE t.account_id = a.id AND t.deleted_at IS NULL) AS txns,
       (SELECT min(txn_date) FROM "transaction" t WHERE t.account_id = a.id AND t.deleted_at IS NULL) AS first,
       (SELECT max(txn_date) FROM "transaction" t WHERE t.account_id = a.id AND t.deleted_at IS NULL) AS last
FROM account a LEFT JOIN institution i ON i.id = a.institution_id
WHERE a.id = ANY(:ids)
"""

CHECKS_SQL = """--sql
SELECT c.account_id, c.import_batch_id, c.account_ref, c.period_start, c.period_end, c.status, c.trusted,
       c.statement_total, c.ledger_total, c.difference, c.statement_rows, c.ledger_rows,
       b.attachment_id, att.filename, b.doc_meta->'accounts' AS doc_accounts, b.doc_meta->>'institution' AS doc_inst
FROM statement_check c
JOIN import_batch b ON b.id = c.import_batch_id
LEFT JOIN attachment att ON att.id = b.attachment_id
WHERE c.account_id = ANY(:ids) AND c.period_end >= :since
ORDER BY c.period_start, c.account_id
"""

SOURCES_SQL = """--sql
SELECT account_id, source_type, count(*) AS n, min(txn_date) AS first, max(txn_date) AS last
FROM "transaction"
WHERE account_id = ANY(:ids) AND deleted_at IS NULL AND txn_date >= :since
GROUP BY account_id, source_type ORDER BY account_id, source_type
"""

# Each transaction in the second account with its best same-amount counterpart in the first.
PAIRS_SQL = """--sql
SELECT b.id, b.txn_date, b.amount, b.description, b.source_type,
       m.id AS m_id, m.txn_date AS m_date, m.description AS m_desc, m.source_type AS m_src
FROM "transaction" b
LEFT JOIN LATERAL (
    SELECT a.id, a.txn_date, a.description, a.source_type
    FROM "transaction" a
    WHERE a.account_id = :a AND a.deleted_at IS NULL AND a.amount = b.amount
      AND abs(a.txn_date - b.txn_date) <= 5
    ORDER BY abs(a.txn_date - b.txn_date), a.id
    LIMIT 1
) m ON true
WHERE b.account_id = :b AND b.deleted_at IS NULL
ORDER BY b.txn_date, b.id
"""

WINDOW_SQL = """--sql
SELECT id, txn_date, amount, description, source_type
FROM "transaction"
WHERE account_id = :a AND deleted_at IS NULL AND txn_date BETWEEN :s AND :e
ORDER BY txn_date, id
"""


async def main(a: int, b: int) -> None:
    async with get_engine().connect() as conn:
        ex = lambda sql, **kw: conn.execute(text(sql), kw)  # noqa: E731
        print("== accounts")
        for r in (await ex(ACCOUNT_SQL, ids=[a, b])).mappings():
            print(json.dumps(dict(r), default=str))
        acct_b = (await ex(ACCOUNT_SQL, ids=[b])).mappings().one()
        since = acct_b["first"].replace(day=1)
        print(f"\n== statement checks since {since}")
        for r in (await ex(CHECKS_SQL, ids=[a, b], since=since)).mappings():
            print(json.dumps(dict(r), default=str))
        print(f"\n== transactions by source since {since}")
        for r in (await ex(SOURCES_SQL, ids=[a, b], since=since)).mappings():
            print(dict(r))
        pairs = (await ex(PAIRS_SQL, a=a, b=b)).mappings().all()
        matched = [p for p in pairs if p["m_id"]]
        print(f"\n== {len(matched)}/{len(pairs)} txns in #{b} have a same-amount txn in #{a} within 5 days")
        for p in pairs:
            m = f"-> {p['m_id']} {p['m_date']} [{p['m_src']}] {(p['m_desc'] or '')[:35]}" if p["m_id"] else "-> (none)"
            print(f"  {p['id']} {p['txn_date']} {p['amount']:>10} [{p['source_type']}] {(p['description'] or '')[:35]:<35} {m}")
        used = {p["m_id"] for p in matched}
        rows = (await ex(WINDOW_SQL, a=a, s=acct_b["first"], e=acct_b["last"])).mappings().all()
        rest = [r for r in rows if r["id"] not in used]
        print(f"\n== {len(rest)}/{len(rows)} txns in #{a} during #{b}'s span not paired with #{b}")
        for r in rest:
            print(f"  {r['id']} {r['txn_date']} {r['amount']:>10} [{r['source_type']}] {(r['description'] or '')[:50]}")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
