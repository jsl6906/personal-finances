"""Untangle the older statement imports whose check said "this document probably covers several accounts" (2026-10-04).

Most were one card split across a Tiller account and an account the statement backfill created for it:
  A. Chase x6044: 97 (statements) folds into 18 Amazon Visa (Tiller); Tiller repeats in 18 are dropped as in F.
  C. 82 "Unknown Card (2007-11) x2009" holds rows of Amex Gold (...1001) and Costco (...1000) statements: each row moves
     to its statement's card (53 / 46), dropping those Tiller already has.
  F. Sapphire x2180: 19 (Tiller, rows repeated 3-7 times) merges into 8 (statements 2012-2025). Tiller rows matching
     a statement row survive (keeping Tiller names and categories), their statement copies and Tiller repeats go,
     exact repeats among the rest collapse to one; 19 becomes a hidden Tiller shell.
The rest really cover several accounts and are split per account (as #779 was):
  B. BofA 2009 combined statements, by the running balance; rows inserted into the wrong account move to the right one.
  D/E. Census FCU #122 and PayFlex HSA #659, by where each row's transaction lives.
Afterwards every affected statement gets its confident check fixes; for x2180 also every "not on the statement" removal.

Dry run by default; pass --apply to commit. Run: scripts/with_env.ps1 .env.azure python scripts/untangle_legacy_statements.py
"""

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cleanup_accounts import PAIRS, drop_copies, match_copy, move_rows, q
from merge_savings_0982 import repoint_import_dups
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine
from ledger.imports.coverage import apply_fixes, auto_fix, save_checks
from ledger.models import ImportBatch

MATCH_DAYS = 3
EXPECTED = {
    8: "Freedom Rewards Card",
    18: "Amazon Visa (2018-23)",
    19: "Sapphire Preferred",
    38: "HSA (2015-17)",
    46: "Costco TrueEarnings Card",
    53: "Rewards Plus Gold Card",
    56: "Salary Checking",
    57: "Spending Checking",
    59: "Checking (2008-10)",
    62: "Checking (2009)",
    82: "Unknown Card (2007-11)",
    84: "Statement Account (2008-10)",
    95: "PayFlex Systems USA, Inc.",
    97: "Chase",
}
AMEX_CARDS = {"31001": 53, "1001": 53, "71001": 53, "31000": 46, "1000": 46}
SPLIT_BY_BALANCE = [66, 73, 89, 96]
SPLIT_BY_LINK = [122, 659]
SAPPHIRE_SHELL = "Sapphire Preferred - Tiller copy 2"
SAPPHIRE_NOTE = "Card x2180: Sapphire Preferred history (Tiller account 19) merged in 2026-10-04."
UNKNOWN_AMEX = "Unknown Card (2009-18)"
ROOT = Path(__file__).resolve().parents[2]


def dec(v) -> Decimal | None:
    try:
        return Decimal(str(v).replace(",", "")) if v not in (None, "") else None
    except ArithmeticError:
        return None


async def drop(conn, pairs: list[tuple[int, int]], now: datetime) -> None:
    """Soft-delete each copy in favour of its kept twin; sources, notes, import links and pending dups follow."""
    if not pairs:
        return
    await repoint_import_dups(conn, pairs)
    await drop_copies(conn, pairs, now, True)
    await q(
        conn,
        f"UPDATE import_row r SET transaction_id = p.k FROM {PAIRS} WHERE r.transaction_id = p.d",
        d=[d for d, _ in pairs],
        k=[k for _, k in pairs],
    )
    await q(
        conn,
        'UPDATE "transaction" SET transfer_match_id = NULL WHERE transfer_match_id = ANY(CAST(:d AS bigint[]))',
        d=[d for d, _ in pairs],
    )


async def pair_ids(conn, ids: list[int], target: int, taken: set[int] | None = None) -> tuple[list, list[int]]:
    """match_copy for a subset of rows: each pairs with the closest same-amount target row, unused ones first."""
    rows = (
        await q(
            conn,
            'SELECT id, txn_date, amount FROM "transaction" WHERE id = ANY(CAST(:i AS bigint[])) ORDER BY txn_date, id',
            i=ids,
        )
    ).all()
    by_amount = defaultdict(list)
    for t in await q(
        conn,
        'SELECT id, txn_date, amount FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL',
        a=target,
    ):
        by_amount[t.amount].append(t)
    used = set(taken or ())
    pairs, leftover = [], []
    for c in rows:
        near = [t for t in by_amount.get(c.amount, []) if abs((t.txn_date - c.txn_date).days) <= MATCH_DAYS]
        if near:
            best = min(near, key=lambda t: (t.id in used, abs((t.txn_date - c.txn_date).days), t.id))
            used.add(best.id)
            pairs.append((c.id, best.id))
        else:
            leftover.append(c.id)
    return pairs, leftover


