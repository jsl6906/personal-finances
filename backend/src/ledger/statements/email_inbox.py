"""Inbound email: poll an IMAP folder for bills/receipts forwarded by allowed senders and file them.

PDF/image attachments become Bills & statements, CSV/Excel attachments become imports, and a message with neither
(e.g. an order confirmation) is filed as a text statement built from its body. Replies to emailed questions are
saved as notes on the transactions asked about (and any receipt they attach is filed against them).
"""

import asyncio
import hashlib
import imaplib
import logging
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import ledger.imports.jobs  # noqa: F401  (registers the handlers filed items are queued for)
import ledger.statements.jobs  # noqa: F401
from ledger.alerts.questions import TOKEN_RE, people, replies_saved
from ledger.config import get_settings
from ledger.db.engine import get_sessionmaker
from ledger.imports.parsing import detect_kind
from ledger.imports.service import ImportError_, create_batch
from ledger.jobs.worker import notify_worker
from ledger.models import AlertEvent, Attachment, ImportBatch, Statement, Transaction, TransactionNote
from ledger.statements.service import create_statement

log = logging.getLogger(__name__)

MIN_IMAGE_BYTES = 20_000  # smaller images are logos, icons and tracking pixels
MAX_BODY_CHARS = 100_000
MAX_REPLY_CHARS = 4000
MAX_PER_POLL = 25
_UNSAFE = re.compile(r"[^A-Za-z0-9 #&()_,'-]+")  # no dots: the body's filename must not look like a file type
_REPLY_PREFIX = re.compile(r"^((re|aw|sv)\s*:\s*)+", re.IGNORECASE)
# Where the quoted original starts in a reply (Gmail/Apple, Outlook, mobile signatures).
_QUOTED = re.compile(
    r"^(On\b[^\n]*(?:\n[^\n]*)?\bwrote:[ \t]*$|-+ ?Original Message ?-+|_{10,}[ \t]*$|From: .+$|Sent from my .+$)",
    re.MULTILINE | re.IGNORECASE,
)


def allowed_senders() -> set[str]:
    return {a.strip().lower() for a in (get_settings().mail_allowed_senders or "").split(",") if a.strip()}


def inbox_configured() -> bool:
    s = get_settings()
    return bool(s.imap_host and s.imap_username and s.imap_password and allowed_senders())


