"""Add "Home & Renters Insurance" (Mortgage & Rent group) and move the insurance premiums into it (2026-10-05).

Rows: merchant 'homeowners' (2012/2013 card premiums + 2022/2023 escrow disbursements on the mortgage account) and
Travelers renters insurance (2010-11). Merchant rules route future rows. The Liberty Mutual 2024 deposit is a claim
payout and stays in Reimbursement.

Dry run by default; --apply commits.
Run: scripts/with_env.ps1 .env.azure python scripts/home_insurance.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from taxonomy_cleanup import ROOT, q, rows_of

from ledger.db.engine import dispose_engine, get_engine

NAME = "Home & Renters Insurance"
GROUP = "Mortgage & Rent"
MERCHANTS = ["homeowners", "travelers renters insurance premium"]
NOTE = "home insurance category 2026-10-05"


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
            group_id = (await q(s, "SELECT id FROM category_group WHERE name = :n", n=GROUP)).scalar_one_or_none()
            if group_id is None or (await q(s, "SELECT 1 FROM category WHERE name = :n", n=NAME)).first():
                raise SystemExit(f"Group {GROUP!r} missing or category {NAME!r} already exists; aborting")
            rows = (
                await q(
                    s,
                    """--sql
                    SELECT t.id, t.txn_date, t.amount, a.name AS account, c.name AS category, t.description
                    FROM "transaction" t LEFT JOIN account a ON a.id = t.account_id
                    LEFT JOIN category c ON c.id = t.category_id
                    WHERE t.deleted_at IS NULL AND t.merchant = ANY(:m) ORDER BY t.txn_date
                    """,
                    m=MERCHANTS,
                )
            ).all()
            ids = [r.id for r in rows]

            if apply:
                backup = {
                    "category_rule": await rows_of(
                        s, "SELECT * FROM category_rule WHERE match_type = 'merchant' AND pattern = ANY(:m)", m=MERCHANTS
                    ),
                    "transaction": await rows_of(
                        s,
                        "SELECT id, category_id, category_rule_id, suggested_category_id, suggestion_confidence, "
                        'suggestion_reason FROM "transaction" WHERE id = ANY(:ids)',
                        ids=ids,
                    ),
                }
                out = ROOT / "logs" / f"home_insurance_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            cid = (
                await q(
                    s,
                    "INSERT INTO category (group_id, name, type) VALUES (:g, :n, 'expense') RETURNING id",
                    g=group_id,
                    n=NAME,
                )
            ).scalar_one()
            log.append(f"created category {cid} {NAME!r} in {GROUP}")

            for r in rows:
                log.append(
                    f"  #{r.id} {r.txn_date} {r.amount:>9} {r.account[:28]:<28} {r.category or '-':<28} {r.description}"
                )
            res = await q(
                s,
                'UPDATE "transaction" SET category_id = :c, category_rule_id = NULL, suggested_category_id = NULL, '
                "suggestion_confidence = NULL, suggestion_reason = NULL WHERE id = ANY(:ids)",
                c=cid,
                ids=ids,
            )
            log.append(f"transactions moved: {res.rowcount}")

            for m in MERCHANTS:
                existing = (
                    await q(s, "SELECT id FROM category_rule WHERE match_type = 'merchant' AND pattern = :p", p=m)
                ).scalar_one_or_none()
                if existing:
                    await q(s, "UPDATE category_rule SET category_id = :c WHERE id = :i", c=cid, i=existing)
                    log.append(f"rule {existing} merchant:{m!r} repointed")
                else:
                    rid = (
                        await q(
                            s,
                            "INSERT INTO category_rule (match_type, pattern, category_id, source, note) "
                            "VALUES ('merchant', :p, :c, 'user', :n) RETURNING id",
                            p=m,
                            c=cid,
                            n=NOTE,
                        )
                    ).scalar_one()
                    log.append(f"rule {rid} merchant:{m!r} added")
            await s.commit()

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
