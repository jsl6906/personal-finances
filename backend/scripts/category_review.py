"""Read-only taxonomy review: groups/categories with usage, plus top merchants for chosen categories.

Run: scripts/with_env.ps1 .env.azure python scripts/category_review.py [category name ...]
"""

import asyncio
import os
import sys

from sqlalchemy import text

from ledger.db.engine import dispose_engine, get_sessionmaker


async def main(names: list[str]) -> None:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(text("""--sql
            SELECT g.id gid, g.name grp, g.type gtype, g.hide_from_reports ghide, c.id cid, c.name cat, c.type ctype,
                   c.is_active, c.hide_from_reports chide,
                   count(t.id) n, coalesce(sum(t.amount), 0) total, min(t.txn_date) first, max(t.txn_date) last,
                   (SELECT count(*) FROM category_rule r WHERE r.category_id = c.id) rules,
                   (SELECT count(*) FROM budget b WHERE b.category_id = c.id) budgets
            FROM category_group g
            LEFT JOIN category c ON c.group_id = g.id
            LEFT JOIN "transaction" t ON t.category_id = c.id AND t.deleted_at IS NULL
            GROUP BY g.id, c.id ORDER BY g.sort_order, g.name, c.name
        """))).all()
        cur = None
        for r in rows:
            if r.gid != cur:
                cur = r.gid
                print(f"\n## [{r.gid}] {r.grp} ({r.gtype}{', hidden' if r.ghide else ''})")
            if r.cid:
                flags = ("" if r.is_active else " INACTIVE") + (" hidden" if r.chide else "")
                print(
                    f"  [{r.cid:>3}] {r.cat:<40} {r.ctype:<8} n={r.n:>5} total={r.total:>12} "
                    f"{r.first}..{r.last} rules={r.rules} budgets={r.budgets}{flags}"
                )
        for name in names:
            print(f"\n### {name}: top merchants")
            for r in (await s.execute(text("""--sql
                SELECT coalesce(mp.display_name, t.merchant, lower(t.description)) m, count(*) n, sum(t.amount) total,
                       min(t.txn_date) first, max(t.txn_date) last, min(t.description) example, min(t.merchant) mkey
                FROM "transaction" t JOIN category c ON c.id = t.category_id
                LEFT JOIN merchant_profile mp ON mp.key = t.merchant
                WHERE c.name = :n AND t.deleted_at IS NULL
                GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT :lim
            """), {"n": name, "lim": int(os.environ.get("REVIEW_LIMIT", "60"))})).all():
                print(f"  {r.n:>4} {r.total:>11} {r.first}..{r.last}  {r.m[:40]:<40} | {r.example[:50]} | {r.mkey}")
        await s.rollback()
    await dispose_engine()


asyncio.run(main(sys.argv[1:]))