async def repoint_account(conn, src: int, target: int, batch_ids: list[int] | None = None) -> str:
    """Import rows / batch defaults that name `src` now name `target` (optionally only for some batches)."""
    only = "" if batch_ids is None else " AND batch_id = ANY(:b)"
    n_rows = (
        await q(
            conn,
            f"UPDATE import_row SET account_id = :t WHERE account_id = :s{only}",
            t=target,
            s=src,
            **({} if batch_ids is None else {"b": batch_ids}),
        )
    ).rowcount
    only = "" if batch_ids is None else " AND id = ANY(:b)"
    extra = {} if batch_ids is None else {"b": batch_ids}
    n_def = (
        await q(
            conn,
            "UPDATE import_batch SET defaults = jsonb_set(defaults, '{account_id}', to_jsonb(CAST(:t AS integer))) "
            f"WHERE defaults->>'account_id' = :s{only}",
            t=target,
            s=str(src),
            **extra,
        )
    ).rowcount
    n_map = (
        await q(
            conn,
            f"""--sql
            UPDATE import_batch SET defaults = jsonb_set(defaults, '{{account_map}}', (
                SELECT jsonb_object_agg(e.key, CASE WHEN e.value = to_jsonb(CAST(:s AS integer))
                                                    THEN to_jsonb(CAST(:t AS integer)) ELSE e.value END)
                FROM jsonb_each(defaults->'account_map') e))
            WHERE jsonb_typeof(defaults->'account_map') = 'object' AND EXISTS (
                SELECT 1 FROM jsonb_each(defaults->'account_map') e WHERE e.value = to_jsonb(CAST(:s AS integer))){only}
            """,
            t=target,
            s=src,
            **extra,
        )
    ).rowcount
    return f"{n_rows} import rows, {n_def} batch defaults, {n_map} account maps"


async def merge_refs(conn, src: int, target: int, skip: set[str] = frozenset()) -> dict:
    refs = dict(
        (r.id, r.external_refs or {})
        for r in await q(conn, "SELECT id, external_refs FROM account WHERE id = ANY(:ids)", ids=[src, target])
    )
    merged = {**refs[target]}
    for k, v in refs[src].items():
        if k in skip:
            continue
        if k in merged:
            raise SystemExit(f"Account {target} already has a {k} link; cannot take {src}'s")
        merged[k] = v
    await q(conn, "UPDATE account SET external_refs = CAST(:r AS jsonb) WHERE id = :t", r=json.dumps(merged), t=target)
    return merged


# ---------- A. Chase x6044 ----------
async def fold_chase(conn, now: datetime, log: list[str]) -> None:
    src, target = 97, 18
    pairs, leftover = await match_copy(conn, src, target, False)
    await drop(conn, pairs, now)
    await move_rows(conn, leftover, target)
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
    await q(conn, "DELETE FROM account WHERE id = :s", s=src)
    log.append(f"A. 97 -> 18: {len(pairs)} dropped as Tiller dups, {len(leftover)} moved; {moved}; 97 removed; refs {refs}")


# ---------- C. Amex rows in 82 ----------
async def rehome_amex(conn, now: datetime, log: list[str]) -> None:
    rows = (
        await q(
            conn,
            """--sql
            SELECT t.id, b.id AS batch_id, b.doc_meta->>'account_last4' AS l4 FROM "transaction" t
            LEFT JOIN import_batch b ON b.id = t.import_batch_id
            WHERE t.account_id = 82 AND t.deleted_at IS NULL
            """,
        )
    ).all()
    for target in sorted(set(AMEX_CARDS.values())):
        ids = [r.id for r in rows if AMEX_CARDS.get(r.l4) == target]
        batches = sorted({r.batch_id for r in rows if AMEX_CARDS.get(r.l4) == target})
        all_batches = [
            b
            for (b,) in await q(
                conn,
                "SELECT id FROM import_batch WHERE source_type = 'document' AND doc_meta->>'account_last4' = ANY(:l)",
                l=[k for k, v in AMEX_CARDS.items() if v == target],
            )
        ]
        pairs, leftover = await pair_ids(conn, ids, target)
        await drop(conn, pairs, now)
        await move_rows(conn, leftover, target)
        moved = await repoint_account(conn, 82, target, sorted(set(batches) | set(all_batches)))
        log.append(
            f"C. 82 -> {target}: {len(ids)} rows from {len(batches)} statements: {len(pairs)} dropped as Tiller dups, "
            f"{len(leftover)} moved; {moved}"
        )
    left = (
        await q(
            conn,
            'SELECT count(*), min(txn_date), max(txn_date) FROM "transaction" WHERE account_id = 82 AND deleted_at IS NULL',
        )
    ).one()
    await q(conn, "UPDATE account SET name = :n WHERE id = 82", n=UNKNOWN_AMEX)
    log.append(f"   82 keeps {left[0]} rows {left[1]}..{left[2]}; renamed {UNKNOWN_AMEX!r}")


