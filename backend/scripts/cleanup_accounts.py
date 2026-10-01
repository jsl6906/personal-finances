"""One-off cleanup of accounts and institutions (approved 2026-09-30).

Dry run by default; pass --apply to commit. Writes a per-transaction log to logs/cleanup_accounts.log.
Run against Azure: scripts/with_env.ps1 .env.azure python scripts/cleanup_accounts.py [--apply]
"""

import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine
from ledger.services.normalize import fingerprint

KEEP = object()
CLEAR = ""

ALLY, AMEX, BOA, CENSUS = "Ally Bank", "American Express", "Bank of America", "Census Federal Credit Union"
CHASE, CITI, CITIZENS, MACYS = "Chase", "Citibank", "Citizens Bank", "Macy's"
TSP, VA529, NY529 = "Thrift Savings Plan", "Virginia529", "New York's 529 College Savings Program"
FSA, ECSI, AIDV = "Federal Student Aid", "Heartland ECSI", "Aidvantage"

INSTITUTION_RENAMES = {
    9: ("PennyMac Loan", "PennyMac"),
    15: ("VA 529", VA529),
    83: ("Heartland ECSI / Rochester Institute of Technology", ECSI),
}

# id: (current name, new name, institution (None = no institution), account_type, mask)
ACCOUNTS = {
    2: ("Josh and Mona's Checking", KEEP, ALLY, KEEP, KEEP),
    3: ("Josh and Mona's Savings", KEEP, ALLY, KEEP, KEEP),
    74: ("Mona's IRA", KEEP, ALLY, KEEP, KEEP),
    54: ("Checking x0982", "Online Savings (2009-12)", ALLY, "savings", KEEP),
    45: ("Amy's CD  (x8345)", "Amy's CD", ALLY, "savings", KEEP),
    51: ("Moms CD (x8352)", "Mom's CD", ALLY, "savings", KEEP),
    52: ("Moms CD 2 (x6938)", "Mom's CD 2", ALLY, "savings", KEEP),
    13: ("Synoptic Freelancing (x1224)", "Synoptic Freelancing Checking", ALLY, KEEP, KEEP),
    44: ("Mona's Private Practice (x5897)", "Mona's Private Practice Checking", ALLY, KEEP, KEEP),
    21: ("Nancys Checking", "Nancy's Checking", ALLY, KEEP, KEEP),
    43: ("Nancys Savings", "Nancy's Savings", ALLY, KEEP, KEEP),
    26: ("Amex EveryDay Card", "EveryDay Card", AMEX, KEEP, KEEP),
    46: ("Costco TrueEarnings Card", KEEP, AMEX, KEEP, KEEP),
    53: ("Rewards Plus Gold Card", KEEP, AMEX, KEEP, KEEP),
    55: ("Amex Savings x7497", "Personal Savings", AMEX, "savings", "7497"),
    82: ("American Express ···2009", "Unknown Card (2007-11)", AMEX, KEEP, KEEP),
    56: ("BoA x3657", "Salary Checking", BOA, KEEP, KEEP),
    57: ("BoA x1594", "Spending Checking", BOA, KEEP, KEEP),
    62: ("BoA x1604", "Checking (2009)", BOA, KEEP, KEEP),
    58: ("Upromise Credit Card (OLD)", "Upromise Mastercard", BOA, KEEP, "8993"),
    59: ("Census FCU", "Checking (2008-10)", CENSUS, KEEP, KEEP),
    84: ("Census Federal Credit Union ···8153", "Statement Account (2008-10)", CENSUS, "checking", KEEP),
    4: ("Amazon Prime", "Amazon Prime Visa", CHASE, "credit_card", KEEP),
    18: ("Amazon Card", "Amazon Visa (2021-23)", CHASE, KEEP, KEEP),
    22: ("old Amazon Card 2", "Amazon Visa (2018-21)", CHASE, KEEP, KEEP),
    34: ("Old Amazon.com Credit Card", "Amazon Visa (2009-18)", CHASE, KEEP, "2797"),
    19: ("Chase Sapphire Preferred Card", "Sapphire Preferred", CHASE, KEEP, KEEP),
    8: ("Freedom Rewards Card", KEEP, CHASE, KEEP, KEEP),
    49: ("Chase HSA", "HSA (2009-15)", CHASE, "savings", "5572"),
    39: ("Chase Hide 2", "Unknown Card (2017)", CHASE, "credit_card", KEEP),
    87: ("JPMorgan Chase ···0516", "Unknown Card (2009)", CHASE, KEEP, KEEP),
    31: ("Hidden Sapphire Preferred", "Sapphire Preferred - Tiller copy", CHASE, "credit_card", KEEP),
    33: ("Chase Hide 1", "Amazon Visa (2009-18) - Tiller copy", CHASE, "credit_card", KEEP),
    30: ("Old Amazon Credit Card 2", "Amazon Visa (2018-21) - Tiller copy", CHASE, KEEP, KEEP),
    48: ("JOSHUAH  LATIMORE", "HSA (2009-15) - Tiller copy", CHASE, "savings", KEEP),
    50: ("joshuah latimore", "HSA (2009-15) - Tiller copy 2", CHASE, "savings", KEEP),
    5: ("Costco Card", "Costco Anywhere Visa", CITI, KEEP, KEEP),
    11: ("Costco Anywhere Visa®\u00a0Card by Citi", "Costco Anywhere Visa (2016-24)", CITI, KEEP, KEEP),
    6: ("Home Depot Card", KEEP, CITI, KEEP, KEEP),
    12: ("The Home Depot Consumer Credit Card", "Home Depot Card (2020-24)", CITI, KEEP, KEEP),
    35: ("Prestige", "Prestige Card", CITI, "credit_card", KEEP),
    41: ("J. LATIMORE", "Unknown Card - Annual Fee (2017)", None, "credit_card", KEEP),
    61: ("Citizens Amy's CD (x6775)", "Amy's CD (2009-10)", CITIZENS, "savings", KEEP),
    60: ("Citizens Mom's CD (x6821)", "Mom's CD (2009-10)", CITIZENS, "savings", KEEP),
    23: ("Citizen's Checking", "Checking (2009-20)", CITIZENS, KEEP, "1338"),
    36: ("Macy's Card -6300", "Macy's Card (2007-18)", MACYS, KEEP, KEEP),
    47: ("Macy's Store Card", KEEP, MACYS, KEEP, KEEP),
    29: ("XXXX-XXXX-XXXX-9574", "Macy's Card (2018-19)", MACYS, "credit_card", KEEP),
    32: ("XXXX-XXXX-XXXX-4607", "Macy's Card (2018-19) - card 4607", MACYS, "credit_card", KEEP),
    28: ("XXXX-XXXX-XXXX-3353", "Macy's Card (2018-19) - card 3353", MACYS, "credit_card", KEEP),
    38: ("Payflex HSA", "HSA (2015-17)", "PayFlex", "savings", KEEP),
    42: ("Health Savings Account", "Health Savings Account (2016)", "PayFlex", KEEP, KEEP),
    37: ("PayPal Account", "PayPal Balance", "PayPal", "other", KEEP),
    9: ("Mortgage: 8904 Longmead Ct", "Mortgage - 8904 Longmead Ct", "PennyMac", "mortgage", KEEP),
    20: ("Josh's TSP", "Josh's TSP (2010-22)", TSP, KEEP, KEEP),
    10: ("Thrift Savings Plan - Civilian", "Josh's TSP (2022-24)", TSP, "retirement", KEEP),
    7: ("Lifecycle Fund", "L Fund (Lifecycle)", TSP, "retirement", KEEP),
    78: ("C Fund", "C Fund (Common Stock)", TSP, KEEP, KEEP),
    77: ("F Fund", "F Fund (Fixed Income)", TSP, KEEP, KEEP),
    76: ("G Fund", "G Fund (Government Securities)", TSP, KEEP, KEEP),
    75: ("I Fund", "I Fund (International)", TSP, KEEP, KEEP),
    79: ("S Fund", "S Fund (Small Cap)", TSP, KEEP, KEEP),
    14: ("Emmett's 529 Account", "Emmett's 529", VA529, "investment", KEEP),
    16: ("Tuition Track Portfolio Emmett", "Emmett's Tuition Track Portfolio", VA529, "investment", KEEP),
    17: ("Tuition Track Portfolio Leyla", "Leyla's Tuition Track Portfolio", VA529, "investment", KEEP),
    15: ("VA 529 2039 Portfolio Leyla", "Leyla's 2039 Portfolio", VA529, "investment", CLEAR),
    40: ("Jared's 529 Account", "Jared's 529", NY529, "investment", "4701"),
    72: ("Josh's Federal Perkins Loan", "Josh's Perkins Loan", ECSI, KEEP, KEEP),
    73: ("Josh's Federal Pell Grants", "Josh's Pell Grants", FSA, "other", KEEP),
    63: ("Josh's Subsidized Consolidation Loan", KEEP, FSA, KEEP, KEEP),
    71: ("Josh's Unsubsidized Consolidation Loan", KEEP, FSA, KEEP, KEEP),
    70: ("Josh's Subsidized Loan 1", KEEP, FSA, KEEP, KEEP),
    69: ("Josh's Subsidized Loan 2", KEEP, FSA, KEEP, KEEP),
    68: ("Josh's Subsidized Loan 3", KEEP, FSA, KEEP, KEEP),
    65: ("Josh's Subsidized Loan 4", KEEP, FSA, KEEP, KEEP),
    67: ("Josh's Subsidized Loan 5", KEEP, FSA, KEEP, KEEP),
    66: ("Josh's Subsidized Loan 6", KEEP, FSA, KEEP, KEEP),
    64: ("Josh's Unsubsidized Loan 6", KEEP, FSA, KEEP, KEEP),
    24: ("Mona's Unsubsidized Loans", KEEP, AIDV, KEEP, KEEP),
    25: ("Mona's Subsidized Loans", KEEP, AIDV, KEEP, KEEP),
    80: ("DL Subsidized", "Mona's Direct Subsidized Loan", AIDV, KEEP, KEEP),
    81: ("DL Unsubsidized", "Mona's Direct Unsubsidized Loan", AIDV, KEEP, KEEP),
    27: ("CR-V Auto Loan", KEEP, None, KEEP, KEEP),
}

