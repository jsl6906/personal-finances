from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.alerts import email_html as mail
from ledger.alerts import questions
from ledger.alerts.mailer import EMAIL_RE, MailNotConfigured, preview_html, send_email, smtp_configured
from ledger.alerts.service import active_recipients, footer, render_digest
from ledger.config import get_settings
from ledger.db.engine import get_session
from ledger.jobs.worker import enqueue, notify_worker
from ledger.models import AlertEvent, AlertRecipient, AlertRule, Anomaly
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
    rows = await session.execute(
        select(AlertEvent, Anomaly.status)
        .outerjoin(Anomaly, AlertEvent.subject_key == func.concat("anomaly:", Anomaly.id))
        .order_by(AlertEvent.id.desc())
        .limit(limit)
    )
    return [AlertEventOut.model_validate(e).model_copy(update={"finding_status": st}) for e, st in rows]


@router.post("/test")
async def send_test(session: AsyncSession = Depends(get_session)):
    to = await active_recipients(session)
    if not smtp_configured():
        raise HTTPException(409, "SMTP is not configured; set SMTP_HOST and SMTP_FROM in the environment")
    if not to:
        raise HTTPException(409, "Add at least one recipient first")
    try:
        body = mail.alert_card("Test", "good", "Email alerts are working", f"Sent to {', '.join(to)}.", None)
        html_body = mail.layout(kicker="Test", heading="Ledger test email", body=body, footer=footer())
        await send_email(to, "Ledger test email", "Email alerts from Ledger are working.\n", html_body)
    except Exception as exc:
        raise HTTPException(502, f"Sending failed: {exc}") from None
    return {"sent_to": to}


@router.get("/people")
async def list_people(session: AsyncSession = Depends(get_session)):
    """Addresses a question can be sent to (household members with an email, plus alert recipients)."""
    return await questions.people(session)


class QuestionIn(BaseModel):
    transaction_ids: list[int] = Field(min_length=1, max_length=questions.MAX_TRANSACTIONS)
    to: list[str] = Field(min_length=1, max_length=10)
    message: str = Field(min_length=1, max_length=4000)
    subject: str | None = Field(None, max_length=200)
    reply_to: str | None = Field(None, max_length=254)


@router.post("/questions")
async def ask_question(body: QuestionIn, session: AsyncSession = Depends(get_session)):
    try:
        return await questions.ask(session, body.transaction_ids, body.to, body.message, body.subject, body.reply_to)
    except questions.QuestionError as exc:
        raise HTTPException(422, str(exc)) from None
    except MailNotConfigured as exc:
        raise HTTPException(409, str(exc)) from None
    except questions.DeliveryError as exc:
        raise HTTPException(502, str(exc)) from None


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
    d = await render_digest(session, date.today(), ai=ai)
    return {**d, "html": preview_html(d["html"])}


@router.post("/digest/send", response_model=JobOut, status_code=202)
async def send_digest_now(session: AsyncSession = Depends(get_session)):
    return await _enqueue(session, "weekly_digest", {"force": True})
