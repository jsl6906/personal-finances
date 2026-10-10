"""Fix import #596's Chili's row and drop Tiller's repeated rows in Costco TrueEarnings (approved 2026-10-10).

#596 (Chase x2180, 2016_05_21.pdf) row 31 "CHILI'S #503 ALEXANDRIA VA" -19.12 on 2016-05-04 was linked as a duplicate
of 39078, one of nine identical Tiller copies in account 46 (Costco TrueEarnings Amex). Account 8 has no such charge,
so the row becomes a new transaction there.
Account 46 holds ~212 groups of identical Tiller rows (same date, amount and description; 2014-12 to 2016-06), each
copy with its own Tiller id. Copies stay apart only when their Full Description carries different reference numbers.

Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/fix_import_596_amex_repeats.py [--apply]
"""

import asyncio
import json
import re
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from cleanup_accounts import drop_copies, q
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import _relink, save_checks
from ledger.imports.service import transaction_factory
from ledger.models import DuplicatePair, ImportBatch, ImportRow
from ledger.services.categorize import apply_rules

BATCH, ROW, WRONG, HOME = 596, 68561, 39078, 8
AMEX = 46
# Trailing Amex reference number in Tiller's Full Description, e.g. "... 703-922-5100 320161260402325768".
REF = re.compile(r"(\d{15,})\s*$")
ROOT = Path(__file__).resolve().parents[2]


def ref_of(full_desc: str | None) -> str | None:
    m = REF.search(full_desc or "")
    return m.group(1) if m else None


async def fix_row(conn, session: AsyncSession, now: datetime, log: list[str]) -> None:
    batch = await session.get(ImportBatch, BATCH)
    row = await session.get(ImportRow, ROW)
    if (
        batch.status != "committed"
        or row.batch_id != BATCH
        or row.txn_date != date(2016, 5, 4)
        or row.amount != Decimal("-19.12")
        or row.decision != "skip_duplicate"
        or row.transaction_id is not None
    ):
        raise SystemExit(f"Import row {ROW} changed since the review, aborting")
    sources = (await q(conn, "SELECT transaction_id FROM transaction_source WHERE import_row_id = :r", r=ROW)).scalars()
    if list(sources) != [WRONG]:
        raise SystemExit(f"Row {ROW} is no longer linked to {WRONG} only, aborting")
    clash = (
        await q(
            conn,
            """--sql
            SELECT id FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL AND amount = -19.12
              AND txn_date BETWEEN '2016-05-01' AND '2016-05-07'
            """,
            a=HOME,
        )
    ).scalars().all()
    if clash:
        raise SystemExit(f"Account {HOME} already has -19.12 around 2016-05-04 ({clash}), aborting")

    make = await transaction_factory(session, batch)
    t = make(row, HOME)
    session.add(t)
    await session.flush()
    await _relink(session, batch, row, t.id, "created")
    await session.execute(
        update(DuplicatePair)
        .where(DuplicatePair.import_row_id == row.id)
        .values(status="confirmed_separate", txn_b_id=t.id, decided_at=now)
    )
    row.transaction_id, row.decision = t.id, "keep"
    await session.flush()
    await apply_rules(session, [t.id])
    log.append(f"#{BATCH} row {row.row_index + 1}: new transaction {t.id} in account {HOME} (was matched to {WRONG})")


