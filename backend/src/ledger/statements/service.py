"""Bills & statements: upload -> Gemini extraction -> suggested series/transaction links -> user approval."""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.imports.parsing import detect_kind, parse_date
from ledger.imports.service import ImportError_, store_attachment
from ledger.models import (
    Attachment,
    Category,
    Statement,
    StatementSeries,
    StatementUsage,
    Tag,
    Transaction,
    statement_transaction,
    transaction_tag,
)

log = logging.getLogger(__name__)


async def create_statement(
    session: AsyncSession, data: bytes, filename: str, content_type: str | None, transaction_ids: list[int] | None = None
) -> Statement:
    from ledger.config import get_settings
    from ledger.jobs.worker import enqueue

    kind = detect_kind(filename)
    # Extension-less text/plain is an email body (receipts that arrive as the message itself).
    if kind != "document" and not (kind is None and content_type == "text/plain"):
        raise ImportError_("Upload a PDF or image of the bill or statement")
    if len(data) > get_settings().max_upload_mb * 1024 * 1024:
        raise ImportError_(f"File is larger than {get_settings().max_upload_mb} MB")
    att = await store_attachment(session, data, filename, content_type, source="statement")
    st = Statement(attachment_id=att.id, suggestion={"preset_transaction_ids": transaction_ids or []})
    session.add(st)
    await session.flush()
    job = await enqueue(session, "extract_bill", {"statement_id": st.id})
    st.job_id = job.id
    return st


async def _match_series(session: AsyncSession, vendor: str | None, service: str | None) -> tuple[int | None, str]:
    rows = (await session.scalars(select(StatementSeries).where(StatementSeries.is_active))).all()
    v, s = (vendor or "").lower(), (service or "").lower()
    for sr in rows:
        if v and s and (sr.vendor or "").lower() == v and (sr.service_type or "").lower() == s:
            return sr.id, f"Same vendor and service as {sr.name}"
    for sr in rows:
        if s and (sr.name.lower() == s or (sr.service_type or "").lower() == s):
            return sr.id, f"Service type matches {sr.name}"
    for sr in rows:
        if v and (sr.vendor or "").lower() == v:
            return sr.id, f"Vendor matches {sr.name}"
    return None, "No existing series matches; a new one will be created"


_CANDIDATES = text(
    """--sql
    SELECT t.id, t.txn_date, t.amount, t.description,
           similarity(coalesce(t.merchant, ''), lower(CAST(:vendor AS text))) AS sim,
           abs(t.amount + CAST(:amt AS numeric)) AS diff,
           EXISTS (SELECT 1 FROM statement_transaction x WHERE x.transaction_id = t.id) AS already_linked,
           EXISTS (
               SELECT 1 FROM statement_transaction x
               JOIN statement s ON s.id = x.statement_id
               JOIN "transaction" t2 ON t2.id = x.transaction_id
               WHERE s.series_id = CAST(:series AS integer) AND t2.merchant = t.merchant
           ) AS series_merchant
    FROM "transaction" t
    WHERE t.deleted_at IS NULL
      AND t.amount < 0
      AND t.txn_date BETWEEN :lo AND :hi
      AND (CAST(:amt AS numeric) IS NULL
           OR abs(t.amount + CAST(:amt AS numeric)) <= greatest(1.00, CAST(:amt AS numeric) * 0.02))
    ORDER BY t.txn_date
    LIMIT 200
    """
)


async def link_candidates(session: AsyncSession, st: Statement, series_id: int | None) -> list[dict]:
    anchor = st.statement_date or st.period_end or st.due_date or date.today()
    lo = anchor - timedelta(days=10)
    hi = (st.due_date or anchor + timedelta(days=30)) + timedelta(days=20)
    rows = (
        await session.execute(
            _CANDIDATES,
            {"vendor": st.vendor or "", "amt": st.amount_due, "series": series_id, "lo": lo, "hi": hi},
        )
    ).mappings()
    out = []
    for r in rows:
        sim = float(r["sim"])
        if st.amount_due is None and sim < 0.3:
            continue
        score, reasons = 0.0, []
        if st.amount_due is not None:
            exact = r["diff"] == 0
            score += 0.5 if exact else 0.3
            reasons.append("Exact amount" if exact else f"Amount within {money(r['diff'])}")
        score += 0.3 * sim
        if sim >= 0.3:
            reasons.append(f"Payee resembles {st.vendor}")
        if r["series_merchant"]:
            score += 0.2
            reasons.append("Same payee as earlier bills in this series")
        if r["already_linked"]:
            score -= 0.3
            reasons.append("Already linked to another statement")
        out.append(
            {
                "transaction_id": r["id"],
                "date": r["txn_date"].isoformat(),
                "amount": str(r["amount"]),
                "description": r["description"],
                "score": round(max(score, 0), 3),
                "reason": "; ".join(reasons),
            }
        )
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:3]


def money(v) -> str:
    return f"${Decimal(v):,.2f}"


async def _category_for(session: AsyncSession, service: str | None) -> int | None:
    if not service:
        return None
    return await session.scalar(select(Category.id).where(func.lower(Category.name) == service.lower()))


