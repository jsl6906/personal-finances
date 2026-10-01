from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.api.imports import _brief
from ledger.config import get_settings
from ledger.db.engine import get_session
from ledger.imports.service import ImportError_
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import Category, Statement, StatementSeries, StatementUsage, statement_transaction
from ledger.schemas import SeriesIn, SeriesOut, SeriesPoint, StatementApprove, StatementOut, UsageOut
from ledger.statements.service import approve_statement, create_statement, linked_transactions

router = APIRouter(tags=["statements"])


def _out(st: Statement, txns: list) -> StatementOut:
    return StatementOut(
        id=st.id,
        attachment_id=st.attachment_id,
        filename=st.attachment.filename,
        series_id=st.series_id,
        series_name=st.series.name if st.series else None,
        status=st.status,
        document_type=st.document_type,
        vendor=st.vendor,
        account_ref=st.account_ref,
        statement_date=st.statement_date,
        period_start=st.period_start,
        period_end=st.period_end,
        due_date=st.due_date,
        amount_due=st.amount_due,
        summary=st.summary,
        suggestion=st.suggestion,
        error=st.error,
        job_id=st.job_id,
        usage=[UsageOut.model_validate(u) for u in st.usage],
        transactions=[_brief(t) for t in txns],
        created_at=st.created_at,
        approved_at=st.approved_at,
    )


async def _get(session: AsyncSession, sid: int) -> Statement:
    st = await session.get(Statement, sid, populate_existing=True)
    if st is None:
        raise HTTPException(404, "Statement not found")
    return st


async def _one(session: AsyncSession, sid: int) -> StatementOut:
    st = await _get(session, sid)
    return _out(st, (await linked_transactions(session, [sid])).get(sid, []))


@router.post("/statements", response_model=StatementOut, status_code=201)
async def upload_statement(
    file: UploadFile = File(...), transaction_ids: str | None = Form(None), session: AsyncSession = Depends(get_session)
):
    ids = [int(x) for x in (transaction_ids or "").split(",") if x.strip().isdigit()]
    data = await file.read(get_settings().max_upload_mb * 1024 * 1024 + 1)
    try:
        st = await create_statement(session, data, file.filename or "statement", file.content_type, ids)
    except ImportError_ as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from None
    await session.commit()
    notify_worker()
    return await _one(session, st.id)


@router.get("/statements", response_model=list[StatementOut])
async def list_statements(
    status: list[str] | None = Query(None),
    series_id: int | None = None,
    limit: int = Query(100, le=1000),
    session: AsyncSession = Depends(get_session),
):
    q = select(Statement).order_by(Statement.id.desc()).limit(limit)
    if status:
        q = q.where(Statement.status.in_(status))
    if series_id:
        q = q.where(Statement.series_id == series_id)
    rows = (await session.scalars(q)).unique().all()
    links = await linked_transactions(session, [s.id for s in rows])
    return [_out(s, links.get(s.id, [])) for s in rows]


@router.get("/statements/for-transaction/{txn_id}", response_model=list[StatementOut])
async def statements_for_transaction(txn_id: int, session: AsyncSession = Depends(get_session)):
    ids = (
        await session.scalars(
            select(statement_transaction.c.statement_id).where(statement_transaction.c.transaction_id == txn_id)
        )
    ).all()
    return [await _one(session, i) for i in ids]


@router.get("/statements/{sid}", response_model=StatementOut)
async def get_statement(sid: int, session: AsyncSession = Depends(get_session)):
    return await _one(session, sid)


@router.post("/statements/{sid}/approve", response_model=StatementOut)
async def approve(sid: int, body: StatementApprove, session: AsyncSession = Depends(get_session)):
    st = await _get(session, sid)
    if st.status not in ("suggested", "approved"):
        raise HTTPException(409, f"Statement is {st.status}")
    try:
        await approve_statement(session, st, body)
    except ImportError_ as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from None
    await session.commit()
    return await _one(session, sid)


