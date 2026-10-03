"""AI merchant review: run it, list pending suggestions, accept (optionally edited) or dismiss them."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.ai.merchant_review import candidates, lock_review, pending_keys, same_words
from ledger.db.engine import get_session
from ledger.db.filters import id_in
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import Job, MerchantSuggestion
from ledger.schemas import IdList, JobOut
from ledger.services import merchants

router = APIRouter(tags=["merchants"])
JOB = "merchant_review"


class RunIn(BaseModel):
    all: bool = False


class AcceptItem(BaseModel):
    id: int
    target_key: str | None = Field(None, max_length=200)
    keys: list[str] | None = Field(None, max_length=1000)
    display_name: str | None = Field(None, max_length=200)


class AcceptIn(BaseModel):
    items: list[AcceptItem] = Field(min_length=1, max_length=2000)


async def _key_info(session: AsyncSession, keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    sql = text(
        """--sql
        SELECT t.merchant AS key, count(*) AS n, max(t.txn_date) AS last_date,
               (array_agg(t.description ORDER BY t.txn_date DESC, t.id DESC))[1] AS sample
        FROM "transaction" t
        WHERE t.deleted_at IS NULL AND t.merchant = ANY(CAST(:keys AS text[]))
        GROUP BY t.merchant
        """
    )
    info = {r["key"]: dict(r) for r in (await session.execute(sql, {"keys": keys})).mappings()}
    named = await merchants.display_names(session, keys)
    return {
        k: {
            "key": k,
            "name": named.get(k) or merchants.default_name(k),
            "count": info.get(k, {}).get("n", 0),
            "last_date": info.get(k, {}).get("last_date"),
            "sample": info.get(k, {}).get("sample"),
        }
        for k in keys
    }


async def _pending(session: AsyncSession, ids: list[int]) -> list[MerchantSuggestion]:
    return list(
        (
            await session.scalars(
                select(MerchantSuggestion)
                .where(id_in(MerchantSuggestion.id, ids), MerchantSuggestion.status == "pending")
                .order_by(MerchantSuggestion.id)
            )
        ).all()
    )


@router.get("/merchants/review")
async def review_state(session: AsyncSession = Depends(get_session)):
    job = await session.scalar(select(Job).where(Job.type == JOB).order_by(Job.id.desc()).limit(1))
    rows = (
        await session.scalars(
            select(MerchantSuggestion).where(MerchantSuggestion.status == "pending").order_by(MerchantSuggestion.id)
        )
    ).all()
    info = await _key_info(session, sorted({k for s in rows for k in (s.target_key, *s.source_keys)}))
    suggestions = [
        {
            "id": s.id,
            "kind": s.kind,
            "display_name": s.display_name,
            "reason": s.reason,
            # Only capitalization / punctuation differs from the automatic name.
            "minor": s.kind == "rename" and same_words(s.display_name, info[s.target_key]["name"]),
            "target": info[s.target_key],
            "sources": [info[k] for k in s.source_keys],
        }
        for s in rows
    ]
    return {
        "job": JobOut.model_validate(job) if job else None,
        "unreviewed": len({c["key"] for c in await candidates(session)} - await pending_keys(session)),
        "suggestions": suggestions,
    }


@router.post("/merchants/review/run", response_model=JobOut, status_code=202)
async def run_review(body: RunIn, session: AsyncSession = Depends(get_session)):
    busy = await session.scalar(select(Job.id).where(Job.type == JOB, Job.status.in_(["queued", "running"])))
    if busy:
        raise HTTPException(409, "A merchant review is already running")
    job = await enqueue(session, JOB, {"all": body.all})
    await session.commit()
    notify_worker()
    await session.refresh(job)
    return job


@router.post("/merchants/review/accept")
async def accept_suggestions(body: AcceptIn, session: AsyncSession = Depends(get_session)):
    await lock_review(session)
    rows = {s.id: s for s in await _pending(session, [i.id for i in body.items])}
    accepted = moved = 0
    errors: list[str] = []
    for item in body.items:
        s = rows.get(item.id)
        if s is None:
            continue
        try:
            async with session.begin_nested():
                moved += await merchants.accept_suggestion(session, s, item.target_key, item.keys, item.display_name)
            accepted += 1
        except merchants.MerchantError as exc:
            errors.append(f"{s.display_name or s.target_key}: {exc}")
    await session.commit()
    return {"accepted": accepted, "moved": moved, "errors": errors}


@router.post("/merchants/review/dismiss")
async def dismiss_suggestions(body: IdList, session: AsyncSession = Depends(get_session)):
    await lock_review(session)
    rows = await _pending(session, body.ids)
    for s in rows:
        s.status = "dismissed"
    await merchants.mark_reviewed(session, [k for s in rows for k in (s.target_key, *s.source_keys)])
    await session.commit()
    return {"dismissed": len(rows)}