# Copy accounts: (copy, target, compare absolute amounts). Matching rows are soft-deleted, the rest move to target.
COPIES = [
    (31, 19, False),
    (33, 34, False),
    (30, 22, False),
    (88, 34, False),
    (89, 58, False),
    (92, 53, False),
    (93, 46, False),
    (48, 49, False),
    (90, 49, False),
    (83, 72, True),
]
# Pieces of one real account: every live row moves to target.
MERGES = [(50, 49), (85, 23), (86, 40), (32, 29), (28, 29)]
# Accounts removed after their rows and source links move to target (their link kind is free on the target).
REMOVE = {88: 34, 89: 58, 92: 53, 93: 46, 90: 49, 83: 72, 85: 23, 86: 40, 91: 55, 1: None}
REMOVE_NAMES = {91: "Amex Savings Account", 1: "Smoke Checking 183421"}
STAY_OPEN = {2, 3, 4, 5, 6, 7, 8, 9, 74, 75, 76, 77, 78, 79, 80, 81}
SHELLS = {31, 33, 30, 48, 50, 32, 28}
MATCH_DAYS = 3

log: list[str] = []


def note(msg: str) -> None:
    log.append(msg)


async def q(conn, sql: str, **params):
    return await conn.execute(text(sql), params)


async def match_copy(conn, copy_id: int, target_id: int, use_abs: bool) -> tuple[list[tuple[int, int]], list[int]]:
    """Pair each copy row with the closest-dated target row of the same amount, preferring unused ones.

    A copy row that only matches an already-paired target row is a repeat inside the copy (Tiller re-imports).
    """
    rows_sql = (
        'SELECT id, txn_date, amount FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL ORDER BY txn_date, id'
    )
    copies = (await q(conn, rows_sql, a=copy_id)).all()
    by_amount: dict = defaultdict(list)
    for t in (await q(conn, rows_sql, a=target_id)).all():
        by_amount[abs(t.amount) if use_abs else t.amount].append(t)
    used: set[int] = set()
    pairs, leftover = [], []
    for c in copies:
        near = [
            t
            for t in by_amount.get(abs(c.amount) if use_abs else c.amount, [])
            if abs((t.txn_date - c.txn_date).days) <= MATCH_DAYS
        ]
        if near:
            best = min(near, key=lambda t: (t.id in used, abs((t.txn_date - c.txn_date).days), t.id))
            used.add(best.id)
            pairs.append((c.id, best.id))
        else:
            leftover.append(c.id)
    return pairs, leftover


