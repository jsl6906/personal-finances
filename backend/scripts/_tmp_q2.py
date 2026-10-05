import asyncio

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_sessionmaker

QUERIES = {
    "loan/auto rows by account": """--sql
        SELECT c.name cat, a.name acct, a.account_type, count(*) n, sum(t.amount) total, min(t.txn_date), max(t.txn_date)
        FROM "transaction" t JOIN category c ON c.id = t.category_id LEFT JOIN account a ON a.id = t.account_id
        WHERE c.name IN ('Loan Payment', 'Auto Payment', 'Student Loan') AND t.deleted_at IS NULL
        GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC
    """,
    "home services zelle/check/synchrony/costco": """--sql
        SELECT t.txn_date, t.amount, t.description, t.notes FROM "transaction" t JOIN category c ON c.id = t.category_id
        WHERE c.name = 'Home Services' AND t.deleted_at IS NULL
          AND (t.merchant ~ '^(zelle from mona|check|synchrony|costco|vp services|mlm)' OR t.description ~* 'zelle payment from mona')
        ORDER BY t.merchant, t.txn_date LIMIT 60
    """,
    "transfer flags on loan rows": """--sql
        SELECT c.name, count(*) FILTER (WHERE t.transfer_match_id IS NOT NULL) matched, count(*)
        FROM "transaction" t JOIN category c ON c.id = t.category_id
        WHERE c.name IN ('Loan Payment', 'Auto Payment') AND t.deleted_at IS NULL GROUP BY 1
    """,
    "checks in reviewed categories": """--sql
        SELECT c.name, t.txn_date, t.amount, t.description, t.notes FROM "transaction" t JOIN category c ON c.id = t.category_id
        WHERE c.name IN ('Auto & Transport', 'Books, Amusement, & Entertainment', 'Auto Payment', 'Gifts & Donations')
          AND t.merchant IN ('check', 'echeck deposit') AND t.deleted_at IS NULL ORDER BY 1, 2
    """,
    "income: positives by merchant's usual expense category": """--sql
        WITH usual AS (
            SELECT DISTINCT ON (t.merchant) t.merchant, c.name cat
            FROM "transaction" t JOIN category c ON c.id = t.category_id
            WHERE c.type = 'expense' AND t.deleted_at IS NULL AND t.merchant IS NOT NULL
            GROUP BY t.merchant, c.name ORDER BY t.merchant, count(*) DESC
        )
        SELECT coalesce(u.cat, '(none)') usual, count(*) n, sum(t.amount) total
        FROM "transaction" t JOIN category c ON c.id = t.category_id LEFT JOIN usual u ON u.merchant = t.merchant
        WHERE c.name = 'Income' AND t.deleted_at IS NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 25
    """,
    "aliases": "SELECT a.alias, c.name FROM category_alias a JOIN category c ON c.id = a.category_id ORDER BY 1",
    "budgets": """--sql
        SELECT b.id, c.name cat, g.name grp, b.amount, b.period_type FROM budget b
        LEFT JOIN category c ON c.id = b.category_id LEFT JOIN category_group g ON g.id = b.group_id ORDER BY 1
    """,
    "rules on affected categories": """--sql
        SELECT c.name, r.match_type, r.pattern, r.source FROM category_rule r JOIN category c ON c.id = r.category_id
        WHERE c.name IN ('Coffee / Bakery / Ice Cream', 'Fast Food', 'Books, Amusement, & Entertainment', 'Home Services',
          'Home Center / Tools / Garden', 'Service Fee', 'Finance Charge', 'Gifts & Donations', 'Auto & Transport')
        ORDER BY 1, 3
    """,
}


async def main() -> None:
    async with get_sessionmaker()() as s:
        for title, sql in QUERIES.items():
            print(f"\n### {title}")
            for r in (await s.execute(text(sql))).all():
                print("  ", " | ".join(str(v)[:70] for v in r))
        await s.rollback()
    await dispose_engine()


asyncio.run(main())
