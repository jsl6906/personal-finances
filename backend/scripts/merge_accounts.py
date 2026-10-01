"""Second pass after cleanup_accounts.py: fold re-linked Tiller feeds into the current account (approved 2026-10-01).

Dry run by default; pass --apply to commit. Run: scripts/with_env.ps1 .env.azure python scripts/merge_accounts.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from cleanup_accounts import drop_copies, match_copy, move_rows, q
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine

# (old feed, current account): matching rows in the old feed are soft-deleted, the rest move to the current account.
FOLDS = [(22, 18), (11, 5), (12, 6)]
# id: (expected current name, new name)
RENAMES = {
    18: ("Amazon Visa (2021-23)", "Amazon Visa (2018-23)"),
    22: ("Amazon Visa (2018-21)", "Amazon Visa (2018-23) - Tiller copy"),
    30: ("Amazon Visa (2018-21) - Tiller copy", "Amazon Visa (2018-23) - Tiller copy 2"),
    11: ("Costco Anywhere Visa (2016-24)", "Costco Anywhere Visa - Tiller copy"),
    12: ("Home Depot Card (2020-24)", "Home Depot Card - Tiller copy"),
    5: ("Costco Anywhere Visa", None),
    6: ("Home Depot Card", None),
}
SHELLS = [22, 11, 12]
ROOT = Path(__file__).resolve().parents[2]


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        has_sources = (await q(conn, "SELECT to_regclass('transaction_source') IS NOT NULL")).scalar_one()
        names = dict((await q(conn, "SELECT id, name FROM account WHERE id = ANY(:ids)", ids=list(RENAMES))).all())
        wrong = [i for i, (old, _) in RENAMES.items() if names.get(i) != old]
        if wrong:
            raise SystemExit(f"Accounts changed since the review, aborting: {wrong}")

        if apply:
            ids = sorted({i for f in FOLDS for i in f} | {30})
            rows = await q(
                conn,
                "SELECT id, account_id, deleted_at, fingerprint, category_id, category_source, notes, transfer_match_id "
                'FROM "transaction" WHERE account_id = ANY(:a)',
                a=ids,
            )
            backup = {
                "account": [dict(r._mapping) for r in await q(conn, "SELECT * FROM account WHERE id = ANY(:a)", a=ids)],
                "transaction": [dict(r._mapping) for r in rows],
            }
            out = ROOT / "logs" / f"merge_accounts_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            log.append(f"backup: {out}")

        for old, current in FOLDS:
            pairs, leftover = await match_copy(conn, old, current, False)
            await drop_copies(conn, pairs, now, has_sources)
            await move_rows(conn, leftover, current)
            log.append(f"fold {old} -> {current}: {len(pairs)} soft-deleted, {len(leftover)} moved")

        for i in RENAMES:
            await q(conn, "UPDATE account SET name = :n WHERE id = :i", n=f"__merge_{i}", i=i)
        for i, (old, new) in RENAMES.items():
            await q(conn, "UPDATE account SET name = :n WHERE id = :i", n=new or old, i=i)
        await q(conn, "UPDATE account SET is_hidden = true, is_closed = true WHERE id = ANY(:ids)", ids=SHELLS)

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            log.append(f"transfer matching: {await match_transfers(session)}")

        summary = await q(
            conn,
            """--sql
            SELECT a.id, a.name, a.is_hidden, a.is_closed, count(t.id) AS n, min(t.txn_date) AS first, max(t.txn_date) AS last
            FROM account a LEFT JOIN "transaction" t ON t.account_id = a.id AND t.deleted_at IS NULL
            WHERE a.id = ANY(:ids) GROUP BY a.id ORDER BY a.name
            """,
            ids=list(RENAMES),
        )
        for r in summary:
            flags = ("H" if r.is_hidden else "-") + ("C" if r.is_closed else "-")
            log.append(f"  {r.id:>3} {flags} {r.n:>6} {r.first}..{r.last}  {r.name}")

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