PAIRS = "unnest(CAST(:d AS bigint[]), CAST(:k AS bigint[])) AS p(d, k)"


async def drop_copies(conn, pairs: list[tuple[int, int]], now: datetime, has_sources: bool) -> None:
    """Same carry-over as the Duplicates page: category, notes and tags survive on the kept row."""
    if not pairs:
        return
    ids = {"d": [d for d, _ in pairs], "k": [k for _, k in pairs]}
    await q(
        conn,
        f"""--sql
        UPDATE "transaction" keep SET category_id = d.category_id, category_source = d.category_source
        FROM {PAIRS} JOIN "transaction" d ON d.id = p.d
        WHERE keep.id = p.k AND keep.category_id IS NULL AND d.category_id IS NOT NULL
        """,
        **ids,
    )
    await q(
        conn,
        f"""--sql
        UPDATE "transaction" keep SET notes = CASE WHEN coalesce(keep.notes, '') = '' THEN d.notes
                                                   ELSE keep.notes || E'\\n' || d.notes END
        FROM {PAIRS} JOIN "transaction" d ON d.id = p.d
        WHERE keep.id = p.k AND coalesce(d.notes, '') <> '' AND strpos(coalesce(keep.notes, ''), d.notes) = 0
        """,
        **ids,
    )
    await q(
        conn,
        f"""--sql
        INSERT INTO transaction_tag (transaction_id, tag_id)
        SELECT p.k, tt.tag_id FROM {PAIRS} JOIN transaction_tag tt ON tt.transaction_id = p.d ON CONFLICT DO NOTHING
        """,
        **ids,
    )
    if has_sources:
        await q(
            conn,
            f"UPDATE transaction_source s SET transaction_id = p.k, role = 'matched' FROM {PAIRS} WHERE s.transaction_id = p.d",
            **ids,
        )
        await q(conn, f"UPDATE transaction_note n SET transaction_id = p.k FROM {PAIRS} WHERE n.transaction_id = p.d", **ids)
    await q(conn, 'UPDATE "transaction" SET deleted_at = :now WHERE id = ANY(CAST(:d AS bigint[]))', now=now, d=ids["d"])
    await q(
        conn,
        """--sql
        UPDATE duplicate_pair SET status = 'confirmed_duplicate', decided_at = :now
        WHERE status = 'pending' AND (txn_a_id = ANY(CAST(:d AS bigint[])) OR txn_b_id = ANY(CAST(:d AS bigint[])))
        """,
        now=now,
        d=ids["d"],
    )


