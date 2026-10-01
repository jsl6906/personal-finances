"""Duplicate detection shared by imports, standalone scans, and (later) source sync and backfill.

Tier 1: exact fingerprint / external id match. Tier 2: same amount within a date window, scored on date gap,
account and description similarity (pg_trgm). Tier 3: Gemini adjudicates ambiguous pairs.
Pairs confirmed as separate are kept so they are never flagged again.
"""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.ai.client import AIUnavailable
from ledger.models import Account, DuplicatePair, ImportRow, Transaction

log = logging.getLogger(__name__)

WINDOW_DAYS = 3
THRESHOLD = 0.55
AI_BATCH = 40


def score_pair(dd: int, same_acct: bool | None, sim: float, exact: bool) -> tuple[float, list[str]]:
    if exact:
        return 1.0, ["Exact match on account, date, amount and merchant"]
    s, reasons = 0.35, ["Same amount"]
    if dd == 0:
        s += 0.25
        reasons.append("Same date")
    else:
        s += 0.15 if dd == 1 else 0.05
        reasons.append(f"Dates {dd} day{'s' if dd > 1 else ''} apart")
    if same_acct is True:
        s += 0.2
        reasons.append("Same account")
    elif same_acct is None:
        s += 0.1
        reasons.append("Account unknown on one side")
    else:
        reasons.append("Different accounts")
    s += 0.2 * sim
    reasons.append(f"Description similarity {sim:.0%}")
    return round(min(s, 0.99), 3), reasons


_IMPORT_CANDIDATES = text(
    """--sql
    SELECT r.id AS row_id, t.id AS txn_id, abs(r.txn_date - t.txn_date) AS dd,
           CASE WHEN r.account_id IS NULL OR t.account_id IS NULL THEN NULL
                ELSE r.account_id = t.account_id END AS same_acct,
           similarity(coalesce(r.merchant, ''), coalesce(t.merchant, '')) AS sim,
           (r.fingerprint = t.fingerprint
             OR (r.external_id IS NOT NULL AND r.external_id = t.external_id
                 AND r.account_id IS NOT DISTINCT FROM t.account_id)) AS exact
    FROM import_row r
    JOIN "transaction" t
      ON t.amount = r.amount
     AND t.txn_date BETWEEN r.txn_date - CAST(:w AS integer) AND r.txn_date + CAST(:w AS integer)
     AND t.deleted_at IS NULL
    WHERE r.batch_id = :b AND r.decision = 'pending'
    """
)


async def detect_import_duplicates(session: AsyncSession, batch_id: int, window: int = WINDOW_DAYS) -> dict:
    """Flag incoming rows that match existing transactions; exact matches are auto-skipped."""
    best: dict[int, tuple[float, int, list[str], bool]] = {}
    for r in (await session.execute(_IMPORT_CANDIDATES, {"b": batch_id, "w": window})).mappings():
        sc, reasons = score_pair(r["dd"], r["same_acct"], float(r["sim"]), bool(r["exact"]))
        if sc >= THRESHOLD and (r["row_id"] not in best or sc > best[r["row_id"]][0]):
            best[r["row_id"]] = (sc, r["txn_id"], reasons, bool(r["exact"]))

    exact = 0
    for row_id, (sc, txn_id, reasons, is_exact) in best.items():
        await session.execute(
            insert(DuplicatePair)
            .values(
                txn_a_id=txn_id,
                import_row_id=row_id,
                score=Decimal(str(sc)),
                reasons=reasons,
                status="confirmed_duplicate" if is_exact else "pending",
                decided_at=datetime.now(UTC) if is_exact else None,
            )
            .on_conflict_do_nothing()
        )
        if is_exact:
            exact += 1
            await session.execute(update(ImportRow).where(ImportRow.id == row_id).values(decision="skip_duplicate"))
    await session.execute(
        update(ImportRow)
        .where(ImportRow.batch_id == batch_id, ImportRow.decision == "pending", ImportRow.id.not_in(list(best) or [0]))
        .values(decision="insert")
    )
    return {"exact_duplicates": exact, "possible_duplicates": len(best) - exact}


