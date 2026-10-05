"""Untangle import #779 and fold the duplicate Ally x8083 account (2026-10-04).

#779 "Sep 2019 Ally Bank Statement.pdf" is a combined statement for four Ally accounts, read before statements were
split by account: all 34 rows went to Josh and Mona's Checking (x1897), and 5 rows that belong to Savings x5902 and
Nancy's Checking x8083 were inserted there as new transactions although Tiller already had them in those accounts.
102 "Ally Bank Interest Checking ···8083" was created by the backfill because Nancy's Checking (21) had no mask.

Dry run by default; pass --apply to commit. Run: scripts/with_env.ps1 .env.azure python scripts/fix_import_779.py [--apply]
"""

import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cleanup_accounts import PAIRS, drop_copies, match_copy, move_rows, q
from merge_savings_0982 import repoint_import_dups
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import auto_fix, save_checks
from ledger.models import ImportBatch, ImportRow

BATCH = 779
TARGET, ABSORB = 21, 102
# (ref, product name, type, ledger account, statement row indexes); sections follow the printed Balance column.
SECTIONS = [
    ("1897", "Interest Checking", "checking", 2, range(0, 18)),
    ("1224", "Interest Checking", "checking", 13, range(18, 19)),
    ("8083", "Interest Checking", "checking", TARGET, range(19, 23)),
    ("5902", "Online Savings", "savings", 3, range(23, 34)),
]
# Transactions #779 created on account 2 -> the Tiller transaction already recording that row in the right account.
TWINS = {65236: 20972, 65237: 20699, 65238: 21075, 65239: 20805, 65240: 20698}
ROOT = Path(__file__).resolve().parents[2]


async def statuses(conn, account_ids: list[int], batch_ids: list[int]) -> dict[int, list[tuple]]:
    out: dict[int, list[tuple]] = {}
    for r in await q(
        conn,
        """--sql
        SELECT c.import_batch_id, c.account_ref, c.account_id, c.status, c.statement_total, c.ledger_total
        FROM statement_check c WHERE c.account_id = ANY(:a) OR c.import_batch_id = ANY(:b) ORDER BY 1, 2
        """,
        a=account_ids,
        b=batch_ids,
    ):
        out.setdefault(r.import_batch_id, []).append(tuple(r)[1:])
    return out


async def fold_absorb(conn, now: datetime, log: list[str]) -> None:
    pairs, leftover = await match_copy(conn, ABSORB, TARGET, False)
    await repoint_import_dups(conn, pairs)
    await drop_copies(conn, pairs, now, True)
    # Statement rows that created a dropped copy now link to the kept transaction.
    n_links = (
        await q(
            conn,
            f"UPDATE import_row r SET transaction_id = p.k FROM {PAIRS} WHERE r.transaction_id = p.d",
            d=[d for d, _ in pairs],
            k=[k for _, k in pairs],
        )
    ).rowcount
    await move_rows(conn, leftover, TARGET)
    log.append(f"fold {ABSORB} -> {TARGET}: {len(pairs)} soft-deleted ({n_links} row links moved), {len(leftover)} moved")

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

    refs = dict(
        (r.id, r.external_refs or {})
        for r in await q(conn, "SELECT id, external_refs FROM account WHERE id = ANY(:ids)", ids=[TARGET, ABSORB])
    )
    merged = {**refs[TARGET]}
    for k, v in refs[ABSORB].items():
        if k in merged:
            raise SystemExit(f"Account {TARGET} already has a {k} link; cannot absorb {ABSORB}")
        merged[k] = v
    await q(conn, "DELETE FROM account WHERE id = :s", s=ABSORB)
    await q(
        conn,
        "UPDATE account SET external_refs = CAST(:r AS jsonb), mask = '8083' WHERE id = :t",
        r=json.dumps(merged),
        t=TARGET,
    )
    log.append(f"removed account {ABSORB}; {TARGET} mask 8083, refs = {merged}")