async def move_rows(conn, ids: list[int], target_id: int) -> None:
    if not ids:
        return
    rows = (
        await q(
            conn,
            'SELECT id, txn_date, amount, description FROM "transaction" WHERE id = ANY(CAST(:ids AS bigint[]))',
            ids=ids,
        )
    ).all()
    await q(
        conn,
        """--sql
        UPDATE "transaction" t SET account_id = :a, fingerprint = m.fp
        FROM unnest(CAST(:ids AS bigint[]), CAST(:fps AS text[])) AS m(id, fp) WHERE t.id = m.id
        """,
        a=target_id,
        ids=[r.id for r in rows],
        fps=[fingerprint(target_id, r.txn_date, r.amount, r.description) for r in rows],
    )


async def institution_id(conn, name: str) -> int:
    await q(conn, "INSERT INTO institution (name) VALUES (:n) ON CONFLICT (name) DO NOTHING", n=name)
    return (await q(conn, "SELECT id FROM institution WHERE name = :n", n=name)).scalar_one()


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        has_sources = (await q(conn, "SELECT to_regclass('transaction_source') IS NOT NULL")).scalar_one()

        # Verify every account is what the plan expects before touching anything.
        current = {r.id: r for r in (await q(conn, "SELECT id, name, external_refs FROM account")).all()}
        expected = {**{i: spec[0] for i, spec in ACCOUNTS.items()}, **REMOVE_NAMES}
        for i in (*COPIES, *MERGES):
            expected.setdefault(i[0], current[i[0]].name if i[0] in current else None)
        mismatched = [i for i, name in expected.items() if i not in current or current[i].name != name]
        if mismatched:
            raise SystemExit(f"Accounts changed since the review, aborting: {mismatched}")

        if apply:
            touched = sorted({i for c in COPIES for i in c[:2]} | {i for m in MERGES for i in m} | set(REMOVE) - {None})
            backup = {
                "account": [dict(r._mapping) for r in await q(conn, "SELECT * FROM account")],
                "institution": [dict(r._mapping) for r in await q(conn, "SELECT * FROM institution")],
                "transaction": [
                    dict(r._mapping)
                    for r in await q(
                        conn,
                        "SELECT id, account_id, deleted_at, fingerprint, category_id, category_source, notes, transfer_match_id "
                        'FROM "transaction" WHERE account_id = ANY(:a)',
                        a=touched,
                    )
                ],
            }
            out = Path(__file__).resolve().parents[2] / "logs" / f"cleanup_accounts_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            note(f"backup: {out}")

        for iid, (old, new) in INSTITUTION_RENAMES.items():
            got = (await q(conn, "SELECT name FROM institution WHERE id = :i", i=iid)).scalar_one_or_none()
            if got == old:
                await q(conn, "UPDATE institution SET name = :n WHERE id = :i", n=new, i=iid)
                note(f"institution {iid}: {old!r} -> {new!r}")

        # Copies and merges.
        stats = {"soft_deleted": 0, "moved": 0}
        for copy_id, target_id, use_abs in COPIES:
            pairs, leftover = await match_copy(conn, copy_id, target_id, use_abs)
            await drop_copies(conn, pairs, now, has_sources)
            await move_rows(conn, leftover, target_id)
            stats["soft_deleted"] += len(pairs)
            stats["moved"] += len(leftover)
            note(f"copy {copy_id} -> {target_id}: {len(pairs)} soft-deleted, {len(leftover)} unmatched moved")
            if leftover:
                moved_sql = (
                    'SELECT id, txn_date, amount, description FROM "transaction" '
                    "WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY txn_date"
                )
                for r in (await q(conn, moved_sql, ids=leftover)).all():
                    note(f"    moved {r.id} {r.txn_date} {r.amount} {r.description[:70]}")
        for src, target_id in MERGES:
            ids = list(
                (await q(conn, 'SELECT id FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL', a=src)).scalars()
            )
            await move_rows(conn, ids, target_id)
            stats["moved"] += len(ids)
            note(f"merge {src} -> {target_id}: {len(ids)} moved")

        # Account fields (temporary names first so swaps can't hit the unique constraint).
        renames = {i: spec[1] for i, spec in ACCOUNTS.items() if spec[1] is not KEEP and spec[1] != current[i].name}
        for i in renames:
            await q(conn, "UPDATE account SET name = :n WHERE id = :i", n=f"__cleanup_{i}", i=i)
        for i, (old, new, inst, typ, mask) in ACCOUNTS.items():
            sets, params = [], {"i": i}
            if i in renames:
                sets.append("name = :name")
                params["name"] = new
            params["inst"] = await institution_id(conn, inst) if inst else None
            sets.append("institution_id = :inst")
            if typ is not KEEP:
                sets.append("account_type = :typ")
                params["typ"] = typ
            if mask is not KEEP:
                sets.append("mask = :mask")
                params["mask"] = mask or None
            await q(conn, f"UPDATE account SET {', '.join(sets)} WHERE id = :i", **params)
            if i in renames:
                note(f"account {i}: {old!r} -> {new!r}")

        # Removed accounts hand their rows, balances, holdings, batches and source links to the target.
        for src, target_id in REMOVE.items():
            if target_id is not None:
                await q(conn, 'UPDATE "transaction" SET account_id = :t WHERE account_id = :s', t=target_id, s=src)
                await q(
                    conn,
                    """--sql
                    UPDATE account_balance b SET account_id = :t WHERE b.account_id = :s AND NOT EXISTS (
                        SELECT 1 FROM account_balance o WHERE o.account_id = :t AND o.as_of = b.as_of AND o.source = b.source)
                    """,
                    t=target_id,
                    s=src,
                )
                await q(
                    conn,
                    """--sql
                    UPDATE holding h SET account_id = :t WHERE h.account_id = :s AND NOT EXISTS (
                        SELECT 1 FROM holding o WHERE o.account_id = :t AND o.as_of = h.as_of AND o.external_id = h.external_id)
                    """,
                    t=target_id,
                    s=src,
                )
                await q(conn, "UPDATE import_row SET account_id = :t WHERE account_id = :s", t=target_id, s=src)
                await q(
                    conn,
                    """--sql
                    UPDATE import_batch SET defaults = jsonb_set(defaults, '{account_id}', to_jsonb(CAST(:t AS integer)))
                    WHERE defaults->>'account_id' = :s
                    """,
                    t=target_id,
                    s=str(src),
                )
                src_refs = current[src].external_refs or {}
                tgt_refs = (await q(conn, "SELECT external_refs FROM account WHERE id = :t", t=target_id)).scalar_one() or {}
                clash = [k for k in src_refs if k in tgt_refs]
                if clash:
                    raise SystemExit(f"Account {target_id} already has a {clash} link; cannot absorb {src}")
                await q(
                    conn,
                    "UPDATE account SET external_refs = CAST(:r AS jsonb) WHERE id = :t",
                    r=json.dumps({**tgt_refs, **src_refs}),
                    t=target_id,
                )
            left = (await q(conn, 'SELECT count(*) FROM "transaction" WHERE account_id = :s', s=src)).scalar_one()
            if left and target_id is None:
                note(f"kept account {src} ({current[src].name!r}): {left} deleted transactions still reference it")
                continue
            if left:
                raise SystemExit(f"Account {src} still has {left} transactions; not removing")
            await q(conn, "DELETE FROM account WHERE id = :s", s=src)
            note(f"removed account {src} ({current[src].name!r})" + (f" into {target_id}" if target_id else ""))

        await q(conn, "UPDATE account SET is_hidden = true WHERE id = ANY(:ids)", ids=list(SHELLS))
        closed = (
            (
                await q(
                    conn,
                    "UPDATE account SET is_closed = true WHERE NOT (id = ANY(:keep)) AND NOT is_closed RETURNING id",
                    keep=list(STAY_OPEN),
                )
            )
            .scalars()
            .all()
        )
        note(f"closed {len(closed)} accounts: {sorted(closed)}")

        gone = (
            (
                await q(
                    conn,
                    "DELETE FROM institution i WHERE NOT EXISTS (SELECT 1 FROM account a WHERE a.institution_id = i.id) RETURNING name",
                )
            )
            .scalars()
            .all()
        )
        note(f"removed {len(gone)} institutions: {sorted(gone)}")

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            transfers = await match_transfers(session)
        note(f"transfer matching: {transfers}")

        summary = (
            await q(
                conn,
                """--sql
                SELECT coalesce(i.name, '(none)') AS inst, a.id, a.name, a.account_type, a.mask, a.is_hidden, a.is_closed,
                       (SELECT count(*) FROM "transaction" t WHERE t.account_id = a.id AND t.deleted_at IS NULL) AS n
                FROM account a LEFT JOIN institution i ON i.id = a.institution_id ORDER BY 1, a.name
                """,
            )
        ).all()
        note("\nRESULT")
        for r in summary:
            flags = ("H" if r.is_hidden else "-") + ("C" if r.is_closed else "-")
            note(f"  {r.inst:<40} {r.id:>3} {flags} {r.account_type:<11} {r.mask or '':<5} {r.n:>6}  {r.name}")

        print(
            f"{'APPLIED' if apply else 'DRY RUN'}: {stats}, transfers {transfers}, institutions removed {len(gone)}, closed {len(closed)}"
        )
        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    try:
        asyncio.run(run(apply))
    finally:
        out = Path(__file__).resolve().parents[2] / "logs" / "cleanup_accounts.log"
        out.parent.mkdir(exist_ok=True)
        out.write_text("\n".join(log), encoding="utf-8")
        print(f"log: {out}")
