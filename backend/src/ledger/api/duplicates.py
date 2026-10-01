from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.api.imports import _brief
from ledger.db.engine import get_session
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import DuplicatePair, Transaction
from ledger.schemas import JobOut, PairDecisionIn, ScanIn, TxnPairOut

router = APIRouter(prefix="/duplicates", tags=["duplicates"])


async def _pair_out(session: AsyncSession, p: DuplicatePair) -> TxnPairOut | None:
    a = await session.get(Transaction, p.txn_a_id)
    b = await session.get(Transaction, p.txn_b_id)
    if a is None or b is None:
        return None
    return TxnPairOut(
        id=p.id,
        status=p.status,
        score=p.score,
        reasons=p.reasons,
        ai_probability=p.ai_probability,
        ai_reason=p.ai_reason,
        a=_brief(a),
        b=_brief(b),
    )


@router.post("/scan", response_model=JobOut, status_code=202)
async def scan(body: ScanIn, session: AsyncSession = Depends(get_session)):
    job = await enqueue(session, "dup_scan", {"since": body.since.isoformat() if body.since else None})
    await session.commit()
    notify_worker()
    await session.refresh(job)
    return job


@router.get("", response_model=list[TxnPairOut])
async def list_pairs(
    status: str = "pending", limit: int = Query(100, le=1000), session: AsyncSession = Depends(get_session)
):
    live_a = select(Transaction.id).where(Transaction.deleted_at.is_(None))
    q = (
        select(DuplicatePair)
        .where(
            DuplicatePair.status == status,
            DuplicatePair.txn_b_id.is_not(None),
            DuplicatePair.txn_a_id.in_(live_a),
            DuplicatePair.txn_b_id.in_(live_a),
        )
        .order_by(DuplicatePair.score.desc(), DuplicatePair.id)
        .limit(limit)
    )
    out = [await _pair_out(session, p) for p in (await session.scalars(q)).all()]
    return [p for p in out if p]


@router.get("/for/{txn_id}", response_model=list[TxnPairOut])
async def pairs_for_transaction(txn_id: int, session: AsyncSession = Depends(get_session)):
    q = select(DuplicatePair).where(
        DuplicatePair.txn_b_id.is_not(None), or_(DuplicatePair.txn_a_id == txn_id, DuplicatePair.txn_b_id == txn_id)
    )
    out = [await _pair_out(session, p) for p in (await session.scalars(q)).all()]
    return [p for p in out if p]


@router.post("/{pair_id}/decide", response_model=TxnPairOut)
async def decide(pair_id: int, body: PairDecisionIn, session: AsyncSession = Depends(get_session)):
    p = await session.get(DuplicatePair, pair_id)
    if p is None or p.txn_b_id is None:
        raise HTTPException(404, "Pair not found")
    now = datetime.now(UTC)
    if body.decision == "separate":
        p.status = "confirmed_separate"
    else:
        keep_id = body.keep_id or p.txn_a_id
        if keep_id not in (p.txn_a_id, p.txn_b_id):
            raise HTTPException(422, "keep_id must be one of the pair")
        drop_id = p.txn_b_id if keep_id == p.txn_a_id else p.txn_a_id
        keep, drop = await session.get(Transaction, keep_id), await session.get(Transaction, drop_id)
        # Carry over anything the removed copy knew that the survivor doesn't.
        if keep.category_id is None and drop.category_id is not None:
            keep.category_id, keep.category_source = drop.category_id, drop.category_source
        if drop.notes and drop.notes not in (keep.notes or ""):
            keep.notes = f"{keep.notes}\n{drop.notes}" if keep.notes else drop.notes
        keep.tags = list({t.id: t for t in [*keep.tags, *drop.tags]}.values())
        drop.deleted_at = now
        p.status = "confirmed_duplicate"
    p.decided_at = now
    await session.commit()
    out = await _pair_out(session, p)
    if out is None:
        raise HTTPException(404, "Pair transactions no longer exist")
    return out
