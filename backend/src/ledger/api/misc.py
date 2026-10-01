from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger import auth
from ledger.db.engine import get_session
from ledger.models import Account, Category, Job, Transaction
from ledger.schemas import JobOut, LoginIn

public = APIRouter(tags=["auth"])
router = APIRouter()


@public.post("/auth/login")
async def login(body: LoginIn, request: Request):
    auth.check_login(request, body.password)
    return {"ok": True}


@public.post("/auth/logout")
async def logout(request: Request):
    auth.logout(request)
    return {"ok": True}


@public.get("/auth/me")
async def me(request: Request):
    return {"authenticated": auth.is_authenticated(request)}


@router.get("/jobs", response_model=list[JobOut], tags=["jobs"])
async def list_jobs(type: str | None = None, limit: int = Query(20, le=200), session: AsyncSession = Depends(get_session)):
    q = select(Job).order_by(Job.id.desc()).limit(limit)
    if type:
        q = q.where(Job.type == type)
    return (await session.scalars(q)).all()


@router.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)):
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, tags=["jobs"])
async def cancel_job(job_id: int, session: AsyncSession = Depends(get_session)):
    await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(["queued", "running"]))
        .values(status="cancelled", finished_at=func.now())
    )
    await session.commit()
    return await get_job(job_id, session)


@router.get("/summary", tags=["summary"])
async def summary(start: date = Query(...), end: date = Query(...), session: AsyncSession = Depends(get_session)):
    live = Transaction.deleted_at.is_(None)
    in_period = [live, Transaction.txn_date >= start, Transaction.txn_date <= end]
    reportable = Category.hide_from_reports.is_not(True)
    not_transfer = and_(
        func.coalesce(Category.type, "") != "transfer",
        or_(Transaction.transfer_match_id.is_(None), Transaction.category_id.is_not(None)),
    )
    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(case((Transaction.amount < 0, -Transaction.amount), else_=0)), 0),
                func.coalesce(func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)), 0),
                func.count().filter(Transaction.category_id.is_(None)),
            )
            .select_from(Transaction)
            .outerjoin(Category, Transaction.category_id == Category.id)
            .where(*in_period, reportable, not_transfer)
        )
    ).one()
    last_txn = await session.scalar(select(func.max(Transaction.txn_date)).where(live))
    accounts = await session.scalar(select(func.count()).select_from(Account).where(Account.is_closed.is_(False)))
    uncategorized_all = await session.scalar(select(func.count()).where(live, Transaction.category_id.is_(None)))
    return {
        "count": row[0],
        "spent": row[1],
        "income": row[2],
        "uncategorized": row[3],
        "uncategorized_all": uncategorized_all,
        "last_transaction": last_txn,
        "accounts": accounts,
    }
