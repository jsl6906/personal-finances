"""Merge the accounts behind the statement checks' "In another account" rows (reviewed 2026-10-05).

  1. Chase Amazon card renumbered 6044 -> 1010 in April 2021; Tiller's "Amazon Card" feed (18) kept following it while
     4 got its own feed from 2021-07. 18's rows from CUTOVER on move to 4 (copies 4 already has are dropped);
     18 becomes "Amazon Visa (2018-21)" x6044 and the 1010 statements read into 18 now name 4.
  2. PayFlex HSA x8960: 95 (made by the statement backfill) is absorbed into 38 and removed; 42 (a second Tiller feed,
     rows repeated up to 20x) folds into 38 and stays as a hidden Tiller shell; exact Tiller repeats then collapse.
  3. Amex x2009 ("account ending 32009"): every row of 82 is a copy of a 26 EveryDay Card row; 82 is removed.
  4. Macy's x6300: 47 Macy's Store Card (Tiller) folds into 36 and stays as a hidden Tiller shell. The statements
     itemize each purchase line, so Tiller rows equal to a day's statement lines are dropped into them.
  5. Census FCU 84 holds the share savings / loan sections of the combined statements: renamed only.
Afterwards every affected statement gets its confident check fixes, and statements whose check changed also get
their "not on the statement" removals of 85%+ (a listed twin of the same amount and payee nearby).

Dry run by default; pass --apply to commit. Run: scripts/with_env.ps1 .env.azure python scripts/merge_elsewhere_accounts.py
"""

import asyncio
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

import untangle_legacy_statements as ul
from cleanup_accounts import PAIRS, move_rows, q
from sqlalchemy.ext.asyncio import AsyncSession
from untangle_legacy_statements import backup, dedupe_tiller, merge_refs, pair_ids, repoint_account, statuses

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import apply_fixes, auto_fix, save_checks
from ledger.models import ImportBatch

CUTOVER = date(2021, 4, 9)
EXPECTED = {
    4: "Amazon Prime Visa",
    18: "Amazon Visa (2018-23)",
    26: "EveryDay Card",
    36: "Macy's Card (2007-18)",
    38: "HSA (2015-17)",
    42: "Health Savings Account (2016)",
    47: "Macy's Store Card",
    82: "Unknown Card (2009-18)",
    84: "Statement Account (2008-10)",
    95: "PayFlex Systems USA, Inc.",
}
RENAMES = {18: "Amazon Visa (2018-21)", 84: "Savings & Loan (2008-10)"}
MASKS = {18: "6044", 38: "8960", 26: "2009"}
# 2017_05_31.pdf was split between 95 and 38 by untangle_legacy_statements.py; both halves are 38 now.
UNSPLIT = 659
EXTRA_CONFIDENCE = 85
DROPPED: dict[int, int] = {}
_drop = ul.drop


async def drop(conn, pairs: list[tuple[int, int]], now: datetime) -> None:
    DROPPED.update(pairs)
    await _drop(conn, pairs, now)


# dedupe_tiller looks drop up in its own module.
ul.drop = drop


async def repoint_confirmed_dups(conn, log: list[str]) -> None:
    """Import rows confirmed as duplicates of a dropped copy now point at its survivor (statement checks use them)."""

    def final(i: int) -> int:
        while i in DROPPED:
            i = DROPPED[i]
        return i

    d = list(DROPPED)
    n = (
        await q(
            conn,
            f"""--sql
            UPDATE duplicate_pair dp SET txn_a_id = p.k FROM {PAIRS}
            WHERE dp.txn_a_id = p.d AND dp.import_row_id IS NOT NULL AND dp.status = 'confirmed_duplicate'
            """,
            d=d,
            k=[final(i) for i in d],
        )
    ).rowcount
    log.append(f"confirmed import duplicates repointed to survivors: {n}")


async def ids_of(conn, account: int, since: date | None = None) -> list[int]:
    return [
        i
        for (i,) in await q(
            conn,
            'SELECT id FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL '
            "AND txn_date >= coalesce(CAST(:s AS date), '-infinity'::date)",
            a=account,
            s=since,
        )
    ]