async def fix_batch(conn, session: AsyncSession, now: datetime, log: list[str]) -> None:
    batch = await session.get(ImportBatch, BATCH)
    rows = {r.row_index: r for r in (await session.scalars(select(ImportRow).where(ImportRow.batch_id == BATCH))).all()}

    # Each section's running balance must chain row to row, and together they must give the statement's totals.
    accounts = []
    for ref, name, kind, account_id, idx in SECTIONS:
        mine = [rows[i] for i in idx]
        bal = [Decimal(r.raw["Balance"]) for r in mine]
        for prev, cur, r in zip(bal, bal[1:], mine[1:], strict=False):
            if prev + r.amount != cur:
                raise SystemExit(f"Row {r.row_index} breaks the {ref} balance chain")
        opening, closing = bal[0] - mine[0].amount, bal[-1]
        total = sum((r.amount for r in mine), Decimal(0))
        accounts.append(
            {
                "ref": ref,
                "last4": ref,
                "name": name,
                "account_type": kind,
                "opening_balance": float(opening),
                "closing_balance": float(closing),
                "rows": len(mine),
                "reconciliation": {
                    "sum_of_rows": f"{total:.2f}",
                    "balance_change": f"{closing - opening:.2f}",
                    "reconciles": True,
                },
            }
        )
        for r in mine:
            r.raw = {**r.raw, "Account": ref}
            r.account_id = account_id
        log.append(f"  section {ref} -> account {account_id}: {len(mine)} rows, {opening} -> {closing}")
    meta = batch.doc_meta
    if f"{sum(Decimal(str(a['opening_balance'])) for a in accounts):.2f}" != f"{Decimal(str(meta['opening_balance'])):.2f}":
        raise SystemExit("Section opening balances don't add up to the statement's")
    if len(rows) != sum(len(s[4]) for s in SECTIONS):
        raise SystemExit("Unexpected row count")
    batch.doc_meta = {**meta, "accounts": accounts, "unassigned_rows": 0, "split_by_script": now.isoformat()}
    batch.defaults = {**batch.defaults, "account_map": {ref: aid for ref, _, _, aid, _ in SECTIONS}}

    # The 5 inserted rows become matches of the Tiller transactions in the right accounts.
    section_of = {i: aid for _, _, _, aid, idx in SECTIONS for i in idx}
    check = await q(
        conn,
        f"""--sql
        SELECT d.id, d.import_batch_id, d.deleted_at, d.amount, d.txn_date, d.account_id, d.notes,
               d.transfer_match_id, k.id AS k_id, k.deleted_at AS k_del, k.amount AS k_amount, k.txn_date AS k_date,
               k.account_id AS k_acct, k.description AS k_desc
        FROM {PAIRS} JOIN "transaction" d ON d.id = p.d JOIN "transaction" k ON k.id = p.k
        """,
        d=list(TWINS),
        k=list(TWINS.values()),
    )
    by_txn = {r.transaction_id: r for r in rows.values() if r.transaction_id}
    for t in check:
        row = by_txn.get(t.id)
        ok = (
            row is not None
            and t.import_batch_id == BATCH
            and t.deleted_at is None
            and t.k_del is None
            and t.amount == t.k_amount
            and abs((t.txn_date - t.k_date).days) <= 1
            and t.k_acct == section_of[row.row_index]
        )
        if not ok:
            raise SystemExit(f"Transaction {t.id} / twin {t.k_id} no longer look like the reviewed pair")
        log.append(
            f"  row {row.row_index:>2} {t.txn_date} {t.amount:>9}: drop {t.id} (notes={t.notes!r}, "
            f"transfer={t.transfer_match_id}) -> {t.k_id} {t.k_date} acct {t.k_acct} {t.k_desc!r}"
        )
        row.decision, row.transaction_id = "skip_duplicate", None
    await session.flush()
    # Created txns only carry the batch's default note; matched rows never copy that onto the kept transaction.
    await q(
        conn,
        """UPDATE "transaction" SET notes = NULL WHERE id = ANY(CAST(:d AS bigint[])) AND notes = :n""",
        d=list(TWINS),
        n=batch.defaults.get("notes"),
    )
    await drop_copies(conn, list(TWINS.items()), now, True)
    counts = Counter(r.decision for r in rows.values())
    batch.stats = {
        **batch.stats,
        "inserted": 0,
        "kept_separate": counts.get("keep", 0),
        "skipped_duplicates": counts.get("skip_duplicate", 0),
        "linked_to_existing": counts.get("skip_duplicate", 0),
        "untangled": {"dropped": list(TWINS), "at": now.isoformat()},
    }
    await session.flush()


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        accts = {
            r.id: r for r in await q(conn, "SELECT id, name, mask FROM account WHERE id = ANY(:ids)", ids=[TARGET, ABSORB])
        }
        if (
            accts.get(TARGET) is None
            or accts[TARGET].name != "Nancy's Checking"
            or accts[TARGET].mask
            or accts.get(ABSORB) is None
            or accts[ABSORB].mask != "8083"
            or not accts[ABSORB].name.startswith("Ally Bank Interest Checking")
        ):
            raise SystemExit("Accounts changed since the review, aborting")
        status = (await q(conn, "SELECT status, doc_meta ? 'accounts' FROM import_batch WHERE id = :b", b=BATCH)).one()
        if status[0] != "committed" or status[1]:
            raise SystemExit(f"Import {BATCH} is {status[0]} / already split, aborting")
        before = await statuses(conn, [2, 3, 13, TARGET, ABSORB], [BATCH])

        if apply:
            ids = [2, 3, 13, TARGET, ABSORB]
            backup = {
                "account": [dict(r._mapping) for r in await q(conn, "SELECT * FROM account WHERE id = ANY(:a)", a=ids)],
                "transaction": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        "SELECT id, account_id, deleted_at, fingerprint, category_id, category_source, notes, "
                        'transfer_match_id FROM "transaction" WHERE account_id = ANY(:a) OR id = ANY(:t)',
                        a=[TARGET, ABSORB],
                        t=list(TWINS) + list(TWINS.values()),
                    )
                ],
                "transaction_source": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        'SELECT s.* FROM transaction_source s JOIN "transaction" t ON t.id = s.transaction_id '
                        "WHERE t.account_id = ANY(:a) OR t.id = ANY(:t)",
                        a=[ABSORB],
                        t=list(TWINS),
                    )
                ],
                "transaction_note": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        'SELECT n.* FROM transaction_note n JOIN "transaction" t ON t.id = n.transaction_id '
                        "WHERE t.account_id = ANY(:a) OR t.id = ANY(:t)",
                        a=[ABSORB],
                        t=list(TWINS),
                    )
                ],
                "duplicate_pair": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        'SELECT dp.* FROM duplicate_pair dp JOIN "transaction" t ON t.id IN (dp.txn_a_id, dp.txn_b_id) '
                        "WHERE t.account_id = ANY(:a)",
                        a=[ABSORB],
                    )
                ],
                "import_row": [
                    dict(r._mapping)
                    for r in await q(
                        conn, "SELECT * FROM import_row WHERE account_id = :a OR batch_id = :b", a=ABSORB, b=BATCH
                    )
                ],
                "import_batch": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        "SELECT id, defaults, doc_meta, stats FROM import_batch WHERE id = :b OR defaults::text ~ :pat",
                        b=BATCH,
                        pat=f"\\m{ABSORB}\\M",
                    )
                ],
            }
            out = ROOT / "logs" / f"fix_import_779_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            log.append(f"backup: {out}")

        await fold_absorb(conn, now, log)
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            log.append(f"import #{BATCH}:")
            await fix_batch(conn, session, now, log)
            log.append(f"transfer matching: {await match_transfers(session)}")
            batch_ids = (
                (
                    await q(
                        conn,
                        """--sql
                    SELECT DISTINCT c.import_batch_id FROM statement_check c
                    WHERE c.account_id = ANY(:a) OR c.import_batch_id = :b
                    """,
                        a=[TARGET, ABSORB],
                        b=BATCH,
                    )
                )
                .scalars()
                .all()
            )
            for bid in sorted(batch_ids):
                b = await session.get(ImportBatch, bid)
                if bid != BATCH:
                    await save_checks(session, b)
                    continue
                fixed = await auto_fix(session, b)
                log.append(f"#{BATCH} confident fixes applied: {fixed['applied']}")
                for c in fixed["checks"]:
                    for i in c["detail"]["issues"]:
                        side = i["txn"] or i["row"]
                        log.append(
                            f"  {c['account_ref']} {i['kind']} {side['date']} {side['amount']} {side['description'][:40]!r}"
                            f" fix={i['fix']} conf={i.get('confidence')} | {i['hint']}"
                        )
            # Releases the savepoint only; the outer transaction decides (dry run rolls back).
            await session.commit()
        after = await statuses(conn, [], list(before))

        log.append("statement checks for the 8083 account and #779 (before -> after):")
        same = 0
        for bid in sorted(before):
            b = {c[0]: c[1:] for c in before[bid] if c[0] == "8083" or bid == BATCH}
            a = {c[0]: c[1:] for c in after.get(bid, []) if c[0] == "8083" or bid == BATCH}
            for ref in sorted(b.keys() | a.keys()):
                (ba, bs, bst, bl), (aa, as_, ast, al) = b.get(ref, (None,) * 4), a.get(ref, (None,) * 4)
                if (bs, bst, bl) == (as_, ast, al) and bid != BATCH:
                    same += 1
                    continue
                log.append(f"  #{bid} {ref or '-'}: acct {ba}->{aa} {bs}->{as_} stmt {bst}->{ast} ledger {bl}->{al}")
        log.append(f"  ({same} other 8083 checks unchanged apart from the account)")

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
