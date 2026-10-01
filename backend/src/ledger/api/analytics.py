from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics import reports
from ledger.budgets.service import add_months
from ledger.db.engine import get_session
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import Anomaly, Category, Transaction
from ledger.schemas import JobOut

router = APIRouter(tags=["analytics"])


def _range(start: date | None, end: date | None, months: int) -> tuple[date, date]:
    today = date.today()
    end = end or (add_months(date(today.year, today.month, 1), 1) - timedelta(days=1))
    start = start or add_months(date(end.year, end.month, 1), -(months - 1))
    if start > end:
        raise HTTPException(422, "start must be before end")
    return start, end


@router.get("/analytics/span")
async def span(session: AsyncSession = Depends(get_session)):
    return await reports.span(session)


@router.get("/analytics/cashflow")
async def cashflow(
    start: date | None = None,
    end: date | None = None,
    months: int = Query(12, ge=1, le=240),
    session: AsyncSession = Depends(get_session),
):
    return await reports.cashflow(session, *_range(start, end, months))


@router.get("/analytics/categories")
async def categories(
    start: date | None = None,
    end: date | None = None,
    months: int = Query(1, ge=1, le=240),
    session: AsyncSession = Depends(get_session),
):
    s, e = _range(start, end, months)
    return {"start": s, "end": e, "rows": await reports.category_breakdown(session, s, e)}


@router.get("/analytics/category-trend")
async def category_trend(
    start: date | None = None,
    end: date | None = None,
    months: int = Query(12, ge=1, le=240),
    level: Literal["group", "category"] = "group",
    top: int = Query(6, ge=1, le=20),
    ids: list[int] | None = Query(None),
    kind: Literal["expense", "income"] = "expense",
    other: bool = False,
    session: AsyncSession = Depends(get_session),
):
    return await reports.category_trend(session, *_range(start, end, months), level, top, ids, kind, other)


@router.get("/analytics/contributors")
async def contributors(
    start: date,
    end: date,
    basis: Literal["reports", "all"] = "reports",
    kind: Literal["expense", "income"] | None = None,
    level: Literal["group", "category"] = "category",
    ids: list[int] | None = Query(None),
    exclude: list[int] | None = Query(None),
    account_id: int | None = None,
    merchant: str | None = None,
    limit: int = Query(8, ge=1, le=25),
    session: AsyncSession = Depends(get_session),
):
    if start > end:
        raise HTTPException(422, "start must be before end")
    return await reports.contributors(
        session,
        start,
        end,
        basis=basis,
        kind=kind,
        level=level,
        ids=ids,
        exclude=exclude,
        account_id=account_id,
        merchant=merchant,
        limit=limit,
    )


@router.get("/analytics/merchants")
async def merchants(
    start: date | None = None,
    end: date | None = None,
    months: int = Query(3, ge=1, le=240),
    limit: int = Query(15, le=100),
    session: AsyncSession = Depends(get_session),
):
    return await reports.top_merchants(session, *_range(start, end, months), limit)


@router.get("/anomalies")
async def list_anomalies(
    status: Literal["open", "reviewed", "dismissed", "all"] = "open",
    transaction_id: int | None = None,
    category_id: int | None = None,
    group_id: int | None = None,
    account_id: int | None = None,
    merchant: str | None = None,
    limit: int = Query(50, le=500),
    session: AsyncSession = Depends(get_session),
):
    conds = [] if status == "all" else [Anomaly.status == status]
    if transaction_id:
        conds.append(Anomaly.transaction_id == transaction_id)
    if category_id:
        conds.append(or_(Anomaly.category_id == category_id, Transaction.category_id == category_id))
    if group_id:
        in_group = select(Category.id).where(Category.group_id == group_id)
        conds.append(or_(Anomaly.category_id.in_(in_group), Transaction.category_id.in_(in_group)))
    if account_id:
        conds.append(Transaction.account_id == account_id)
    if merchant:
        conds.append(func.coalesce(Transaction.merchant, func.lower(Transaction.description)) == merchant)
    rows = (
        await session.execute(
            select(Anomaly, Category.name, Transaction.description, Transaction.txn_date)
            .outerjoin(Category, Category.id == Anomaly.category_id)
            .outerjoin(Transaction, Transaction.id == Anomaly.transaction_id)
            .where(*conds)
            .order_by(Anomaly.period.desc(), Anomaly.score.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": a.id,
            "kind": a.kind,
            "period": a.period,
            "title": a.title,
            "detail": a.detail,
            "ai_note": a.ai_note,
            "amount": float(a.amount),
            "baseline": float(a.baseline) if a.baseline is not None else None,
            "score": float(a.score),
            "status": a.status,
            "transaction_id": a.transaction_id,
            "category_id": a.category_id,
            "category_name": cat,
            "series_id": a.series_id,
            "transaction_description": desc,
            "transaction_date": tdate,
            "created_at": a.created_at,
        }
        for a, cat, desc, tdate in rows
    ]


@router.post("/anomalies/{anomaly_id}/{action}")
async def set_anomaly_status(
    anomaly_id: int, action: Literal["dismiss", "review", "reopen"], session: AsyncSession = Depends(get_session)
):
    a = await session.get(Anomaly, anomaly_id)
    if a is None:
        raise HTTPException(404, "Finding not found")
    a.status = {"dismiss": "dismissed", "review": "reviewed", "reopen": "open"}[action]
    await session.commit()
    return {"id": a.id, "status": a.status}


@router.post("/anomalies/run", response_model=JobOut, status_code=202)
async def run_detection(months: list[date] | None = Query(None), session: AsyncSession = Depends(get_session)):
    payload = {"months": [m.isoformat() for m in months]} if months else {}
    job = await enqueue(session, "detect_anomalies", payload)
    await session.commit()
    notify_worker()
    await session.refresh(job)
    return job


@router.get("/anomalies/counts")
async def anomaly_counts(session: AsyncSession = Depends(get_session)):
    return dict((await session.execute(select(Anomaly.status, func.count()).group_by(Anomaly.status))).all())
