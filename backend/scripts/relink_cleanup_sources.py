"""Move import sources stranded on rows soft-deleted by cleanup_accounts.py / merge_accounts.py to the kept rows.

Migration 0014 backfilled transaction_source after those scripts ran, so "created" sources landed on the deleted
copies and /transactions?batch= showed nothing. The copy->kept pairing is rebuilt by replaying both scripts' matching
against their backups. Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/relink_cleanup_sources.py [--apply]
"""

import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from cleanup_accounts import COPIES, MATCH_DAYS, q
from merge_accounts import FOLDS

from ledger.db.engine import dispose_engine, get_engine

LOGS = Path(__file__).resolve().parents[2] / "logs"
CLEANUP = LOGS / "cleanup_accounts_backup_20261001011354.json"
MERGE = LOGS / "merge_accounts_backup_20261001011725.json"


def replay(backup: Path, plan: list[tuple[int, int, bool]], facts: dict) -> dict[int, int]:
    """Same pairing as cleanup_accounts.match_copy, applied in plan order to the backed-up account state."""
    rows = json.loads(backup.read_text(encoding="utf-8"))["transaction"]
    live: dict[int, int] = {r["id"]: r["account_id"] for r in rows if r["deleted_at"] is None}
    order = lambda i: (facts[i][0], i)  # noqa: E731
    pairs: dict[int, int] = {}
    for copy_id, target_id, use_abs in plan:
        key = (lambda a: abs(a)) if use_abs else (lambda a: a)
        by_amount: dict = defaultdict(list)
        for i in sorted((i for i, a in live.items() if a == target_id), key=order):
            by_amount[key(facts[i][1])].append(i)
        used: set[int] = set()
        for c in sorted((i for i, a in live.items() if a == copy_id), key=order):
            near = [t for t in by_amount.get(key(facts[c][1]), []) if abs((facts[t][0] - facts[c][0]).days) <= MATCH_DAYS]
            if near:
                best = min(near, key=lambda t: (t in used, abs((facts[t][0] - facts[c][0]).days), t))
                used.add(best)
                pairs[c] = best
                del live[c]
            else:
                live[c] = target_id
    return pairs


async def run(apply: bool) -> None:
    ids = {r["id"] for b in (CLEANUP, MERGE) for r in json.loads(b.read_text(encoding="utf-8"))["transaction"]}
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        rows = await q(
            conn,
            'SELECT id, txn_date, amount, deleted_at FROM "transaction" WHERE id = ANY(CAST(:ids AS bigint[]))',
            ids=sorted(ids),
        )
        facts = {r.id: (r.txn_date, r.amount, r.deleted_at) for r in rows}

        first = replay(CLEANUP, COPIES, facts)
        second = replay(MERGE, [(old, cur, False) for old, cur in FOLDS], facts)
        for label, pairs, stamp in (("cleanup", first, "2026-10-01 01:13:54"), ("merge", second, "2026-10-01 01:17:2")):
            actual = {i for i, f in facts.items() if f[2] is not None and str(f[2]).startswith(stamp)}
            off = len(actual ^ set(pairs))
            print(f"{label}: replayed {len(pairs)} pairs, {len(actual)} rows deleted then, mismatched {off}")
            if actual != set(pairs):
                raise SystemExit("Replay does not reproduce the deletions, aborting")

        mapping = {}
        for d in (*first, *second):
            k = first.get(d) or second[d]
            while k in second:
                k = second[k]
            mapping[d] = k
        dead = [k for k in set(mapping.values()) if facts[k][2] is not None]
        if dead:
            raise SystemExit(f"{len(dead)} kept rows are deleted, aborting: {dead[:10]}")

        params = {"d": list(mapping), "k": list(mapping.values())}
        pairs_sql = "unnest(CAST(:d AS bigint[]), CAST(:k AS bigint[])) AS p(d, k)"
        if apply:
            affected = await q(
                conn,
                "SELECT id, transaction_id, role FROM transaction_source WHERE transaction_id = ANY(CAST(:d AS bigint[]))",
                d=params["d"],
            )
            out = LOGS / f"relink_cleanup_sources_backup_{datetime.now(UTC):%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps([dict(r._mapping) for r in affected]), encoding="utf-8")
            print(f"backup: {out}")
        dupes = await q(
            conn,
            f"""--sql
            DELETE FROM transaction_source s USING {pairs_sql}
            WHERE s.transaction_id = p.d AND EXISTS (
                SELECT 1 FROM transaction_source x WHERE x.transaction_id = p.k AND x.import_row_id = s.import_row_id)
            """,
            **params,
        )
        moved = await q(
            conn,
            f"UPDATE transaction_source s SET transaction_id = p.k, role = 'matched' FROM {pairs_sql} "
            "WHERE s.transaction_id = p.d",
            **params,
        )
        notes = await q(
            conn,
            f"UPDATE transaction_note n SET transaction_id = p.k FROM {pairs_sql} WHERE n.transaction_id = p.d",
            **params,
        )
        left = (
            await q(
                conn,
                """--sql
                SELECT count(*) FROM transaction_source s JOIN "transaction" t ON t.id = s.transaction_id
                WHERE t.deleted_at IS NOT NULL
                """,
            )
        ).scalar_one()
        print(f"sources moved {moved.rowcount}, already on kept row {dupes.rowcount}, notes moved {notes.rowcount}")
        print(f"sources still on deleted rows: {left}")
        if apply:
            await txn.commit()
            print("applied")
        else:
            await txn.rollback()
            print("dry run (pass --apply to commit)")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
