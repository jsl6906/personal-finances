"""Email household members a question about one or more transactions."""

import re
import uuid
from datetime import UTC, datetime
from email.utils import make_msgid, parseaddr

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.alerts import email_html as mail
from ledger.alerts.mailer import EMAIL_RE, MailNotConfigured, send_email, smtp_configured
from ledger.alerts.service import _url, money
from ledger.config import get_settings
from ledger.models import AlertEvent, AlertRecipient, HouseholdMember, Transaction, TransactionNote

MAX_TRANSACTIONS = 100
# Embedded in the question's Message-ID; replies quote it back in In-Reply-To/References.
TOKEN_RE = re.compile(r"ledger-q-([0-9a-f]{32})")


def replies_saved() -> bool:
    """Whether replies to questions reach the polled inbox and get saved on the transactions."""
    from ledger.statements.email_inbox import inbox_configured

    return bool(get_settings().mail_inbound_address) and inbox_configured()


class QuestionError(ValueError):
    pass


class DeliveryError(RuntimeError):
    pass


async def people(session: AsyncSession) -> list[dict]:
    """Who can be asked: household members with an email, then alert recipients (one entry per address)."""
    out: dict[str, dict] = {}
    members = await session.scalars(
        select(HouseholdMember).where(HouseholdMember.email.is_not(None)).order_by(HouseholdMember.name)
    )
    for m in members.all():
        email = m.email.strip().lower()
        if EMAIL_RE.match(email):
            out.setdefault(email, {"email": email, "name": m.name})
    for r in (await session.scalars(select(AlertRecipient).order_by(AlertRecipient.id))).all():
        out.setdefault(r.email, {"email": r.email, "name": r.name})
    return list(out.values())


def _who(p: dict) -> str:
    return p["name"] or p["email"]


def _money(v) -> str:
    return ("-" if v < 0 else "") + money(abs(v))


def _names(ps: list[dict]) -> str:
    names = [_who(p) for p in ps]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _detail(t: Transaction) -> str:
    return " · ".join(x for x in (t.category.name if t.category else "Uncategorized", t.account and t.account.name) if x)


def _table(txns: list[Transaction]) -> str:
    cell = f"padding:9px 8px;border-bottom:1px solid {mail.DIVIDER};vertical-align:top"
    date_cell = f"{cell};width:1%;white-space:nowrap;color:{mail.MUTED};font-size:13px"
    rows = []
    for t in txns:
        color = mail.TONES["good"][0] if t.amount > 0 else mail.TEXT
        title = mail.link(t.description, _url(f"/transactions/{t.id}"), mail.TEXT)
        rows.append(
            f'<tr><td style="{date_cell}">{t.txn_date:%d %b %Y}</td>'
            f'<td style="{cell}"><div style="font-weight:600">{title}'
            f'</div><div style="font-size:12px;color:{mail.MUTED}">{mail.esc(_detail(t))}</div></td>'
            f'<td align="right" style="{cell};white-space:nowrap;font-weight:600;color:{color}">{_money(t.amount)}</td></tr>'
        )
    if len(txns) > 1:
        total = sum(t.amount for t in txns)
        rows.append(
            f'<tr><td></td><td style="padding:9px 8px;color:{mail.MUTED}">Total</td>'
            f'<td align="right" style="padding:9px 8px;white-space:nowrap;font-weight:600">{_money(total)}</td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;font-family:{mail.FONT};font-size:14px;color:{mail.TEXT}">{"".join(rows)}</table>'
    )


