"""Email alerts: budget overspend, out-of-norm spending, large transactions and a weekly digest.

Each condition becomes an AlertEvent with a stable subject_key, so it is emailed once. New events from one
evaluation run are batched into a single email.
"""

import html
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.alerts.mailer import send_email, smtp_configured
from ledger.analytics.reports import EXPENSE, FROM, INCOME, REPORTABLE, category_breakdown, top_merchants
from ledger.budgets.service import budget_status, period_for
from ledger.config import get_settings
from ledger.models import AlertEvent, AlertRecipient, AlertRule, Anomaly, Budget, DuplicatePair, Statement, Transaction
from ledger.models.analytics import ANOMALY_LABELS

log = logging.getLogger(__name__)

OUT_OF_NORM_KINDS = ("category_spike", "large_for_merchant", "bill_increase", "new_merchant", "unmatched_transfer")
ANOMALY_LOOKBACK_DAYS = 7
PACE_MIN_ELAPSED = 0.33
PACE_MARGIN = Decimal("1.10")


def money(v) -> str:
    return f"${Decimal(str(v)):,.2f}"


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


async def rules(session: AsyncSession) -> dict[str, AlertRule]:
    return {r.kind: r for r in (await session.scalars(select(AlertRule))).all()}


async def large_txn_threshold(session: AsyncSession) -> Decimal:
    rule = await session.get(AlertRule, "large_transaction")
    value = (rule.params or {}).get("threshold") if rule else None
    return Decimal(str(value if value else get_settings().large_txn_threshold))


async def _budget_candidates(session: AsyncSession, rule: AlertRule, today: date) -> list[dict]:
    out = []
    kinds = set((await session.scalars(select(Budget.period_type).distinct())).all())
    for kind in kinds:
        period = period_for(kind, today)
        status = await budget_status(session, period, today)
        elapsed = status["period"]["elapsed"]
        for r in status["rows"]:
            if r["kind"] != "expense" or r["period_type"] != kind or r["budget"] <= 0:
                continue
            key = f"budget:{r['budget_id']}:{period.start:%Y-%m-%d}"
            if r["actual"] > r["budget"]:
                out.append(
                    {
                        "kind": "budget_overspend",
                        "subject_key": f"{key}:over",
                        "title": f"Budget · {r['name']} {r['pct']:.0f}% of {period.label}",
                        "body": f"{r['name']} has reached {money(r['actual'])} against a {money(r['budget'])} budget "
                        f"for {period.label} ({money(-r['left'])} over).",
                        "link": "/budgets",
                    }
                )
            elif (
                (rule.params or {}).get("pace", True)
                and elapsed >= PACE_MIN_ELAPSED
                and r["projected"] > r["budget"] * PACE_MARGIN
            ):
                out.append(
                    {
                        "kind": "budget_overspend",
                        "subject_key": f"{key}:pace",
                        "title": f"Budget · {r['name']} on pace for {money(r['projected'])}",
                        "body": f"{r['name']} is at {money(r['actual'])} of {money(r['budget'])} with "
                        f"{(1 - elapsed) * 100:.0f}% of {period.label} left; at this rate it ends near "
                        f"{money(r['projected'])}.",
                        "link": "/budgets",
                    }
                )
    return out


async def _anomaly_candidates(session: AsyncSession, kinds: tuple[str, ...], rule_kind: str, today: date) -> list[dict]:
    since = datetime.combine(today - timedelta(days=ANOMALY_LOOKBACK_DAYS), datetime.min.time(), UTC)
    rows = (
        await session.scalars(
            select(Anomaly)
            .where(Anomaly.kind.in_(kinds), Anomaly.status == "open", Anomaly.created_at >= since)
            .order_by(Anomaly.score.desc())
        )
    ).all()
    return [
        {
            "kind": rule_kind,
            "subject_key": f"anomaly:{a.id}",
            "title": f"{ANOMALY_LABELS.get(a.kind, 'Out-of-norm')} · {a.title} {money(a.amount)}",
            "body": a.detail + (f"\n{a.ai_note}" if a.ai_note else ""),
            "link": f"/transactions/{a.transaction_id}" if a.transaction_id else "/findings",
        }
        for a in rows
    ]


async def evaluate(session: AsyncSession, today: date | None = None) -> dict:
    """Record new alert events for enabled rules and email them in one batch."""
    today = today or date.today()
    enabled = {k: r for k, r in (await rules(session)).items() if r.enabled}
    candidates: list[dict] = []
    if "budget_overspend" in enabled:
        candidates += await _budget_candidates(session, enabled["budget_overspend"], today)
    if "out_of_norm" in enabled:
        candidates += await _anomaly_candidates(session, OUT_OF_NORM_KINDS, "out_of_norm", today)
    if "large_transaction" in enabled:
        candidates += await _anomaly_candidates(session, ("large_transaction",), "large_transaction", today)

    new: list[AlertEvent] = []
    for c in candidates:
        event_id = await session.scalar(
            insert(AlertEvent)
            .values(**c)
            .on_conflict_do_nothing(index_elements=[AlertEvent.subject_key])
            .returning(AlertEvent.id)
        )
        if event_id:
            new.append(await session.get(AlertEvent, event_id))
    await session.commit()
    if new:
        subject = f"Ledger: {new[0].title}" if len(new) == 1 else f"Ledger: {len(new)} new alerts"
        await deliver(session, new, subject, *render_alerts(new))
    return {"candidates": len(candidates), "new": len(new), "status": new[0].status if new else None}


