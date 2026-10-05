"""Account 2 (Josh and Mona's Checking x1897) statement diffs, 2015-06..2016-04 and #719 / #734 (2026-10-04).

- #719 (2018_07.pdf) and #734 (2018_10.pdf) are combined Ally statements read before statements were split by account:
  rows of x1224 / x8083 / x5902 counted towards x1897, and the ones nobody matched were inserted into x1897. Split by
  the running balance, as #779 was; mis-inserted rows give way to the twin in the right account (or move there).
- 2015_06..2016_04: Tiller copies of the statement's SQC*MONA card purchases a day early (and one -60 "Transfer to
  Synoptic Freelancing" whose only statement counterpart is a SQC*MONA card credit on x1224) are not on the statements:
  remove the x1897 "extra" rows, as the check suggests.

Dry run by default; pass --apply to commit. Run: scripts/with_env.ps1 .env.azure python scripts/fix_account2_statements.py
"""

import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from cleanup_accounts import move_rows, q
from sqlalchemy.ext.asyncio import AsyncSession
from untangle_legacy_statements import batch_rows, chains, drop, pair_ids, statuses, write_split

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import apply_fixes, auto_fix, save_checks
from ledger.models import ImportBatch

ACCOUNT = 2
# Ally prints x1897, x1224, x8083, x5902 in that order; (first row, last row, ledger account) per balance section.
SECTIONS = {
    719: [(0, 25, 2), (26, 28, 13), (29, 30, 21), (31, 35, 3)],
    734: [(0, 17, 2), (18, 18, 13), (19, 19, 21), (20, 21, 3)],
}
SPLIT = list(SECTIONS)
EXTRAS = list(range(981, 992))
ROOT = Path(__file__).resolve().parents[2]


async def split(conn, bid: int, now: datetime, log: list[str]) -> None:
    rows = await batch_rows(conn, bid)
    assign = {i: acct for first, last, acct in SECTIONS[bid] for i in range(first, last + 1)}
    starts = {first for first, _, _ in SECTIONS[bid]}
    if sorted(assign) != [r.row_index for r in rows]:
        raise SystemExit(f"#{bid}: sections don't cover the rows")
    for prev, r in zip(rows, rows[1:], strict=False):
        if chains(prev, r) == (r.row_index in starts):
            raise SystemExit(f"#{bid}: row {r.row_index} doesn't fit the balance sections")
    linked = {r.link for r in rows if r.link}
    fixes, moves = [], defaultdict(list)
    for r in rows:
        want = assign[r.row_index]
        if r.created and r.acct != want:
            pairs, leftover = await pair_ids(conn, [r.link], want, taken=linked)
            fixes.extend(pairs)
            linked.update(k for _, k in pairs)
            moves[want].extend(leftover)
            what = f"-> twin {pairs[0][1]}" if pairs else "moved"
            desc = r.raw.get("Description", "")[:25]
            log.append(f"   #{bid} row {r.row_index} {r.amount:>9} {desc!r}: account {want} {what}")
    await drop(conn, fixes, now)
    for target, ids in moves.items():
        await move_rows(conn, ids, target)
    await write_split(conn, bid, assign, await batch_rows(conn, bid), log)


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    batches = SPLIT + EXTRAS
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        legacy = (
            await q(
                conn,
                "SELECT count(*) FROM import_batch WHERE id = ANY(:b) "
                "AND coalesce(jsonb_array_length(doc_meta->'accounts'), 0) = 0",
                b=SPLIT,
            )
        ).scalar()
        if legacy != len(SPLIT):
            raise SystemExit("Already split, aborting")
        before = await statuses(conn, batches)
        if apply:
            data = {
                "import_row": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        "SELECT id, batch_id, account_id, transaction_id, decision, raw FROM import_row "
                        "WHERE batch_id = ANY(:b)",
                        b=SPLIT,
                    )
                ],
                "import_batch": [
                    dict(r._mapping)
                    for r in await q(
                        conn, "SELECT id, defaults, doc_meta, stats FROM import_batch WHERE id = ANY(:b)", b=batches
                    )
                ],
            }
            out = ROOT / "logs" / f"fix_account2_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(data, default=str), encoding="utf-8")
            log.append(f"backup: {out}")

        for bid in SPLIT:
            await split(conn, bid, now, log)

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            for bid in batches:
                b = await session.get(ImportBatch, bid)
                if b.status != "committed":
                    log.append(f"#{bid}: {b.status}, left for the import wizard")
                    continue
                res = await auto_fix(session, b)
                extras = [
                    (i, {"fix": i["fix"], "row_id": i["row_id"], "transaction_id": i["transaction_id"]})
                    for c in res["checks"]
                    if c["account_id"] == ACCOUNT and bid in EXTRAS
                    for i in c["detail"]["issues"]
                    if i["kind"] == "extra" and i["fix"] == "remove"
                ]
                if res["applied"]:
                    log.append(f"#{bid}: {res['applied']} confident fixes")
                if extras:
                    applied = (await apply_fixes(session, b, [f for _, f in extras], auto=True))["applied"]
                    log.append(f"#{bid}: removed {applied} more:")
                    log.extend(
                        f"     {i['txn']['date']} {i['txn']['amount']:>9} {i['txn']['description'][:40]!r}"
                        f" c={i['confidence']}"
                        for i, _ in extras
                    )
            log.append(f"transfer matching: {await match_transfers(session)}")
            for bid in batches:
                b = await session.get(ImportBatch, bid)
                if b.status == "committed":
                    await save_checks(session, b)
            # Releases the savepoint only; the outer transaction decides (dry run rolls back).
            await session.commit()
        after = await statuses(conn, batches)
        log.append("checks (before -> after):")
        for bid in batches:
            b = [(c[0], c[2], str(c[3]), str(c[4])) for c in before.get(bid, []) if c[1] in (ACCOUNT, None) or bid in SPLIT]
            a = [(c[0], c[1], c[2], str(c[3]), str(c[4])) for c in after.get(bid, []) if c[1] == ACCOUNT or bid in SPLIT]
            log.append(f"  #{bid}: {b}\n      -> {a}")

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
