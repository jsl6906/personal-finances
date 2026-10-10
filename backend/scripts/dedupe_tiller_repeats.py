"""Drop Tiller's repeated rows in Amazon Visa (2009-18) and Costco Anywhere Visa (approved 2026-10-10).

The Tiller sheet holds hand-imported rows without a Tiller Transaction ID, several times over; the feed gives each
repeat its own synthetic id, so accounts 34 and 5 carry groups of identical rows (same date, amount, description).
Each group keeps as many copies as the most rows any one statement matches to it (at least one); the rest are
soft-deleted (their Tiller ids stay known, so the next sync won't re-add them). Statement rows paired with a dropped
copy are re-pointed at the kept one.

Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/dedupe_tiller_repeats.py [--apply]
"""

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from cleanup_accounts import drop_copies, q
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import check_batch, save_checks
from ledger.models import ImportBatch

ACCOUNTS = {34: "Amazon Visa (2009-18)", 5: "Costco Anywhere Visa"}
ROOT = Path(__file__).resolve().parents[2]


async def plan(conn, account_id: int, log: list[str]) -> list[tuple[int, int]]:
    """(dropped, kept) pairs for the account's groups of identical Tiller rows."""
    rows = (
        await q(
            conn,
            """--sql
            SELECT t.id, t.txn_date, t.amount, t.description, t.category_id, t.category_source,
                   EXISTS (SELECT 1 FROM transaction_source s JOIN import_batch b ON b.id = s.import_batch_id
                           WHERE s.transaction_id = t.id AND b.source_type = 'document') AS on_statement
            FROM "transaction" t
            WHERE t.account_id = :a AND t.deleted_at IS NULL AND t.source_type = 'tiller'
              AND (t.txn_date, t.amount, t.description) IN (
                SELECT txn_date, amount, description FROM "transaction"
                WHERE account_id = :a AND deleted_at IS NULL AND source_type = 'tiller'
                GROUP BY 1, 2, 3 HAVING count(*) > 1)
            ORDER BY t.id
            """,
            a=account_id,
        )
    ).all()
    groups = defaultdict(list)
    for r in rows:
        groups[(r.txn_date, r.amount, r.description)].append(r)
    group_of = {r.id: key for key, g in groups.items() for r in g}

    # Statement rows linked or paired to a copy; each counts toward the closest-dated group it touches.
    links = (
        await q(
            conn,
            """--sql
            SELECT DISTINCT x.txn_id, ir.id AS row_id, ir.batch_id, ir.txn_date
            FROM (
                SELECT transaction_id AS txn_id, import_row_id AS row_id FROM transaction_source
                WHERE transaction_id = ANY(:ids) AND import_row_id IS NOT NULL
                UNION
                SELECT txn_a_id, import_row_id FROM duplicate_pair
                WHERE txn_a_id = ANY(:ids) AND import_row_id IS NOT NULL
                  AND status IN ('pending', 'confirmed_duplicate')
            ) x
            JOIN import_row ir ON ir.id = x.row_id
            JOIN import_batch b ON b.id = ir.batch_id
            WHERE b.source_type = 'document' AND b.status IN ('review', 'committed') AND ir.decision <> 'invalid'
            """,
            ids=list(group_of),
        )
    ).all()
    candidates = defaultdict(set)
    for link in links:
        candidates[(link.row_id, link.batch_id, link.txn_date)].add(group_of[link.txn_id])
    per_batch: dict = defaultdict(Counter)
    for (_, batch_id, row_date), keys in candidates.items():
        key = min(keys, key=lambda k: (abs((k[0] - row_date).days) if row_date else 0, k[0]))
        per_batch[key][batch_id] += 1

    pairs: list[tuple[int, int]] = []
    multi = 0
    for key, g in sorted(groups.items()):
        need = max(1, max(per_batch[key].values(), default=0))
        ordered = sorted(
            g, key=lambda t: (not t.on_statement, t.category_source != "user", t.category_id is None, t.id)
        )
        keep = ordered[: min(need, len(g))]
        pairs += [(t.id, keep[0].id) for t in ordered[len(keep) :]]
        if len(keep) > 1:
            multi += 1
            log.append(f"    keep {len(keep)} of {len(g)}: {key[0]} {key[1]:>9} {key[2][:40]} (statement lists {need})")
    log.insert(
        len(log) - multi,
        f"account {account_id}: {len(pairs)} copies dropped across {len(groups)} groups; "
        f"{multi} groups keep more than one copy:",
    )
    return pairs


