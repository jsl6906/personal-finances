"""Taxonomy follow-up (2026-10-05): fold the remaining investment categories into Transfer; Utilities -> Home Services.

- TSP Deposit (retirement-account contributions) and Change in Investment (HSA fund gains/losses) fold into Transfer,
  like Investments did in utilities_shopping_cleanup.py. Retired names become category aliases.
- Every Utilities category moves into the Home Services group and the Utilities group is deleted.

Dry run by default; --apply commits.
Run: scripts/with_env.ps1 .env.azure python scripts/fold_investments_utilities.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from taxonomy_cleanup import ROOT, q, rows_of

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine

DELETED = {"TSP Deposit": "Transfer", "Change in Investment": "Transfer"}
MERGE_GROUP = ("Utilities", "Home Services")


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
            groups = {r.name: r.id for r in await q(s, "SELECT id, name FROM category_group")}
            cats = {r.name: r.id for r in await q(s, "SELECT id, name FROM category")}
            missing = sorted(n for n in {*DELETED, *DELETED.values()} if n not in cats)
            missing += sorted(g for g in MERGE_GROUP if g not in groups)
            if missing:
                raise SystemExit(f"Taxonomy changed since the review (missing {missing}); aborting")
            deleted_ids = {cats[n]: n for n in DELETED}
            src_group, dest_group = (groups[g] for g in MERGE_GROUP)

            if apply:
                backup = {
                    "category_group": await rows_of(s, "SELECT * FROM category_group"),
                    "category": await rows_of(s, "SELECT * FROM category"),
                    "category_rule": await rows_of(s, "SELECT * FROM category_rule"),
                    "category_alias": await rows_of(s, "SELECT * FROM category_alias"),
                    "budget": await rows_of(s, "SELECT * FROM budget"),
                    "import_row": await rows_of(
                        s, "SELECT id, category_id FROM import_row WHERE category_id = ANY(:c)", c=list(deleted_ids)
                    ),
                    "anomaly": await rows_of(
                        s, "SELECT id, category_id FROM anomaly WHERE category_id = ANY(:c)", c=list(deleted_ids)
                    ),
                    "transaction": await rows_of(
                        s,
                        "SELECT id, category_id, suggested_category_id, suggestion_confidence, suggestion_reason "
                        'FROM "transaction" WHERE category_id = ANY(:c) OR suggested_category_id = ANY(:c)',
                        c=list(deleted_ids),
                    ),
                }
                out = ROOT / "logs" / f"fold_investments_utilities_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            # 1. Investment categories -> Transfer.
            for r in await q(
                s,
                """--sql
                SELECT c.name, a.name AS account, count(*) n, sum(t.amount) total, min(t.txn_date) first,
                       max(t.txn_date) last
                FROM "transaction" t JOIN category c ON c.id = t.category_id LEFT JOIN account a ON a.id = t.account_id
                WHERE t.category_id = ANY(:c) GROUP BY 1, 2 ORDER BY 1, 2
                """,
                c=list(deleted_ids),
            ):
                log.append(f"{r.name} -> Transfer: {r.account}: {r.n} rows, {r.total:,.2f} ({r.first}..{r.last})")
            res = await q(
                s,
                'UPDATE "transaction" SET category_id = :d, suggested_category_id = NULL, suggestion_confidence = NULL, '
                "suggestion_reason = NULL WHERE category_id = ANY(:c)",
                d=cats["Transfer"],
                c=list(deleted_ids),
            )
            log.append(f"transactions moved: {res.rowcount}")
            for table in ("category_rule", "import_row", "anomaly", "statement_series", "spread_rule", "budget"):
                for cid, name in deleted_ids.items():
                    res = await q(
                        s, f"UPDATE {table} SET category_id = :d WHERE category_id = :c", d=cats[DELETED[name]], c=cid
                    )
                    if res.rowcount:
                        log.append(f"{table}: {name} -> {DELETED[name]}: {res.rowcount}")
            for cid, name in deleted_ids.items():
                await q(s, "UPDATE category_alias SET category_id = :d WHERE category_id = :c", d=cats[DELETED[name]], c=cid)
                await q(
                    s,
                    "INSERT INTO category_alias (alias, category_id) VALUES (:a, :c) "
                    "ON CONFLICT (alias) DO UPDATE SET category_id = EXCLUDED.category_id",
                    a=name.lower(),
                    c=cats[DELETED[name]],
                )
            await q(s, "DELETE FROM category WHERE id = ANY(:c)", c=list(deleted_ids))
            log.append(f"deleted categories {sorted(DELETED)}; aliases -> Transfer")

            # 2. Utilities -> Home Services.
            moved = (
                await q(
                    s,
                    "UPDATE category SET group_id = :d WHERE group_id = :g RETURNING name",
                    d=dest_group,
                    g=src_group,
                )
            ).scalars()
            log.append(f"{MERGE_GROUP[0]} -> {MERGE_GROUP[1]}: {', '.join(sorted(moved))}")
            budgets = {
                r.group_id: r.id
                for r in await q(s, "SELECT id, group_id FROM budget WHERE group_id = ANY(:g)", g=[src_group, dest_group])
            }
            if src_group in budgets:
                if dest_group in budgets:
                    raise SystemExit("Both groups have a group budget; merge them by hand first")
                await q(s, "UPDATE budget SET group_id = :d WHERE id = :i", d=dest_group, i=budgets[src_group])
                log.append(f"budget {budgets[src_group]} moved to {MERGE_GROUP[1]}")
            await q(s, "DELETE FROM category_group WHERE id = :g", g=src_group)
            log.append(f"deleted group {MERGE_GROUP[0]}")

            log.append(f"transfer matching: {await match_transfers(s)}")
            await s.commit()

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
