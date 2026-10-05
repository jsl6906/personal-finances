"""Stop double-counting mortgage escrow (2026-10-05).

The PennyMac payments from checking/savings (Mortgage & Rent) already include escrow, so the escrow payouts on the
mortgage account (county taxes, homeowners insurance) become Transfer. The 2026-01-15 payment row (description is just
a date) becomes Mortgage Payment like the other payment rows. Account-scoped rules (priority 50, ahead of the
'homeowners' merchant rule) route future rows.

Dry run by default; --apply commits.
Run: scripts/with_env.ps1 .env.azure python scripts/mortgage_escrow.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from taxonomy_cleanup import ROOT, q, rows_of

from ledger.db.engine import dispose_engine, get_engine

ACCOUNT = "Mortgage - 8904 Longmead Ct"
NOTE = "mortgage escrow 2026-10-05"
# (regex on description, destination category)
RULES = [
    ("tax|insurance|escrow", "Transfer"),
    ("^[0-9]{2}/[0-9]{2}/[0-9]{4}$", "Mortgage Payment"),
]


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
            acct = (await q(s, "SELECT id FROM account WHERE name = :n", n=ACCOUNT)).scalar_one()
            cats = {r.name: r.id for r in await q(s, "SELECT id, name FROM category")}
            rows = (
                await q(
                    s,
                    """--sql
                    SELECT t.id, t.txn_date, t.amount, c.name AS category, c.type, t.description
                    FROM "transaction" t LEFT JOIN category c ON c.id = t.category_id
                    WHERE t.deleted_at IS NULL AND t.account_id = :a AND coalesce(c.type, '') <> 'transfer'
                    ORDER BY t.txn_date
                    """,
                    a=acct,
                )
            ).all()

            if apply:
                backup = {
                    "transaction": await rows_of(
                        s,
                        "SELECT id, category_id, category_rule_id, suggested_category_id, suggestion_confidence, "
                        'suggestion_reason FROM "transaction" WHERE id = ANY(:ids)',
                        ids=[r.id for r in rows],
                    )
                }
                out = ROOT / "logs" / f"mortgage_escrow_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            for pattern, dest in RULES:
                rid = (
                    await q(
                        s,
                        "INSERT INTO category_rule (match_type, pattern, category_id, account_id, priority, source, note) "
                        "VALUES ('regex', :p, :c, :a, 50, 'user', :n) RETURNING id",
                        p=pattern,
                        c=cats[dest],
                        a=acct,
                        n=NOTE,
                    )
                ).scalar_one()
                log.append(f"rule {rid} regex:{pattern!r} on {ACCOUNT} -> {dest} (priority 50)")
                ids = (
                    (
                        await q(
                            s,
                            'SELECT id FROM "transaction" WHERE id = ANY(:ids) AND description ~* :p',
                            ids=[r.id for r in rows],
                            p=pattern,
                        )
                    )
                    .scalars()
                    .all()
                )
                for r in rows:
                    if r.id in ids:
                        log.append(f"  #{r.id} {r.txn_date} {r.amount:>10} {r.category or '-':<26} {r.description}")
                await q(
                    s,
                    'UPDATE "transaction" SET category_id = :c, category_rule_id = :r, suggested_category_id = NULL, '
                    "suggestion_confidence = NULL, suggestion_reason = NULL WHERE id = ANY(:ids)",
                    c=cats[dest],
                    r=rid,
                    ids=list(ids),
                )
                log.append(f"  moved {len(ids)}")
                rows = [r for r in rows if r.id not in ids]
            for r in rows:
                log.append(f"left as is: #{r.id} {r.txn_date} {r.amount} {r.category} {r.description}")
            await s.commit()

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
