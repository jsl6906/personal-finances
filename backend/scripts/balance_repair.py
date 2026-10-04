"""Correct statement rows whose amount was misread, using the statement's own running balance column.

Finds committed document imports whose rows don't add up to an account's balance change, where the printed running balance
pins the misread rows (imports.service.balance_fixes). Fixing a row updates its amount and the batch's reconciliation,
re-points the row's ledger link (or corrects the transaction it created) and re-saves the statement checks.
Dry run unless --apply:
  $env:PYTHONIOENCODING="utf-8"; scripts/with_env.ps1 .env.azure python scripts/balance_repair.py [--batch N ...] [--apply]
"""

import argparse
import asyncio
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, select, update

from ledger.db.engine import dispose_engine, get_sessionmaker
from ledger.imports.coverage import MATCH_DAYS, _add_source, _note, save_checks
from ledger.imports.service import _dec, _reconcile, balance_fixes
from ledger.models import ImportBatch, ImportRow, Transaction, TransactionSource
from ledger.services.normalize import fingerprint


async def _repair_row(session, batch: ImportBatch, row: ImportRow, old: Decimal, new: Decimal, account_id: int | None):
    filename = batch.attachment.filename if batch.attachment else f"import #{batch.id}"
    row.amount = new
    row.raw = {**row.raw, "Amount": f"{new:.2f}"}
    if row.account_id and row.txn_date:
        row.fingerprint = fingerprint(row.account_id, row.txn_date, new, row.description)
    if row.transaction_id:
        txn = await session.get(Transaction, row.transaction_id)
        await session.execute(update(TransactionSource).where(TransactionSource.import_row_id == row.id).values(amount=new))
        if txn and txn.deleted_at is None and txn.amount == old:
            txn.amount = new
            txn.fingerprint = fingerprint(txn.account_id, txn.txn_date, txn.amount, txn.description)
            _note(session, batch, txn.id, f"Amount corrected from {old:.2f} to {new:.2f}: misread from {filename}")
            return f"created txn {txn.id} amount corrected"
        return f"created txn {row.transaction_id} left as is"
    stale = (
        await session.scalars(
            select(TransactionSource.transaction_id).where(
                TransactionSource.import_row_id == row.id, TransactionSource.role == "matched"
            )
        )
    ).all()
    await session.execute(
        delete(TransactionSource).where(TransactionSource.import_row_id == row.id, TransactionSource.role == "matched")
    )
    if account_id is None and stale:
        account_id = await session.scalar(select(Transaction.account_id).where(Transaction.id == stale[0]))
    taken = select(TransactionSource.transaction_id).where(
        TransactionSource.import_batch_id == batch.id, TransactionSource.import_row_id != row.id
    )
    if not row.txn_date:
        return f"unlinked from {stale}; row has no date"
    cands = (
        await session.scalars(
            select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.deleted_at.is_(None),
                Transaction.amount == new,
                Transaction.txn_date.between(
                    row.txn_date - timedelta(days=MATCH_DAYS), row.txn_date + timedelta(days=MATCH_DAYS)
                ),
                Transaction.id.not_in(taken),
            )
        )
    ).all()
    if not cands:
        return f"unlinked from {stale}; no ledger transaction of {new:.2f} nearby"
    best = min(cands, key=lambda t: (abs((t.txn_date - row.txn_date).days), t.id))
    await _add_source(session, batch, row, best.id, "matched")
    return f"re-linked {stale} -> txn {best.id} ({best.description})"


async def repair(session, batch: ImportBatch) -> list[str]:
    meta = dict(batch.doc_meta or {})
    accounts = [dict(a) for a in meta.get("accounts") or []]
    rows = (
        await session.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_index))
    ).all()
    groups: dict[str, list[ImportRow]] = defaultdict(list)
    for r in rows:
        if r.amount is not None:
            groups[(r.raw.get("Account") or "") if accounts else ""].append(r)
    targets = [(a, a["ref"], a.get("opening_balance"), a.get("closing_balance")) for a in accounts] or [
        (None, "", meta.get("opening_balance"), meta.get("closing_balance"))
    ]
    account_map = batch.defaults.get("account_map") or {}
    log: list[str] = []
    fixed_meta = list(meta.get("balance_fixed") or [])
    for a, ref, opening, closing in targets:
        recon = a.get("reconciliation") if a else meta.get("reconciliation")
        if not recon or recon.get("reconciles"):
            continue
        mine = groups.get(ref, [])
        fixes = balance_fixes(
            [r.amount for r in mine], [_dec(r.raw.get("Balance")) for r in mine], _dec(opening), _dec(closing)
        )
        account_id = account_map.get(ref) if a else batch.defaults.get("account_id")
        for pos, new in sorted(fixes.items()):
            row = mine[pos]
            old = row.amount
            outcome = await _repair_row(session, batch, row, old, new, account_id)
            fixed_meta.append({"row": row.row_index, "read": f"{old:.2f}", "amount": f"{new:.2f}"})
            log.append(
                f"  ···{ref} row {row.row_index + 1} {row.txn_date} {row.description}: {old:.2f} -> {new:.2f}; {outcome}"
            )
        if fixes:
            total = sum((r.amount for r in mine), Decimal(0))
            new_recon = _reconcile(total, opening, closing)
            if a:
                a["reconciliation"] = new_recon
            else:
                meta["reconciliation"] = new_recon
    if not log:
        return log
    checked = [a["reconciliation"] for a in accounts if a.get("reconciliation")]
    if checked:
        meta["accounts"] = accounts
        meta["reconciliation"] = {
            "sum_of_rows": f"{sum(Decimal(r['sum_of_rows']) for r in checked):.2f}",
            "balance_change": f"{sum(Decimal(r['balance_change']) for r in checked):.2f}",
            "reconciles": all(r["reconciles"] for r in checked),
        }
    meta["balance_fixed"] = fixed_meta
    batch.doc_meta = meta
    await session.flush()
    for c in await save_checks(session, batch):
        log.append(
            f"  check ···{c['account_ref']}: {c['status']} stmt {c['statement_total']} ledger {c['ledger_total']}"
            f" trusted={c['trusted']}"
        )
    return log


async def main(batch_ids: list[int], apply: bool) -> None:
    async with get_sessionmaker()() as session:
        q = select(ImportBatch.id).where(
            ImportBatch.source_type == "document", ImportBatch.status == "committed", ImportBatch.doc_meta.is_not(None)
        )
        if batch_ids:
            q = q.where(ImportBatch.id.in_(batch_ids))
        ids = (await session.scalars(q.order_by(ImportBatch.id))).all()
        repaired = 0
        for bid in ids:
            batch = await session.get(ImportBatch, bid)
            log = await repair(session, batch)
            if log:
                repaired += 1
                print(f"#{bid} {batch.attachment.filename if batch.attachment else ''} ({batch.status})")
                print("\n".join(log))
            if apply:
                await session.commit()
            else:
                await session.rollback()
            session.expunge_all()
        print(f"\n{repaired} of {len(ids)} statements {'repaired' if apply else 'would be repaired (dry run)'}")
    await dispose_engine()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, nargs="*", default=[])
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    asyncio.run(main(args.batch, args.apply))