_SCAN_CANDIDATES = text(
    """--sql
    SELECT a.id AS a_id, b.id AS b_id, abs(a.txn_date - b.txn_date) AS dd,
           CASE WHEN a.account_id IS NULL OR b.account_id IS NULL THEN NULL
                ELSE a.account_id = b.account_id END AS same_acct,
           similarity(coalesce(a.merchant, ''), coalesce(b.merchant, '')) AS sim,
           (a.fingerprint = b.fingerprint
             OR (a.external_id IS NOT NULL AND a.external_id = b.external_id
                 AND a.account_id IS NOT DISTINCT FROM b.account_id)) AS exact
    FROM "transaction" a
    JOIN "transaction" b
      ON b.amount = a.amount
     AND b.id > a.id
     AND b.txn_date BETWEEN a.txn_date - CAST(:w AS integer) AND a.txn_date + CAST(:w AS integer)
     AND b.deleted_at IS NULL
    WHERE a.deleted_at IS NULL
      AND a.amount <> 0
      AND (CAST(:since AS date) IS NULL OR a.txn_date >= CAST(:since AS date) OR b.txn_date >= CAST(:since AS date))
      -- lines from the same statement/import are distinct by construction
      AND NOT (a.import_batch_id IS NOT NULL AND a.import_batch_id = b.import_batch_id)
      AND NOT EXISTS (SELECT 1 FROM duplicate_pair p WHERE p.txn_a_id = a.id AND p.txn_b_id = b.id)
    """
)


async def scan_transactions(session: AsyncSession, since: date | str | None = None, window: int = WINDOW_DAYS) -> dict:
    if isinstance(since, str):
        since = date.fromisoformat(since)
    found = 0
    for r in (await session.execute(_SCAN_CANDIDATES, {"w": window, "since": since})).mappings():
        sc, reasons = score_pair(r["dd"], r["same_acct"], float(r["sim"]), bool(r["exact"]))
        if sc < THRESHOLD:
            continue
        res = await session.execute(
            insert(DuplicatePair)
            .values(txn_a_id=r["a_id"], txn_b_id=r["b_id"], score=Decimal(str(sc)), reasons=reasons)
            .on_conflict_do_nothing()
        )
        found += res.rowcount or 0
    kept = await session.scalar(
        select(func.count())
        .select_from(DuplicatePair)
        .where(DuplicatePair.status == "confirmed_separate", DuplicatePair.txn_b_id.is_not(None))
    )
    return {"new_candidates": found, "confirmed_separate_kept": kept or 0}


def _txn_side(t: Transaction | ImportRow, accounts: dict[int, str]) -> dict:
    return {
        "date": t.txn_date.isoformat() if t.txn_date else None,
        "amount": t.amount,
        "description": t.description,
        "account": accounts.get(t.account_id) if t.account_id else None,
    }


async def adjudicate_pending(session: AsyncSession, pair_ids: list[int], progress=None) -> int:
    """Ask Gemini about ambiguous pending pairs; returns how many got a verdict. Missing AI is not an error."""
    from ledger.ai.imports import adjudicate_pairs

    pairs = (
        await session.scalars(
            select(DuplicatePair).where(
                DuplicatePair.id.in_(pair_ids), DuplicatePair.status == "pending", DuplicatePair.score < 0.95
            )
        )
    ).all()
    if not pairs:
        return 0
    accounts = dict((await session.execute(select(Account.id, Account.name))).all())
    judged = 0
    for start in range(0, len(pairs), AI_BATCH):
        chunk = pairs[start : start + AI_BATCH]
        payload = []
        for p in chunk:
            a = await session.get(Transaction, p.txn_a_id)
            b = (
                await session.get(ImportRow, p.import_row_id)
                if p.import_row_id
                else await session.get(Transaction, p.txn_b_id)
            )
            if a and b:
                payload.append({"id": p.id, "a": _txn_side(a, accounts), "b": _txn_side(b, accounts)})
        try:
            verdicts = await adjudicate_pairs(payload)
        except AIUnavailable:
            return judged
        except Exception:
            log.exception("Duplicate adjudication failed; continuing without AI verdicts")
            return judged
        for p in chunk:
            v = verdicts.get(p.id)
            if v:
                p.ai_probability = Decimal(str(round(v.probability_same, 3)))
                p.ai_reason = v.reason[:1000]
                judged += 1
        await session.commit()
        if progress:
            await progress(min(1.0, (start + len(chunk)) / len(pairs)))
    return judged
