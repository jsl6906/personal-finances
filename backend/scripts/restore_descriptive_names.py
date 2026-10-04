"""Give statement copies back the descriptive names of the feed duplicates that statement checks removed.

Before 2026-10-04 removing a feed's copy of a transaction the statement lists kept the statement's generic label
("ACH Withdrawal"); apply_fixes now carries the feed's name, merchant and category over (coverage.carry_over).
Dry run by default:
  $env:PYTHONIOENCODING="utf-8"; scripts/with_env.ps1 .env.azure python scripts/restore_descriptive_names.py [--apply]
"""

import argparse
import asyncio
from collections import Counter
from datetime import timedelta

from sqlalchemy import select

from ledger.db.engine import dispose_engine, get_sessionmaker
from ledger.imports.coverage import carry_over
from ledger.models import ImportBatch, Transaction, TransactionNote, TransactionSource
from ledger.services.dedupe import more_descriptive


async def main(apply: bool) -> None:
    async with get_sessionmaker()() as session:
        removed = (
            await session.execute(
                select(TransactionNote.transaction_id, TransactionNote.import_batch_id)
                .join(Transaction, Transaction.id == TransactionNote.transaction_id)
                .where(
                    TransactionNote.body.like("Removed: not listed on the statement %"),
                    Transaction.deleted_at.is_not(None),
                )
                .order_by(TransactionNote.transaction_id)
            )
        ).all()
        used: set[int] = set()
        counts: Counter = Counter()
        for drop_id, batch_id in removed:
            drop = await session.get(Transaction, drop_id)
            batch = await session.get(ImportBatch, batch_id) if batch_id else None
            if batch is None:
                counts["no batch"] += 1
                continue
            on_statement = select(TransactionSource.transaction_id).where(TransactionSource.import_batch_id == batch_id)
            cands = (
                await session.scalars(
                    select(Transaction).where(
                        Transaction.account_id == drop.account_id,
                        Transaction.amount == drop.amount,
                        Transaction.txn_date.between(drop.txn_date - timedelta(days=1), drop.txn_date + timedelta(days=1)),
                        Transaction.deleted_at.is_(None),
                        Transaction.id.in_(on_statement),
                    )
                )
            ).all()
            cands = sorted(
                (c for c in cands if c.id not in used),
                key=lambda c: (
                    c.description != drop.description,
                    not more_descriptive(drop.description, c.description),
                    abs((c.txn_date - drop.txn_date).days),
                    c.id,
                ),
            )
            if not cands:
                counts["no survivor"] += 1
                continue
            keep = cands[0]
            used.add(keep.id)
            old = keep.description
            if await carry_over(session, batch, keep.id, drop):
                counts["renamed"] += 1
                print(f"  {drop.txn_date} {drop.amount:>10} #{keep.id} {old!r} -> {drop.description!r} (from #{drop.id})")
            else:
                counts["kept"] += 1
        print(f"\nRemoved duplicates: {len(removed)}; {dict(counts)}")
        if apply:
            await session.commit()
            print("Applied.")
        else:
            await session.rollback()
            print("Dry run; pass --apply to save.")
    await dispose_engine()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    asyncio.run(main(p.parse_args().apply))
