"""Statement coverage: compare an official statement's rows for each of its accounts with everything the ledger holds
for that account over the statement period.

Equal totals mean the ledger is complete for the period. Otherwise statement rows are paired one-to-one with ledger
transactions and every unexplained difference becomes a proposed fix that trusts the statement:
  missing -> add the row         extra -> remove the ledger transaction
  amount  -> correct the amount  link  -> (before commit) don't insert a row the ledger already has
Ledger rows near a period edge or on another statement are listed as explained rather than proposed for removal;
when another statement dates one differently (or has a row for it nothing accounts for), the fix is that date.
"""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.filters import id_in
from ledger.imports.parsing import parse_date
from ledger.models import (
    Account,
    Attachment,
    DuplicatePair,
    ImportBatch,
    ImportRow,
    StatementCheck,
    Transaction,
    TransactionNote,
    TransactionSource,
)
from ledger.services.normalize import fingerprint

log = logging.getLogger(__name__)

WINDOW_DAYS = 7  # ledger rows fetched beyond the period so drifted dates can still pair up
MATCH_DAYS = 5  # date drift allowed between a statement row and the same ledger transaction
AMOUNT_DAYS = 4  # window for a same-payee pair whose amounts differ (pending vs. final, tips)
EDGE_DAYS = 3  # ledger rows this close to a period edge may belong to the neighbouring statement
SIMILAR = 0.6
CENT = Decimal("0.005")
ACTIONABLE = ("missing", "extra", "amount", "link")
AUTO_CONFIDENCE = 90  # suggested fixes at least this confident are applied without review


@dataclass
class _Row:
    id: int
    index: int
    date: date
    posted: date | None
    description: str
    merchant: str | None
    amount: Decimal
    decision: str
    incoming: bool  # before commit: the row will be inserted as a new transaction
    link: int | None  # the ledger transaction this row was matched to or created


@dataclass
class _Txn:
    id: int
    date: date
    description: str
    merchant: str | None
    amount: Decimal
    source_type: str


def _money(v: Decimal) -> str:
    return f"{v:.2f}"


def _sim(a_desc: str, a_merchant: str | None, b_desc: str, b_merchant: str | None) -> float:
    if a_merchant and a_merchant == b_merchant:
        return 1.0
    best = SequenceMatcher(None, (a_desc or "").lower(), (b_desc or "").lower()).ratio()
    if a_merchant and b_merchant:
        best = max(best, SequenceMatcher(None, a_merchant, b_merchant).ratio())
    return best


def _gap(s: _Row, t: _Txn) -> int:
    d = abs((t.date - s.date).days)
    return min(d, abs((t.date - s.posted).days)) if s.posted else d


def _row_out(s: _Row) -> dict:
    return {
        "row_id": s.id,
        "row_index": s.index,
        "date": s.date.isoformat(),
        "description": s.description,
        "amount": _money(s.amount),
    }


def _txn_out(t: _Txn) -> dict:
    return {
        "id": t.id,
        "date": t.date.isoformat(),
        "description": t.description,
        "amount": _money(t.amount),
        "source_type": t.source_type,
    }


def _issue(
    kind: str,
    fix: str | None,
    s: _Row | None,
    t: _Txn | None,
    effect: Decimal,
    hint: str | None = None,
    suggested: bool | None = None,
    confidence: int = 0,
) -> dict:
    return {
        "kind": kind,
        "fix": fix,
        "row_id": s.id if s else None,
        "transaction_id": t.id if t else None,
        "row": _row_out(s) if s else None,
        "txn": _txn_out(t) if t else None,
        # How applying the fix changes the ledger total for the period.
        "effect": _money(effect),
        "hint": hint,
        # Pre-selected in the review; None = decided once the whole account is checked.
        "suggested": suggested,
        # Percent; capped once the whole account is checked by how far the statement itself can be trusted.
        "confidence": confidence if fix else 0,
    }