async def fold(conn, src: int, target: int, now: datetime, log: list[str], since: date | None = None) -> None:
    """Rows of src with a same-amount target row within a few days are dropped into it; the rest move to target."""
    ids = await ids_of(conn, src, since)
    pairs, leftover = await pair_ids(conn, ids, target)
    await drop(conn, pairs, now)
    await move_rows(conn, leftover, target)
    log.append(f"{src} -> {target}: {len(ids)} rows; {len(pairs)} dropped as copies, {len(leftover)} moved")


async def remove_account(conn, src: int, target: int, log: list[str]) -> None:
    await q(conn, 'UPDATE "transaction" SET account_id = :t WHERE account_id = :s', t=target, s=src)
    moved = await repoint_account(conn, src, target)
    refs = await merge_refs(conn, src, target)
    for table, key in (("account_balance", "source"), ("holding", "external_id")):
        await q(
            conn,
            f"""--sql
            UPDATE {table} x SET account_id = :t WHERE x.account_id = :s AND NOT EXISTS (
                SELECT 1 FROM {table} o WHERE o.account_id = :t AND o.as_of = x.as_of AND o.{key} = x.{key})
            """,
            t=target,
            s=src,
        )
        await q(conn, f"DELETE FROM {table} WHERE account_id = :s", s=src)
    await q(conn, "UPDATE category_rule SET account_id = :t WHERE account_id = :s", t=target, s=src)
    await q(conn, "DELETE FROM account WHERE id = :s", s=src)
    log.append(f"   {src} removed; {moved}; {target} refs {refs}")


async def make_shell(conn, src: int, target: int, log: list[str]) -> None:
    """Keep a re-linked Tiller feed as a hidden, closed shell so a full Tiller resync maps to it."""
    moved = await repoint_account(conn, src, target)
    base = (await q(conn, "SELECT name FROM account WHERE id = :t", t=target)).scalar_one()
    name, n = f"{base} - Tiller copy", 1
    while (await q(conn, "SELECT 1 FROM account WHERE name = :n AND id <> :s", n=name, s=src)).first():
        n += 1
        name = f"{base} - Tiller copy {n}"
    await q(conn, "UPDATE account SET name = :n, is_hidden = true, is_closed = true WHERE id = :s", n=name, s=src)
    log.append(f"   {src} -> shell {name!r}; {moved}")


async def drop_itemized_totals(conn, account: int, now: datetime, log: list[str]) -> None:
    """Tiller rows (one per purchase) whose amount equals the statement lines of one day within a few days."""
    rows = (
        await q(
            conn,
            """--sql
            SELECT t.id, t.txn_date, t.amount, t.source_type, EXISTS (
                SELECT 1 FROM transaction_source s JOIN import_batch b ON b.id = s.import_batch_id
                WHERE s.transaction_id = t.id AND b.source_type = 'document') AS backed
            FROM "transaction" t WHERE t.account_id = :a AND t.deleted_at IS NULL ORDER BY t.txn_date, t.id
            """,
            a=account,
        )
    ).all()
    tiller = [r for r in rows if r.source_type == "tiller" and not r.backed]
    lines: dict[date, list] = defaultdict(list)
    for r in rows:
        if r.source_type != "tiller":
            lines[r.txn_date].append(r)
    used: set[date] = set()
    pairs = []
    by_day: dict[date, list] = defaultdict(list)
    for t in tiller:
        by_day[t.txn_date].append(t)
    for d, ts in by_day.items():
        if lines.get(d) and sum(t.amount for t in ts) == sum(r.amount for r in lines[d]):
            used.add(d)
            pairs += [(t.id, lines[d][0].id) for t in ts]
    done = {p[0] for p in pairs}
    for t in tiller:
        if t.id in done:
            continue
        for off in sorted(range(-3, 4), key=abs):
            d = t.txn_date + timedelta(days=off)
            if d not in used and lines.get(d) and sum(r.amount for r in lines[d]) == t.amount:
                used.add(d)
                pairs.append((t.id, lines[d][0].id))
                break
    await drop(conn, pairs, now)
    log.append(f"   {account}: {len(pairs)} Tiller purchase totals dropped into the statement's itemized lines")


