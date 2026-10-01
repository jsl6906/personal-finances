from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.crypto import encrypt
from ledger.db.engine import get_session
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import Account, Source
from ledger.schemas import JobOut
from ledger.sources import simplefin, tiller
from ledger.sources.google import service_account_email

router = APIRouter(tags=["sources"])
NAMES = {"tiller": "Tiller", "simplefin": "SimpleFIN"}


async def _out(session: AsyncSession, s: Source) -> dict:
    linked = await session.scalar(select(func.count()).select_from(Account).where(Account.external_refs.has_key(s.kind)))
    return {
        "id": s.id,
        "kind": s.kind,
        "name": s.name,
        "enabled": s.enabled,
        "config": s.config,
        "connected": bool(s.secret) if s.kind == "simplefin" else bool(s.config.get("sheet_id")),
        "linked_accounts": linked or 0,
        "last_sync_at": s.last_sync_at,
        "last_status": s.last_status,
        "last_error": s.last_error,
        "last_result": s.last_result,
    }


async def _upsert(session: AsyncSession, kind: str) -> Source:
    s = await session.scalar(select(Source).where(Source.kind == kind))
    if s is None:
        s = Source(kind=kind, name=NAMES[kind], config={})
        session.add(s)
    return s


@router.get("/sources")
async def list_sources(session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Source).order_by(Source.id))).all()
    return {
        "google_service_account": service_account_email(),
        "sources": [await _out(session, s) for s in rows],
    }


class TillerIn(BaseModel):
    sheet: str = Field(min_length=10, max_length=300)
    lookback_days: int = Field(60, ge=7, le=3650)
    transactions_sheet: str = Field("Transactions", max_length=100)
    balances_sheet: str = Field("Balance History", max_length=100)


@router.put("/sources/tiller")
async def configure_tiller(body: TillerIn, session: AsyncSession = Depends(get_session)):
    try:
        sheet_id = tiller.parse_sheet_id(body.sheet)
        info = await tiller.sheet_info(sheet_id)
    except tiller.TillerError as exc:
        raise HTTPException(422, str(exc)) from None
    if body.transactions_sheet not in info["sheets"]:
        raise HTTPException(
            422, f"'{info['title']}' has no '{body.transactions_sheet}' sheet (found: {', '.join(info['sheets'])})"
        )
    s = await _upsert(session, "tiller")
    s.config = {
        "sheet_id": sheet_id,
        "sheet_title": info["title"],
        "lookback_days": body.lookback_days,
        "transactions_sheet": body.transactions_sheet,
        "balances_sheet": body.balances_sheet if body.balances_sheet in info["sheets"] else None,
    }
    await session.commit()
    return await _out(session, s)


class ClaimIn(BaseModel):
    setup_token: str = Field(min_length=20, max_length=2000)
    lookback_days: int = Field(30, ge=7, le=simplefin.MAX_LOOKBACK_DAYS)


@router.post("/sources/simplefin/claim")
async def connect_simplefin(body: ClaimIn, session: AsyncSession = Depends(get_session)):
    try:
        access = await simplefin.claim(body.setup_token)
    except simplefin.SimpleFinError as exc:
        raise HTTPException(422, str(exc)) from None
    s = await _upsert(session, "simplefin")
    s.secret = encrypt(access)
    s.config = {**(s.config or {}), "lookback_days": body.lookback_days}
    s.last_error = None
    await session.commit()
    return await _out(session, s)


class SourcePatch(BaseModel):
    enabled: bool | None = None
    lookback_days: int | None = Field(None, ge=7, le=3650)


async def _source(session: AsyncSession, sid: int) -> Source:
    s = await session.get(Source, sid)
    if s is None:
        raise HTTPException(404, "Source not found")
    return s


@router.patch("/sources/{sid}")
async def update_source(sid: int, body: SourcePatch, session: AsyncSession = Depends(get_session)):
    s = await _source(session, sid)
    if body.enabled is not None:
        s.enabled = body.enabled
    if body.lookback_days is not None:
        limit = simplefin.MAX_LOOKBACK_DAYS if s.kind == "simplefin" else 3650
        s.config = {**s.config, "lookback_days": min(body.lookback_days, limit)}
    await session.commit()
    return await _out(session, s)


