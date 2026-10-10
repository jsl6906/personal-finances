"""Fold account 103 (Citi Costco ···2656) into 5 Costco Anywhere Visa (approved 2026-10-10).

···2656 is Mona's authorised-user card on the same Citi account as ···3758: one statement and one balance, and
Tiller already feeds both cards into account 5. Committing statement #1239 put the card's 77 rows into a new account
103, after which the ···3758 check removed Tiller's copies from account 5 as "not on the statement". This restores the
Tiller copies (relinking the statement rows to them), retires the 77 statement copies, removes account 103, and
re-runs the affected statement checks (now checked per ledger account, so both cards together).

Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/merge_costco_2656.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from pathlib import Path

from cleanup_accounts import move_rows, q
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import save_checks
from ledger.models import ImportBatch

TARGET, ABSORB, BATCH = 5, 103, 1239
EXPECTED = {TARGET: "Costco Anywhere Visa", ABSORB: "Citi Costco Anywhere Visa Card by Citi ···2656"}
ROWS = 77
REMOVED_ON = date(2026, 10, 10)
REFS = ["citi|3758", "citi|2656"]
NOTE = "Citi account with two cards: ···3758 (Josh) and ···2656 (Mona, authorised user); one statement and balance."
MATCH_DAYS = 5
ROOT = Path(__file__).resolve().parents[2]


def pair(kept: list, dropped: list) -> list[tuple]:
    """One-to-one: each statement copy with the closest-dated, most similar removed Tiller copy of the same amount."""
    cands = sorted(
        (
            abs((d.txn_date - k.txn_date).days),
            -SequenceMatcher(None, k.description.lower(), d.description.lower()).ratio(),
            k.id,
            d.id,
        )
        for k in kept
        for d in dropped
        if d.amount == k.amount and abs((d.txn_date - k.txn_date).days) <= MATCH_DAYS
    )
    by_k, by_d = {k.id: k for k in kept}, {d.id: d for d in dropped}
    out, used_k, used_d = [], set(), set()
    for _, _, kid, did in cands:
        if kid in used_k or did in used_d:
            continue
        used_k.add(kid)
        used_d.add(did)
        out.append((by_k[kid], by_d[did]))
    return sorted(out, key=lambda p: (p[0].txn_date, p[0].id))


async def checks_of(conn, batch_ids: list[int]) -> dict:
    rows = await q(
        conn,
        """--sql
        SELECT import_batch_id, account_ref, account_id, status, statement_total, ledger_total, difference, trusted
        FROM statement_check WHERE import_batch_id = ANY(:b) ORDER BY import_batch_id, account_ref
        """,
        b=batch_ids,
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r.import_batch_id, []).append(
            f"{r.account_ref or '-'}->{r.account_id} {r.status} stmt {r.statement_total} ledger {r.ledger_total} "
            f"diff {r.difference} trusted {r.trusted}"
        )
    return out


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        current = {
            r.id: r
            for r in await q(conn, "SELECT id, name, external_refs, notes FROM account WHERE id = ANY(:i)", i=list(EXPECTED))
        }
        wrong = [i for i, name in EXPECTED.items() if i not in current or current[i].name != name]
        if wrong:
            raise SystemExit(f"Accounts changed since the review, aborting: {wrong}")
        if (current[TARGET].external_refs or {}).get("statement"):
            raise SystemExit(f"Account {TARGET} already has a statement link; aborting")

        kept = (
            await q(
                conn,
                """--sql
                SELECT id, txn_date, amount, description, category_id, deleted_at, import_batch_id, source_type
                FROM "transaction" WHERE account_id = :a ORDER BY txn_date, id
                """,
                a=ABSORB,
            )
        ).all()
        if len(kept) != ROWS or any(k.deleted_at or k.import_batch_id != BATCH for k in kept):
            raise SystemExit(f"Account {ABSORB} no longer holds just the {ROWS} rows of import #{BATCH}; aborting")
        dropped = (
            await q(
                conn,
                """--sql
                SELECT t.id, t.txn_date, t.amount, t.description, t.category_id FROM "transaction" t
                WHERE t.account_id = :a AND t.source_type = 'tiller' AND t.deleted_at::date = :d
                  AND EXISTS (SELECT 1 FROM transaction_note n WHERE n.transaction_id = t.id AND n.import_batch_id = :b
                              AND n.body LIKE 'Removed: not listed on the statement%')
                ORDER BY t.txn_date, t.id
                """,
                a=TARGET,
                d=REMOVED_ON,
                b=BATCH,
            )
        ).all()
        pairs = pair(kept, dropped)
        if len(dropped) != ROWS or len(pairs) != ROWS:
            raise SystemExit(f"Expected {ROWS} removed Tiller copies to pair, found {len(dropped)} / {len(pairs)}; aborting")
        k_ids, d_ids = [k.id for k, _ in pairs], [d.id for _, d in pairs]
        ids = {"k": k_ids, "d": d_ids}
        unnest = "unnest(CAST(:k AS bigint[]), CAST(:d AS bigint[])) AS p(k, d)"

        affected = (
            await q(
                conn,
                "SELECT DISTINCT import_batch_id FROM statement_check WHERE account_id = ANY(:a) ORDER BY 1",
                a=list(EXPECTED),
            )
        ).scalars().all()
        before = await checks_of(conn, list(affected))

        if apply:
            both = k_ids + d_ids
            backup = {
                "account": [dict(r._mapping) for r in await q(conn, "SELECT * FROM account WHERE id = ANY(:a)", a=list(EXPECTED))],
                "transaction": [
                    dict(r._mapping) for r in await q(conn, 'SELECT * FROM "transaction" WHERE id = ANY(:i)', i=both)
                ],
                "transaction_source": [
                    dict(r._mapping)
                    for r in await q(conn, "SELECT * FROM transaction_source WHERE transaction_id = ANY(:i)", i=both)
                ],
                "transaction_note": [
                    dict(r._mapping)
                    for r in await q(conn, "SELECT * FROM transaction_note WHERE transaction_id = ANY(:i)", i=both)
                ],
                "transaction_tag": [
                    dict(r._mapping)
                    for r in await q(conn, "SELECT * FROM transaction_tag WHERE transaction_id = ANY(:i)", i=both)
                ],
                "duplicate_pair": [
                    dict(r._mapping)
                    for r in await q(
                        conn, "SELECT * FROM duplicate_pair WHERE txn_a_id = ANY(:i) OR txn_b_id = ANY(:i)", i=both
                    )
                ],
                "import_row": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        "SELECT id, account_id, transaction_id, decision FROM import_row "
                        "WHERE account_id = :a OR transaction_id = ANY(:i)",
                        a=ABSORB,
                        i=both,
                    )
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
                "statement_check": [
                    dict(r._mapping)
                    for r in await q(conn, "SELECT * FROM statement_check WHERE import_batch_id = ANY(:b)", b=list(affected))
                ],
            }
            out = ROOT / "logs" / f"merge_costco_2656_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            log.append(f"backup: {out}")

        log.append(f"{len(pairs)} statement copies paired with removed Tiller copies:")
        for k, d in pairs:
            days = (d.txn_date - k.txn_date).days
            log.append(
                f"  {k.id} {k.txn_date} {k.amount:>9} {k.description[:32]:<32} <- {d.id} {d.txn_date} ({days:+d}d) "
                f"{d.description[:32]}"
            )
        extra = (
            await q(
                conn,
                f"""--sql
                SELECT (SELECT count(*) FROM transaction_tag WHERE transaction_id = ANY(:k)) AS k_tags,
                       (SELECT count(*) FROM transaction_note WHERE transaction_id = ANY(:k)) AS k_notes,
                       (SELECT count(*) FROM "transaction" WHERE id = ANY(:k) AND coalesce(notes, '') <> '') AS k_txn_notes,
                       (SELECT count(*) FROM {unnest} JOIN "transaction" kt ON kt.id = p.k JOIN "transaction" dt ON dt.id = p.d
                        WHERE dt.category_id IS DISTINCT FROM kt.category_id) AS category_differs,
                       (SELECT count(*) FROM {unnest} JOIN "transaction" dt ON dt.id = p.d WHERE dt.category_id IS NULL) AS d_uncat,
                       (SELECT count(*) FROM transaction_source WHERE transaction_id = ANY(:d)) AS d_sources
                """,
                **ids,
            )
        ).one()
        log.append(f"carry-over: {dict(extra._mapping)}")

        # Restore the Tiller copies; the statement rows become their matched sources.
        await q(conn, 'UPDATE "transaction" SET deleted_at = NULL WHERE id = ANY(:d)', d=d_ids)
        await q(
            conn,
            "DELETE FROM transaction_note WHERE transaction_id = ANY(:d) AND import_batch_id = :b "
            "AND body LIKE 'Removed: not listed on the statement%'",
            d=d_ids,
            b=BATCH,
        )
        await q(
            conn,
            f"""--sql
            UPDATE transaction_source s SET transaction_id = p.d, role = 'matched' FROM {unnest}
            WHERE s.transaction_id = p.k AND NOT EXISTS (
                SELECT 1 FROM transaction_source o WHERE o.transaction_id = p.d AND o.import_row_id = s.import_row_id)
            """,
            **ids,
        )
        await q(conn, "DELETE FROM transaction_source WHERE transaction_id = ANY(:k)", k=k_ids)
        await q(conn, f"UPDATE import_row r SET transaction_id = p.d FROM {unnest} WHERE r.transaction_id = p.k", **ids)
        await q(conn, f"UPDATE transaction_note n SET transaction_id = p.d FROM {unnest} WHERE n.transaction_id = p.k", **ids)
        await q(
            conn,
            f"""--sql
            INSERT INTO transaction_tag (transaction_id, tag_id)
            SELECT p.d, tt.tag_id FROM {unnest} JOIN transaction_tag tt ON tt.transaction_id = p.k ON CONFLICT DO NOTHING
            """,
            **ids,
        )
        await q(
            conn,
            f"""--sql
            UPDATE "transaction" d SET category_id = k.category_id, category_source = k.category_source,
                                       category_rule_id = k.category_rule_id
            FROM {unnest} JOIN "transaction" k ON k.id = p.k
            WHERE d.id = p.d AND d.category_id IS NULL AND k.category_id IS NOT NULL
            """,
            **ids,
        )

        # Retire the statement copies (moved first: account 103 is removed and transactions restrict its deletion).
        await move_rows(conn, k_ids, TARGET)
        await q(conn, 'UPDATE "transaction" SET deleted_at = :now, transfer_match_id = NULL WHERE id = ANY(:k)', now=now, k=k_ids)
        await q(conn, 'UPDATE "transaction" SET transfer_match_id = NULL WHERE transfer_match_id = ANY(:k)', k=k_ids)
        await q(
            conn,
            """--sql
            UPDATE duplicate_pair SET status = 'confirmed_duplicate', decided_at = :now
            WHERE status = 'pending' AND (txn_a_id = ANY(:k) OR txn_b_id = ANY(:k))
            """,
            now=now,
            k=k_ids,
        )
        log.append(f"restored {len(d_ids)} Tiller copies in {TARGET}; retired {len(k_ids)} statement copies")

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

        for table in ("account_balance", "holding", "category_rule", "asset"):
            n = (await q(conn, f"SELECT count(*) FROM {table} WHERE account_id = :s", s=ABSORB)).scalar_one()
            if n:
                raise SystemExit(f"{table} has {n} rows for account {ABSORB}; aborting")
        refs = {**(current[TARGET].external_refs or {}), "statement": REFS}
        await q(conn, "DELETE FROM account WHERE id = :s", s=ABSORB)
        await q(
            conn,
            "UPDATE account SET external_refs = CAST(:r AS jsonb), "
            "notes = CASE WHEN coalesce(notes, '') = '' THEN :note ELSE notes END WHERE id = :t",
            r=json.dumps(refs),
            note=NOTE,
            t=TARGET,
        )
        log.append(f"removed account {ABSORB}; {TARGET} refs = {refs}")

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            for bid in affected:
                await save_checks(session, await session.get(ImportBatch, bid))
            await session.flush()
            log.append(f"transfer matching: {await match_transfers(session)}")
            # Releases the savepoint into the outer transaction (closing the session would roll it back).
            await session.commit()
        after = await checks_of(conn, list(affected))
        log.append("statement checks (before -> after):")
        for bid in affected:
            log.append(f"  #{bid}")
            log.extend(f"      before {c}" for c in before.get(bid, []))
            log.extend(f"      after  {c}" for c in after.get(bid, []))

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    report = ("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log)
    (ROOT / "logs" / f"merge_costco_2656_{'apply' if apply else 'dryrun'}.txt").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
