"""Commit flagged statements whose rows don't add up to their own balance change (approved 2026-10-10).

- Macy's #7, #13, #24: the extraction skipped the periodic finance charge on the closing date (the whole gap); the
  row is added (#24's links to the ledger's Tiller copy).
- Amex Costco #362: ENTERPRISE RAC was read as -215.38; the balances and the ledger say -225.38. Corrected, then the
  statement's links (all same amount, 1-4 days apart) are applied.
- NY 529 #472, #668: only internal exchanges (net 0); the balance change is market movement. #472's 'keep' rows go
  back to 'insert' and link to the ledger; #668 was read into an empty auto-created account (96), which is deleted.
- PayFlex HSA #607, #622, #640, #648, #654, #669: the statements leave out the market change that the ledger carries
  as Tiller's "Investment Gains/Losses", which stay. #607/#622 link the transfer from the investment account;
  #654 drops a second -5 maintenance fee on 04-01 (the statement lists it on 04-03).

Dry run by default; pass --apply to commit. Pass batch ids to limit the run.
Run: scripts/with_env.ps1 .env.azure python scripts/resolve_untrusted_backfill.py [--apply] [batch ids...]
"""

import asyncio
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

import ledger.ai.categorize  # noqa: F401  (registers job handler)
import ledger.analytics.jobs  # noqa: F401  (registers job handlers)
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import apply_fixes, check_batch
from ledger.imports.service import commit_batch
from ledger.jobs.worker import enqueue
from ledger.models import BackfillFile, ImportBatch, ImportRow
from ledger.services.normalize import fingerprint, normalize_merchant
from ledger.sources.backfill import mark_done

# batch -> amount of the finance charge missing on the closing date
FINANCE_CHARGES = {7: Decimal("-1.02"), 13: Decimal("-1.12"), 24: Decimal("-1.79")}
# batch -> (row index, amount read, amount per the balances)
MISREAD = {362: (35, Decimal("-215.38"), Decimal("-225.38"))}
# batch -> number of 'link' fixes expected before commit
LINKS = {24: 1, 362: 18, 472: 2, 607: 1, 622: 1}
REMAP = {668: (96, 40)}
# batch -> (date, amount) of a ledger duplicate the check marks as an edge item; removed after commit
REMOVE = {654: (date(2017, 4, 1), Decimal("-5.00"))}
MUST_BE_OK = {7, 13, 24, 362, 472, 668}
# HSA statements whose ledger must equal the printed balance change once committed
LEDGER_MATCHES_BALANCES = {622, 640, 648, 654, 669}
BATCHES = [7, 13, 24, 362, 472, 668, 607, 622, 640, 648, 654, 669]
CENT = Decimal("0.005")
ROOT = Path(__file__).resolve().parents[2]


def reconcile(batch: ImportBatch, rows: list[ImportRow]) -> None:
    """Recompute the statement's own reconciliation after its rows were corrected."""
    meta = dict(batch.doc_meta)
    rec = dict(meta["reconciliation"])
    total = sum((r.amount for r in rows if r.txn_date and r.amount is not None and r.decision != "invalid"), Decimal(0))
    change = Decimal(rec["balance_change"])
    rec.update(sum_of_rows=f"{total:.2f}", reconciles=abs(abs(change) - abs(total)) < Decimal("0.02"))
    meta["reconciliation"] = rec
    batch.doc_meta = meta


async def add_finance_charge(session: AsyncSession, batch: ImportBatch, rows: list[ImportRow], amount: Decimal) -> str:
    when = date.fromisoformat(batch.doc_meta["period_end"])
    acct = batch.defaults["account_id"]
    desc = "Periodic Finance Charge"
    row = ImportRow(
        batch_id=batch.id,
        row_index=max(r.row_index for r in rows) + 1,
        raw={"Date": when.isoformat(), "Posted": "", "Description": desc, "Details": "", "Amount": f"{amount:.2f}",
             "Account": "", "Balance": "", "Confidence": "1.00"},
        txn_date=when,
        description=desc,
        merchant=normalize_merchant(desc),
        amount=amount,
        account_id=acct,
        fingerprint=fingerprint(acct, when, amount, desc),
        confidence=Decimal(1),
        errors=[],
        decision="insert",
    )
    session.add(row)
    await session.flush()
    rows.append(row)
    batch.row_count = len(rows)
    batch.doc_meta = {
        **batch.doc_meta,
        "added_rows": [{"row": row.row_index, "description": desc, "amount": f"{amount:.2f}", "why": "implied by balances"}],
    }
    reconcile(batch, rows)
    return f"added {desc} {amount} on {when}"