@router.delete("/sources/{sid}", status_code=204)
async def delete_source(sid: int, session: AsyncSession = Depends(get_session)):
    await session.delete(await _source(session, sid))
    await session.commit()
    return Response(status_code=204)


@router.post("/sources/{sid}/sync", response_model=JobOut, status_code=202)
async def sync_now(sid: int, full: bool = False, session: AsyncSession = Depends(get_session)):
    await _source(session, sid)
    job = await enqueue(session, "sync_source", {"source_id": sid, "full": full})
    await session.commit()
    notify_worker()
    await session.refresh(job)
    return job


_LATEST_BALANCES = text(
    """--sql
    SELECT DISTINCT ON (b.account_id) b.account_id, a.name AS account, a.account_type, i.name AS institution,
           a.is_hidden, a.is_closed, b.as_of, b.balance, b.available, b.source,
           (SELECT p.balance FROM account_balance p
             WHERE p.account_id = b.account_id AND p.as_of <= b.as_of - 30
             ORDER BY p.as_of DESC LIMIT 1) AS balance_30d_ago
    FROM account_balance b
    JOIN account a ON a.id = b.account_id
    LEFT JOIN institution i ON i.id = a.institution_id
    ORDER BY b.account_id, b.as_of DESC, b.created_at DESC
    """
)
LIABILITIES = ("credit_card", "loan", "mortgage")


def _f(v):
    return float(v) if v is not None else None


@router.get("/balances")
async def balances(session: AsyncSession = Depends(get_session)):
    rows = [dict(r) for r in (await session.execute(_LATEST_BALANCES)).mappings() if not r["is_closed"]]
    for r in rows:
        for k in ("balance", "available", "balance_30d_ago"):
            r[k] = _f(r[k])
    # Liabilities are reported as positive amounts owed by some institutions and negative by others.
    assets = sum(r["balance"] for r in rows if r["account_type"] not in LIABILITIES)
    debts = sum(abs(r["balance"]) for r in rows if r["account_type"] in LIABILITIES)
    rows.sort(key=lambda r: (r["account_type"] in LIABILITIES, -abs(r["balance"])))
    return {"accounts": rows, "assets": assets, "liabilities": debts, "net_worth": assets - debts}


@router.get("/balances/history")
async def balance_history(
    account_id: int, days: int = Query(365, ge=7, le=3650), session: AsyncSession = Depends(get_session)
):
    sql = text(
        """--sql
        SELECT DISTINCT ON (as_of) as_of, balance FROM account_balance
        WHERE account_id = :a AND as_of >= :since ORDER BY as_of, created_at DESC
        """
    )
    rows = await session.execute(sql, {"a": account_id, "since": date.today() - timedelta(days=days)})
    return [{"as_of": r.as_of, "balance": float(r.balance)} for r in rows]


@router.get("/balances/trend")
async def balance_trend(days: int = Query(365, ge=30, le=3650), session: AsyncSession = Depends(get_session)):
    """Last balance per week for every open account, for sparklines."""
    sql = text(
        """--sql
        SELECT DISTINCT ON (b.account_id, date_trunc('week', b.as_of)) b.account_id, b.as_of, b.balance
        FROM account_balance b
        JOIN account a ON a.id = b.account_id AND NOT a.is_closed
        WHERE b.as_of >= :since
        ORDER BY b.account_id, date_trunc('week', b.as_of), b.as_of DESC, b.created_at DESC
        """
    )
    out: dict[int, list[dict]] = {}
    for r in await session.execute(sql, {"since": date.today() - timedelta(days=days)}):
        out.setdefault(r.account_id, []).append({"as_of": r.as_of, "balance": float(r.balance)})
    return out


@router.get("/holdings")
async def holdings(session: AsyncSession = Depends(get_session)):
    sql = text(
        """--sql
        SELECT h.account_id, a.name AS account, h.as_of, h.symbol, h.description, h.shares, h.market_value,
               h.cost_basis, h.currency, h.source
        FROM holding h
        JOIN account a ON a.id = h.account_id
        WHERE h.as_of = (SELECT max(x.as_of) FROM holding x WHERE x.account_id = h.account_id)
        ORDER BY a.name, h.market_value DESC NULLS LAST
        """
    )
    out = []
    for r in (await session.execute(sql)).mappings():
        d = dict(r)
        for k in ("shares", "market_value", "cost_basis"):
            d[k] = _f(d[k])
        out.append(d)
    return out