# ---------- F. Sapphire x2180 ----------
async def dedupe_tiller(conn, account: int, now: datetime, log: list[str]) -> None:
    """Drop Tiller rows repeating a statement-backed row (same amount, within MATCH_DAYS), then collapse exact
    repeats (same date, amount, description) among the remaining Tiller rows."""
    backed = {
        i
        for (i,) in await q(
            conn,
            """--sql
            SELECT DISTINCT t.id FROM "transaction" t JOIN transaction_source s ON s.transaction_id = t.id
            JOIN import_batch b ON b.id = s.import_batch_id AND b.source_type = 'document'
            WHERE t.account_id = :a AND t.deleted_at IS NULL
            """,
            a=account,
        )
    }
    rows = (
        await q(
            conn,
            'SELECT id, txn_date, amount, lower(description) AS d, source_type FROM "transaction" '
            "WHERE account_id = :a AND deleted_at IS NULL ORDER BY txn_date, id",
            a=account,
        )
    ).all()
    keep_by_amount = defaultdict(list)
    for r in rows:
        if r.id in backed:
            keep_by_amount[r.amount].append(r)
    repeats = []
    for r in rows:
        if r.id in backed or r.source_type != "tiller":
            continue
        near = [k for k in keep_by_amount.get(r.amount, []) if abs((k.txn_date - r.txn_date).days) <= MATCH_DAYS]
        if near:
            repeats.append((r.id, min(near, key=lambda k: (abs((k.txn_date - r.txn_date).days), k.id)).id))
    await drop(conn, repeats, now)
    dropped = {d for d, _ in repeats}

    first: dict[tuple, int] = {}
    exact = []
    for r in rows:
        if r.id in backed or r.id in dropped or r.source_type != "tiller":
            continue
        key = (r.txn_date, r.amount, r.d)
        if key in first:
            exact.append((r.id, first[key]))
        else:
            first[key] = r.id
    await drop(conn, exact, now)
    unbacked = (
        await q(
            conn,
            'SELECT count(*), coalesce(sum(amount), 0) FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL '
            "AND NOT (id = ANY(CAST(:b AS bigint[])))",
            a=account,
            b=list(backed),
        )
    ).one()
    log.append(
        f"   account {account}: {len(rows)} rows; statement-backed {len(backed)}; Tiller repeats of those dropped "
        f"{len(repeats)}; exact Tiller repeats collapsed {len(exact)}; left without a statement row {unbacked[0]} "
        f"({unbacked[1]})"
    )