async def plan_repeats(conn) -> tuple[list[tuple[int, int]], list[str]]:
    """(dropped, kept) pairs for every group of identical Tiller rows in the Amex account."""
    rows = (
        await q(
            conn,
            """--sql
            SELECT t.id, t.txn_date, t.amount, t.description, t.category_id, t.category_source, r.full_desc,
                   EXISTS (SELECT 1 FROM transaction_source s
                           WHERE s.transaction_id = t.id AND s.role = 'matched') AS linked
            FROM "transaction" t
            LEFT JOIN LATERAL (
                SELECT raw->>'Full Description' AS full_desc FROM import_row
                WHERE transaction_id = t.id ORDER BY id LIMIT 1
            ) r ON true
            WHERE t.account_id = :a AND t.deleted_at IS NULL AND t.source_type = 'tiller'
            ORDER BY t.id
            """,
            a=AMEX,
        )
    ).all()
    groups = defaultdict(list)
    for r in rows:
        groups[(r.txn_date, r.amount, r.description)].append(r)

    pairs: list[tuple[int, int]] = []
    detail: list[str] = []
    for (d, amt, desc), g in sorted(groups.items()):
        if len(g) < 2:
            continue
        ordered = sorted(
            g, key=lambda t: (not t.linked, t.category_source != "user", t.category_id is None, not ref_of(t.full_desc), t.id)
        )
        by_ref: dict[str, int] = {}
        for t in ordered:
            if (r := ref_of(t.full_desc)) and r not in by_ref:
                by_ref[r] = t.id
        keepers = set(by_ref.values()) or {ordered[0].id}
        default = next(t.id for t in ordered if t.id in keepers)
        dropped = [(t.id, by_ref.get(ref_of(t.full_desc), default)) for t in g if t.id not in keepers]
        pairs += dropped
        if dropped:
            detail.append(
                f"  {d} {amt:>9} {desc[:28]:<28} keep {len(keepers)} of {len(g)}"
                + (f" ({len(by_ref)} distinct refs)" if len(by_ref) > 1 else "")
            )
    return pairs, detail


async def statuses(conn) -> dict[tuple[int, str], tuple]:
    return {
        (r.import_batch_id, r.account_ref): (r.status, r.statement_total, r.ledger_total)
        for r in await q(
            conn,
            """--sql
            SELECT import_batch_id, account_ref, status, statement_total, ledger_total FROM statement_check
            WHERE account_id = ANY(:a) OR import_batch_id = :b
            """,
            a=[HOME, AMEX],
            b=BATCH,
        )
    }


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        names = dict((await q(conn, "SELECT id, name FROM account WHERE id = ANY(:a)", a=[HOME, AMEX])).all())
        if names != {HOME: "Freedom Rewards Card", AMEX: "Costco TrueEarnings Card"}:
            raise SystemExit(f"Accounts changed since the review, aborting: {names}")
        before = await statuses(conn)

        if apply:
            backup = {
                "transaction": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        "SELECT id, account_id, deleted_at, category_id, category_source, notes FROM \"transaction\" "
                        "WHERE account_id = :a AND source_type = 'tiller' AND deleted_at IS NULL",
                        a=AMEX,
                    )
                ],
                "import_row": [dict(r._mapping) for r in await q(conn, "SELECT * FROM import_row WHERE id = :r", r=ROW)],
                "transaction_source": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        'SELECT s.* FROM transaction_source s JOIN "transaction" t ON t.id = s.transaction_id '
                        "WHERE t.account_id = :a OR s.import_row_id = :r",
                        a=AMEX,
                        r=ROW,
                    )
                ],
                "duplicate_pair": [
                    dict(r._mapping) for r in await q(conn, "SELECT * FROM duplicate_pair WHERE import_row_id = :r", r=ROW)
                ],
            }
            out = ROOT / "logs" / f"fix_import_596_amex_repeats_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            log.append(f"backup: {out}")

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            await fix_row(conn, session, now, log)

            pairs, detail = await plan_repeats(conn)
            await drop_copies(conn, pairs, now, True)
            log.append(f"account {AMEX}: {len(pairs)} repeated Tiller rows soft-deleted across {len(detail)} groups")
            log.extend(detail)

            batch_ids = sorted({b for b, _ in before} | {BATCH})
            for bid in batch_ids:
                await save_checks(session, await session.get(ImportBatch, bid))
            # Releases the savepoint only; the outer transaction decides (dry run rolls back).
            await session.commit()

        after = await statuses(conn)
        log.append(f"statement checks for accounts {HOME}/{AMEX} and #{BATCH} (changed only):")
        for key in sorted(before.keys() | after.keys()):
            if before.get(key) != after.get(key):
                log.append(f"  #{key[0]} {key[1] or '-'}: {before.get(key)} -> {after.get(key)}")

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
