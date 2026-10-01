from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.alerts.mailer import EMAIL_RE, send_email, smtp_configured
from ledger.alerts.service import active_recipients, render_digest
from ledger.config import get_settings
from ledger.db.engine import get_session
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import AlertEvent, AlertRecipient, AlertRule
from ledger.schemas import AlertEventOut, AlertRuleIn, AlertRuleOut, JobOut, RecipientIn, RecipientOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/status")
async def status(session: AsyncSession = Depends(get_session)):
    s = get_settings()
    return {
        "smtp_configured": smtp_configured(),
        "smtp_host": s.smtp_host,
        "smtp_from": s.smtp_from or s.smtp_username,
        "app_base_url": s.app_base_url,
        "digest_weekday": s.digest_weekday,
        "recipients": len(await active_recipients(session)),
    }


@router.get("/rules", response_model=list[AlertRuleOut])
async def list_rules(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(AlertRule).order_by(AlertRule.kind))).all()


@router.put("/rules/{kind}", response_model=AlertRuleOut)
async def update_rule(kind: str, body: AlertRuleIn, session: AsyncSession = Depends(get_session)):
    rule = await session.get(AlertRule, kind)
    if rule is None:
        raise HTTPException(404, "Unknown alert rule")
    if body.enabled is not None:
        rule.enabled = body.enabled
    params = dict(rule.params or {})
    if body.pace is not None and kind == "budget_overspend":
        params["pace"] = body.pace
    if body.threshold is not None and kind == "large_transaction":
        params["threshold"] = float(body.threshold)
    rule.params = params
    await session.commit()
    await session.refresh(rule)
    return rule


@router.get("/recipients", response_model=list[RecipientOut])
async def list_recipients(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(AlertRecipient).order_by(AlertRecipient.id))).all()


@router.post("/recipients", response_model=RecipientOut, status_code=201)
async def add_recipient(body: RecipientIn, session: AsyncSession = Depends(get_session)):
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address")
    r = AlertRecipient(email=email, name=body.name, enabled=body.enabled)
    session.add(r)
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "That address is already a recipient") from None
    await session.refresh(r)
    return r


class RecipientPatch(BaseModel):
    enabled: bool


@router.patch("/recipients/{rid}", response_model=RecipientOut)
async def toggle_recipient(rid: int, body: RecipientPatch, session: AsyncSession = Depends(get_session)):
    r = await session.get(AlertRecipient, rid)
    if r is None:
        raise HTTPException(404, "Recipient not found")
    r.enabled = body.enabled
    await session.commit()
    await session.refresh(r)
    return r


@router.delete("/recipients/{rid}", status_code=204)
async def delete_recipient(rid: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(AlertRecipient, rid)
    if r is None:
        raise HTTPException(404, "Recipient not found")
    await session.delete(r)
    await session.commit()
    return Response(status_code=204)


@router.get("/events", response_model=list[AlertEventOut])
async def list_events(limit: int = Query(20, le=200), session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(AlertEvent).order_by(AlertEvent.id.desc()).limit(limit))).all()


@router.post("/test")
async def send_test(session: AsyncSession = Depends(get_session)):
    to = await active_recipients(session)
    if not smtp_configured():
        raise HTTPException(409, "SMTP is not configured; set SMTP_HOST and SMTP_FROM in the environment")
    if not to:
        raise HTTPException(409, "Add at least one recipient first")
    try:
        await send_email(to, "Ledger test email", "Email alerts from Ledger are working.\n")
    except Exception as exc:
        raise HTTPException(502, f"Sending failed: {exc}") from None
    return {"sent_to": to}


async def _enqueue(session: AsyncSession, job_type: str, payload: dict) -> JobOut:
    job = await enqueue(session, job_type, payload)
    await session.commit()
    notify_worker()
    await session.refresh(job)
    return job


@router.post("/run", response_model=JobOut, status_code=202)
async def run_rules(session: AsyncSession = Depends(get_session)):
    return await _enqueue(session, "evaluate_alerts", {})


@router.get("/digest/preview")
async def preview_digest(ai: bool = False, session: AsyncSession = Depends(get_session)):
    return await render_digest(session, date.today(), ai=ai)


@router.post("/digest/send", response_model=JobOut, status_code=202)
async def send_digest_now(session: AsyncSession = Depends(get_session)):
    return await _enqueue(session, "weekly_digest", {"force": True})