async def extract_into_statement(session: AsyncSession, st: Statement) -> dict:
    from ledger.ai.statements import extract_bill

    att = st.attachment
    content = await session.scalar(select(Attachment.content).where(Attachment.id == att.id))
    r = await extract_bill(content, att.mime_type, att.filename)
    st.document_type, st.vendor, st.summary = r.document_type, r.vendor, r.summary
    st.account_ref = r.account_last4
    st.statement_date, st.due_date = parse_date(r.statement_date), parse_date(r.due_date)
    st.period_start, st.period_end = parse_date(r.period_start), parse_date(r.period_end)
    st.amount_due = Decimal(str(round(r.amount_due, 2))) if r.amount_due is not None else None
    st.extracted = r.model_dump()
    st.usage = [
        StatementUsage(metric=u.metric[:60], value=Decimal(str(u.value)), unit=(u.unit or None), is_primary=u.is_primary)
        for u in r.usage
    ]
    series_id, series_reason = await _match_series(session, r.vendor, r.service_type)
    candidates = await link_candidates(session, st, series_id)
    presets = [i for i in st.suggestion.get("preset_transaction_ids", []) if i]
    for tid in presets:
        t = await session.get(Transaction, tid)
        if t and not any(c["transaction_id"] == tid for c in candidates):
            candidates.insert(
                0,
                {
                    "transaction_id": tid,
                    "date": t.txn_date.isoformat(),
                    "amount": str(t.amount),
                    "description": t.description,
                    "score": 1.0,
                    "reason": "Chosen when uploading",
                },
            )
    st.suggestion = {
        **st.suggestion,
        "series_id": series_id,
        "series_reason": series_reason,
        "new_series_name": None if series_id else (r.service_type or r.vendor or "Other bills"),
        "new_series_category_id": None if series_id else await _category_for(session, r.service_type),
        "service_type": r.service_type,
        "candidates": candidates,
        "transaction_ids": presets
        or ([candidates[0]["transaction_id"]] if candidates and candidates[0]["score"] >= 0.6 else []),
    }
    st.status, st.error = "suggested", None
    return {"candidates": len(candidates), "series_id": series_id}


async def _series_tag(session: AsyncSession, name: str) -> int:
    await session.execute(insert(Tag).values(name=name[:80]).on_conflict_do_nothing())
    return await session.scalar(select(Tag.id).where(Tag.name == name[:80]))


async def approve_statement(session: AsyncSession, st: Statement, body) -> Statement:
    """body: schemas.StatementApprove."""
    for f in ("vendor", "period_start", "period_end", "statement_date", "due_date", "amount_due"):
        if f in body.model_fields_set:
            setattr(st, f, getattr(body, f))
    if body.usage is not None:
        st.usage = [StatementUsage(metric=u.metric, value=u.value, unit=u.unit, is_primary=u.is_primary) for u in body.usage]

    series_id = body.series_id if "series_id" in body.model_fields_set else None
    if series_id is None and not body.new_series_name and "series_id" not in body.model_fields_set:
        series_id = st.suggestion.get("series_id")
    if series_id:
        series = await session.get(StatementSeries, series_id)
        if series is None:
            raise ImportError_("Series not found")
    else:
        name = (body.new_series_name or st.suggestion.get("new_series_name") or st.vendor or "Other bills").strip()
        series = await session.scalar(select(StatementSeries).where(func.lower(StatementSeries.name) == name.lower()))
        if series is None:
            series = StatementSeries(
                name=name[:120],
                vendor=st.vendor,
                service_type=st.suggestion.get("service_type"),
                category_id=body.new_series_category_id
                if body.new_series_category_id is not None
                else st.suggestion.get("new_series_category_id"),
            )
            session.add(series)
            await session.flush()
    if series.tag_id is None:
        series.tag_id = await _series_tag(session, series.name)
    primary = next((u for u in st.usage if u.is_primary), st.usage[0] if st.usage else None)
    if primary and not series.primary_metric:
        series.primary_metric, series.unit = primary.metric, primary.unit
    if not series.vendor and st.vendor:
        series.vendor = st.vendor
    st.series_id = series.id

    ids = list(dict.fromkeys(body.transaction_ids))
    if ids:
        found = (
            await session.scalars(select(Transaction.id).where(Transaction.id.in_(ids), Transaction.deleted_at.is_(None)))
        ).all()
        if len(found) != len(ids):
            raise ImportError_("Unknown transaction id")
    await session.execute(delete(statement_transaction).where(statement_transaction.c.statement_id == st.id))
    if ids:
        await session.execute(
            insert(statement_transaction).values([{"statement_id": st.id, "transaction_id": i} for i in ids])
        )
        await session.execute(
            insert(transaction_tag)
            .values([{"transaction_id": i, "tag_id": series.tag_id} for i in ids])
            .on_conflict_do_nothing()
        )
        if series.category_id:
            await session.execute(
                Transaction.__table__.update()
                .where(Transaction.id.in_(ids), Transaction.category_id.is_(None))
                .values(category_id=series.category_id, category_source="statement")
            )
    st.status, st.approved_at = "approved", datetime.now(UTC)
    return st


async def linked_transactions(session: AsyncSession, statement_ids: list[int]) -> dict[int, list[Transaction]]:
    if not statement_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(statement_transaction.c.statement_id, Transaction)
                .join(Transaction, Transaction.id == statement_transaction.c.transaction_id)
                .where(statement_transaction.c.statement_id.in_(statement_ids), Transaction.deleted_at.is_(None))
                .order_by(Transaction.txn_date)
            )
        )
        .unique()
        .all()
    )
    out: dict[int, list[Transaction]] = {}
    for sid, t in rows:
        out.setdefault(sid, []).append(t)
    return out