@router.post("/statements/{sid}/dismiss", response_model=StatementOut)
async def dismiss(sid: int, session: AsyncSession = Depends(get_session)):
    st = await _get(session, sid)
    st.status = "dismissed"
    await session.commit()
    return await _one(session, sid)


@router.post("/statements/{sid}/reprocess", response_model=StatementOut)
async def reprocess(sid: int, session: AsyncSession = Depends(get_session)):
    st = await _get(session, sid)
    st.status, st.error = "processing", None
    job = await enqueue(session, "extract_bill", {"statement_id": st.id})
    st.job_id = job.id
    await session.commit()
    notify_worker()
    return await _one(session, sid)


@router.delete("/statements/{sid}", status_code=204)
async def delete_statement(sid: int, session: AsyncSession = Depends(get_session)):
    await session.delete(await _get(session, sid))
    await session.commit()
    return Response(status_code=204)


# ---- series ----
async def _series_out(session: AsyncSession, sr: StatementSeries) -> SeriesOut:
    count, latest = (
        await session.execute(
            select(func.count(), func.max(func.coalesce(Statement.period_end, Statement.statement_date))).where(
                Statement.series_id == sr.id, Statement.status == "approved"
            )
        )
    ).one()
    cat = await session.scalar(select(Category.name).where(Category.id == sr.category_id)) if sr.category_id else None
    out = SeriesOut.model_validate(sr)
    out.category_name, out.statement_count, out.latest_period_end = cat, count, latest
    return out


@router.get("/statement-series", response_model=list[SeriesOut])
async def list_series(session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(StatementSeries).order_by(StatementSeries.name))).all()
    return [await _series_out(session, s) for s in rows]


@router.post("/statement-series", response_model=SeriesOut, status_code=201)
async def create_series(body: SeriesIn, session: AsyncSession = Depends(get_session)):
    sr = StatementSeries(**body.model_dump())
    session.add(sr)
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "A series with that name exists") from None
    return await _series_out(session, sr)


@router.put("/statement-series/{series_id}", response_model=SeriesOut)
async def update_series(series_id: int, body: SeriesIn, session: AsyncSession = Depends(get_session)):
    sr = await session.get(StatementSeries, series_id)
    if sr is None:
        raise HTTPException(404, "Series not found")
    for k, v in body.model_dump().items():
        setattr(sr, k, v)
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "A series with that name exists") from None
    return await _series_out(session, sr)


@router.get("/statement-series/{series_id}/history", response_model=list[SeriesPoint])
async def series_history(series_id: int, session: AsyncSession = Depends(get_session)):
    sr = await session.get(StatementSeries, series_id)
    if sr is None:
        raise HTTPException(404, "Series not found")
    rows = (
        (
            await session.scalars(
                select(Statement)
                .where(Statement.series_id == series_id, Statement.status == "approved")
                .order_by(func.coalesce(Statement.period_end, Statement.statement_date, Statement.due_date))
            )
        )
        .unique()
        .all()
    )
    links = await linked_transactions(session, [s.id for s in rows])
    out = []
    for st in rows:
        usage = _primary_usage(st.usage, sr.primary_metric)
        txns = links.get(st.id, [])
        amount = st.amount_due if st.amount_due is not None else (abs(sum(t.amount for t in txns)) if txns else None)
        cpu = (amount / usage.value).quantize(Decimal("0.0001")) if usage and amount and usage.value else None
        out.append(
            SeriesPoint(
                statement_id=st.id,
                filename=st.attachment.filename,
                attachment_id=st.attachment_id,
                period_start=st.period_start,
                period_end=st.period_end,
                statement_date=st.statement_date,
                amount_due=amount,
                usage_value=usage.value if usage else None,
                usage_unit=usage.unit if usage else None,
                cost_per_unit=cpu,
                transactions=[_brief(t) for t in txns],
            )
        )
    return out


def _primary_usage(usage: list[StatementUsage], metric: str | None) -> StatementUsage | None:
    if metric:
        for u in usage:
            if u.metric == metric:
                return u
    return next((u for u in usage if u.is_primary), usage[0] if usage else None)