# ---------- parsing ----------
class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "table", "section", "h1", "h2", "h3", "h4", "h5", "h6"}
    HIDDEN = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self._hidden += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p.close()
    lines = (" ".join(line.split()) for line in "".join(p.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError):
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def sender_of(msg: EmailMessage) -> str:
    return parseaddr(str(msg.get("From", "")))[1].lower()


def authenticated(msg: EmailMessage) -> bool:
    """False when the receiving server reports the From address failed DMARC (i.e. it was likely spoofed)."""
    # The receiving server prepends its verdict; headers below it could have been written by the sender.
    results = msg.get_all("Authentication-Results") or []
    return not results or bool(re.search(r"\bdmarc=pass\b", str(results[0]), re.IGNORECASE))


def documents(msg: EmailMessage) -> list[tuple[str, str, bytes]]:
    """(filename, content type, bytes) of importable attachments, including those of forwarded messages."""
    docs = []
    for part in msg.walk():
        name = part.get_filename()
        if part.is_multipart() or not name or detect_kind(name) is None:
            continue
        data = part.get_payload(decode=True) or b""
        if part.get_content_maintype() == "image" and len(data) < MIN_IMAGE_BYTES:
            continue
        docs.append((name, part.get_content_type(), data))
    return docs


def body_text(msg: EmailMessage) -> str:
    html, plain = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_filename() or part.get_content_maintype() != "text":
            continue
        (html if part.get_content_subtype() == "html" else plain).append(_part_text(part))
    text = "\n\n".join(html_to_text(h) for h in html) if html else "\n\n".join(plain)
    return text.strip()[:MAX_BODY_CHARS]


def reply_text(msg: EmailMessage) -> str:
    """What the person wrote, without the quoted question below it."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = _part_text(part)
    if part.get_content_subtype() == "html":
        text = html_to_text(text)
    if m := _QUOTED.search(text):
        text = text[: m.start()]
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">")).strip()[:MAX_REPLY_CHARS]


# ---------- filing ----------
async def _already_filed(session: AsyncSession, data: bytes) -> bool:
    att_id = await session.scalar(select(Attachment.id).where(Attachment.sha256 == hashlib.sha256(data).hexdigest()))
    if att_id is None:
        return False
    for model in (Statement, ImportBatch):
        if await session.scalar(select(model.id).where(model.attachment_id == att_id).limit(1)):
            return True
    return False


async def question_for(session: AsyncSession, msg: EmailMessage) -> AlertEvent | None:
    """The emailed question this message replies to, if any."""
    refs = " ".join(str(msg.get(h, "")) for h in ("In-Reply-To", "References"))
    if m := TOKEN_RE.search(refs):
        return await session.scalar(select(AlertEvent).where(AlertEvent.subject_key == f"question:{m.group(1)}"))
    subject = " ".join(str(msg.get("Subject", "")).split())
    if not _REPLY_PREFIX.match(subject):
        return None
    # Fallback for mail servers that rewrite Message-IDs: the newest question with the same subject.
    title = _REPLY_PREFIX.sub("", subject)
    recent = await session.scalars(
        select(AlertEvent)
        .where(AlertEvent.kind == "question", AlertEvent.status == "sent")
        .order_by(AlertEvent.id.desc())
        .limit(200)
    )
    return next((e for e in recent if " ".join(e.title.split()) == title), None)


async def save_reply(session: AsyncSession, event: AlertEvent, msg: EmailMessage, who: str) -> list[str]:
    ids = event.transaction_ids or ([int(event.link.rsplit("/", 1)[1])] if event.link else [])
    ids = list(
        (
            await session.scalars(select(Transaction.id).where(Transaction.id.in_(ids), Transaction.deleted_at.is_(None)))
        ).all()
    )
    if not ids:
        log.warning("Reply to question %s: its transactions no longer exist", event.id)
        return []
    created = []
    if text := reply_text(msg):
        body = f"{who} replied: {text}"
        seen = await session.scalar(
            select(TransactionNote.id).where(TransactionNote.transaction_id == ids[0], TransactionNote.body == body)
        )
        if not seen:
            session.add_all(TransactionNote(transaction_id=i, body=body, source="reply") for i in ids)
            created.append(f"reply on {len(ids)} transaction(s)")
    for name, ctype, data in documents(msg):
        if detect_kind(name) != "document" or await _already_filed(session, data):
            continue
        try:
            async with session.begin_nested():
                created.append(f"statement {(await create_statement(session, data, name, ctype, ids)).id}")
        except ImportError_ as exc:
            log.warning("Skipping %s from reply to question %s: %s", name, event.id, exc)
    return created


async def file_message(session: AsyncSession, raw: bytes) -> list[str] | None:
    """File one email; returns what was created, or None when it isn't for the app (left unread). Caller commits."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    subject = " ".join(str(msg.get("Subject", "")).split())
    sender = sender_of(msg)
    event = await question_for(session, msg)
    household = {p["email"]: p for p in await people(session)} if event else {}
    if sender not in allowed_senders() | set(household):
        return None
    if not authenticated(msg):
        log.warning("Ignoring email %r: %s did not pass DMARC", subject, sender)
        return []
    if event:
        created = await save_reply(session, event, msg, (household.get(sender) or {}).get("name") or sender)
        log.info("Reply %r to question %s: %s", subject, event.id, ", ".join(created) or "nothing new")
        return created

    docs = documents(msg)
    if not docs and (text := body_text(msg)):
        name = "Email - " + (" ".join(_UNSAFE.sub(" ", subject).split())[:100] or "no subject")
        header = f"Subject: {subject}\nDate: {msg.get('Date', '')}\n\n"
        docs = [(name, "text/plain", (header + text).encode())]

    created = []
    for name, ctype, data in docs:
        if await _already_filed(session, data):
            continue
        try:
            async with session.begin_nested():
                if detect_kind(name) == "spreadsheet":
                    created.append(f"import {(await create_batch(session, data, name, ctype, origin='email')).id}")
                else:
                    created.append(f"statement {(await create_statement(session, data, name, ctype)).id}")
        except ImportError_ as exc:
            log.warning("Skipping %s from email %r: %s", name, subject, exc)
    log.info("Email %r filed: %s", subject, ", ".join(created) or "nothing new")
    return created


# ---------- IMAP ----------
def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _connect() -> imaplib.IMAP4_SSL:
    s = get_settings()
    conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port, timeout=60)
    conn.login(s.imap_username, s.imap_password.get_secret_value())
    return conn


def _fetch_unseen(senders: set[str]) -> list[tuple[str, bytes]]:
    conn = _connect()
    try:
        conn.select(_quote(get_settings().imap_folder), readonly=True)
        uids: set[int] = set()
        # Searching by sender keeps the app from touching anyone else's mail in a shared mailbox.
        for sender in senders:
            typ, data = conn.uid("SEARCH", "UNSEEN", "FROM", _quote(sender))
            if typ == "OK" and data and data[0]:
                uids.update(int(u) for u in data[0].split())
        out = []
        for uid in sorted(uids, reverse=True)[:MAX_PER_POLL]:
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ == "OK" and data and isinstance(data[0], tuple):
                out.append((str(uid), data[0][1]))
        return out
    finally:
        conn.logout()


def _mark_seen(uids: list[str]) -> None:
    conn = _connect()
    try:
        conn.select(_quote(get_settings().imap_folder))
        conn.uid("STORE", ",".join(uids), "+FLAGS", "(\\Seen)")
    finally:
        conn.logout()


async def poll_inbox() -> int:
    """File unread mail from allowed senders (and question replies) and mark it read; returns items created."""
    if not inbox_configured():
        return 0
    senders = allowed_senders()
    if replies_saved():
        async with get_sessionmaker()() as session:
            senders |= {p["email"] for p in await people(session)}
    done, created = [], 0
    for uid, raw in await asyncio.to_thread(_fetch_unseen, senders):
        try:
            async with get_sessionmaker()() as session:
                made = await file_message(session, raw)
                await session.commit()
        except Exception:
            log.exception("Filing email uid %s failed; it stays unread and is retried next poll", uid)
            continue
        if made is None:
            continue
        done.append(uid)
        created += len(made)
    if done:
        await asyncio.to_thread(_mark_seen, done)
    if created:
        notify_worker()
    return created