def _url(link: str | None) -> str | None:
    base = get_settings().app_base_url
    return f"{base.rstrip('/')}{link}" if base and link else None


def render_alerts(events: list[AlertEvent]) -> tuple[str, str]:
    lines, items = [], []
    for e in events:
        url = _url(e.link)
        lines.append(f"* {e.title}\n  {e.body.replace(chr(10), chr(10) + '  ')}" + (f"\n  {url}" if url else ""))
        title = html.escape(e.title)
        if url:
            title = f'<a href="{html.escape(url)}" style="color:#1f3d2b">{title}</a>'
        body = html.escape(e.body).replace("\n", "<br>")
        items.append(
            f'<li style="margin-bottom:12px"><strong>{title}</strong><br><span style="color:#444">{body}</span></li>'
        )
    return "\n\n".join(lines) + "\n", _wrap("New alerts", f'<ul style="padding-left:18px">{"".join(items)}</ul>')


def _wrap(heading: str, inner: str) -> str:
    return (
        '<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#1c1c1c;max-width:640px">'
        f'<h2 style="font-weight:600;margin:0 0 12px">{html.escape(heading)}</h2>{inner}'
        '<p style="color:#888;font-size:12px;margin-top:24px">Sent by Ledger. Manage rules and recipients on the Alerts '
        "page.</p></div>"
    )


async def active_recipients(session: AsyncSession) -> list[str]:
    return list((await session.scalars(select(AlertRecipient.email).where(AlertRecipient.enabled))).all())


async def deliver(session: AsyncSession, events: list[AlertEvent], subject: str, body: str, html_body: str) -> str:
    to = await active_recipients(session)
    status, error = "sent", None
    if not smtp_configured():
        status, error = "skipped", "SMTP is not configured"
    elif not to:
        status, error = "skipped", "No alert recipients"
    else:
        try:
            await send_email(to, subject, body, html_body)
        except Exception as exc:  # delivery problems are recorded, not raised
            log.exception("Alert email failed")
            status, error = "failed", str(exc)[:1000]
    now = datetime.now(UTC)
    for e in events:
        e.status, e.error, e.recipients = status, error, to if status == "sent" else []
        e.sent_at = now if status == "sent" else None
    await session.commit()
    return status


# ---- weekly digest ----
class _DigestNote(BaseModel):
    summary: str


async def digest_data(session: AsyncSession, today: date) -> dict:
    end = today - timedelta(days=1)
    start = end - timedelta(days=6)
    base_start = start - timedelta(days=56)
    sql = text(
        f"""--sql
        SELECT sum(CASE WHEN t.txn_date >= :start THEN {EXPENSE} ELSE 0 END) AS spent,
               sum(CASE WHEN t.txn_date >= :start THEN {INCOME} ELSE 0 END) AS income,
               sum(CASE WHEN t.txn_date < :start THEN {EXPENSE} ELSE 0 END) / 8 AS weekly_avg,
               count(*) FILTER (WHERE t.txn_date >= :start) AS n
        {FROM}
        WHERE {REPORTABLE} AND t.txn_date BETWEEN :base_start AND :end
        """
    )
    totals = (await session.execute(sql, {"start": start, "end": end, "base_start": base_start})).one()
    month = await budget_status(session, period_for("month", today), today)
    watch = [r for r in month["rows"] if r["kind"] == "expense" and r["status"] in ("over", "pace")]
    open_anomalies = (
        await session.scalars(select(Anomaly).where(Anomaly.status == "open").order_by(Anomaly.created_at.desc()).limit(3))
    ).all()
    return {
        "start": start,
        "end": end,
        "spent": float(totals.spent or 0),
        "income": float(totals.income or 0),
        "weekly_avg": float(totals.weekly_avg or 0),
        "count": totals.n or 0,
        "categories": [c for c in await category_breakdown(session, start, end) if c["spent"] > 0][:5],
        "merchants": await top_merchants(session, start, end, limit=5),
        "budget_label": month["period"]["label"],
        "budget_total": month["total"],
        "budget_watch": watch[:6],
        "anomaly_count": await session.scalar(select(func.count()).select_from(Anomaly).where(Anomaly.status == "open")),
        "anomalies": [{"title": a.title, "detail": a.detail} for a in open_anomalies],
        "new_statements": await session.scalar(
            select(func.count()).select_from(Statement).where(func.date(Statement.created_at) >= start)
        ),
        "statements_to_review": await session.scalar(
            select(func.count()).select_from(Statement).where(Statement.status == "suggested")
        ),
        "uncategorized": await session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.deleted_at.is_(None),
                Transaction.category_id.is_(None),
                Transaction.txn_date >= today - timedelta(days=30),
            )
        ),
        "duplicates": await session.scalar(
            select(func.count()).select_from(DuplicatePair).where(DuplicatePair.status == "pending")
        ),
    }


