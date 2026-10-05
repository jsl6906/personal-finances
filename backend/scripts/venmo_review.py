"""Read-only dump of Venmo-looking transactions with category, merchant and notes.

Run: scripts/with_env.ps1 .env.azure python scripts/venmo_review.py
"""

import asyncio

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_sessionmaker


async def main() -> None:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(text("""--sql
            SELECT t.id, t.txn_date, t.amount, t.account_id, a.name acct, t.description, t.merchant,
                   mp.display_name mname, c.name cat, g.name grp, t.category_source, t.notes,
                   t.transfer_match_id,
                   (SELECT string_agg(n.body, ' || ') FROM transaction_note n WHERE n.transaction_id = t.id) tnotes
            FROM "transaction" t
            LEFT JOIN account a ON a.id = t.account_id
            LEFT JOIN category c ON c.id = t.category_id
            LEFT JOIN category_group g ON g.id = c.group_id
            LEFT JOIN merchant_profile mp ON mp.key = t.merchant
            WHERE t.deleted_at IS NULL
              AND (t.description ILIKE '%venmo%' OR t.original_description ILIKE '%venmo%'
                   OR t.merchant ILIKE '%venmo%' OR a.name ILIKE '%venmo%')
            ORDER BY t.txn_date, t.id
        """))).all()
        for r in rows:
            print(
                f"{r.id:>7} {r.txn_date} {r.amount:>10} a{r.account_id}:{(r.acct or '')[:18]:<18} "
                f"{r.grp or '-'} > {r.cat or '-'} [{r.category_source}] tm={r.transfer_match_id} "
                f"| {r.description[:60]} | m={r.merchant} ({r.mname}) | notes={r.notes} | tn={r.tnotes}"
            )
        print(f"\n{len(rows)} rows")
        await s.rollback()
    await dispose_engine()


asyncio.run(main())
