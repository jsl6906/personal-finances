"""Read-only report: run the statement coverage check over every committed statement document and summarize.

Nothing is written (works before migration 0018 exists). Run against Azure:
  $env:PYTHONIOENCODING="utf-8"; scripts/with_env.ps1 .env.azure python scripts/statement_check_report.py [--top 25]
"""

import argparse
import asyncio
from collections import Counter
from decimal import Decimal

from sqlalchemy import select

from ledger.db.engine import dispose_engine, get_sessionmaker
from ledger.imports.coverage import ACTIONABLE, account_names, check_batch
from ledger.models import ImportBatch


async def main(top: int) -> None:
    async with get_sessionmaker()() as session:
        ids = (
            await session.scalars(
                select(ImportBatch.id)
                .where(ImportBatch.source_type == "document", ImportBatch.status == "committed")
                .order_by(ImportBatch.id)
            )
        ).all()
        statuses: Counter = Counter()
        kinds: Counter = Counter()
        untrusted = 0
        bad: list[tuple] = []
        for n, bid in enumerate(ids, 1):
            batch = await session.get(ImportBatch, bid)
            for c in await check_batch(session, batch):
                statuses[c["status"]] += 1
                untrusted += c["trusted"] is False
                for i in c["detail"]["issues"]:
                    kinds[i["kind"]] += 1
                if c["status"] == "mismatch":
                    fixes = Counter(i["kind"] for i in c["detail"]["issues"] if i["kind"] in ACTIONABLE)
                    bad.append((abs(c["difference"] or Decimal(0)), bid, batch.attachment.filename, c, fixes))
            session.expunge_all()
            if n % 50 == 0:
                print(f"... {n}/{len(ids)}", flush=True)
        await session.rollback()
        names = await account_names(session, list({b[3]["account_id"] for b in bad if b[3]["account_id"]}))

    print(f"\nStatements checked: {len(ids)}; account checks by status: {dict(statuses)}")
    print(f"Checks whose statement doesn't reconcile with its own balances: {untrusted}")
    print(f"Issues by kind: {dict(kinds)}")
    by_account: Counter = Counter()
    for _, _, _, c, _ in bad:
        by_account[names.get(c["account_id"], c["account_ref"])] += 1
    print(f"Mismatches by account: {dict(by_account.most_common())}")
    print(f"\nLargest {top} mismatches:")
    for _diff, bid, filename, c, fixes in sorted(bad, key=lambda b: b[0], reverse=True)[:top]:
        acct = names.get(c["account_id"], c["account_ref"])
        print(
            f"  #{bid} {filename} | {acct} | {c['period_start']}..{c['period_end']} | stmt {c['statement_total']} "
            f"ledger {c['ledger_total']} diff {c['difference']} | {dict(fixes)} | trusted={c['trusted']}"
        )
    await dispose_engine()


async def detail(batch_ids: list[int]) -> None:
    async with get_sessionmaker()() as session:
        for bid in batch_ids:
            batch = await session.get(ImportBatch, bid)
            print(f"\n=== #{bid} {batch.attachment.filename} ({batch.origin}) accounts={batch.defaults.get('account_map')}")
            for c in await check_batch(session, batch):
                print(
                    f"  ref={c['account_ref']!r} account={c['account_id']} {c['status']}"
                    f" {c['period_start']}..{c['period_end']}"
                    f" stmt {c['statement_total']} ({c['statement_rows']}) ledger {c['ledger_total']} ({c['ledger_rows']})"
                    f" {c['detail'].get('message') or ''}"
                )
                kinds = Counter(i["kind"] for i in c["detail"]["issues"])
                print(f"  issues {dict(kinds)}; shifted {len(c['detail']['shifted'])}")
                for i in c["detail"]["issues"][:15]:
                    side = i["row"] or i["txn"]
                    src = f" via {i['txn']['source_type']}" if i["txn"] else ""
                    what = f"{side['date']} {side['amount']:>10} {side['description'][:50]}{src}"
                    print(f"    {i['kind']:9} {what} | {i['hint']}")
        await session.rollback()
    await dispose_engine()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--batch", type=int, nargs="*", help="print the issues for these import batches instead")
    args = p.parse_args()
    asyncio.run(detail(args.batch) if args.batch else main(args.top))