async def fix_misread(batch: ImportBatch, rows: list[ImportRow], idx: int, read: Decimal, amount: Decimal) -> str:
    row = next(r for r in rows if r.row_index == idx)
    if row.amount != read:
        raise SystemExit(f"#{batch.id} row {idx} is {row.amount}, expected {read}; aborting")
    row.amount, row.raw = amount, {**row.raw, "Amount": f"{amount:.2f}"}
    row.fingerprint = fingerprint(row.account_id, row.txn_date, amount, row.description)
    fixed = list(batch.doc_meta.get("balance_fixed") or [])
    fixed.append({"row": idx, "read": f"{read:.2f}", "amount": f"{amount:.2f}"})
    batch.doc_meta = {**batch.doc_meta, "balance_fixed": fixed}
    reconcile(batch, rows)
    return f"row {idx} {read} -> {amount}"


async def remap(conn, batch: ImportBatch, rows: list[ImportRow], old: int, new: int) -> str:
    if batch.defaults.get("account_id") != old:
        raise SystemExit(f"#{batch.id} is mapped to {batch.defaults.get('account_id')}, expected {old}; aborting")
    batch.defaults = {**batch.defaults, "account_id": new}
    for r in rows:
        r.account_id = new
        if r.txn_date and r.amount is not None:
            r.fingerprint = fingerprint(new, r.txn_date, r.amount, r.description)
    return f"account {old} -> {new}"


async def drop_account(conn, acct: int, log: list[str]) -> None:
    n = (await conn.execute(text('SELECT count(*) FROM "transaction" WHERE account_id = :a'), {"a": acct})).scalar()
    users = (
        await conn.execute(
            text("""--sql
                SELECT id FROM import_batch
                WHERE defaults->>'account_id' = :s OR EXISTS (
                    SELECT 1 FROM jsonb_each_text(COALESCE(defaults->'account_map', '{}'::jsonb)) m WHERE m.value = :s)
            """),
            {"s": str(acct)},
        )
    ).scalars().all()
    if n or users:
        log.append(f"KEEP account {acct}: {n} transactions, batches {users}")
        return
    await conn.execute(text("DELETE FROM account WHERE id = :a"), {"a": acct})
    log.append(f"deleted empty account {acct}")


def fmt(checks: list[dict]) -> str:
    return "; ".join(
        f"{c['status']} trusted={c['trusted']} stmt {c['statement_total']} ledger {c['ledger_total']} diff {c['difference']}"
        for c in checks
    )


