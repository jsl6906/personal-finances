"""Split the Census FCU statements (2008-10) into their Main Share and Auto Loan sections (approved 2026-10-10).

Each PDF lists two sections: MAIN SHARE (208153-0, ledger account 84) and NEW AUTO Loan 1 (208153-1, account 59).
Most were read as one account, so loan payments landed in 84 (often with the wrong sign) and 10 statements never
reconciled. Tiller's feed (59) recorded the membership as one net position: whole payroll allotments, loan interest,
share dividends and Ally transfers. Here:
  - the committed statements read as one account are rolled back and every statement is re-split: share rows -> 84,
    loan rows -> 59 as payments (+), plus the loan's finance charges implied by its printed balances (-);
  - Tiller's share-side transactions (initial deposit, dividends, Ally transfers) move to 84;
  - Tiller's payroll allotments keep only the part paid to the loan; the share part comes from the statement rows;
  - every statement is re-committed, its rows linked to the ledger where the ledger already has them.
#81, #90, #110 and #145 were split by split_census_statements.py and are only re-checked.

Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/split_census_sections.py [--apply]
"""

import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

import ledger.ai.categorize  # noqa: F401  (registers job handler)
import ledger.analytics.jobs  # noqa: F401  (registers job handlers)
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import apply_fixes, check_batch, save_checks
from ledger.imports.service import commit_batch, rollback_batch
from ledger.jobs.worker import enqueue
from ledger.models import (
    Account,
    BackfillFile,
    DuplicatePair,
    ImportBatch,
    ImportRow,
    Transaction,
    TransactionNote,
    TransactionSource,
)
from ledger.services.normalize import fingerprint, normalize_merchant
from ledger.sources.backfill import mark_done

SHARE, LOAN = 84, 59
RESPLIT = [32, 34, 37, 41, 43, 47, 49, 59, 67, 74, 97, 122, 133, 157, 169, 179, 192, 203, 215, 227, 247]
ALREADY_SPLIT = [81, 90, 110, 145]
# Allotment rows pass straight through the share section; the payment lines below them are the loan's.
PASS_THROUGH = {122}
SHARE_SIDE = ("Initial Deposit", "Interest earned", "Transfer from Ally Bank")
ALLOTMENT = "AGRI TREAS 301 (FED SALARY)"
SHARE_NAME, SHARE_TYPE = "Main Share (2008-10)", "savings"
LOAN_NOTE = (
    "Census FCU NEW AUTO Loan 1 (member 208153). Payroll allotments (FED SALARY) were split between this loan and "
    "the Main Share account; transfers from the share paid it off in July 2010."
)
CENT = Decimal("0.005")
ROOT = Path(__file__).resolve().parents[2]


def money(v: Decimal) -> str:
    return f"{v:.2f}"


def is_loan(r: ImportRow) -> bool:
    d = r.description or ""
    return "Payment" in d or d == "New loan"


def payroll(r: ImportRow) -> bool:
    d = r.description or ""
    return "FED SALARY" in d or "Payment-ACH" in d


def set_row(r: ImportRow, ref: str, acct: int) -> None:
    r.raw = {**r.raw, "Account": ref, "Amount": money(r.amount)}
    r.account_id = acct
    r.fingerprint = fingerprint(acct, r.txn_date, r.amount, r.description)
    r.decision, r.transaction_id = "insert", None


def finance_row(batch_id: int, index: int, when: date, amount: Decimal, balance: Decimal, payment: ImportRow) -> ImportRow:
    desc = f"Finance charge on {payment.description} ({money(abs(payment.amount))})"
    return ImportRow(
        batch_id=batch_id,
        row_index=index,
        raw={"Date": when.isoformat(), "Posted": "", "Description": desc, "Details": "implied by the loan balance",
             "Amount": money(amount), "Account": "Loan 1", "Balance": money(balance), "Confidence": "1.00"},
        txn_date=when,
        description=desc,
        merchant=normalize_merchant(desc),
        amount=amount,
        account_id=LOAN,
        fingerprint=fingerprint(LOAN, when, amount, desc),
        confidence=Decimal(1),
        errors=[],
        decision="insert",
    )