async def repoint(conn, pairs: list[tuple[int, int]], log: list[str]) -> None:
    """Statement-row pairs and source links on a dropped copy move to the kept copy, without duplicates."""
    target = dict(pairs)
    kept = sorted(set(target.values()))
    existing = {
        (r.import_row_id, r.txn_a_id)
        for r in await q(
            conn,
            "SELECT import_row_id, txn_a_id FROM duplicate_pair WHERE import_row_id IS NOT NULL AND txn_a_id = ANY(:k)",
            k=kept,
        )
    }
    drop_ids, moves = [], []
    for p in await q(
        conn,
        """--sql
        SELECT id, txn_a_id, import_row_id FROM duplicate_pair
        WHERE import_row_id IS NOT NULL AND txn_a_id = ANY(:d) ORDER BY status <> 'confirmed_duplicate', score DESC, id
        """,
        d=list(target),
    ):
        key = (p.import_row_id, target[p.txn_a_id])
        if key in existing:
            drop_ids.append(p.id)
        else:
            existing.add(key)
            moves.append((p.id, target[p.txn_a_id]))
    await q(conn, "DELETE FROM duplicate_pair WHERE id = ANY(CAST(:ids AS bigint[]))", ids=drop_ids)
    await q(
        conn,
        """--sql
        UPDATE duplicate_pair dp SET txn_a_id = m.k
        FROM unnest(CAST(:ids AS bigint[]), CAST(:ks AS bigint[])) AS m(id, k) WHERE dp.id = m.id
        """,
        ids=[i for i, _ in moves],
        ks=[k for _, k in moves],
    )

    seen = {
        (r.transaction_id, r.import_row_id)
        for r in await q(
            conn,
            "SELECT transaction_id, import_row_id FROM transaction_source WHERE import_row_id IS NOT NULL "
            "AND transaction_id = ANY(:k)",
            k=kept,
        )
    }
    clash = []
    for s in await q(
        conn,
        "SELECT id, transaction_id, import_row_id FROM transaction_source WHERE import_row_id IS NOT NULL "
        "AND transaction_id = ANY(:d) ORDER BY id",
        d=list(target),
    ):
        key = (target[s.transaction_id], s.import_row_id)
        if key in seen:
            clash.append(s.id)
        else:
            seen.add(key)
    await q(conn, "DELETE FROM transaction_source WHERE id = ANY(CAST(:ids AS bigint[]))", ids=clash)
    log.append(f"  statement-row pairs: {len(moves)} re-pointed, {len(drop_ids)} merged; {len(clash)} repeat sources merged")


def summary(checks: list[dict]) -> tuple:
    issues = Counter(i["kind"] for c in checks for i in c["detail"]["issues"])
    return (
        tuple(c["status"] for c in checks),
        tuple(str(c["difference"]) for c in checks),
        issues.get("missing", 0),
        issues.get("extra", 0),
    )


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        names = dict((await q(conn, "SELECT id, name FROM account WHERE id = ANY(:a)", a=list(ACCOUNTS))).all())
        if names != ACCOUNTS:
            raise SystemExit(f"Accounts changed since the review, aborting: {names}")

        all_pairs: list[tuple[int, int]] = []
        for account_id in ACCOUNTS:
            all_pairs += await plan(conn, account_id, log)
        dropped = [d for d, _ in all_pairs]

        batch_ids = sorted(
            set(
                (
                    await q(
                        conn,
                        """--sql
                        SELECT c.import_batch_id FROM statement_check c WHERE c.account_id = ANY(:a)
                        UNION
                        SELECT ir.batch_id FROM duplicate_pair dp JOIN import_row ir ON ir.id = dp.import_row_id
                        WHERE dp.txn_a_id = ANY(:d)
                        UNION
                        SELECT s.import_batch_id FROM transaction_source s WHERE s.transaction_id = ANY(:d)
                        """,
                        a=list(ACCOUNTS),
                        d=dropped,
                    )
                ).scalars()
            )
            - {None}
        )

        if apply:
            backup = {
                "transaction": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        'SELECT id, account_id, deleted_at, category_id, category_source, notes FROM "transaction" '
                        "WHERE account_id = ANY(:a) AND source_type = 'tiller' AND deleted_at IS NULL",
                        a=list(ACCOUNTS),
                    )
                ],
                "transaction_source": [
                    dict(r._mapping)
                    for r in await q(conn, "SELECT * FROM transaction_source WHERE transaction_id = ANY(:d)", d=dropped)
                ],
                "transaction_note": [
                    dict(r._mapping)
                    for r in await q(conn, "SELECT * FROM transaction_note WHERE transaction_id = ANY(:d)", d=dropped)
                ],
                "transaction_tag": [
                    dict(r._mapping)
                    for r in await q(conn, "SELECT * FROM transaction_tag WHERE transaction_id = ANY(:d)", d=dropped)
                ],
                "duplicate_pair": [
                    dict(r._mapping)
                    for r in await q(
                        conn, "SELECT * FROM duplicate_pair WHERE txn_a_id = ANY(:d) OR txn_b_id = ANY(:d)", d=dropped
                    )
                ],
            }
            out = ROOT / "logs" / f"dedupe_tiller_repeats_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            log.append(f"backup: {out}")

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            batches = [b for b in [await session.get(ImportBatch, i) for i in batch_ids] if b.source_type == "document"]
            before = {b.id: summary(await check_batch(session, b)) for b in batches}

            await repoint(conn, all_pairs, log)
            await drop_copies(conn, all_pairs, now, True)

            changed = 0
            log.append(f"statement checks ({len(batches)} statements; changed only: status, difference, missing, extra):")
            for b in batches:
                after = summary(await save_checks(session, b))
                if after != before[b.id]:
                    changed += 1
                    log.append(f"  #{b.id} {b.status}: {before[b.id]} -> {after}")
            log.append(f"  {changed} changed")
            # Releases the savepoint only; the outer transaction decides (dry run rolls back).
            await session.commit()

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