async def run(apply: bool, only: set[int]) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    targets = [b for b in BATCHES if not only or b in only]
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            if apply:
                q = lambda sql, **kw: conn.execute(text(sql), kw)  # noqa: E731
                backup = {
                    "batches": targets,
                    "import_batch": [
                        dict(r._mapping)
                        for r in await q(
                            "SELECT id, status, defaults, doc_meta, stats, row_count FROM import_batch WHERE id = ANY(:ids)",
                            ids=targets,
                        )
                    ],
                    "import_row": [dict(r._mapping) for r in await q("SELECT * FROM import_row WHERE batch_id = ANY(:ids)", ids=targets)],
                    "account": [dict(r._mapping) for r in await q("SELECT * FROM account WHERE id = 96")],
                    "transaction": [
                        dict(r._mapping)
                        for r in await q(
                            """--sql
                            SELECT t.* FROM "transaction" t
                            WHERE t.deleted_at IS NULL AND EXISTS (
                                SELECT 1 FROM statement_check c WHERE c.import_batch_id = ANY(:ids)
                                  AND c.account_id = t.account_id
                                  AND t.txn_date BETWEEN c.period_start - 7 AND c.period_end + 7)
                            """,
                            ids=targets,
                        )
                    ],
                }
                out = ROOT / "logs" / f"resolve_untrusted_backfill_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            new_ids: list[int] = []
            for bid in targets:
                batch = await session.get(ImportBatch, bid)
                bf = await session.scalar(select(BackfillFile).where(BackfillFile.import_batch_id == bid))
                head = f"#{bid} f{bf.id if bf else '-'} {bf.path if bf else ''}/{batch.attachment.filename}"
                if batch.status != "review":
                    log.append(f"SKIP {head}: {batch.status}")
                    continue
                rows = list(
                    (await session.scalars(select(ImportRow).where(ImportRow.batch_id == bid).order_by(ImportRow.row_index))).all()
                )
                steps = []
                if bid in FINANCE_CHARGES:
                    steps.append(await add_finance_charge(session, batch, rows, FINANCE_CHARGES[bid]))
                if bid in MISREAD:
                    steps.append(await fix_misread(batch, rows, *MISREAD[bid]))
                if bid in REMAP:
                    steps.append(await remap(conn, batch, rows, *REMAP[bid]))
                reset = (
                    await session.execute(
                        update(ImportRow).where(ImportRow.batch_id == bid, ImportRow.decision == "keep").values(decision="insert")
                    )
                ).rowcount
                if reset:
                    steps.append(f"keep->insert {reset}")
                await session.flush()
                before = await check_batch(session, batch)
                if bid in FINANCE_CHARGES or bid in MISREAD:
                    if not all(c["trusted"] is True for c in before):
                        raise SystemExit(f"{head}: still doesn't reconcile with its balances ({fmt(before)}); aborting")
                links = [
                    {"fix": "link", "row_id": i["row_id"], "transaction_id": i["transaction_id"]}
                    for c in before
                    for i in c["detail"]["issues"]
                    if i["fix"] == "link"
                ]
                if len(links) != LINKS.get(bid, 0):
                    raise SystemExit(f"{head}: {len(links)} links offered, expected {LINKS.get(bid, 0)} ({fmt(before)}); aborting")
                if links:
                    r = await apply_fixes(session, batch, links)
                    if r["skipped"]:
                        raise SystemExit(f"{head}: {r['skipped']} links went stale; aborting")
                    steps.append(f"linked {r['applied']}")
                result = await commit_batch(session, batch, "skip")
                if bf and bf.status == "review":
                    mark_done(bf, result)
                new_ids += result["uncategorized_ids"]
                after = await check_batch(session, batch)
                if bid in REMOVE:
                    when, amount = REMOVE[bid]
                    drops = [
                        {"fix": "remove", "row_id": None, "transaction_id": i["transaction_id"]}
                        for c in after
                        for i in c["detail"]["issues"]
                        if i["fix"] == "remove" and i["txn"]["date"] == when.isoformat() and Decimal(i["txn"]["amount"]) == amount
                    ]
                    if len(drops) != 1:
                        raise SystemExit(f"{head}: expected one {amount} on {when} to remove, found {len(drops)}; aborting")
                    after = (await apply_fixes(session, batch, drops))["checks"]
                    steps.append(f"removed txn {drops[0]['transaction_id']} ({amount} on {when})")
                if bid in MUST_BE_OK and not all(c["status"] == "ok" for c in after):
                    raise SystemExit(f"{head}: {fmt(after)} after commit; aborting")
                if bid in LEDGER_MATCHES_BALANCES:
                    change = Decimal(batch.doc_meta["reconciliation"]["balance_change"])
                    if any(abs(c["ledger_total"] - change) > CENT for c in after):
                        raise SystemExit(f"{head}: ledger {fmt(after)} != balance change {change}; aborting")
                if bid in REMAP:
                    await session.flush()
                    await drop_account(conn, REMAP[bid][0], steps)
                log.append(
                    f"OK   {head}: {', '.join(steps) or 'as is'}; inserted {result['inserted']}, "
                    f"auto-fixed {batch.stats.get('auto_fixed', 0)}\n      before: {fmt(before)}\n      after:  {fmt(after)}"
                )
            if new_ids:
                await enqueue(session, "categorize", {"ids": new_ids})
            await enqueue(session, "detect_anomalies", {})
            left = await session.scalar(
                select(func.count()).select_from(BackfillFile).where(BackfillFile.status.in_(["review", "failed"]))
            )
            log.append(f"flagged backfill files left: {left}")
            await session.commit()
        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(run("--apply" in args, {int(a) for a in args if a.isdigit()}))