async def merge_sapphire(conn, now: datetime, log: list[str]) -> None:
    stmt, tiller = 8, 19
    # 1. Statement copies give way to the Tiller row they match (Tiller has the names and categories).
    stmt_ids = [
        i
        for (i,) in await q(
            conn,
            "SELECT id FROM \"transaction\" WHERE account_id = :a AND deleted_at IS NULL AND source_type <> 'tiller'",
            a=stmt,
        )
    ]
    pairs, leftover = await pair_ids(conn, stmt_ids, tiller)
    await drop(conn, pairs, now)
    log.append(f"F. 8 -> 19: {len(pairs)} statement rows matched a Tiller row ({len(leftover)} without one stay in 8)")

    await dedupe_tiller(conn, tiller, now, log)

    # Everything left moves to 8; 19 stays as a hidden shell so a full Tiller resync maps to it.
    left = [
        i for (i,) in await q(conn, 'SELECT id FROM "transaction" WHERE account_id = :a AND deleted_at IS NULL', a=tiller)
    ]
    await move_rows(conn, left, stmt)
    moved = await repoint_account(conn, tiller, stmt)
    await q(
        conn,
        "UPDATE account SET name = :n, is_hidden = true, is_closed = true WHERE id = :s",
        n=SAPPHIRE_SHELL,
        s=tiller,
    )
    await q(
        conn,
        "UPDATE account SET notes = CASE WHEN coalesce(notes, '') = '' THEN :note ELSE notes || E'\\n' || :note END "
        "WHERE id = :t",
        note=SAPPHIRE_NOTE,
        t=stmt,
    )
    log.append(f"   moved {len(left)} rows 19 -> 8; {moved}; 19 renamed {SAPPHIRE_SHELL!r} (hidden, closed)")
    names = (
        await q(
            conn,
            """--sql
            SELECT extract(year FROM c.period_end)::int y, string_agg(DISTINCT b.doc_meta->>'account_name', ' / ')
            FROM statement_check c JOIN import_batch b ON b.id = c.import_batch_id
            WHERE c.account_id IN (8, 19) GROUP BY 1 ORDER BY 1
            """,
        )
    ).all()
    log.append("   x2180 statement names by year: " + "; ".join(f"{y}: {n}" for y, n in names))


# ---------- splits ----------
async def batch_rows(conn, bid: int):
    return (
        await q(
            conn,
            """--sql
            SELECT r.id, r.row_index, r.amount, r.raw, r.decision, r.transaction_id,
                   coalesce(t.id, m.tid) AS link, coalesce(t.account_id, m.acct) AS acct,
                   coalesce(t.import_batch_id = r.batch_id, false) AS created
            FROM import_row r
            LEFT JOIN "transaction" t ON t.id = r.transaction_id AND t.deleted_at IS NULL
            LEFT JOIN LATERAL (SELECT ts.transaction_id AS tid, tt.account_id AS acct FROM transaction_source ts
                JOIN "transaction" tt ON tt.id = ts.transaction_id AND tt.deleted_at IS NULL
                WHERE ts.import_row_id = r.id AND ts.role = 'matched' ORDER BY ts.id LIMIT 1) m ON true
            WHERE r.batch_id = :b ORDER BY r.row_index
            """,
            b=bid,
        )
    ).all()


def chains(prev, r) -> bool:
    pb, b = dec(prev.raw.get("Balance")), dec(r.raw.get("Balance"))
    if pb is None or b is None or r.amount is None:
        return False
    return abs(pb + r.amount - b) < Decimal("0.005")


async def write_split(conn, bid: int, assign: dict[int, int], rows, log: list[str]) -> None:
    """Store a per-account split: row Account column + account_id, doc_meta.accounts and defaults.account_map."""
    accts = {
        r.id: r
        for r in await q(
            conn, "SELECT id, name, mask, account_type FROM account WHERE id = ANY(:i)", i=list(set(assign.values()))
        )
    }
    ref_of = {a: (accts[a].mask or accts[a].name) for a in accts}
    meta_accounts = []
    for a in sorted(set(assign.values()), key=lambda a: min(i for i, x in assign.items() if x == a)):
        mine = [r for r in rows if assign[r.row_index] == a and r.amount is not None]
        total = sum((r.amount for r in mine), Decimal(0))
        opening = closing = recon = None
        if (
            mine
            and all(chains(p, r) for p, r in zip(mine, mine[1:], strict=False))
            and dec(mine[0].raw.get("Balance")) is not None
        ):
            opening = dec(mine[0].raw["Balance"]) - mine[0].amount
            closing = dec(mine[-1].raw["Balance"])
            recon = {"sum_of_rows": f"{total:.2f}", "balance_change": f"{closing - opening:.2f}", "reconciles": True}
        meta_accounts.append(
            {
                "ref": ref_of[a],
                "last4": accts[a].mask,
                "name": accts[a].name,
                "account_type": accts[a].account_type,
                "opening_balance": None if opening is None else float(opening),
                "closing_balance": None if closing is None else float(closing),
                "rows": len(mine),
                "reconciliation": recon,
            }
        )
    for r in rows:
        a = assign[r.row_index]
        await q(
            conn,
            "UPDATE import_row SET account_id = :a, "
            "raw = raw || jsonb_build_object('Account', CAST(:ref AS text)) WHERE id = :i",
            a=a,
            ref=ref_of[a],
            i=r.id,
        )
    await q(
        conn,
        """--sql
        UPDATE import_batch SET
            doc_meta = doc_meta || jsonb_build_object('accounts', CAST(:accts AS jsonb), 'unassigned_rows', 0,
                                                      'split_by_script', CAST(:now AS text)),
            defaults = defaults || jsonb_build_object('account_map', CAST(:map AS jsonb))
        WHERE id = :b
        """,
        accts=json.dumps(meta_accounts),
        now=datetime.now(UTC).isoformat(),
        map=json.dumps({ref_of[a]: a for a in accts}),
        b=bid,
    )
    log.append(
        f"   #{bid}: "
        + "; ".join(f"{m['ref']} {m['rows']} rows{' reconciles' if m['reconciliation'] else ''}" for m in meta_accounts)
    )