def section(ref: str, name: str, kind: str, opening: Decimal, closing: Decimal, rows: list[ImportRow]) -> dict:
    total = sum((r.amount for r in rows), Decimal(0))
    change = closing - opening
    if abs(total - change) > CENT:
        raise SystemExit(f"{name}: rows sum to {total} but the balance changed by {change}; aborting")
    return {
        "ref": ref, "name": name, "account_type": kind, "last4": None,
        "opening_balance": float(opening), "closing_balance": float(closing), "rows": len(rows),
        "reconciliation": {"sum_of_rows": money(total), "balance_change": money(change), "reconciles": True},
    }


async def resplit(session: AsyncSession, batch: ImportBatch, share_bal: Decimal, owed: Decimal, log: list[str]):
    """Re-split one statement's rows; returns the share balance and loan balance owed at its end."""
    rows = list(
        (await session.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_index))).all()
    )
    if any(r.txn_date is None or r.amount is None for r in rows):
        raise SystemExit(f"#{batch.id} has unreadable rows; aborting")
    order = lambda rs: sorted(rs, key=lambda r: (r.txn_date, r.row_index))  # noqa: E731
    share = [] if batch.id in PASS_THROUGH else order([r for r in rows if not is_loan(r)])
    loan = order([r for r in rows if r not in share])
    next_index = max(r.row_index for r in rows) + 1

    bal = share_bal
    for r in share:
        bal += r.amount
        if r.raw.get("Balance") and Decimal(r.raw["Balance"]) != bal:
            raise SystemExit(f"#{batch.id} share row {r.row_index}: balance {r.raw['Balance']} != {bal}; aborting")
        set_row(r, "0", SHARE)
    share_end = bal

    added: list[ImportRow] = []
    owed_end = owed
    for r in loan:
        printed = Decimal(r.raw["Balance"]) if r.raw.get("Balance") else None
        if r.description == "New loan":
            owed_end += -r.amount
            if printed != owed_end:
                raise SystemExit(f"#{batch.id} new loan row: balance {printed} != {owed_end}; aborting")
        elif batch.id in PASS_THROUGH and printed is None:
            if r.amount <= 0:
                raise SystemExit(f"#{batch.id} allotment row {r.row_index} is {r.amount}; aborting")
        elif batch.id in PASS_THROUGH:
            # The payment line repeats the allotment above it; it becomes the finance charge of that payment.
            interest = printed - (owed_end - abs(r.amount))
            r.raw = {**r.raw, "Payment": money(abs(r.amount))}
            r.description = f"Finance charge on {r.description} ({money(abs(r.amount))})"
            r.amount, owed_end = -interest, printed
        else:
            if printed is None:
                raise SystemExit(f"#{batch.id} loan row {r.row_index} has no balance; aborting")
            r.amount = abs(r.amount)
            interest = printed - (owed_end - r.amount)
            if not Decimal(0) <= interest < 60:
                raise SystemExit(f"#{batch.id} loan row {r.row_index}: implied interest {interest}; aborting")
            owed_end = printed
            if interest:
                f = finance_row(batch.id, next_index, r.txn_date, -interest, printed, r)
                next_index += 1
                session.add(f)
                added.append(f)
        set_row(r, "Loan 1", LOAN)
    await session.flush()
    loan_rows = loan + added

    accounts = [section("0", "Main Share", "savings", share_bal, share_end, share)]
    if loan_rows:
        accounts.append(section("Loan 1", "New Auto Loan", "loan", -owed, -owed_end, loan_rows))
    meta = dict(batch.doc_meta or {})
    meta.update(
        accounts=accounts,
        unassigned_rows=0,
        reconciliation={
            "sum_of_rows": money(sum((Decimal(a["reconciliation"]["sum_of_rows"]) for a in accounts), Decimal(0))),
            "balance_change": money(sum((Decimal(a["reconciliation"]["balance_change"]) for a in accounts), Decimal(0))),
            "reconciles": True,
        },
        split_by_script=datetime.now(UTC).isoformat(),
    )
    batch.doc_meta = meta
    batch.defaults = {**batch.defaults, "account_id": None if loan_rows else SHARE,
                      "account_map": {"0": SHARE, **({"Loan 1": LOAN} if loan_rows else {})}}
    batch.row_count = len(rows) + len(added)
    charges = ", ".join(f"{f.txn_date} {f.amount}" for f in added)
    log.append(
        f"#{batch.id} {meta.get('period_start')}: share {share_bal} -> {share_end} ({len(share)} rows); "
        f"loan {owed} -> {owed_end} ({len(loan)} rows{f', finance charges {charges}' if charges else ''})"
    )
    return share_end, owed_end, share, loan_rows


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            ids = RESPLIT + ALREADY_SPLIT
            if apply:
                q = lambda sql, **kw: conn.execute(text(sql), kw)  # noqa: E731
                rows_q = "SELECT id FROM import_row WHERE batch_id = ANY(:b)"
                backup = {
                    "account": [dict(r._mapping) for r in await q("SELECT * FROM account WHERE id = ANY(:a)", a=[SHARE, LOAN])],
                    "transaction": [dict(r._mapping) for r in await q('SELECT * FROM "transaction" WHERE account_id = ANY(:a)', a=[SHARE, LOAN])],
                    "import_batch": [dict(r._mapping) for r in await q("SELECT * FROM import_batch WHERE id = ANY(:b)", b=ids)],
                    "import_row": [dict(r._mapping) for r in await q("SELECT * FROM import_row WHERE batch_id = ANY(:b)", b=ids)],
                    "transaction_source": [dict(r._mapping) for r in await q("SELECT * FROM transaction_source WHERE import_batch_id = ANY(:b)", b=ids)],
                    "transaction_note": [dict(r._mapping) for r in await q("SELECT * FROM transaction_note WHERE import_batch_id = ANY(:b)", b=ids)],
                    "duplicate_pair": [dict(r._mapping) for r in await q(f"SELECT * FROM duplicate_pair WHERE import_row_id IN ({rows_q})", b=ids)],
                    "statement_check": [dict(r._mapping) for r in await q("SELECT * FROM statement_check WHERE import_batch_id = ANY(:b)", b=ids)],
                    "backfill_file": [dict(r._mapping) for r in await q("SELECT * FROM backfill_file WHERE import_batch_id = ANY(:b)", b=ids)],
                }
                out = ROOT / "logs" / f"split_census_sections_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            batches = {b: await session.get(ImportBatch, b) for b in ids}
            share_acct, loan_acct = await session.get(Account, SHARE), await session.get(Account, LOAN)
            if share_acct.mask != "8153" or loan_acct.account_type != "loan":
                raise SystemExit("Accounts changed since the review; aborting")

            # 1. Roll back statements committed as one account; every re-split statement starts from review.
            for bid in RESPLIT:
                b = batches[bid]
                if b.status == "committed":
                    # #122 was split by where its rows' transactions lived (untangle_legacy_statements.py), not by section.
                    if b.doc_meta.get("split_by_script") and bid not in PASS_THROUGH:
                        raise SystemExit(f"#{bid} was already split; aborting")
                    n = await rollback_batch(session, b)
                    b.status, b.committed_at = "review", None
                    log.append(f"#{bid}: rolled back ({n} transactions removed)")
                elif b.status != "review":
                    raise SystemExit(f"#{bid} is {b.status}; aborting")
                await session.execute(delete(TransactionSource).where(TransactionSource.import_batch_id == bid))
                await session.execute(
                    delete(DuplicatePair).where(
                        DuplicatePair.import_row_id.in_(select(ImportRow.id).where(ImportRow.batch_id == bid))
                    )
                )
            await session.flush()
            left = (await session.execute(text('SELECT count(*) FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL'), {"a": SHARE})).scalar()
            if left:
                raise SystemExit(f"{left} transactions left in {SHARE} after the rollbacks; aborting")

            # 2. Tiller's share-side transactions move to the share account.
            tiller = list(
                (await session.scalars(
                    select(Transaction).where(Transaction.account_id == LOAN, Transaction.deleted_at.is_(None)).order_by(Transaction.txn_date, Transaction.id)
                )).all()
            )
            moved = [t for t in tiller if t.description in SHARE_SIDE]
            for t in moved:
                t.account_id = SHARE
                t.fingerprint = fingerprint(SHARE, t.txn_date, t.amount, t.description)
            log.append(f"moved {len(moved)} Tiller transactions to {SHARE}: " + ", ".join(f"{t.txn_date} {t.amount} {t.description}" for t in moved))

            # 3. Re-split every statement, chaining both balances through all of them.
            share_bal, owed = Decimal(0), Decimal(0)
            share_rows: list[ImportRow] = []
            loan_rows: list[ImportRow] = []
            for b in sorted(batches.values(), key=lambda b: b.doc_meta["period_start"]):
                if b.id in ALREADY_SPLIT:
                    accts = {a["ref"]: a for a in b.doc_meta["accounts"]}
                    s, l = accts["0"], accts["Loan 1"]
                    if Decimal(str(s["opening_balance"])) != share_bal or -Decimal(str(l["opening_balance"])) != owed:
                        raise SystemExit(f"#{b.id} doesn't continue the balances ({share_bal}, {owed}); aborting")
                    share_bal, owed = Decimal(str(s["closing_balance"])), -Decimal(str(l["closing_balance"]))
                    loan_rows += (await session.scalars(select(ImportRow).where(ImportRow.batch_id == b.id))).all()
                    continue
                share_bal, owed, s_rows, l_rows = await resplit(session, b, share_bal, owed, log)
                share_rows += s_rows
                loan_rows += l_rows

            # 4. Each allotment keeps the part paid to the loan; the statement's share rows hold the rest.
            loan_part: dict[date, Decimal] = defaultdict(Decimal)
            share_part: dict[date, tuple[Decimal, ImportRow]] = {}
            for r in loan_rows:
                if r.amount > 0 and payroll(r):
                    loan_part[r.txn_date] += r.amount
            for r in share_rows:
                if "FED SALARY" in (r.description or ""):
                    share_part[r.txn_date] = (r.amount, r)
            for t in [t for t in tiller if t.description == ALLOTMENT]:
                lp, (sp, srow) = loan_part.get(t.txn_date, Decimal(0)), share_part.get(t.txn_date, (Decimal(0), None))
                if lp + sp != t.amount:
                    raise SystemExit(f"allotment {t.id} {t.txn_date} {t.amount} != loan {lp} + share {sp}; aborting")
                if not sp:
                    continue
                if not lp:
                    t.account_id = SHARE
                    t.fingerprint = fingerprint(SHARE, t.txn_date, t.amount, t.description)
                    log.append(f"allotment {t.txn_date} {t.amount}: all to the share, moved to {SHARE}")
                    continue
                session.add(TransactionNote(
                    transaction_id=t.id, source="import", import_batch_id=srow.batch_id,
                    attachment_id=batches[srow.batch_id].attachment_id,
                    body=f"Split per the Census FCU statement: {money(lp)} to the auto loan, {money(sp)} to the Main Share (was {money(t.amount)})",
                ))
                t.amount = lp
                t.fingerprint = fingerprint(LOAN, t.txn_date, t.amount, t.description)
            log.append(f"allotments split: {sum(1 for d in share_part if d in loan_part)}")

            # Tiller sometimes dated a loan's interest on another payment of the month; the statement's balances date it.
            interest = [t for t in tiller if t.description == "Car Loan Interest"]
            charges = [r for r in loan_rows if r.amount < 0 and (r.description or "").startswith("Finance charge")]
            close = lambda t, r, days: t.amount == r.amount and abs((t.txn_date - r.txn_date).days) <= days  # noqa: E731
            for r in charges:
                if any(close(t, r, 5) for t in interest):
                    continue
                far = [t for t in interest if close(t, r, 31) and not any(close(t, c, 5) for c in charges)]
                if len(far) != 1:
                    continue
                t = far[0]
                session.add(TransactionNote(
                    transaction_id=t.id, source="import", import_batch_id=r.batch_id,
                    attachment_id=batches[r.batch_id].attachment_id,
                    body=f"Date corrected from {t.txn_date.isoformat()} to {r.txn_date.isoformat()} per the Census FCU statement's loan balances",
                ))
                log.append(f"interest {t.id} {t.amount}: {t.txn_date} -> {r.txn_date}")
                t.txn_date = r.txn_date
                t.fingerprint = fingerprint(LOAN, t.txn_date, t.amount, t.description)
            await session.flush()

            # 5. Re-commit, linking statement rows to the ledger copies the check finds.
            new_ids: list[int] = []
            for b in sorted((batches[i] for i in RESPLIT), key=lambda b: b.doc_meta["period_start"]):
                before = await check_batch(session, b)
                links = [
                    {"fix": "link", "row_id": i["row_id"], "transaction_id": i["transaction_id"]}
                    for c in before for i in c["detail"]["issues"] if i["fix"] == "link"
                ]
                if links:
                    r = await apply_fixes(session, b, links)
                    if r["skipped"]:
                        raise SystemExit(f"#{b.id}: {r['skipped']} links went stale; aborting")
                result = await commit_batch(session, b, "skip")
                bf = await session.scalar(select(BackfillFile).where(BackfillFile.import_batch_id == b.id))
                if bf and bf.status in ("review", "failed"):
                    mark_done(bf, result)
                new_ids += result["uncategorized_ids"]
                after = await check_batch(session, b)
                state = "; ".join(f"{c['account_ref']}:{c['status']} diff {c['difference']}" for c in after)
                log.append(f"#{b.id}: linked {len(links)}, inserted {result['inserted']} -> {state}")
                if not all(c["status"] == "ok" for c in after):
                    for c in after:
                        for i in c["detail"]["issues"]:
                            side = i["row"] or i["txn"]
                            log.append(f"      {c['account_ref']} {i['kind']} {side['date']} {side['amount']} {side['description'][:50]} | {i['hint']}")
                    raise SystemExit("\n".join(log) + f"\n#{b.id} doesn't check out after commit; aborting")
            for bid in ALREADY_SPLIT:
                checks = await save_checks(session, batches[bid])
                if not all(c["status"] == "ok" for c in checks):
                    raise SystemExit(f"#{bid}: {[(c['account_ref'], c['status'], str(c['difference'])) for c in checks]}; aborting")
            log.append(f"#{', #'.join(map(str, ALREADY_SPLIT))}: still ok")

            # 6. Both accounts must run through every printed balance.
            await session.flush()
            for b in sorted(batches.values(), key=lambda b: b.doc_meta["period_start"]):
                pe = date.fromisoformat(b.doc_meta["period_end"])
                for a in b.doc_meta["accounts"]:
                    acct = SHARE if a["ref"] == "0" else LOAN
                    held = (await session.execute(
                        text('SELECT coalesce(sum(amount), 0) FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL AND txn_date <= :d'),
                        {"a": acct, "d": pe},
                    )).scalar()
                    if abs(held - Decimal(str(a["closing_balance"]))) > CENT:
                        raise SystemExit("\n".join(log) + f"\n#{b.id} {a['ref']}: ledger {held} != printed {a['closing_balance']} on {pe}; aborting")
            log.append("balances: both accounts match every statement's closing balance")

            share_acct.name, share_acct.account_type = SHARE_NAME, SHARE_TYPE
            loan_acct.notes = LOAN_NOTE
            if new_ids:
                await enqueue(session, "categorize", {"ids": new_ids})
            await enqueue(session, "detect_anomalies", {})
            flagged = (await session.execute(text("SELECT count(*) FROM backfill_file WHERE status IN ('review', 'failed')"))).scalar()
            log.append(f"flagged backfill files left: {flagged}")
            await session.commit()
        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv[1:]))