async def _links(session: AsyncSession, batch: ImportBatch, rows: list[ImportRow]) -> dict[int, int]:
    """row id -> the ledger transaction it created or was matched to (before commit: its best duplicate candidate)."""
    ids = [r.id for r in rows]
    links: dict[int, int] = {}
    if batch.status == "committed":
        links.update({r.id: r.transaction_id for r in rows if r.transaction_id})
        for row_id, txn_id in (
            await session.execute(
                select(TransactionSource.import_row_id, TransactionSource.transaction_id)
                .where(id_in(TransactionSource.import_row_id, ids), TransactionSource.role == "matched")
                .order_by(TransactionSource.id)
            )
        ).all():
            links.setdefault(row_id, txn_id)
        statuses = ["confirmed_duplicate"]
        wanted = {r.id for r in rows if r.decision == "skip_duplicate" and r.id not in links}
    else:
        statuses = ["pending", "confirmed_duplicate"]
        wanted = {r.id for r in rows if r.decision in ("skip_duplicate", "pending")}
    if wanted:
        for row_id, txn_id in (
            await session.execute(
                select(DuplicatePair.import_row_id, DuplicatePair.txn_a_id)
                .where(id_in(DuplicatePair.import_row_id, list(wanted)), DuplicatePair.status.in_(statuses))
                .order_by(DuplicatePair.score.desc(), DuplicatePair.id)
            )
        ).all():
            links.setdefault(row_id, txn_id)
    return links


async def check_batch(session: AsyncSession, batch: ImportBatch) -> list[dict]:
    """One coverage result per statement account; [] for anything that isn't a reviewed or committed statement."""
    if batch.source_type != "document" or batch.status not in ("review", "committed"):
        return []
    meta = batch.doc_meta or {}
    rows = list(
        (await session.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_index))).all()
    )
    links = await _links(session, batch, rows)
    accounts = meta.get("accounts") or []
    groups: dict[str, list[ImportRow]] = defaultdict(list)
    for r in rows:
        groups[(r.raw.get("Account") or "") if accounts else ""].append(r)
    period = (parse_date(meta.get("period_start")), parse_date(meta.get("period_end")))
    account_map = batch.defaults.get("account_map") or {}
    out = []
    for a in accounts or [None]:
        ref = a["ref"] if a else ""
        recon = a.get("reconciliation") if a else meta.get("reconciliation")
        mapped = (account_map.get(ref) if a else None) or (batch.defaults.get("account_id") if len(accounts) <= 1 else None)
        out.append(
            await _check_account(session, batch, ref, groups.get(ref, []), links, period, recon, mapped, not accounts)
        )
    return out