def render(
    txns: list[Transaction], message: str, asker: dict | None, subject: str | None = None, saved: bool = False
) -> dict:
    n = len(txns)
    about = "this transaction" if n == 1 else f"{n} transactions"
    heading = f"{_who(asker)} has a question about {about}" if asker else f"A question about {about}"
    if not subject:
        subject = f"Question: {txns[0].description} {_money(txns[0].amount)}" if n == 1 else f"Question about {about}"
    open_url = _url(f"/transactions/{txns[0].id}") if n == 1 else _url("/transactions")
    if saved:
        also = f" and sent to {_who(asker)}" if asker else ""
        what = "transaction" if n == 1 else "transactions"
        reply = f"Reply to this email to answer — your reply is saved on the {what} in Ledger{also}."
    else:
        reply = f"Reply to this email to answer{f' — it goes to {_who(asker)}' if asker else ''}."

    lines = [
        f"{t.txn_date:%d %b %Y}  {t.description}  {_money(t.amount)}  ({_detail(t)})"
        + (f"\n  {u}" if (u := _url(f"/transactions/{t.id}")) else "")
        for t in txns
    ]
    text = f"{heading}\n\n{message}\n\n" + "\n".join(lines) + f"\n\n{reply}\n"
    body = mail.quote(message) + _table(txns) + mail.button("Open in Ledger" if n == 1 else "Open Ledger", open_url)
    html = mail.layout(
        kicker="Question",
        heading=heading,
        body=body,
        preheader=message[:140],
        footer=f"{mail.esc(reply)} Sent from Ledger.",
    )
    return {"subject": subject, "text": text, "html": html}


async def ask(
    session: AsyncSession,
    txn_ids: list[int],
    to: list[str],
    message: str,
    subject: str | None = None,
    reply_to: str | None = None,
) -> dict:
    message = message.strip()
    if not message:
        raise QuestionError("Write a question first")
    known = {p["email"]: p for p in await people(session)}
    to = list(dict.fromkeys(e.strip().lower() for e in to))
    if not to:
        raise QuestionError("Choose who to ask")
    unknown = [e for e in to if e not in known]
    if unknown:
        raise QuestionError(f"Not a household member or alert recipient: {', '.join(unknown)}")
    reply_to = reply_to.strip().lower() if reply_to else None
    if reply_to and reply_to not in known:
        raise QuestionError("Replies can only go to a household member or alert recipient")
    ids = list(dict.fromkeys(txn_ids))
    if not 0 < len(ids) <= MAX_TRANSACTIONS:
        raise QuestionError(f"Ask about 1 to {MAX_TRANSACTIONS} transactions")
    txns = list(
        (
            await session.scalars(
                select(Transaction)
                .where(Transaction.id.in_(ids), Transaction.deleted_at.is_(None))
                .order_by(Transaction.txn_date.desc(), Transaction.id.desc())
            )
        )
        .unique()
        .all()
    )
    if len(txns) != len(ids):
        raise QuestionError("Some of those transactions no longer exist")
    if not smtp_configured():
        raise MailNotConfigured("SMTP is not configured; set SMTP_HOST and SMTP_FROM in the environment")

    asker = known.get(reply_to) if reply_to else None
    saved = replies_saved()
    email = render(txns, message, asker, (subject or "").strip() or None, saved)
    token = uuid.uuid4().hex
    s = get_settings()
    domain = parseaddr(s.smtp_from or s.smtp_username or "")[1].rpartition("@")[2] or "ledger.local"
    reply_addrs = [a for a in ((s.mail_inbound_address if saved else None), reply_to) if a]
    event = AlertEvent(
        kind="question",
        subject_key=f"question:{token}",
        title=email["subject"][:300],
        body=message,
        link=f"/transactions/{txns[0].id}" if len(txns) == 1 else None,
        transaction_ids=[t.id for t in txns],
    )
    session.add(event)
    try:
        await send_email(
            to,
            email["subject"],
            email["text"],
            email["html"],
            reply_to=", ".join(reply_addrs) or None,
            message_id=make_msgid(f"ledger-q-{token}", domain=domain),
        )
    except Exception as exc:
        event.status, event.error = "failed", str(exc)[:1000]
        await session.commit()
        raise DeliveryError(f"Sending failed: {exc}") from None
    event.status, event.recipients, event.sent_at = "sent", to, datetime.now(UTC)
    note = f"Asked {_names([known[e] for e in to])}: {message}"
    session.add_all(TransactionNote(transaction_id=t.id, body=note, source="question") for t in txns)
    await session.commit()
    return {"sent_to": to, "event_id": event.id, "subject": email["subject"]}