async def split_by_balance(conn, bid: int, now: datetime, log: list[str]) -> None:
    rows = await batch_rows(conn, bid)
    sections: list[list] = []
    for r in rows:
        if not sections or not chains(sections[-1][-1], r):
            sections.append([])
        sections[-1].append(r)
    assign: dict[int, int] = {}
    for sec in sections:
        held = Counter(r.acct for r in sec if r.acct and not r.created).most_common(1)
        if not held:
            raise SystemExit(f"#{bid}: rows {sec[0].row_index}-{sec[-1].row_index} have no matched transaction")
        for r in sec:
            assign[r.row_index] = held[0][0]
    linked = {r.link for r in rows if r.link}
    fixes, moves = [], defaultdict(list)
    for r in rows:
        want = assign[r.row_index]
        if r.created and r.acct != want:
            pairs, leftover = await pair_ids(conn, [r.link], want, taken=linked)
            if pairs:
                fixes.extend(pairs)
                linked.add(pairs[0][1])
            else:
                moves[want].extend(leftover)
    await drop(conn, fixes, now)
    for target, ids in moves.items():
        await move_rows(conn, ids, target)
    log.append(
        f"B. #{bid}: {len(sections)} sections; {len(fixes)} inserted rows dropped into their twin, "
        f"{sum(len(v) for v in moves.values())} moved to the right account"
    )
    await write_split(conn, bid, assign, await batch_rows(conn, bid), log)


async def split_by_link(conn, bid: int, log: list[str]) -> None:
    rows = await batch_rows(conn, bid)
    default = (await q(conn, "SELECT (defaults->>'account_id')::int FROM import_batch WHERE id = :b", b=bid)).scalar()
    assign = {r.row_index: r.acct or default for r in rows}
    log.append(f"D/E. #{bid}: split by where each row's transaction lives")
    await write_split(conn, bid, assign, rows, log)


async def statuses(conn, batch_ids: list[int]) -> dict[int, list[tuple]]:
    out: dict[int, list[tuple]] = defaultdict(list)
    for r in await q(
        conn,
        "SELECT import_batch_id, account_ref, account_id, status, statement_total, ledger_total "
        "FROM statement_check WHERE import_batch_id = ANY(:b) ORDER BY 1, 2",
        b=batch_ids,
    ):
        out[r.import_batch_id].append(tuple(r)[1:])
    return out