async def _check_account(
    session: AsyncSession,
    batch: ImportBatch,
    ref: str,
    rows: list[ImportRow],
    links: dict[int, int],
    period: tuple[date | None, date | None],
    recon: dict | None,
    mapped: int | None,
    legacy: bool,
) -> dict:
    """`mapped`: the ledger account chosen for this statement account at import; `legacy`: the document was read
    before statements were split by account, so its rows may span several accounts."""
    review = batch.status == "review"
    valid = [r for r in rows if r.txn_date and r.amount is not None and r.decision != "invalid"]
    trusted = None if not recon else bool(recon.get("reconciles"))
    result = {
        "account_ref": ref,
        "account_id": None,
        "period_start": period[0],
        "period_end": period[1],
        "statement_total": None,
        "ledger_total": None,
        "difference": None,
        "statement_rows": len(valid),
        "ledger_rows": 0,
        "status": "unverified",
        "trusted": trusted,
        "detail": {"unreadable_rows": len(rows) - len(valid), "issues": [], "shifted": []},
    }
    if not valid and not all(period):
        result["detail"]["message"] = "No readable rows and no statement period"
        return result
    S = [
        _Row(
            id=r.id,
            index=r.row_index,
            date=r.txn_date,
            posted=r.posted_date,
            description=r.description or "",
            merchant=r.merchant,
            amount=r.amount,
            decision=r.decision,
            incoming=review and r.decision in ("insert", "keep"),
            link=None if review and r.decision in ("insert", "keep") else links.get(r.id),
        )
        for r in valid
    ]

    # Before commit, the account the user picked; afterwards, where the linked transactions actually live (accounts
    # get merged and re-linked after import).
    linked = [s.link for s in S if s.link]
    acct_of = dict(
        (
            await session.execute(
                select(Transaction.id, Transaction.account_id).where(
                    id_in(Transaction.id, linked), Transaction.deleted_at.is_(None)
                )
            )
        ).all()
    )
    picked = Counter(r.account_id for r in valid if r.account_id).most_common(1)
    held = Counter(acct_of[i] for i in linked if acct_of.get(i)).most_common(1)
    if mapped and not await session.scalar(select(Account.id).where(Account.id == mapped)):
        mapped = None
    order = (
        [picked and picked[0][0], mapped, held and held[0][0]]
        if review
        else [held and held[0][0], mapped, picked and picked[0][0]]
    )
    account_id = next((a for a in order if a), None)
    if account_id is None:
        result["detail"]["message"] = "Choose the ledger account for this statement account"
        return result
    result["account_id"] = account_id

    dates = [s.date for s in S] or list(period)
    ps, pe = period[0] or min(dates), period[1] or max(dates)
    if ps > pe:
        ps, pe = pe, ps
    result["period_start"], result["period_end"] = ps, pe
    result["detail"]["period_source"] = "statement" if all(period) else "rows"
    result["detail"]["rows_outside_period"] = sum(1 for d in dates if not ps <= d <= pe)

    L = {
        t.id: _Txn(t.id, t.txn_date, t.description, t.merchant, t.amount, t.source_type)
        for t in (
            await session.execute(
                select(
                    Transaction.id,
                    Transaction.txn_date,
                    Transaction.description,
                    Transaction.merchant,
                    Transaction.amount,
                    Transaction.source_type,
                ).where(
                    Transaction.account_id == account_id,
                    Transaction.deleted_at.is_(None),
                    or_(
                        Transaction.txn_date.between(ps - timedelta(days=WINDOW_DAYS), pe + timedelta(days=WINDOW_DAYS)),
                        id_in(Transaction.id, linked),
                    ),
                )
            )
        ).all()
    }

    claimed: dict[int, _Row] = {}
    matched: dict[int, _Txn] = {}

    def pair(s: _Row, t: _Txn) -> None:
        claimed[t.id] = s
        matched[s.id] = t

    issues: list[dict] = []
    # Rows recorded in another account. Before commit that duplicate match is suspect (the row goes missing here);
    # afterwards it's explained here and judged by that account's own statements.
    cross = {s.id: acct_of[s.link] for s in S if s.link and acct_of.get(s.link) not in (None, account_id)}
    cross_names = await account_names(session, list(set(cross.values())))
    away = {} if review else cross
    for s in S:
        if s.id in away:
            issues.append(_issue("elsewhere", None, s, None, Decimal(0), f"Recorded in {cross_names.get(away[s.id])}"))
        elif s.link and s.link in L and s.link not in claimed:
            pair(s, L[s.link])
    if legacy and len(away) >= max(3, len(S) // 4):
        result["detail"]["message"] = (
            f"{len(away)} rows match transactions in other accounts; this document probably covers several accounts"
        )
    # Ledger rows backed by another statement are that statement's to explain; don't pair them up here.
    others = await _on_other_statements(session, batch.id, list(L))

    # Same amount, nearby date: rows whose link went missing pair up silently; rows about to be inserted would
    # duplicate a ledger transaction no other statement row accounts for.
    open_rows = [s for s in S if s.id not in matched and s.id not in away and (not s.incoming or s.decision == "insert")]
    cands = sorted(
        (_gap(s, t), -_sim(s.description, s.merchant, t.description, t.merchant), s.incoming, s.index, t.id)
        for s in open_rows
        for t in L.values()
        if t.id not in claimed and t.id not in others and t.amount == s.amount and _gap(s, t) <= MATCH_DAYS
    )
    by_index = {s.index: s for s in open_rows}
    for gap, neg_sim, _, idx, tid in cands:
        s = by_index[idx]
        if s.id in matched or tid in claimed:
            continue
        pair(s, L[tid])
        if s.incoming:
            hint = f"Same amount {'on the same day' if gap == 0 else f'{gap} days apart'}; no other statement row matches it"
            similar = -neg_sim >= SIMILAR
            conf = 97 if gap == 0 and similar else 92 if gap == 0 or (gap <= 2 and similar) else 80
            issues.append(_issue("link", "link", s, L[tid], -s.amount, hint, confidence=conf))

    # Same payee and sign, different amount.
    open_rows = [s for s in open_rows if s.id not in matched]
    cands = []
    for s in open_rows:
        for t in L.values():
            if t.id in claimed or t.id in others or (t.amount > 0) != (s.amount > 0) or _gap(s, t) > AMOUNT_DAYS:
                continue
            sim = _sim(s.description, s.merchant, t.description, t.merchant)
            if sim >= SIMILAR:
                cands.append((_gap(s, t), -sim, s.index, t.id))
    for gap, neg_sim, idx, tid in sorted(cands):
        s = by_index[idx]
        if s.id in matched or tid in claimed:
            continue
        t = L[tid]
        pair(s, t)
        # Before commit the row also stops being inserted.
        effect = (s.amount - t.amount) - (s.amount if s.incoming else 0)
        conf = 85 if gap == 0 and -neg_sim >= 0.999 else 75 if -neg_sim >= 0.8 else 65
        hint = f"Ledger has {_money(t.amount)}, the statement {_money(s.amount)}"
        issues.append(_issue("amount", "amount", s, t, effect, hint, confidence=conf))

    for s in S:
        if s.id in matched or s.id in away or s.incoming:
            continue
        hint, conf = None, 90
        if s.link and s.link in claimed:
            hint = f"Was linked to the same transaction as statement row {claimed[s.link].index + 1}"
        elif s.id in cross:
            hint, conf = f"Marked a duplicate of a transaction in {cross_names.get(cross[s.id])}", 50
        elif s.link:
            hint, conf = "Was linked to a transaction that has since been deleted", 60
        issues.append(_issue("missing", "add", s, None, s.amount, hint, confidence=conf))

    free = [t for t in L.values() if t.id not in claimed and ps <= t.date <= pe]
    away_rows = [s for s in S if s.id in away]

    def near(t: _Txn) -> bool:
        return (t.date - ps).days < EDGE_DAYS or (pe - t.date).days < EDGE_DAYS

    def redate(row: _Row, name: str, t: _Txn, hint: str, conf: int) -> dict:
        effect = Decimal(0) if ps <= row.date <= pe else -t.amount
        issue = _issue("edge", "date", row, t, effect, hint, suggested=True, confidence=conf)
        issue["row"]["statement"] = name
        return issue

    elsewhere = await _rows_elsewhere(
        session,
        batch.id,
        account_id,
        {t.amount for t in free if t.id not in others and near(t)},
        ps - timedelta(days=WINDOW_DAYS),
        pe + timedelta(days=WINDOW_DAYS),
    )
    used: set[int] = set()
    for t in sorted(free, key=lambda t: (t.date, t.id)):
        twin = next(
            (
                c
                for c in L.values()
                if c.id in claimed and c.amount == t.amount and abs((c.date - t.date).days) <= MATCH_DAYS
            ),
            None,
        )
        filed = next((s for s in away_rows if s.amount == t.amount and _gap(s, t) <= MATCH_DAYS), None)
        hint = f"Same amount as {twin.description} on {twin.date.isoformat()}, which the statement lists" if twin else None
        twin_days = abs((twin.date - t.date).days) if twin else None
        twin_similar = bool(twin) and _sim(twin.description, twin.merchant, t.description, t.merchant) >= SIMILAR
        near_edge = near(t)
        # Rows of neighbouring statements with this amount: one nothing accounts for is probably this transaction.
        nearby = sorted(
            (holder is not None, _gap(r, t), -_sim(r.description, r.merchant, t.description, t.merchant), r.id, r, name)
            for r, name, holder in (elsewhere if near_edge and t.id not in others else [])
            if r.amount == t.amount and _gap(r, t) <= MATCH_DAYS and r.id not in used and holder != t.id
        )
        if t.id in others:
            name, row = others[t.id]
            if row and row.date != t.date:
                issues.append(redate(row, name, t, f"Listed on {name} dated {row.date.isoformat()}", 95))
            else:
                issues.append(_issue("edge", None, None, t, Decimal(0), f"Listed on {name}"))
        elif nearby and not nearby[0][0]:
            *_, row, name = nearby[0]
            used.add(row.id)
            hint = f"{name} lists it on {row.date.isoformat()}; nothing else accounts for it"
            issues.append(redate(row, name, t, hint, 90))
        elif nearby and not (twin and twin_days <= 1):
            *_, row, name = nearby[0]
            hint = f"{name} lists {_money(row.amount)} on {row.date.isoformat()} as another transaction; likely a duplicate"
            issues.append(_issue("edge", "remove", None, t, -t.amount, hint, confidence=60))
        # A copy the statement lists within a day means a duplicate, not a transaction from the next statement.
        elif near_edge and not (twin and twin_days <= 1):
            hint = hint or "Close to the statement's start or end"
            issues.append(_issue("edge", "remove", None, t, -t.amount, hint, confidence=20))
        elif filed:
            # The statement's row was matched to a copy in another account; either copy could be the wrong one.
            hint = (
                f"Statement row {filed.index + 1} ({filed.description}) is recorded in "
                f"{cross_names.get(away[filed.id])}; one of the two copies is probably misfiled"
            )
            issues.append(_issue("extra", "remove", None, t, -t.amount, hint, suggested=False, confidence=30))
        else:
            if twin and twin_days <= 1:
                conf = 95 if twin_similar else 88
            else:
                conf = 85 if twin and twin_similar else 75
            issues.append(_issue("extra", "remove", None, t, -t.amount, hint, confidence=conf))

    # Loan and some savings statements print amounts with the opposite sign to the ledger.
    missing = [i for i in issues if i["kind"] == "missing"]
    extras = [t for t in free if t.id not in others]
    flipped = sum(
        1
        for i in missing
        if any(
            t.amount == -Decimal(i["row"]["amount"])
            and abs((t.date - date.fromisoformat(i["row"]["date"])).days) <= MATCH_DAYS
            for t in extras
        )
    )
    reversed_signs = flipped >= max(2, len(missing) // 2)
    if reversed_signs:
        result["detail"]["message"] = (
            f"{flipped} statement rows match ledger transactions with the opposite sign; "
            "the statement's sign convention looks reversed, so no fixes are suggested"
        )

    shifted = [
        {"row": _row_out(s), "txn": _txn_out(matched[s.id])}
        for s in S
        if s.id in matched and not ps <= matched[s.id].date <= pe
    ]
    for i in issues:
        if i["suggested"] is None:
            i["suggested"] = i["kind"] in ACTIONABLE and trusted is not False and not reversed_signs
    # Without the statement's own balances to vouch for its rows, a person should look first.
    cap = 99 if trusted else 85 if trusted is None else 0
    if reversed_signs:
        cap = 0
    elif result["detail"].get("message"):
        cap = min(cap, 50)
    for i in issues:
        i["confidence"] = min(i["confidence"], cap)

    stmt_total = sum((s.amount for s in S), Decimal(0))
    in_period = [t for t in L.values() if ps <= t.date <= pe]
    ledger_total = sum((t.amount for t in in_period), Decimal(0)) + sum((s.amount for s in S if s.incoming), Decimal(0))
    diff = stmt_total - ledger_total
    actionable = any(i["kind"] in ACTIONABLE for i in issues)
    status = "mismatch" if actionable else "ok" if abs(diff) < CENT and not issues else "explained"
    result.update(
        statement_total=stmt_total,
        ledger_total=ledger_total,
        difference=diff,
        ledger_rows=len(in_period) + sum(1 for s in S if s.incoming),
        status=status,
    )
    result["detail"].update(issues=issues, shifted=shifted, totals_match=abs(diff) < CENT)
    return result


_ROW_COLS = (
    ImportRow.id,
    ImportRow.row_index,
    ImportRow.txn_date,
    ImportRow.posted_date,
    ImportRow.description,
    ImportRow.merchant,
    ImportRow.amount,
    ImportRow.decision,
)


def _other_row(r) -> _Row:
    return _Row(
        r.id, r.row_index, r.txn_date, r.posted_date, r.description or "", r.merchant, r.amount, r.decision, False, None
    )


async def _on_other_statements(
    session: AsyncSession, batch_id: int, txn_ids: list[int]
) -> dict[int, tuple[str, _Row | None]]:
    """Ledger transactions created by or matched to rows of other statement documents -> (that document's name, its
    row when those statements agree on one date)."""
    if not txn_ids:
        return {}
    q = (
        select(TransactionSource.transaction_id, Attachment.filename, *_ROW_COLS)
        .join(ImportBatch, ImportBatch.id == TransactionSource.import_batch_id)
        .join(Attachment, Attachment.id == ImportBatch.attachment_id)
        .outerjoin(ImportRow, ImportRow.id == TransactionSource.import_row_id)
        .where(
            id_in(TransactionSource.transaction_id, txn_ids),
            TransactionSource.import_batch_id != batch_id,
            ImportBatch.source_type == "document",
            ImportBatch.status == "committed",
        )
        .order_by(Attachment.filename, ImportRow.id)
    )
    found = defaultdict(list)
    for r in (await session.execute(q)).all():
        found[r.transaction_id].append(r)
    out = {}
    for tid, hits in found.items():
        dated = [r for r in hits if r.id and r.txn_date and r.amount is not None]
        one = len({r.txn_date for r in dated}) == 1 and len(dated) == len(hits)
        out[tid] = (hits[0].filename, _other_row(dated[0]) if one else None)
    return out


async def _rows_elsewhere(
    session: AsyncSession, batch_id: int, account_id: int, amounts: set[Decimal], lo: date, hi: date
) -> list[tuple[_Row, str, int | None]]:
    """Rows of other statements for this account with one of these amounts -> (row, document name, the live ledger
    transaction that accounts for it or None)."""
    if not amounts:
        return []
    found = (
        await session.execute(
            select(*_ROW_COLS, ImportRow.account_id, ImportRow.transaction_id, Attachment.filename)
            .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
            .join(Attachment, Attachment.id == ImportBatch.attachment_id)
            .where(
                ImportRow.batch_id != batch_id,
                ImportBatch.source_type == "document",
                ImportBatch.status == "committed",
                ImportRow.decision != "invalid",
                ImportRow.amount.in_(list(amounts)),
                ImportRow.txn_date.between(lo, hi),
            )
        )
    ).all()
    if not found:
        return []
    held: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row_id, txn_id, acct in (
        await session.execute(
            select(TransactionSource.import_row_id, Transaction.id, Transaction.account_id)
            .join(Transaction, Transaction.id == TransactionSource.transaction_id)
            .where(id_in(TransactionSource.import_row_id, [r.id for r in found]), Transaction.deleted_at.is_(None))
        )
    ).all():
        held[row_id].append((txn_id, acct))
    direct = [r.transaction_id for r in found if r.transaction_id]
    live = dict(
        (
            await session.execute(
                select(Transaction.id, Transaction.account_id).where(
                    id_in(Transaction.id, direct), Transaction.deleted_at.is_(None)
                )
            )
        ).all()
    )
    out = []
    for r in found:
        h = held[r.id] + ([(r.transaction_id, live[r.transaction_id])] if r.transaction_id in live else [])
        mine = next((tid for tid, acct in h if acct == account_id), None)
        # Rows recorded in another account, or read into another account and never linked, belong elsewhere.
        if mine is None and (h or r.account_id != account_id):
            continue
        out.append((_other_row(r), r.filename, mine))
    return out


def _store(batch: ImportBatch, c: dict) -> dict:
    return {**c, "import_batch_id": batch.id, "account_ref": c["account_ref"][:100], "checked_at": datetime.now(UTC)}


async def save_checks(session: AsyncSession, batch: ImportBatch, checks: list[dict] | None = None) -> list[dict]:
    """Persist the batch's coverage checks (recomputed unless given); returns them."""
    if checks is None:
        checks = await check_batch(session, batch)
    await session.execute(delete(StatementCheck).where(StatementCheck.import_batch_id == batch.id))
    if checks:
        await session.execute(insert(StatementCheck).values([_store(batch, c) for c in checks]))
    return checks


async def auto_fix(session: AsyncSession, batch: ImportBatch) -> dict:
    """Check the batch, apply the suggested fixes confident enough to need no review, and store the result."""
    checks = await check_batch(session, batch)
    picks = [
        {"fix": i["fix"], "row_id": i["row_id"], "transaction_id": i["transaction_id"]}
        for c in checks
        for i in c["detail"]["issues"]
        if i["fix"] and i["suggested"] and i["confidence"] >= AUTO_CONFIDENCE
    ]
    if not picks:
        return {"applied": 0, "skipped": 0, "new_ids": [], "checks": await save_checks(session, batch, checks)}
    result = await apply_fixes(session, batch, picks, auto=True)
    if result["applied"]:
        batch.stats = {**batch.stats, "auto_fixed": batch.stats.get("auto_fixed", 0) + result["applied"]}
    return result


def gate_message(checks: list[dict], labels: dict[str, str] | None = None) -> str | None:
    """Why a statement shouldn't be committed without a person looking at it (None when it checks out)."""
    for c in checks:
        if c["status"] != "mismatch" or c["trusted"] is False:
            continue
        n = sum(1 for i in c["detail"]["issues"] if i["kind"] in ACTIONABLE)
        who = (labels or {}).get(c["account_ref"]) or (f"···{c['account_ref']}" if c["account_ref"] else "Statement")
        return (
            f"{who}: statement rows total {_money(c['statement_total'])} but the ledger would have "
            f"{_money(c['ledger_total'])} for the period ({n} difference{'s' if n != 1 else ''} to review)"
        )
    return None


# ---------- applying fixes ----------
async def apply_fixes(session: AsyncSession, batch: ImportBatch, fixes: list[dict], auto: bool = False) -> dict:
    """Apply fixes chosen from the batch's current check results; stale or unknown ones are skipped."""
    from ledger.imports.service import transaction_factory
    from ledger.services.categorize import apply_rules

    current = await check_batch(session, batch)
    offered = {
        (i["fix"], i["row_id"], i["transaction_id"]): (c, i) for c in current for i in c["detail"]["issues"] if i["fix"]
    }
    filename = batch.attachment.filename if batch.attachment else f"import #{batch.id}"
    tag = " (applied automatically)" if auto else ""
    now = datetime.now(UTC)
    make = None
    applied = skipped = 0
    new_ids: list[int] = []
    touched: set[int] = set()
    for f in fixes:
        hit = offered.pop((f["fix"], f.get("row_id"), f.get("transaction_id")), None)
        if hit is None:
            skipped += 1
            continue
        c, issue = hit
        row = await session.get(ImportRow, issue["row_id"]) if issue["row_id"] else None
        txn = await session.get(Transaction, issue["transaction_id"]) if issue["transaction_id"] else None
        if issue["fix"] == "add":
            if batch.status == "review":
                row.decision = "keep"
            else:
                make = make or await transaction_factory(session, batch)
                t = make(row, c["account_id"])
                session.add(t)
                await session.flush()
                await _relink(session, batch, row, t.id, "created")
                await session.execute(
                    update(DuplicatePair)
                    .where(DuplicatePair.import_row_id == row.id)
                    .values(status="confirmed_separate", txn_b_id=t.id, decided_at=now)
                )
                row.transaction_id, row.decision = t.id, "keep"
                new_ids.append(t.id)
        elif issue["fix"] == "remove":
            txn.deleted_at = now
            await session.execute(
                update(Transaction).where(Transaction.transfer_match_id == txn.id).values(transfer_match_id=None)
            )
            txn.transfer_match_id = None
            _note(session, batch, txn.id, f"Removed: not listed on the statement {filename}{tag}")
        elif issue["fix"] == "amount":
            body = f"Amount corrected from {_money(txn.amount)} to {_money(row.amount)} per {filename}{tag}"
            _note(session, batch, txn.id, body)
            txn.amount = row.amount
            txn.fingerprint = fingerprint(txn.account_id, txn.txn_date, txn.amount, txn.description)
            await _link_row(session, batch, row, txn.id, now)
        elif issue["fix"] == "link":
            await _link_row(session, batch, row, txn.id, now)
        elif issue["fix"] == "date":
            other = await session.get(ImportBatch, row.batch_id)
            name = other.attachment.filename if other.attachment else f"import #{other.id}"
            body = f"Date corrected from {txn.txn_date.isoformat()} to {row.txn_date.isoformat()} per {name}{tag}"
            _note(session, other, txn.id, body)
            txn.txn_date = row.txn_date
            txn.fingerprint = fingerprint(txn.account_id, txn.txn_date, txn.amount, txn.description)
            await _add_source(session, other, row, txn.id, "matched")
            touched.add(other.id)
        applied += 1
    await session.flush()
    if new_ids:
        await apply_rules(session, new_ids)
    for other_id in touched - {batch.id}:
        await save_checks(session, await session.get(ImportBatch, other_id))
    checks = await save_checks(session, batch)
    return {"applied": applied, "skipped": skipped, "new_ids": new_ids, "checks": checks}


def _note(session: AsyncSession, batch: ImportBatch, txn_id: int, body: str) -> None:
    session.add(
        TransactionNote(
            transaction_id=txn_id, body=body, source="import", import_batch_id=batch.id, attachment_id=batch.attachment_id
        )
    )


async def _relink(session: AsyncSession, batch: ImportBatch, row: ImportRow, txn_id: int, role: str) -> None:
    await session.execute(delete(TransactionSource).where(TransactionSource.import_row_id == row.id))
    await _add_source(session, batch, row, txn_id, role)


async def _add_source(session: AsyncSession, batch: ImportBatch, row: ImportRow, txn_id: int, role: str) -> None:
    await session.execute(
        insert(TransactionSource)
        .values(
            transaction_id=txn_id,
            role=role,
            import_batch_id=batch.id,
            import_row_id=row.id,
            attachment_id=batch.attachment_id,
            txn_date=row.txn_date,
            description=row.description,
            amount=row.amount,
        )
        .on_conflict_do_nothing()
    )


async def _link_row(session: AsyncSession, batch: ImportBatch, row: ImportRow, txn_id: int, now: datetime) -> None:
    """Record the statement row as a source of an existing transaction (before commit: mark it a duplicate)."""
    if batch.status == "review":
        await session.execute(
            delete(DuplicatePair).where(DuplicatePair.import_row_id == row.id, DuplicatePair.txn_a_id == txn_id)
        )
        session.add(
            DuplicatePair(
                txn_a_id=txn_id,
                import_row_id=row.id,
                score=Decimal("0.5"),
                reasons=["Matched by the statement check"],
                status="confirmed_duplicate",
                decided_at=now,
            )
        )
        row.decision = "skip_duplicate"
    else:
        await _relink(session, batch, row, txn_id, "matched")


# ---------- listing / bulk ----------
async def check_all(session: AsyncSession, progress=None, apply: bool = False) -> dict:
    """Re-run the check for every committed statement document; `apply` also applies the confident fixes."""
    ids = (
        await session.scalars(
            select(ImportBatch.id)
            .where(ImportBatch.source_type == "document", ImportBatch.status == "committed")
            .order_by(ImportBatch.id)
        )
    ).all()
    counts: Counter = Counter()
    new_ids: list[int] = []
    for n, bid in enumerate(ids, 1):
        batch = await session.get(ImportBatch, bid)
        try:
            if apply:
                r = await auto_fix(session, batch)
                checks = r["checks"]
            else:
                checks = await save_checks(session, batch)
            for c in checks:
                counts[c["status"]] += 1
            await session.commit()
            if apply:
                counts["fixed"] += r["applied"]
                new_ids += r["new_ids"]
        except Exception:
            log.exception("Statement check failed for batch %s", bid)
            await session.rollback()
            counts["failed"] += 1
        if progress and (n % 10 == 0 or n == len(ids)):
            verb = "Checked and fixed" if apply else "Checked"
            fixed = f" · {counts['fixed']} fixes applied" if apply else ""
            await progress(n / len(ids), f"{verb} {n} of {len(ids)} statements{fixed}")
    return {"statements": len(ids), **counts, "new_ids": new_ids}


async def account_names(session: AsyncSession, ids: list[int]) -> dict[int, str]:
    return dict((await session.execute(select(Account.id, Account.name).where(id_in(Account.id, ids)))).all())