async def _ai_summary(d: dict) -> str | None:
    if not get_settings().gemini_key:
        return None
    from ledger.ai.client import generate

    facts = {
        k: v
        for k, v in d.items()
        if k
        in ("spent", "income", "weekly_avg", "count", "categories", "merchants", "budget_total", "budget_watch", "anomalies")
    }
    try:
        note: _DigestNote = await generate(
            str(facts),
            purpose="weekly_digest",
            tier="lite",
            schema=_DigestNote,
            temperature=0.3,
            system="Write a 2-3 sentence plain summary of this household's past week of spending for a weekly email. "
            "Mention how the week compares with the typical week and anything that needs attention. Use $ amounts. "
            "No greetings, no advice lectures.",
        )
        return note.summary.strip()
    except Exception:
        log.exception("Digest summary failed")
        return None


async def render_digest(session: AsyncSession, today: date, ai: bool = True) -> dict:
    d = await digest_data(session, today)
    label = f"{d['start']:%d %b} – {d['end']:%d %b %Y}"
    summary = await _ai_summary(d) if ai else None
    diff = d["spent"] - d["weekly_avg"]
    sections: list[tuple[str, list[str]]] = [
        (
            "The week",
            [
                f"Spent {money(d['spent'])} across {d['count']} transactions; income {money(d['income'])}.",
                f"Typical week (last 8): {money(d['weekly_avg'])} — this week was {money(abs(diff))} "
                f"{'above' if diff >= 0 else 'below'}.",
            ],
        ),
        ("Top categories", [f"{c['category']}: {money(c['spent'])}" for c in d["categories"]] or ["No spending recorded."]),
        ("Top merchants", [f"{m['example']}: {money(m['spent'])} ({m['count']})" for m in d["merchants"]] or ["—"]),
    ]
    bt = d["budget_total"]
    budget_lines = [f"{money(bt['spent'])} of {money(bt['budget'])} budgeted so far."] if bt["count"] else []
    budget_lines += [
        f"{r['name']}: {money(r['actual'])} of {money(r['budget'])}"
        + (" — over" if r["status"] == "over" else f" — on pace for {money(r['projected'])}")
        for r in d["budget_watch"]
    ]
    if budget_lines:
        sections.append((f"Budgets · {d['budget_label']}", budget_lines))
    todo = []
    if d["anomaly_count"]:
        todo.append(_n(d["anomaly_count"], "out-of-norm finding") + " open")
    if d["statements_to_review"]:
        todo.append(_n(d["statements_to_review"], "statement") + " awaiting review")
    if d["new_statements"]:
        todo.append(_n(d["new_statements"], "new statement") + " this week")
    if d["uncategorized"]:
        todo.append(_n(d["uncategorized"], "uncategorized transaction") + " (last 30 days)")
    if d["duplicates"]:
        todo.append(_n(d["duplicates"], "possible duplicate") + " to review")
    if todo:
        sections.append(("To review", todo))

    text_body = (f"{summary}\n\n" if summary else "") + "\n\n".join(
        f"{h}\n" + "\n".join(f"- {line}" for line in lines) for h, lines in sections
    )
    url = _url("/")
    if url:
        text_body += f"\n\nOpen Ledger: {url}"
    inner = (f'<p style="font-size:15px">{html.escape(summary)}</p>' if summary else "") + "".join(
        f'<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#555;margin:18px 0 6px">'
        f'{html.escape(h)}</h3><ul style="padding-left:18px;margin:0">'
        + "".join(f"<li>{html.escape(line)}</li>" for line in lines)
        + "</ul>"
        for h, lines in sections
    )
    if url:
        inner += f'<p><a href="{html.escape(url)}">Open Ledger</a></p>'
    return {
        "subject": f"Ledger weekly digest · {label}",
        "title": f"Weekly digest · {label}",
        "text": text_body + "\n",
        "html": _wrap(f"Weekly digest · {label}", inner),
        "summary": summary,
    }


async def send_digest(session: AsyncSession, today: date | None = None, force: bool = False) -> dict:
    today = today or date.today()
    rule = await session.get(AlertRule, "weekly_digest")
    if not force and (rule is None or not rule.enabled):
        return {"status": "disabled"}
    iso = today.isocalendar()
    key = f"digest:{iso.year}-W{iso.week:02d}" + (f":manual:{datetime.now(UTC):%Y%m%d%H%M%S}" if force else "")
    if await session.scalar(select(AlertEvent.id).where(AlertEvent.subject_key == key)):
        return {"status": "already_sent"}
    d = await render_digest(session, today)
    event = AlertEvent(kind="weekly_digest", subject_key=key, title=d["title"], body=d["text"], link="/")
    session.add(event)
    await session.commit()
    status = await deliver(session, [event], d["subject"], d["text"], d["html"])
    return {"status": status, "event_id": event.id}