async def backup(
    conn, now: datetime, accounts: list[int], batches: list[int], prefix: str = "untangle_legacy"
) -> Path:
    async def rows(sql: str, **p) -> list[dict]:
        return [dict(r._mapping) for r in await q(conn, sql, **p)]

    txn_ids = 'SELECT id FROM "transaction" WHERE account_id = ANY(:a)'
    data = {
        "account": await rows("SELECT * FROM account WHERE id = ANY(:a)", a=accounts),
        "transaction": await rows(
            "SELECT id, account_id, deleted_at, fingerprint, category_id, category_source, notes, transfer_match_id "
            'FROM "transaction" WHERE account_id = ANY(:a)',
            a=accounts,
        ),
        "transaction_source": await rows(
            f"SELECT * FROM transaction_source WHERE transaction_id IN ({txn_ids})", a=accounts
        ),
        "transaction_note": await rows(f"SELECT * FROM transaction_note WHERE transaction_id IN ({txn_ids})", a=accounts),
        "duplicate_pair": await rows(
            f"SELECT * FROM duplicate_pair WHERE txn_a_id IN ({txn_ids}) OR txn_b_id IN ({txn_ids})", a=accounts
        ),
        "import_row": await rows(
            "SELECT id, batch_id, account_id, transaction_id, decision, raw FROM import_row "
            "WHERE batch_id = ANY(:b) OR account_id = ANY(:a)",
            a=accounts,
            b=batches,
        ),
        "import_batch": await rows("SELECT id, defaults, doc_meta, stats FROM import_batch WHERE id = ANY(:b)", b=batches),
    }
    out = ROOT / "logs" / f"{prefix}_backup_{now:%Y%m%d%H%M%S}.json"
    out.write_text(json.dumps(data, default=str), encoding="utf-8")
    return out


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        names = dict((await q(conn, "SELECT id, name FROM account WHERE id = ANY(:i)", i=list(EXPECTED))).all())
        wrong = [i for i, n in EXPECTED.items() if not (names.get(i) or "").startswith(n)]
        if wrong:
            raise SystemExit(f"Accounts changed since the review, aborting: {wrong}")
        legacy = [
            b
            for (b,) in await q(
                conn,
                "SELECT DISTINCT import_batch_id FROM statement_check "
                "WHERE detail->>'message' LIKE '%probably covers several%'",
            )
        ]
        touched = sorted(EXPECTED)
        batches = sorted(
            set(legacy)
            | {
                b
                for (b,) in await q(
                    conn, "SELECT DISTINCT import_batch_id FROM statement_check WHERE account_id = ANY(:a)", a=touched
                )
            }
            | {
                b
                for (b,) in await q(
                    conn,
                    'SELECT DISTINCT import_batch_id FROM "transaction" '
                    "WHERE account_id = ANY(:a) AND import_batch_id IS NOT NULL",
                    a=touched,
                )
            }
        )
        before = await statuses(conn, batches)
        if apply:
            log.append(f"backup: {await backup(conn, now, touched, batches)}")

        await fold_chase(conn, now, log)
        await dedupe_tiller(conn, 18, now, log)
        await rehome_amex(conn, now, log)
        await merge_sapphire(conn, now, log)
        for bid in SPLIT_BY_BALANCE:
            await split_by_balance(conn, bid, now, log)
        for bid in SPLIT_BY_LINK:
            await split_by_link(conn, bid, log)

        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
            log.append(f"transfer matching: {await match_transfers(session)}")
            fixed: Counter = Counter()
            for bid in batches:
                b = await session.get(ImportBatch, bid)
                if not (b and b.source_type == "document" and b.status == "committed"):
                    continue
                res = await auto_fix(session, b)
                fixed["confident"] += res["applied"]
                # Statements are the truth for x2180: Tiller rows they don't list go too (edges stay for review).
                extras = [
                    {"fix": i["fix"], "row_id": i["row_id"], "transaction_id": i["transaction_id"]}
                    for c in res["checks"]
                    if c["account_id"] == 8
                    for i in c["detail"]["issues"]
                    if i["kind"] == "extra" and i["fix"] == "remove"
                ]
                if extras:
                    fixed["x2180 extras"] += (await apply_fixes(session, b, extras, auto=True))["applied"]
            log.append(f"check fixes applied: {dict(fixed)}")
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
        still = [b for b, v in after.items() if b in legacy and any(c[2] != "ok" for c in v)]
        log.append(f"former 'several accounts' statements: {len(legacy)}; not ok now: {len(still)}")
        for bid in legacy:
            was = [c[1:3] for c in before.get(bid, [])]
            log.append(f"  #{bid}: {was} -> {[c[:5] for c in after.get(bid, [])]}")
        msg = (
            await q(
                conn,
                "SELECT count(*) FROM statement_check WHERE detail->>'message' LIKE '%probably covers several%'",
            )
        ).scalar()
        log.append(f"checks still saying 'probably covers several accounts': {msg}")
        if "--detail" in sys.argv:
            for r in await q(
                conn,
                "SELECT import_batch_id, account_ref, detail FROM statement_check "
                "WHERE import_batch_id = ANY(:b) AND status <> 'ok' ORDER BY 1",
                b=legacy,
            ):
                issues = r.detail["issues"]
                auto = sum(1 for i in issues if i["fix"] and i["suggested"] and i["confidence"] >= 90)
                kinds = Counter(f"{i['kind']}/{i['fix']}" for i in issues)
                log.append(
                    f"  #{r.import_batch_id} {r.account_ref}: {dict(kinds)} auto={auto} {r.detail.get('message') or ''}"
                )
                for i in issues[:5]:
                    side = i["txn"] or i["row"]
                    log.append(
                        f"      {i['kind']:8} {side['date']} {side['amount']:>9} {side['description'][:35]!r} "
                        f"c={i['confidence']} | {i['hint']}"
                    )

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))
