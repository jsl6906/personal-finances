"""Fold the split Ally Online Savings x0982 records into account 54 (approved 2026-10-01).

43 "Nancy's Savings" is the same account re-linked by Tiller in 2012 without a mask (becomes a hidden Tiller shell);
101 was created by the statement backfill because "982" didn't match mask 0982 (absorbed and removed).
Dry run by default; pass --apply to commit. Run: scripts/with_env.ps1 .env.azure python scripts/merge_savings_0982.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from cleanup_accounts import PAIRS, drop_copies, match_copy, move_rows, q
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine

TARGET, SHELL, ABSORB = 54, 43, 101
EXPECTED = {
    TARGET: "Online Savings (2009-12)",
    SHELL: "Nancy's Savings",
    ABSORB: "Ally Bank Online Savings ···982",
}
NEW_NAME = "Online Savings (2009-16)"
SHELL_NAME = f"{NEW_NAME} - Tiller copy"
NOTE = 'Ally x0982. Tiller re-linked it as "Nancys Savings" (no mask) from 2012.'
ROOT = Path(__file__).resolve().parents[2]


async def repoint_import_dups(conn, pairs: list[tuple[int, int]]) -> None:
    """Pending import-row duplicates of a dropped copy now point at the kept row (drop_copies would close them)."""
    if not pairs:
        return
    ids = {"d": [d for d, _ in pairs], "k": [k for _, k in pairs]}
    await q(
        conn,
        f"""--sql
        DELETE FROM duplicate_pair dp USING {PAIRS}
        WHERE dp.txn_a_id = p.d AND dp.import_row_id IS NOT NULL AND dp.status = 'pending'
          AND EXISTS (SELECT 1 FROM duplicate_pair o WHERE o.import_row_id = dp.import_row_id AND o.txn_a_id = p.k)
        """,
        **ids,
    )
    await q(
        conn,
        f"""--sql
        UPDATE duplicate_pair dp SET txn_a_id = p.k FROM {PAIRS}
        WHERE dp.txn_a_id = p.d AND dp.import_row_id IS NOT NULL AND dp.status = 'pending'
        """,
        **ids,
    )


async def fold(conn, src: int, now: datetime, has_sources: bool, log: list[str]) -> None:
    pairs, leftover = await match_copy(conn, src, TARGET, False)
    detail = (
        await q(
            conn,
            f"""--sql
            SELECT d.txn_date, d.amount, d.description AS d_desc, k.txn_date AS k_date, k.description AS k_desc
            FROM {PAIRS} JOIN "transaction" d ON d.id = p.d JOIN "transaction" k ON k.id = p.k ORDER BY d.txn_date
            """,
            d=[d for d, _ in pairs],
            k=[k for _, k in pairs],
        )
    ).all()
    await repoint_import_dups(conn, pairs)
    await drop_copies(conn, pairs, now, has_sources)
    await move_rows(conn, leftover, TARGET)
    log.append(f"fold {src} -> {TARGET}: {len(pairs)} soft-deleted, {len(leftover)} moved")
    log.extend(f"    {r.txn_date} {r.amount:>9} {r.d_desc[:30]:<30} = {r.k_date} {r.k_desc[:30]}" for r in detail)


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        has_sources = (await q(conn, "SELECT to_regclass('transaction_source') IS NOT NULL")).scalar_one()
        current = {
            r.id: r
            for r in await q(conn, "SELECT id, name, external_refs FROM account WHERE id = ANY(:ids)", ids=list(EXPECTED))
        }
        wrong = [i for i, name in EXPECTED.items() if i not in current or current[i].name != name]
        if wrong:
            raise SystemExit(f"Accounts changed since the review, aborting: {wrong}")

        if apply:
            ids = list(EXPECTED)
            backup = {
                "account": [dict(r._mapping) for r in await q(conn, "SELECT * FROM account WHERE id = ANY(:a)", a=ids)],
                "transaction": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        "SELECT id, account_id, deleted_at, fingerprint, category_id, category_source, notes, transfer_match_id "
                        'FROM "transaction" WHERE account_id = ANY(:a)',
                        a=ids,
                    )
                ],
                "duplicate_pair": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        'SELECT dp.* FROM duplicate_pair dp JOIN "transaction" t ON t.id IN (dp.txn_a_id, dp.txn_b_id) '
                        "WHERE t.account_id = ANY(:a)",
                        a=ids,
                    )
                ],
                "import_row": [
                    dict(r._mapping)
                    for r in await q(conn, "SELECT id, account_id FROM import_row WHERE account_id = ANY(:a)", a=ids)
                ],
                "import_batch": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        "SELECT id, defaults FROM import_batch WHERE defaults->>'account_id' = :s OR EXISTS ("
                        "SELECT 1 FROM jsonb_each(CASE WHEN jsonb_typeof(defaults->'account_map') = 'object' "
                        "THEN defaults->'account_map' ELSE '{}' END) e WHERE e.value = to_jsonb(CAST(:i AS integer)))",
                        s=str(ABSORB),
                        i=ABSORB,
                    )
                ],
            }
            out = ROOT / "logs" / f"merge_savings_0982_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            log.append(f"backup: {out}")

        await fold(conn, SHELL, now, has_sources, log)
        await fold(conn, ABSORB, now, has_sources, log)

        # 101 is removed: everything that still references it moves to the target.
        await q(conn, 'UPDATE "transaction" SET account_id = :t WHERE account_id = :s', t=TARGET, s=ABSORB)
        for table, key in (("account_balance", "source"), ("holding", "external_id")):
            await q(
                conn,
                f"""--sql
                UPDATE {table} x SET account_id = :t WHERE x.account_id = :s AND NOT EXISTS (
                    SELECT 1 FROM {table} o WHERE o.account_id = :t AND o.as_of = x.as_of AND o.{key} = x.{key})
                """,
                t=TARGET,
                s=ABSORB,
            )
            await q(conn, f"DELETE FROM {table} WHERE account_id = :s", s=ABSORB)
        n_rows = (await q(conn, "UPDATE import_row SET account_id = :t WHERE account_id = :s", t=TARGET, s=ABSORB)).rowcount
        n_def = (
            await q(
                conn,
                """--sql
                UPDATE import_batch SET defaults = jsonb_set(defaults, '{account_id}', to_jsonb(CAST(:t AS integer)))
                WHERE defaults->>'account_id' = :s
                """,
                t=TARGET,
                s=str(ABSORB),
            )
        ).rowcount
        n_map = (
            await q(
                conn,
                """--sql
                UPDATE import_batch SET defaults = jsonb_set(defaults, '{account_map}', (
                    SELECT jsonb_object_agg(e.key, CASE WHEN e.value = to_jsonb(CAST(:s AS integer))
                                                        THEN to_jsonb(CAST(:t AS integer)) ELSE e.value END)
                    FROM jsonb_each(defaults->'account_map') e))
                WHERE jsonb_typeof(defaults->'account_map') = 'object' AND EXISTS (
                    SELECT 1 FROM jsonb_each(defaults->'account_map') e WHERE e.value = to_jsonb(CAST(:s AS integer)))
                """,
                t=TARGET,
                s=ABSORB,
            )
        ).rowcount
        log.append(f"repointed {ABSORB} -> {TARGET}: {n_rows} import rows, {n_def} batch defaults, {n_map} account maps")

        refs = {**(current[TARGET].external_refs or {})}
        for k, v in (current[ABSORB].external_refs or {}).items():
            if k in refs:
                raise SystemExit(f"Account {TARGET} already has a {k} link; cannot absorb {ABSORB}")
            refs[k] = v
        await q(conn, "DELETE FROM account WHERE id = :s", s=ABSORB)
        await q(
            conn,
            "UPDATE account SET name = :n, external_refs = CAST(:r AS jsonb), mask = '0982', "
            "notes = CASE WHEN coalesce(notes, '') = '' THEN :note ELSE notes END WHERE id = :t",
            n=NEW_NAME,
            r=json.dumps(refs),
            note=NOTE,
            t=TARGET,
        )
        await q(
            conn,
            "UPDATE account SET name = :n, is_hidden = true, is_closed = true WHERE id = :s",
            n=SHELL_NAME,
            s=SHELL,
        )
        log.append(f"removed account {ABSORB}; {TARGET} refs = {refs}")

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            log.append(f"transfer matching: {await match_transfers(session)}")

        pending = (
            await q(
                conn,
                """--sql
                SELECT count(*) FROM duplicate_pair dp JOIN "transaction" t ON t.id = dp.txn_a_id
                WHERE dp.status = 'pending' AND dp.import_row_id IS NOT NULL AND t.account_id = :t
                """,
                t=TARGET,
            )
        ).scalar_one()
        log.append(f"pending import duplicates against {TARGET}: {pending}")

        summary = await q(
            conn,
            """--sql
            SELECT a.id, a.name, a.mask, a.is_hidden, a.is_closed, count(t.id) AS n, min(t.txn_date) AS first, max(t.txn_date) AS last
            FROM account a LEFT JOIN "transaction" t ON t.account_id = a.id AND t.deleted_at IS NULL
            WHERE a.id = ANY(:ids) GROUP BY a.id ORDER BY a.id
            """,
            ids=[TARGET, SHELL],
        )
        for r in summary:
            flags = ("H" if r.is_hidden else "-") + ("C" if r.is_closed else "-")
            log.append(f"  {r.id:>3} {flags} {r.n:>6} {r.first}..{r.last}  {r.name} ({r.mask})")

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