async def unsplit(conn, bid: int, log: list[str]) -> None:
    await q(
        conn,
        "UPDATE import_batch SET doc_meta = doc_meta - 'accounts' - 'unassigned_rows' - 'split_by_script', "
        "defaults = defaults - 'account_map' WHERE id = :b",
        b=bid,
    )
    n = (await q(conn, "UPDATE import_row SET raw = raw - 'Account' WHERE batch_id = :b", b=bid)).rowcount
    log.append(f"   #{bid}: per-account split removed ({n} rows)")


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        names = dict((await q(conn, "SELECT id, name FROM account WHERE id = ANY(:i)", i=list(EXPECTED))).all())
        wrong = [i for i, n in EXPECTED.items() if not (names.get(i) or "").startswith(n)]
        if wrong:
            raise SystemExit(f"Accounts changed since the review, aborting: {wrong}")
        touched = sorted(EXPECTED)
        batches = sorted(
            {
                b
                for (b,) in await q(
                    conn,
                    """--sql
                    SELECT import_batch_id FROM statement_check WHERE account_id = ANY(:a)
                    UNION SELECT import_batch_id FROM "transaction"
                    WHERE account_id = ANY(:a) AND import_batch_id IS NOT NULL
                    UNION SELECT batch_id FROM import_row WHERE account_id = ANY(:a)
                    """,
                    a=touched,
                )
            }
        )
        elsewhere_sql = (
            "SELECT count(*) FROM statement_check c, jsonb_array_elements(c.detail->'issues') i "
            "WHERE i->>'kind' = 'elsewhere'"
        )
        elsewhere_before = (await q(conn, elsewhere_sql)).scalar()
        before = await statuses(conn, batches)
        if apply:
            log.append(f"backup: {await backup(conn, now, touched, batches, 'merge_elsewhere')}")

        # 1. Amazon x1010
        log.append("1. Amazon")
        await fold(conn, 18, 4, now, log, since=CUTOVER)
        b1010 = [
            b
            for (b,) in await q(
                conn,
                "SELECT b.id FROM import_batch b JOIN attachment a ON a.id = b.attachment_id "
                "WHERE a.filename LIKE '%statements-1010%'",
            )
        ]
        log.append(f"   1010 statements: {await repoint_account(conn, 18, 4, b1010)}")

        # 2. PayFlex HSA x8960
        log.append("2. HSA")
        await fold(conn, 95, 38, now, log)
        await remove_account(conn, 95, 38, log)
        await fold(conn, 42, 38, now, log)
        await make_shell(conn, 42, 38, log)
        await dedupe_tiller(conn, 38, now, log)
        await unsplit(conn, UNSPLIT, log)

        # 3. Amex x2009
        log.append("3. Amex")
        await fold(conn, 82, 26, now, log)
        await remove_account(conn, 82, 26, log)

        # 4. Macy's x6300
        log.append("4. Macy's")
        await fold(conn, 47, 36, now, log)
        await make_shell(conn, 47, 36, log)
        await drop_itemized_totals(conn, 36, now, log)

        for i, n in RENAMES.items():
            await q(conn, "UPDATE account SET name = :n WHERE id = :i", n=n, i=i)
        for i, m in MASKS.items():
            await q(conn, "UPDATE account SET mask = :m WHERE id = :i", m=m, i=i)
        log.append(f"renamed {RENAMES}; masks {MASKS}")
        await repoint_confirmed_dups(conn, log)

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            log.append(f"transfer matching: {await match_transfers(session)}")
            applied: Counter = Counter()
            for bid in batches:
                b = await session.get(ImportBatch, bid)
                if not (b and b.source_type == "document" and b.status == "committed"):
                    continue
                res = await auto_fix(session, b)
                applied["confident"] += res["applied"]
                was = [(c[0], c[1], c[2]) for c in before.get(bid, [])]
                if was == [(c["account_ref"], c["account_id"], c["status"]) for c in res["checks"]]:
                    continue
                extras = [
                    {"fix": i["fix"], "row_id": i["row_id"], "transaction_id": i["transaction_id"]}
                    for c in res["checks"]
                    for i in c["detail"]["issues"]
                    if i["kind"] == "extra" and i["suggested"] and i["confidence"] >= EXTRA_CONFIDENCE
                ]
                if extras:
                    applied["extras 85%+"] += (await apply_fixes(session, b, extras, auto=True))["applied"]
            log.append(f"check fixes applied: {dict(applied)}")
            log.append(f"transfer matching: {await match_transfers(session)}")
            for bid in batches:
                b = await session.get(ImportBatch, bid)
                if b and b.source_type == "document" and b.status == "committed":
                    await save_checks(session, b)
            # Releases the savepoint only; the outer transaction decides (dry run rolls back).
            await session.commit()
        after = await statuses(conn, batches)

        def tally(st: dict) -> Counter:
            return Counter(c[2] for v in st.values() for c in v)

        log.append(f"\nchecks over {len(batches)} affected statements: {dict(tally(before))} -> {dict(tally(after))}")
        log.append(
            f"'In another account' rows (all statements): {elsewhere_before} -> {(await q(conn, elsewhere_sql)).scalar()}"
        )
        for bid in batches:
            was = [(c[1], c[2], str(c[3] - c[4]) if c[3] is not None and c[4] is not None else None) for c in before[bid]]
            now_ = [(c[1], c[2], str(c[3] - c[4]) if c[3] is not None and c[4] is not None else None) for c in after[bid]]
            if was != now_:
                log.append(f"  #{bid}: {was} -> {now_}")
                if "--detail" in sys.argv and any(c[1] != "ok" for c in now_):
                    for r in await q(
                        conn,
                        "SELECT c.trusted, c.period_start, c.period_end, i FROM statement_check c, "
                        "jsonb_array_elements(c.detail->'issues') i WHERE c.import_batch_id = :b",
                        b=bid,
                    ):
                        i, side = r.i, r.i["txn"] or r.i["row"]
                        log.append(
                            f"      {i['kind']:8} {side['date']} {side['amount']:>9} {side['description'][:40]!r} "
                            f"c={i['confidence']} trusted={r.trusted} {r.period_start}..{r.period_end} | {i['hint']}"
                        )
        for r in await q(
            conn,
            """--sql
            SELECT a.id, a.name, a.mask, a.is_hidden, a.is_closed, count(t.id) n, min(t.txn_date) lo, max(t.txn_date) hi
            FROM account a LEFT JOIN "transaction" t ON t.account_id = a.id AND t.deleted_at IS NULL
            WHERE a.id = ANY(:a) GROUP BY a.id ORDER BY a.id
            """,
            a=touched,
        ):
            flags = ("H" if r.is_hidden else "-") + ("C" if r.is_closed else "-")
            log.append(f"  {r.id:>3} {flags} {r.n:>5} {r.lo}..{r.hi}  {r.name} x{r.mask}")
        for acct in (4, 18, 26, 36, 38):
            st = Counter(s for (s,) in await q(conn, "SELECT status FROM statement_check WHERE account_id = :a", a=acct))
            log.append(f"  checks for {acct}: {dict(st)}")
        for r in await q(
            conn,
            """--sql
            SELECT c.account_id, i->>'kind' k, i->'row'->>'date' d, i->'row'->>'amount' amt, i->>'hint' h
            FROM statement_check c, jsonb_array_elements(c.detail->'issues') i
            WHERE i->>'kind' = 'elsewhere' ORDER BY 1, 3
            """,
        ):
            log.append(f"  still elsewhere: acct {r.account_id} {r.d} {r.amt} | {r.h}")

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
