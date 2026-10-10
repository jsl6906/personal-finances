"""Split four Census FCU statements into their share and auto-loan sections (approved 2026-10-10).

#81, #90, #110 and #145 were read as one account. Each lists two FED SALARY allotments in the MAIN SHARE section
(balance unchanged: the allotments go straight to the loan) and two loan payments in the NEW AUTO Loan 1 section.
The allotments were rightly matched to account 59 (Tiller), which is the auto loan; the loan payment lines were read
as -250 share withdrawals and created phantom transactions in account 84. They become the finance-charge rows of the
loan section, linked to 59's "Car Loan Interest" transactions, and the phantoms are soft-deleted.
Account 59 is renamed and typed as the loan it is.

Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/split_census_statements.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cleanup_accounts import q
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import save_checks
from ledger.models import ImportBatch, ImportRow

SHARE, LOAN = 84, 59
PAYMENT = Decimal("250.00")
# Batch -> (share balance, loan opening balance, finance charges) as printed on the PDFs.
STATEMENTS = {
    81: (Decimal("161.36"), Decimal("12293.32"), (Decimal("24.76"), Decimal("24.30"))),
    90: (Decimal("161.36"), Decimal("11842.38"), (Decimal("23.85"), Decimal("23.39"))),
    110: (Decimal("50.19"), Decimal("10823.53"), (Decimal("21.80"), Decimal("21.34"))),
    145: (Decimal("50.25"), Decimal("9216.42"), (Decimal("18.56"), Decimal("18.09"))),
}
LOAN_NAME, LOAN_NOTE = (
    "Auto Loan - Hyundai Tiburon (2008-10)",
    "Census FCU NEW AUTO Loan 1 (member 208153). Payroll allotments (FED SALARY) were paid straight to this loan.",
)
ROOT = Path(__file__).resolve().parents[2]


async def split(conn, session: AsyncSession, bid: int, now: datetime, log: list[str]) -> list[int]:
    share_bal, loan_open, charges = STATEMENTS[bid]
    batch = await session.get(ImportBatch, bid)
    rows = (await session.scalars(select(ImportRow).where(ImportRow.batch_id == bid).order_by(ImportRow.row_index))).all()
    if batch.status != "committed" or "accounts" in (batch.doc_meta or {}) or len(rows) != 4:
        raise SystemExit(f"#{bid} changed since the review, aborting")
    allotments, payments = rows[0::2], rows[1::2]

    # The printed loan balances must chain: opening - (payment - charge) = balance after each payment.
    bal = loan_open
    for r, charge in zip(payments, charges, strict=True):
        bal -= PAYMENT - charge
        if Decimal(r.raw.get("Balance") or "0") != bal:
            raise SystemExit(f"#{bid} row {r.row_index}: loan balance {r.raw.get('Balance')} != {bal}, aborting")

    phantoms = []
    for a, p, charge in zip(allotments, payments, charges, strict=True):
        linked = (
            await q(
                conn,
                'SELECT t.account_id, t.amount, t.txn_date FROM transaction_source s JOIN "transaction" t '
                "ON t.id = s.transaction_id WHERE s.import_row_id = :r AND s.role = 'matched' AND t.deleted_at IS NULL",
                r=a.id,
            )
        ).all()
        if [(x.account_id, x.amount, x.txn_date) for x in linked] != [(LOAN, PAYMENT, a.txn_date)]:
            raise SystemExit(f"#{bid} row {a.row_index}: allotment isn't matched to the loan's payment, aborting")
        phantom = (
            await q(
                conn,
                """--sql
                SELECT t.id, t.account_id, t.amount, t.transfer_match_id,
                       (SELECT count(*) FROM transaction_source s WHERE s.transaction_id = t.id) AS n_src,
                       (SELECT count(*) FROM transaction_note n WHERE n.transaction_id = t.id) AS n_notes
                FROM "transaction" t WHERE t.id = :t AND t.deleted_at IS NULL
                """,
                t=p.transaction_id,
            )
        ).one_or_none()
        if (
            p.decision != "insert"
            or p.amount != -PAYMENT
            or phantom is None
            or (phantom.account_id, phantom.amount, phantom.n_src, phantom.n_notes) != (SHARE, -PAYMENT, 1, 0)
        ):
            raise SystemExit(f"#{bid} row {p.row_index}: phantom withdrawal isn't as reviewed ({phantom}), aborting")
        interest = (
            await q(
                conn,
                """--sql
                SELECT id FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL AND txn_date = :d
                  AND amount = :amt AND description = 'Car Loan Interest'
                """,
                a=LOAN,
                d=p.txn_date,
                amt=-charge,
            )
        ).scalars().all()
        if len(interest) != 1:
            raise SystemExit(f"#{bid} row {p.row_index}: expected one Car Loan Interest {-charge}, found {interest}")

        if phantom.transfer_match_id:
            await q(
                conn,
                'UPDATE "transaction" SET transfer_match_id = NULL WHERE id = :t OR transfer_match_id = :t',
                t=phantom.id,
            )
        await q(conn, 'UPDATE "transaction" SET deleted_at = :now WHERE id = :t', now=now, t=phantom.id)
        await q(conn, "DELETE FROM transaction_source WHERE import_row_id = :r", r=p.id)
        await q(
            conn,
            """--sql
            INSERT INTO transaction_source (transaction_id, role, import_batch_id, import_row_id, attachment_id,
                                            txn_date, description, amount)
            VALUES (:t, 'matched', :b, :r, :att, :d, :desc, :amt)
            """,
            t=interest[0],
            b=bid,
            r=p.id,
            att=batch.attachment_id,
            d=p.txn_date,
            desc=f"Finance charge on {p.description} (250.00)",
            amt=-charge,
        )
        p.raw = {**p.raw, "Account": "Loan 1", "Amount": f"{-charge:.2f}", "Payment": "250.00"}
        p.amount, p.description = -charge, f"Finance charge on {p.description} (250.00)"
        p.decision, p.transaction_id, p.account_id = "skip_duplicate", None, LOAN
        a.raw, a.account_id = {**a.raw, "Account": "Loan 1"}, LOAN
        phantoms.append(phantom.id)
        log.append(
            f"  {p.txn_date}: allotment +250 -> {LOAN}; row {p.row_index} -250 -> finance charge -{charge} "
            f"(txn {interest[0]}); phantom {phantom.id} in {SHARE} soft-deleted"
        )

    total = 2 * PAYMENT - sum(charges)
    accounts = [
        {
            "ref": "0",
            "name": "Main Share",
            "account_type": "savings",
            "opening_balance": float(share_bal),
            "closing_balance": float(share_bal),
            "rows": 0,
            "reconciliation": {"sum_of_rows": "0.00", "balance_change": "0.00", "reconciles": True},
        },
        {
            "ref": "Loan 1",
            "name": "New Auto Loan",
            "account_type": "loan",
            "opening_balance": float(-loan_open),
            "closing_balance": float(-bal),
            "rows": 4,
            "reconciliation": {"sum_of_rows": f"{total:.2f}", "balance_change": f"{bal * -1 + loan_open:.2f}",
                               "reconciles": True},
        },
    ]
    batch.doc_meta = {**batch.doc_meta, "accounts": accounts, "unassigned_rows": 0, "split_by_script": now.isoformat()}
    batch.defaults = {**batch.defaults, "account_map": {"0": SHARE, "Loan 1": LOAN}}
    batch.stats = {
        **batch.stats,
        "inserted": 0,
        "skipped_duplicates": 4,
        "linked_to_existing": 4,
        "untangled": {"dropped": phantoms, "at": now.isoformat()},
    }
    await session.flush()
    return phantoms


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        accts = {r.id: r for r in await q(conn, "SELECT * FROM account WHERE id = ANY(:a)", a=[SHARE, LOAN])}
        if accts[LOAN].name != "Checking (2008-10)" or accts[SHARE].mask != "8153":
            raise SystemExit("Accounts changed since the review, aborting")
        before = {
            (r.import_batch_id, r.account_id): (r.status, r.difference)
            for r in await q(
                conn,
                "SELECT import_batch_id, account_id, status, difference FROM statement_check WHERE account_id = ANY(:a)",
                a=[SHARE, LOAN],
            )
        }

        if apply:
            bids = list(STATEMENTS)
            backup = {
                "account": [dict(r._mapping) for r in accts.values()],
                "import_batch": [
                    dict(r._mapping)
                    for r in await q(conn, "SELECT id, defaults, doc_meta, stats FROM import_batch WHERE id = ANY(:b)", b=bids)
                ],
                "import_row": [
                    dict(r._mapping) for r in await q(conn, "SELECT * FROM import_row WHERE batch_id = ANY(:b)", b=bids)
                ],
                "transaction_source": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        "SELECT s.* FROM transaction_source s JOIN import_row r ON r.id = s.import_row_id "
                        "WHERE r.batch_id = ANY(:b)",
                        b=bids,
                    )
                ],
                "transaction": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        'SELECT * FROM "transaction" WHERE id IN (SELECT transaction_id FROM import_row '
                        "WHERE batch_id = ANY(:b) AND transaction_id IS NOT NULL)",
                        b=bids,
                    )
                ],
            }
            out = ROOT / "logs" / f"split_census_statements_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            log.append(f"backup: {out}")

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            for bid in STATEMENTS:
                log.append(f"#{bid}:")
                await split(conn, session, bid, now, log)

            await q(
                conn,
                "UPDATE account SET name = :n, account_type = 'loan', notes = :note, updated_at = :now WHERE id = :a",
                n=LOAN_NAME,
                note=LOAN_NOTE,
                now=now,
                a=LOAN,
            )
            log.append(f"account {LOAN}: 'Checking (2008-10)' -> {LOAN_NAME!r}, type checking -> loan")

            batch_ids = sorted({b for b, _ in before} | set(STATEMENTS))
            for bid in batch_ids:
                await save_checks(session, await session.get(ImportBatch, bid))
            # Releases the savepoint only; the outer transaction decides (dry run rolls back).
            await session.commit()

        after = {
            (r.import_batch_id, r.account_id): (r.status, r.difference)
            for r in await q(
                conn,
                "SELECT import_batch_id, account_id, status, difference FROM statement_check "
                "WHERE import_batch_id = ANY(:b)",
                b=batch_ids,
            )
        }
        log.append("statement checks for the Census accounts (changed only):")
        for key in sorted(before.keys() | after.keys()):
            if before.get(key) != after.get(key):
                log.append(f"  #{key[0]} acct {key[1]}: {before.get(key)} -> {after.get(key)}")

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
