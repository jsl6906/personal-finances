"""Commit trusted backfill statements that don't reconcile exactly (approved 2026-10-10).

Per batch: 'keep' rows go back to 'insert' (see commit_reconciling_backfill.py); the statement must add up to its own
balance change; 'link' fixes of at least AUTO_CONFIDENCE are applied before commit; after commit the app's auto-fix
applies the other confident fixes, then ledger rows the check explains (a copy of a listed charge a few days apart, a
pending amount, or a charge and its reversal that no statement lists) are removed too. Unexplained leftovers stay on
the statement check for review in the app.

NY 529 quarterlies print balances that include market changes, so they never add up to their own balance change;
those that match the ledger exactly are committed as they are.

Dry run by default; pass --apply to commit. Pass batch ids to limit the run.
Run: scripts/with_env.ps1 .env.azure python scripts/commit_review_backfill.py [--apply] [batch ids...]
"""

import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

import ledger.ai.categorize  # noqa: F401  (registers job handler)
import ledger.analytics.jobs  # noqa: F401  (registers job handlers)
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import ACTIONABLE, AUTO_CONFIDENCE, apply_fixes, check_batch
from ledger.imports.service import commit_batch
from ledger.jobs.worker import enqueue
from ledger.models import BackfillFile, ImportBatch, ImportRow
from ledger.sources.backfill import mark_done

BATCHES = [
    492,  # Chase HSA 2014 summary
    # Costco Anywhere Visa (account 5)
    1152, 1148, 1144, 1132, 1123, 1119, 1115, 1110, 1105, 1100, 1087, 908, 903, 899, 894, 888, 884, 879, 874, 868, 862,
    # Amazon Visa 2009-18 (account 34)
    550, 555, 546, 542, 559, 623, 603, 599, 590, 595, 678, 685, 670, 674, 664, 660, 655, 645, 641, 714, 711, 707, 704,
    701, 695,
]
# Jared's NY 529 (account 40) quarterlies that match the ledger.
BALANCED_UNTRUSTED = [826, 784, 772, 683, 430, 437, 482, 111, 146, 217, 382, 249, 82, 180]
# The HSA summary omits the monthly "Investment Gains" entries, which are real.
NO_REMOVALS = {492}
EXPLAINED = ("Same amount as ", "Probably the pending amount of ", "Cancels out ")
REMOVE_CONFIDENCE = 85
ROOT = Path(__file__).resolve().parents[2]


def explained_removals(checks: list[dict]) -> list[dict]:
    return [
        {"fix": "remove", "row_id": i["row_id"], "transaction_id": i["transaction_id"]}
        for c in checks
        for i in c["detail"]["issues"]
        if i["kind"] == "extra"
        and i["fix"] == "remove"
        and i["suggested"]
        and i["confidence"] >= REMOVE_CONFIDENCE
        and (i["hint"] or "").startswith(EXPLAINED)
    ]


def summary(checks: list[dict]) -> str:
    out = []
    for c in checks:
        left = [i for i in c["detail"]["issues"] if i["kind"] in ACTIONABLE]
        kinds = Counter(f"{i['kind']}@{i['confidence']}" for i in left)
        out.append(f"{c['status']} diff {c['difference']} left {dict(kinds.most_common(4))}{'+' if len(kinds) > 4 else ''}")
    return "; ".join(out)


async def run(apply: bool, only: set[int]) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    targets = [b for b in BATCHES + BALANCED_UNTRUSTED if not only or b in only]
    tally = Counter()
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            if apply:
                rows = await conn.execute(
                    text("SELECT id, batch_id, decision FROM import_row WHERE batch_id = ANY(:ids)"), {"ids": targets}
                )
                txns = await conn.execute(
                    text("""--sql
                        SELECT t.* FROM "transaction" t
                        WHERE t.deleted_at IS NULL AND EXISTS (
                            SELECT 1 FROM statement_check c WHERE c.import_batch_id = ANY(:ids)
                              AND c.account_id = t.account_id
                              AND t.txn_date BETWEEN c.period_start - 7 AND c.period_end + 7)
                    """),
                    {"ids": targets},
                )
                backup = {
                    "batches": targets,
                    "import_row_decisions": [dict(r._mapping) for r in rows],
                    "transaction": [dict(r._mapping) for r in txns],
                }
                out = ROOT / "logs" / f"commit_review_backfill_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            new_ids: list[int] = []
            for bid in targets:
                batch = await session.get(ImportBatch, bid)
                bf = await session.scalar(select(BackfillFile).where(BackfillFile.import_batch_id == bid))
                head = f"#{bid} f{bf.id if bf else '-'} {batch.attachment.filename if batch.attachment else ''}"
                if batch.status != "review":
                    tally["skipped"] += 1
                    log.append(f"SKIP {head}: {batch.status}")
                    continue
                async with session.begin_nested() as sp:
                    reset = (
                        await session.execute(
                            update(ImportRow)
                            .where(ImportRow.batch_id == bid, ImportRow.decision == "keep")
                            .values(decision="insert")
                        )
                    ).rowcount
                    checks = await check_batch(session, batch)
                    untrusted = bid in BALANCED_UNTRUSTED
                    if (
                        not checks
                        or (untrusted and not all(c["status"] == "ok" for c in checks))
                        or (not untrusted and not all(c["trusted"] is True for c in checks))
                    ):
                        await sp.rollback()
                        tally["skipped"] += 1
                        log.append(f"SKIP {head}: {[(c['status'], c['trusted']) for c in checks]}")
                        continue
                    before = summary(checks)
                    links = [
                        {"fix": "link", "row_id": i["row_id"], "transaction_id": i["transaction_id"]}
                        for c in checks
                        for i in c["detail"]["issues"]
                        if i["fix"] == "link" and i["suggested"] and i["confidence"] >= AUTO_CONFIDENCE
                    ]
                    linked = (await apply_fixes(session, batch, links))["applied"] if links else 0
                    result = await commit_batch(session, batch, "skip")
                    if bf and bf.status == "review":
                        mark_done(bf, result)
                    new_ids += result["uncategorized_ids"]
                    after = await check_batch(session, batch)
                    removed = 0
                    for _ in range(3):
                        drops = [] if bid in NO_REMOVALS else explained_removals(after)
                        if not drops:
                            break
                        r = await apply_fixes(session, batch, drops)
                        removed, after = removed + r["applied"], r["checks"]
                    if untrusted and not all(c["status"] == "ok" for c in after):
                        raise SystemExit(f"{head}: {summary(after)} after commit; aborting")
                state = "ok" if all(c["status"] in ("ok", "explained") for c in after) else "left"
                tally[state] += 1
                tally["removed"] += removed
                left = sum(
                    (Decimal(i["effect"]) for c in after for i in c["detail"]["issues"] if i["fix"] and i["suggested"]),
                    Decimal(0),
                )
                log.append(
                    f"{state.upper():<4} {head}: keep->insert {reset}, linked {linked}, inserted {result['inserted']}, "
                    f"auto-fixed {batch.stats.get('auto_fixed', 0)}, explained removed {removed}\n      before: {before}\n      after:  "
                    f"{summary(after)}; remaining suggested fixes would move {left}"
                )
            if new_ids:
                await enqueue(session, "categorize", {"ids": new_ids})
            await enqueue(session, "detect_anomalies", {})
            log.append(f"{dict(tally)}")
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
