import re
from email.message import EmailMessage

import aiosmtplib

from ledger.config import get_settings

EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")


class MailNotConfigured(RuntimeError):
    pass


def smtp_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and (s.smtp_from or s.smtp_username))


def _one_line(s: str) -> str:
    return " ".join(s.split())[:200]


async def send_email(to: list[str], subject: str, text: str, html: str | None = None) -> None:
    s = get_settings()
    if not smtp_configured():
        raise MailNotConfigured("SMTP is not configured (set SMTP_HOST and SMTP_FROM)")
    msg = EmailMessage()
    msg["From"] = s.smtp_from or s.smtp_username
    msg["To"] = ", ".join(to)
    msg["Subject"] = _one_line(subject)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    await aiosmtplib.send(
        msg,
        hostname=s.smtp_host,
        port=s.smtp_port,
        username=s.smtp_username,
        password=s.smtp_password.get_secret_value() if s.smtp_password else None,
        use_tls=s.smtp_security == "ssl",
        start_tls=True if s.smtp_security == "starttls" else False,
        timeout=30,
    )
